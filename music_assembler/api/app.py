"""FastAPI application — Music Assembly control plane."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from music_assembler import __version__
from music_assembler.assemble_options import normalize_channel
from music_assembler.api.auth import require_api_auth
from music_assembler.api.config import ApiSettings
from music_assembler.api.dashboard_auth import (
    clear_dashboard_session,
    has_dashboard_session,
    set_dashboard_session,
)
from music_assembler.api.extend_runner import run_extend_job
from music_assembler.api import gcp_jobs
from music_assembler.api import job_cancel
from music_assembler.api import job_runs
from music_assembler.api import job_status
from music_assembler.api import assembly_health
from music_assembler.api import r2_catalog
from music_assembler.api import uploader_client
from music_assembler.api.cache import dashboard_cache
from music_assembler.api.media import stream_r2_object
from music_assembler.api.openapi_docs import install_openapi_docs
from music_assembler.api.progress_store import read_progress_json, write_meta_json, write_progress_json
from music_assembler.api.progress_store import patch_meta_gcp_execution_id
from music_assembler.extend_from_r2 import count_pending_r2_sources
from music_assembler.r2_storage import r2_client, r2_config_from_env

app = FastAPI(
    title="Music Assembly API",
    description="Trigger Cloud Run assembly/extend jobs, monitor progress, browse R2 outputs.",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


class StartJobRequest(BaseModel):
    category: str | None = Field(
        default=None,
        description="R2 category folder (defaults to ASSEMBLY_CATEGORY).",
        examples=["korean"],
    )
    channel: str | None = Field(
        default=None,
        description="YouTube channel slug — output path music-video/{channel}/.",
        examples=["nappabeats"],
    )
    images_folder: str | None = Field(
        default=None,
        description="R2 subfolder under post-processed/ for background stills (defaults to category).",
        examples=["korean"],
    )
    thumbnail_text: str | None = Field(default=None, description="Text burned into the thumbnail.", examples=["OMYO"])
    duration_min: int | None = Field(default=None, ge=15, le=240, description="Target mix length in minutes.")
    variance_min: int | None = Field(default=None, ge=0, le=60, description="Random length variance (+/- minutes).")
    count: int = Field(default=1, ge=1, le=10, description="Parallel assembly jobs to start (one video each).")
    queue_youtube: bool = Field(
        default=True,
        description=(
            "After encode + R2 upload, register the video on the youtube-uploader pending queue "
            "(default on; set false to skip). Requires UPLOADER_API_* on the music-assemble worker."
        ),
    )

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        return normalize_channel(value)

    @field_validator("images_folder")
    @classmethod
    def _validate_images_folder(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        folder = value.strip().strip("/")
        if not folder or ".." in folder or "/" in folder or "\\" in folder:
            raise ValueError("images_folder must be a single folder name under post-processed/")
        return folder


class StartExtendRequest(BaseModel):
    category: str | None = Field(default=None, description="R2 category (defaults to ASSEMBLY_CATEGORY).")
    limit: int | None = Field(default=1, ge=1, le=20, description="Images per batch when process_all is false.")
    process_all: bool = Field(default=False, description="Extend every pending pre-processed image.")
    force: bool = Field(default=False, description="Include images that would normally be skipped.")
    parallel: bool = Field(
        default=True,
        description="When limit>1, start one Cloud Run Job per image (recommended).",
    )


class DashboardLoginRequest(BaseModel):
    password: str = Field(description="Value of ASSEMBLY_DASHBOARD_PASSWORD.")


class CancelJobRequest(BaseModel):
    confirm: bool = Field(default=False, description="Set true to cancel; false returns a preview only.")


def _settings() -> ApiSettings:
    return ApiSettings.from_env()


def _r2():
    cfg = r2_config_from_env()
    return r2_client(cfg), cfg.bucket


def _new_execution_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"asm_{ts}_{uuid.uuid4().hex[:8]}"


def _new_extend_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"ext_{ts}_{uuid.uuid4().hex[:8]}"


def _cache_key(*parts: str) -> str:
    return ":".join(parts)


def _version_info() -> dict[str, str]:
    revision = os.environ.get("K_REVISION", "local").strip() or "local"
    build = os.environ.get("ASSEMBLY_BUILD_ID", revision).strip() or revision
    return {
        "version": __version__,
        "revision": revision,
        "build": build,
        "dashboard": f"v{__version__} · {revision}",
    }


def _invalidate_category_cache(category: str) -> None:
    dashboard_cache.invalidate_prefix(_cache_key("stats", category))
    dashboard_cache.invalidate_prefix(_cache_key("videos", category))
    dashboard_cache.invalidate_prefix(_cache_key("assets", category))


def _queue_extend_job_local(
    client,
    bucket: str,
    *,
    execution_id: str,
    category: str,
    max_images: int | None,
    force: bool,
) -> dict[str, Any]:
    label = (
        f"{max_images} image(s)"
        if max_images is not None and max_images != 1
        else "next pending image"
    )
    write_meta_json(
        client,
        bucket,
        execution_id,
        category=category,
        job_type="extend",
        limit=max_images,
        process_all=max_images is None,
    )
    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=0,
        stage=f"Queued locally — {label}…",
        category=category,
        status="running",
        extra={"job_type": "extend", "host": "local"},
    )
    threading.Thread(
        target=run_extend_job,
        kwargs={
            "execution_id": execution_id,
            "category": category,
            "max_images": max_images,
            "force": force,
        },
        daemon=True,
    ).start()
    return {
        "execution_id": execution_id,
        "status": "running",
        "category": category,
        "max_images": max_images,
        "host": "local",
    }


def _queue_extend_job(
    client,
    bucket: str,
    settings: ApiSettings,
    *,
    execution_id: str,
    category: str,
    max_images: int | None,
    force: bool,
    exclude_gcp_ids: set[str],
) -> dict[str, Any]:
    if not settings.extend_use_gcp:
        return _queue_extend_job_local(
            client,
            bucket,
            execution_id=execution_id,
            category=category,
            max_images=max_images,
            force=force,
        )
    label = (
        f"{max_images} image(s)"
        if max_images is not None and max_images != 1
        else "next pending image"
    )
    write_meta_json(
        client,
        bucket,
        execution_id,
        category=category,
        job_type="extend",
        limit=max_images,
        process_all=max_images is None,
    )
    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=0,
        stage=f"Queued on Cloud Run — {label}…",
        category=category,
        status="running",
        extra={"job_type": "extend"},
    )
    try:
        result = gcp_jobs.start_extend_job(
            settings,
            execution_id=execution_id,
            category=category,
            max_images=max_images,
            force=force,
            exclude_gcp_ids=exclude_gcp_ids,
        )
    except Exception as e:
        write_progress_json(
            client,
            bucket,
            execution_id,
            pct=0,
            stage=f"Failed to start: {e}",
            category=category,
            status="failed",
            extra={"job_type": "extend"},
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to start Cloud Run extend job ({execution_id}): {e}",
        ) from e
    gcp_id = result.get("gcp_execution_id")
    if gcp_id:
        exclude_gcp_ids.add(gcp_id)
        patch_meta_gcp_execution_id(client, bucket, execution_id, gcp_id)
        write_progress_json(
            client,
            bucket,
            execution_id,
            pct=1,
            stage="Cloud Run execution started…",
            category=category,
            status="running",
            extra={"job_type": "extend"},
        )
    return {
        "execution_id": execution_id,
        "status": "running",
        "category": category,
        "gcp_execution_id": gcp_id,
        "max_images": max_images,
        "host": "cloud_run",
    }


@app.get("/health")
def health() -> dict[str, str]:
    info = _version_info()
    return {
        "status": "ok",
        "service": "music-assembly-api",
        "version": info["version"],
        "revision": info["revision"],
    }


@app.get("/v1/version")
def api_version() -> dict[str, str]:
    return _version_info()


@app.get("/v1/capabilities")
def capabilities(settings: ApiSettings = Depends(_settings)) -> dict[str, Any]:
    return {
        "service": "music-assembly-api",
        **_version_info(),
        "gcp_project": settings.gcp_project,
        "gcp_region": settings.gcp_region,
        "assembly_job": settings.assembly_job_name,
        "extend_job": settings.extend_job_name,
        "default_category": settings.default_category,
        "configured_channels": list(settings.configured_channels),
        "auth": {
            "api": "X-API-Key" if settings.api_key else "none",
            "dashboard": "password+cookie" if settings.dashboard_password else "none",
        },
        "endpoints": [
            "POST /v1/assembly/jobs",
            "GET /v1/assembly/jobs",
            "GET /v1/assembly/runs",
            "GET /v1/assembly/jobs/{id}",
            "GET /v1/assembly/jobs/{id}/progress",
            "POST /v1/extend/jobs",
            "GET /v1/extend/runs",
            "GET /v1/extend/pending",
            "GET /v1/extend/jobs/{id}/progress",
            "GET /v1/jobs/{id}/cancel",
            "POST /v1/jobs/{id}/cancel",
            "GET /v1/videos",
            "GET /v1/videos/{id}",
            "GET /v1/media/thumbnail",
            "GET /v1/media/video",
            "GET /v1/media/asset",
            "GET /v1/dashboard/snapshot",
            "GET /v1/dashboard/stats",
            "GET /v1/assets",
            "GET /v1/observability",
            "GET /v1/categories",
            "GET /v1/categories/{category}/inventory",
            "GET /v1/background-folders",
            "GET /v1/channels",
            "GET /v1/cron/assembly-health",
            "POST /v1/cron/assembly-health",
        ],
    }


@app.get("/v1/dashboard")
def dashboard_summary(
    category: str | None = None,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    cat = category or settings.default_category
    client, bucket = _r2()
    try:
        jobs = gcp_jobs.list_executions(settings, limit=10)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    videos = r2_catalog.list_videos(client, bucket, limit=5)
    inventory = r2_catalog.category_inventory(client, bucket, cat)
    return {
        "category": cat,
        "recent_jobs": jobs,
        "recent_videos": videos,
        "inventory": inventory,
    }


@app.post("/v1/assembly/jobs")
def start_job(
    body: StartJobRequest,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    category = (body.category or settings.default_category).strip()
    images_folder = body.images_folder
    channel = body.channel
    if not channel or not str(channel).strip():
        raise HTTPException(
            status_code=400,
            detail="channel is required (YouTube channel slug for music-video/{channel}/ output)",
        )
    client, bucket = _r2()
    _invalidate_category_cache(category)
    if images_folder and images_folder != category:
        _invalidate_category_cache(images_folder)

    jobs: list[dict[str, Any]] = []
    assigned_gcp: set[str] = set()
    for _ in range(body.count):
        execution_id = _new_execution_id()
        write_meta_json(
            client,
            bucket,
            execution_id,
            category=category,
            channel=channel,
            images_folder=images_folder,
            duration_min=body.duration_min,
            variance_min=body.variance_min,
            thumbnail_text=body.thumbnail_text,
        )
        write_progress_json(
            client,
            bucket,
            execution_id,
            pct=0,
            stage="Queued on Cloud Run…",
            category=category,
            status="running",
        )
        try:
            result = gcp_jobs.start_assembly_job(
                settings,
                execution_id=execution_id,
                category=category,
                channel=channel,
                images_folder=images_folder,
                thumbnail_text=body.thumbnail_text,
                duration_min=body.duration_min,
                variance_min=body.variance_min,
                queue_youtube=body.queue_youtube,
                exclude_gcp_ids=assigned_gcp,
            )
        except Exception as e:
            write_progress_json(
                client,
                bucket,
                execution_id,
                pct=0,
                stage=f"Failed to start: {e}",
                category=category,
                status="failed",
            )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to start Cloud Run Job ({execution_id}): {e}",
            ) from e
        gcp_id = result.get("gcp_execution_id")
        if gcp_id:
            assigned_gcp.add(gcp_id)
            patch_meta_gcp_execution_id(client, bucket, execution_id, gcp_id)
            write_progress_json(
                client,
                bucket,
                execution_id,
                pct=1,
                stage="Cloud Run execution started…",
                category=category,
                status="running",
            )
        result["api_execution_id"] = execution_id
        jobs.append(result)

    if body.count == 1:
        return {**jobs[0], "jobs": jobs, "count": 1}
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/v1/assembly/jobs")
def list_jobs(
    status: str | None = Query(default=None, pattern="^(running|succeeded|failed)$"),
    limit: int = Query(default=25, ge=1, le=100),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    try:
        jobs = gcp_jobs.list_executions(settings, limit=limit, status=status)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/v1/assembly/jobs/{execution_id}")
def get_job(
    execution_id: str,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    client, bucket = _r2()
    progress = read_progress_json(client, bucket, execution_id)
    if execution_id.startswith("asm_"):
        if progress is None:
            raise HTTPException(status_code=404, detail="Execution not found")
        return {
            "execution_id": execution_id,
            "status": progress.get("status", "running"),
            "pct": progress.get("pct", 0),
            "stage": progress.get("stage", ""),
            "updated_at": progress.get("updated_at"),
            "progress": progress,
        }
    row = gcp_jobs.get_execution(settings, execution_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    if progress:
        row["progress"] = progress
    return row


@app.get("/v1/assembly/runs")
def list_r2_runs(
    limit: int = Query(default=25, ge=1, le=100),
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    """List assembly runs registered on R2 (``jobs/asm_*/meta.json``)."""
    client, bucket = _r2()
    runs = job_runs.list_r2_job_runs(client, bucket, id_prefix="asm_", limit=limit)
    return {"runs": runs, "count": len(runs)}


@app.post("/v1/cron/assembly-health")
@app.get("/v1/cron/assembly-health")
def cron_assembly_health(
    limit: int = Query(default=30, ge=1, le=100),
    repair: bool = Query(
        default=True,
        description="Rewrite false succeeded jobs on R2 when output video is missing",
    ),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    """Audit recent assembly jobs (intended for Cloud Scheduler every few hours)."""
    client, bucket = _r2()
    runs = job_runs.list_r2_job_runs(client, bucket, id_prefix="asm_", limit=limit)
    report = assembly_health.audit_recent_assemblies(
        settings, client, bucket, runs, repair=repair
    )
    status_code = 200 if not report["issues"] else 207
    return JSONResponse(content=report, status_code=status_code)


@app.get("/v1/media/thumbnail")
def media_thumbnail(
    channel: str,
    video_id: str,
    _auth: None = Depends(require_api_auth),
) -> Response:
    """Stable thumbnail URL for the dashboard (avoids presigned URL churn)."""
    client, bucket = _r2()
    ch = normalize_channel(channel)
    if not ch:
        raise HTTPException(status_code=400, detail="Invalid channel")
    key = r2_catalog.find_thumbnail_key(client, bucket, video_id=video_id, channel=ch)
    if not key:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    resp = client.get_object(Bucket=bucket, Key=key)
    return Response(
        content=resp["Body"].read(),
        media_type=resp.get("ContentType") or "image/png",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/v1/media/video")
def media_video(
    request: Request,
    channel: str,
    video_id: str,
    _auth: None = Depends(require_api_auth),
) -> Response:
    """Stream MP4 with Range support for in-browser preview."""
    client, bucket = _r2()
    ch = normalize_channel(channel)
    if not ch:
        raise HTTPException(status_code=400, detail="Invalid channel")
    key = r2_catalog.find_video_key(client, bucket, video_id=video_id, channel=ch)
    if not key:
        raise HTTPException(status_code=404, detail="Video not found")
    return stream_r2_object(
        client, bucket, key, request, media_type="video/mp4", cache_control="private, max-age=3600"
    )


@app.get("/v1/media/asset")
def media_asset(
    category: str,
    pool: str = Query(pattern="^(pre-processed|pre-used|post-processed|post-used)$"),
    name: str = Query(min_length=1, max_length=512),
    _auth: None = Depends(require_api_auth),
) -> Response:
    """Proxy a single pre/post-processed image (loaded on demand from dashboard)."""
    client, bucket = _r2()
    try:
        key = r2_catalog.asset_object_key(category, pool, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
    except client.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise HTTPException(status_code=404, detail="Asset not found") from exc
        raise
    ext = name.rsplit(".", 1)[-1].lower()
    media = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }.get(ext, resp.get("ContentType") or "application/octet-stream")
    return Response(
        content=resp["Body"].read(),
        media_type=media,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/v1/dashboard/stats")
def dashboard_stats(
    category: str | None = None,
    refresh: bool = Query(default=False, description="Bypass server cache"),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    """Cached inventory + extend pending (fast initial dashboard load)."""
    cat = category or settings.default_category
    cache_key = _cache_key("stats", cat)

    def load() -> dict[str, Any]:
        client, bucket = _r2()
        cfg = r2_config_from_env(category=cat)
        return {
            "category": cat,
            "inventory": r2_catalog.category_inventory(client, bucket, cat),
            "extend_pending": count_pending_r2_sources(client, cfg, force=False),
        }

    if refresh:
        data = load()
        hit = False
        dashboard_cache.set(cache_key, data, 45.0)
    else:
        data, hit = dashboard_cache.get_or_set(cache_key, 45.0, load)
    return JSONResponse(
        content={**data, "cache": {"hit": hit, "ttl_sec": 45}},
        headers={"X-Cache": "HIT" if hit else "MISS"},
    )


@app.get("/v1/observability")
def observability(_auth: None = Depends(require_api_auth)) -> dict[str, Any]:
    return {
        "cache": dashboard_cache.stats(),
        "service": "music-assembly-api",
    }


@app.get("/v1/assets")
def list_assets(
    category: str | None = None,
    pool: str = Query(pattern="^(pre-processed|pre-used|post-processed|post-used)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    """List image filenames + metadata only (no bytes)."""
    cat = category or settings.default_category
    cache_key = _cache_key("assets", cat, pool, str(limit))

    def load() -> dict[str, Any]:
        client, bucket = _r2()
        items = r2_catalog.list_assets(client, bucket, category=cat, pool=pool, limit=limit)
        return {"category": cat, "pool": pool, "items": items, "count": len(items)}

    data, hit = dashboard_cache.get_or_set(cache_key, 60.0, load)
    return JSONResponse(
        content={**data, "cache": {"hit": hit, "ttl_sec": 60}},
        headers={"X-Cache": "HIT" if hit else "MISS"},
    )


@app.get("/v1/dashboard/snapshot")
def dashboard_snapshot(
    category: str | None = None,
    light: bool = Query(default=False, description="Jobs only — skip stats"),
    refresh: bool = Query(default=False, description="Bypass stats cache; reconcile GCP job status"),
    job_limit: int = Query(default=100, ge=1, le=200),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    """Poll endpoint for jobs. Use /v1/dashboard/stats and tab loaders for the rest."""
    cat = category or settings.default_category
    client, bucket = _r2()

    with ThreadPoolExecutor(max_workers=2) as pool:
        asm_future = pool.submit(
            job_runs.list_r2_job_runs, client, bucket, id_prefix="asm_", limit=job_limit
        )
        ext_future = pool.submit(
            job_runs.list_r2_job_runs, client, bucket, id_prefix="ext_", limit=job_limit
        )
        asm_raw = asm_future.result()
        ext_raw = ext_future.result()

    assembly_runs = job_status.reconcile_assembly_runs(
        settings, client, bucket, asm_raw, patch_r2=True
    )
    extend_runs = job_status.reconcile_extend_runs(
        settings, client, bucket, ext_raw, patch_r2=True
    )
    has_running = job_status.has_running_jobs(assembly_runs) or job_status.has_running_jobs(
        extend_runs
    )

    out: dict[str, Any] = {
        "category": cat,
        "assembly_runs": assembly_runs,
        "extend_runs": extend_runs,
        "has_running": has_running,
    }
    if not light:
        if refresh:
            dashboard_cache.invalidate_prefix(_cache_key("stats", cat))
        cache_key = _cache_key("stats", cat)

        def load_stats() -> dict[str, Any]:
            cfg = r2_config_from_env(category=cat)
            return {
                "inventory": r2_catalog.category_inventory(client, bucket, cat),
                "extend_pending": count_pending_r2_sources(client, cfg, force=False),
            }

        if refresh:
            stats = load_stats()
            hit = False
            dashboard_cache.set(cache_key, stats, 45.0)
        else:
            stats, hit = dashboard_cache.get_or_set(cache_key, 45.0, load_stats)
        out["inventory"] = stats["inventory"]
        out["extend_pending"] = stats["extend_pending"]
        out["stats_cache_hit"] = hit
    return JSONResponse(
        content=out,
        headers={"Cache-Control": "no-store", "X-Job-Poll": "1"},
    )


@app.post("/v1/extend/jobs")
def start_extend(
    body: StartExtendRequest,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    category = (body.category or settings.default_category).strip()
    client, bucket = _r2()
    _invalidate_category_cache(category)
    cfg = r2_config_from_env(category=category)
    pending = count_pending_r2_sources(client, cfg, force=body.force)
    if pending == 0:
        raise HTTPException(status_code=409, detail="No pending pre-processed images on R2")

    batch = pending if body.process_all else min(body.limit or 1, pending)
    assigned_gcp: set[str] = set()
    jobs: list[dict[str, Any]] = []

    if body.parallel and batch > 1:
        for _ in range(batch):
            execution_id = _new_extend_id()
            jobs.append(
                _queue_extend_job(
                    client,
                    bucket,
                    settings,
                    execution_id=execution_id,
                    category=category,
                    max_images=1,
                    force=body.force,
                    exclude_gcp_ids=assigned_gcp,
                )
            )
        return {
            "parallel": True,
            "jobs": jobs,
            "batch_size": len(jobs),
            "category": category,
            "pending": pending,
            "host": "cloud_run",
        }

    execution_id = _new_extend_id()
    max_images = None if body.process_all else batch
    job = _queue_extend_job(
        client,
        bucket,
        settings,
        execution_id=execution_id,
        category=category,
        max_images=max_images,
        force=body.force,
        exclude_gcp_ids=assigned_gcp,
    )
    return {
        **job,
        "parallel": False,
        "pending": pending,
        "batch_size": batch,
        "host": "cloud_run",
    }


@app.get("/v1/extend/pending")
def extend_pending(
    category: str | None = None,
    force: bool = False,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    cat = category or settings.default_category
    client, _ = _r2()
    cfg = r2_config_from_env(category=cat)
    pending = count_pending_r2_sources(client, cfg, force=force)
    return {"category": cat, "pending": pending}


@app.get("/v1/extend/runs")
def list_extend_runs(
    limit: int = Query(default=25, ge=1, le=100),
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    client, bucket = _r2()
    runs = job_runs.list_r2_job_runs(client, bucket, id_prefix="ext_", limit=limit)
    return {"runs": runs, "count": len(runs)}


@app.get("/v1/extend/jobs/{execution_id}/progress")
def extend_progress(
    execution_id: str,
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    client, bucket = _r2()
    progress = read_progress_json(client, bucket, execution_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="Extend run not found")
    return {
        "execution_id": execution_id,
        "pct": progress.get("pct", 0),
        "stage": progress.get("stage", ""),
        "status": progress.get("status", "unknown"),
        "updated_at": progress.get("updated_at"),
    }


@app.get("/v1/jobs/{execution_id}/cancel")
def cancel_job_preview(
    execution_id: str,
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    client, bucket = _r2()
    preview = job_cancel.cancel_job_preview(client, bucket, execution_id)
    if not preview.get("found"):
        raise HTTPException(status_code=404, detail="Job not found")
    return preview


@app.post("/v1/jobs/{execution_id}/cancel")
def cancel_job(
    execution_id: str,
    body: CancelJobRequest,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    client, bucket = _r2()
    if not body.confirm:
        preview = job_cancel.cancel_job_preview(client, bucket, execution_id)
        if not preview.get("found"):
            raise HTTPException(status_code=404, detail="Job not found")
        return preview
    result = job_cancel.cancel_job(client, bucket, execution_id, settings)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="Job not found")
    return result


@app.get("/v1/assembly/jobs/{execution_id}/progress")
def job_progress(
    execution_id: str,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    client, bucket = _r2()
    if execution_id.startswith("asm_"):
        meta = job_runs.load_r2_job_run(client, bucket, execution_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Execution not found")
        rows = job_status.reconcile_assembly_runs(
            settings, client, bucket, [meta], patch_r2=False
        )
        return JSONResponse(
            content=rows[0],
            headers={"Cache-Control": "no-store"},
        )
    progress = read_progress_json(client, bucket, execution_id)
    gcp_row = gcp_jobs.get_execution(settings, execution_id)
    if progress is None and gcp_row is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    status = "unknown"
    if progress:
        status = progress.get("status", "running")
    elif gcp_row:
        status = gcp_row["status"]
    return {
        "execution_id": execution_id,
        "gcp_execution_id": gcp_row["execution_id"] if gcp_row else None,
        "gcp_status": gcp_row["status"] if gcp_row else None,
        "pct": progress.get("pct", 0) if progress else (100 if status == "succeeded" else 0),
        "stage": progress.get("stage", "") if progress else "",
        "status": status,
        "updated_at": progress.get("updated_at") if progress else None,
    }


@app.get("/v1/categories")
def list_cats(_auth: None = Depends(require_api_auth)) -> dict[str, Any]:
    client, bucket = _r2()
    return {"categories": r2_catalog.list_categories(client, bucket)}


@app.get("/v1/background-folders")
def list_background_folders(_auth: None = Depends(require_api_auth)) -> dict[str, Any]:
    """Subfolders under ``post-processed/`` — selectable background pools for assembly."""
    client, bucket = _r2()
    folders = r2_catalog.list_background_folders(client, bucket)
    return {"folders": folders, "count": len(folders)}


@app.get("/v1/categories/{category}/inventory")
def inventory(
    category: str,
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    client, bucket = _r2()
    return {"category": category, "counts": r2_catalog.category_inventory(client, bucket, category)}


@app.get("/v1/channels")
def list_channels(
    category: str | None = None,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    """YouTube uploader channels + R2-discovered folders for assembly ``channel`` slugs."""
    cat = category or settings.default_category
    client, bucket = _r2()
    discovered = r2_catalog.discover_video_channels(client, bucket)
    uploader_rows = uploader_client.fetch_uploader_channels(settings)
    channels, channel_details = uploader_client.merge_channel_list(
        uploader_channels=uploader_rows,
        configured=settings.configured_channels,
        discovered=discovered,
    )
    return {
        "category": cat,
        "channels": channels,
        "channel_details": channel_details,
        "configured": list(settings.configured_channels),
        "discovered": discovered,
        "uploader": {
            "configured": bool(settings.uploader_api_url and settings.uploader_api_key),
            "count": len(uploader_rows),
        },
    }


@app.get("/v1/videos")
def list_videos(
    channel: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    summary: bool = Query(default=True, description="Metadata only — no title/description reads"),
    _auth: None = Depends(require_api_auth),
) -> JSONResponse:
    ch = normalize_channel(channel) if channel else None
    cache_key = _cache_key("videos", ch or "all", "summary" if summary else "full", str(limit))

    def load() -> dict[str, Any]:
        client, bucket = _r2()
        videos = r2_catalog.list_videos(
            client,
            bucket,
            channel=ch,
            limit=limit,
            stable_media_urls=True,
            summary_only=summary,
        )
        return {"channel": ch, "videos": videos, "count": len(videos), "summary": summary}

    data, hit = dashboard_cache.get_or_set(cache_key, 60.0 if summary else 30.0, load)
    return JSONResponse(
        content={**data, "cache": {"hit": hit}},
        headers={"X-Cache": "HIT" if hit else "MISS"},
    )


@app.get("/v1/videos/{video_id}")
def get_video(
    video_id: str,
    channel: str | None = None,
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    ch = normalize_channel(channel) if channel else None
    client, bucket = _r2()
    row = r2_catalog.get_video(client, bucket, video_id=video_id, channel=ch)
    if row is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return row


@app.post("/v1/dashboard/login")
def dashboard_login(
    body: DashboardLoginRequest,
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    """Unlock the web dashboard (sets an httpOnly session cookie)."""
    if not settings.dashboard_password:
        return JSONResponse({"ok": True, "dashboard_auth": "disabled"})
    if body.password != settings.dashboard_password:
        raise HTTPException(status_code=401, detail="Invalid password")
    response = JSONResponse({"ok": True})
    set_dashboard_session(response, settings)
    return response


@app.post("/v1/dashboard/logout")
def dashboard_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    clear_dashboard_session(response)
    return response


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page(
    request: Request,
    settings: ApiSettings = Depends(_settings),
) -> str:
    if not has_dashboard_session(request, settings):
        return _LOGIN_HTML
    return _DASHBOARD_HTML.replace("__DEFAULT_CATEGORY__", settings.default_category)


_DASHBOARD_DESIGN_FONTS = """  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
  <link href="https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,500;1,400&family=Figtree:wght@400;500;600;700&display=swap" rel="stylesheet"/>"""

_DASHBOARD_DESIGN_ROOT_CSS = """    /* Wispr Flow design tokens — see DESIGN.md */
    :root {
      --color-cream-paper: #ffffeb;
      --color-white: #ffffff;
      --color-stone-mist: #e4e4d0;
      --color-pale-lavender-tint: #f7f1ff;
      --color-midnight-ink: #1a1a1a;
      --color-graphite-veil: #8a8a80;
      --color-smoke: #5f5f59;
      --color-charcoal: #222222;
      --color-deep-forest-teal: #034f46;
      --color-lavender-whisper: #f0d7ff;
      --color-lavender-light: #f3e3ff;
      --font-figtree: 'Figtree', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-eb-garamond: 'EB Garamond', 'Times New Roman', Georgia, serif;
      --page-max-width: 1200px;
      --section-gap: 64px;
      --card-padding: 32px;
      --element-gap: 16px;
      --radius-nav: 14px;
      --radius-cards: 20px;
      --radius-badges: 9999px;
      --radius-images: 12px;
      --radius-buttons: 10px;
      --radius-small: 6px;
    }"""

_LOGIN_HTML = (
    """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Music Assembly</title>
"""
    + _DASHBOARD_DESIGN_FONTS
    + """
  <style>
"""
    + _DASHBOARD_DESIGN_ROOT_CSS
    + """
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      background: var(--color-cream-paper);
      color: var(--color-midnight-ink);
      font-family: var(--font-figtree);
      font-size: 16px;
      line-height: 1.3;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 50px 20px;
    }
    .login-wrap { width: min(420px, 100%); }
    .wordmark {
      font-size: 14px;
      font-weight: 600;
      letter-spacing: 0.01em;
      margin: 0 0 40px;
    }
    .wordmark-a { color: var(--color-midnight-ink); }
    .wordmark-b { color: var(--color-deep-forest-teal); }
    h1 {
      font-family: var(--font-figtree);
      font-size: 28px;
      font-weight: 600;
      line-height: 1.2;
      letter-spacing: -0.02em;
      margin: 0 0 8px;
      color: var(--color-midnight-ink);
    }
    .lead { color: var(--color-smoke); font-size: 15px; font-weight: 400; margin: 0 0 28px; }
    input {
      width: 100%;
      background: var(--color-white);
      color: var(--color-midnight-ink);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      padding: 14px 16px;
      font: inherit;
      margin-bottom: 20px;
    }
    input:focus {
      outline: none;
      border-color: var(--color-midnight-ink);
    }
    .btn-primary {
      width: 100%;
      background: var(--color-lavender-whisper);
      color: var(--color-midnight-ink);
      border: 1px solid var(--color-midnight-ink);
      border-radius: var(--radius-buttons);
      padding: 16px 32px;
      font-size: 14px;
      font-weight: 600;
      letter-spacing: 0.02em;
      text-transform: none;
      cursor: pointer;
    }
    .btn-primary:hover { background: var(--color-lavender-light); }
    .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }
    .btn-primary.is-loading {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      cursor: wait;
    }
    .spinner {
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid var(--color-stone-mist);
      border-top-color: var(--color-deep-forest-teal);
      border-radius: 50%;
      animation: spin 0.65s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    #err { color: var(--color-smoke); font-size: 14px; margin-top: 20px; min-height: 1.2em; }
  </style>
</head>
<body>
  <div class="login-wrap">
    <p class="wordmark"><span class="wordmark-a">Music</span><span class="wordmark-b">Assembly</span></p>
    <h1>Sign in</h1>
    <p class="lead">Enter your dashboard password to continue.</p>
    <form id="loginForm">
      <input type="password" id="password" autocomplete="current-password" autofocus placeholder="Password" />
      <button type="submit" class="btn-primary" id="submitBtn">Continue</button>
    </form>
    <p id="err"></p>
  </div>
  <script>
    document.getElementById('loginForm').onsubmit = async (e) => {
      e.preventDefault();
      const btn = document.getElementById('submitBtn');
      const err = document.getElementById('err');
      btn.disabled = true;
      btn.classList.add('is-loading');
      btn.innerHTML = '<span class="spinner" aria-hidden="true"></span><span>Signing in…</span>';
      err.textContent = '';
      try {
        const r = await fetch('/v1/dashboard/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: document.getElementById('password').value }),
        });
        if (!r.ok) {
          err.textContent = r.status === 401 ? 'Wrong password' : 'Login failed';
          btn.disabled = false;
          btn.classList.remove('is-loading');
          btn.textContent = 'Continue';
          return;
        }
        window.location.reload();
      } catch (_) {
        err.textContent = 'Connection error';
        btn.disabled = false;
        btn.classList.remove('is-loading');
        btn.textContent = 'Continue';
      }
    };
  </script>
</body>
</html>
"""
)


_DASHBOARD_HTML = (
    """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Music Assembly</title>
"""
    + _DASHBOARD_DESIGN_FONTS
    + """
  <style>
"""
    + _DASHBOARD_DESIGN_ROOT_CSS
    + """
    * { box-sizing: border-box; }
    body {
      font-family: var(--font-figtree);
      font-size: 16px;
      line-height: 1.3;
      background: var(--color-cream-paper);
      color: var(--color-midnight-ink);
      margin: 0;
      padding: 0 0 56px;
    }
    .page {
      max-width: var(--page-max-width);
      margin: 0 auto;
      padding: 0 32px;
    }
    h1, h2, h3 {
      font-family: var(--font-figtree);
      font-weight: 600;
      color: var(--color-midnight-ink);
      letter-spacing: -0.02em;
    }
    h2 {
      font-size: 18px;
      line-height: 1.25;
      margin: 0 0 4px;
    }
    h2::after { display: none; }
    h3 {
      font-size: 15px;
      line-height: 1.3;
      margin: 16px 0 8px;
    }
    .card-desc {
      color: var(--color-smoke);
      font-size: 14px;
      line-height: 1.4;
      margin: 0 0 20px;
    }
    .muted {
      color: var(--color-smoke);
      font-size: 14px;
      line-height: 1.3;
    }
    .site-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      padding: 14px 0;
      margin: 0 0 8px;
      border-bottom: 1px solid var(--color-stone-mist);
      background: transparent;
      border-radius: 0;
    }
    .wordmark {
      font-size: 15px;
      font-weight: 600;
      letter-spacing: -0.01em;
      margin: 0;
    }
    .wordmark-a { color: var(--color-midnight-ink); }
    .wordmark-b { color: var(--color-deep-forest-teal); }
    .nav-actions { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
    .nav-link {
      background: transparent;
      color: var(--color-smoke);
      border: none;
      border-radius: var(--radius-buttons);
      padding: 6px 10px;
      font-size: 13px;
      font-weight: 500;
    }
    .nav-link:hover { color: var(--color-midnight-ink); background: var(--color-pale-lavender-tint); }
    .stats-strip {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 0 0 20px;
    }
    .stat-chip {
      display: inline-flex;
      align-items: baseline;
      gap: 6px;
      padding: 6px 12px;
      background: var(--color-white);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-badges);
      font-size: 13px;
    }
    .stat-label { color: var(--color-smoke); }
    .stat-value { font-weight: 600; color: var(--color-midnight-ink); font-variant-numeric: tabular-nums; }
    .main-nav {
      display: inline-flex;
      gap: 2px;
      padding: 3px;
      margin: 0 0 20px;
      background: var(--color-white);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
    }
    .main-tab {
      background: transparent;
      color: var(--color-smoke);
      border: none;
      border-radius: calc(var(--radius-buttons) - 2px);
      padding: 8px 16px;
      font-size: 14px;
      font-weight: 500;
      text-decoration: none;
    }
    .main-tab:hover { color: var(--color-midnight-ink); }
    .main-tab.active {
      background: var(--color-midnight-ink);
      color: var(--color-white);
      font-weight: 600;
      text-decoration: none;
    }
    .main-section { display: none; }
    .main-section.active { display: block; }
    .job-nav {
      display: flex;
      gap: 16px;
      margin: 0 0 16px;
      border-bottom: 1px solid var(--color-stone-mist);
    }
    .job-tab {
      background: transparent;
      color: var(--color-smoke);
      border: none;
      padding: 0 0 10px;
      font-size: 14px;
      font-weight: 500;
      margin: 0;
      text-decoration: none;
    }
    .job-tab:hover { color: var(--color-midnight-ink); }
    .job-tab.active {
      color: var(--color-midnight-ink);
      font-weight: 600;
      box-shadow: inset 0 -2px 0 var(--color-deep-forest-teal);
      text-decoration: none;
    }
    .job-panel { display: none; }
    .job-panel.active { display: block; }
    .create-grid {
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 16px;
      align-items: start;
    }
    .form-stack { display: flex; flex-direction: column; gap: 14px; }
    .form-row-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .form-row-3 {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
    }
    details.advanced {
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      padding: 0 14px;
      background: var(--color-cream-paper);
    }
    details.advanced summary {
      cursor: pointer;
      padding: 12px 0;
      font-size: 13px;
      font-weight: 500;
      color: var(--color-smoke);
      list-style: none;
    }
    details.advanced summary::-webkit-details-marker { display: none; }
    details.advanced[open] summary { margin-bottom: 4px; color: var(--color-midnight-ink); }
    details.advanced .form-stack { padding-bottom: 14px; }
    .card-actions { margin-top: 20px; display: flex; flex-direction: column; gap: 12px; }
    .hint { font-size: 12px; color: var(--color-graphite-veil); margin: 4px 0 0; line-height: 1.35; }
    .checkbox-row {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      font-size: 14px;
      color: var(--color-midnight-ink);
    }
    .checkbox-row input { width: auto; margin: 2px 0 0; }
    .checkbox-row label { margin: 0; font-size: 14px; font-weight: 400; text-transform: none; letter-spacing: 0; }
    .card {
      background: var(--color-white);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-cards);
      box-shadow: none;
      padding: 24px;
      margin-bottom: 16px;
    }
    .card label {
      display: block;
      font-size: 13px;
      font-weight: 500;
      text-transform: none;
      letter-spacing: 0;
      color: var(--color-midnight-ink);
      margin-bottom: 6px;
    }
    .card p { margin: 0 0 12px; }
    button, select, input {
      font-family: var(--font-figtree);
      font-size: 14px;
      border-radius: var(--radius-buttons);
    }
    select, input {
      background: var(--color-white);
      color: var(--color-midnight-ink);
      border: 1px solid var(--color-stone-mist);
      padding: 10px 14px;
      width: 100%;
      margin-top: 4px;
    }
    select:focus, input:focus {
      outline: none;
      border-color: var(--color-midnight-ink);
    }
    button {
      cursor: pointer;
      border: none;
      background: transparent;
      padding: 0;
      font-weight: 400;
    }
    button:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-primary, #runBtn, #extendBtn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: var(--color-lavender-whisper);
      color: var(--color-midnight-ink);
      border: 1px solid var(--color-midnight-ink);
      border-radius: var(--radius-buttons);
      padding: 10px 18px;
      font-size: 14px;
      font-weight: 600;
      text-transform: none;
    }
    .btn-primary:hover, #runBtn:hover, #extendBtn:hover { background: var(--color-lavender-light); }
    .btn-secondary {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: var(--color-white);
      color: var(--color-midnight-ink);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      padding: 10px 18px;
      font-size: 14px;
      font-weight: 500;
    }
    .btn-secondary:hover { border-color: var(--color-midnight-ink); }
    .btn-ghost {
      background: transparent;
      color: var(--color-midnight-ink);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      padding: 8px 16px;
      font-size: 14px;
      font-weight: 500;
      text-decoration: none;
    }
    .btn-ghost:hover { border-color: var(--color-midnight-ink); background: var(--color-pale-lavender-tint); text-decoration: none; }
    button.secondary, .tab, .subtab, button.copy-btn, button.danger {
      background: transparent;
      color: var(--color-smoke);
      border: none;
      padding: 6px 10px;
      font-size: 13px;
      font-weight: 500;
      text-decoration: none;
      border-radius: var(--radius-small);
    }
    button.secondary:hover, .tab:hover, .subtab:hover,
    button.copy-btn:hover, button.danger:hover:not(:disabled) {
      color: var(--color-midnight-ink);
      background: var(--color-pale-lavender-tint);
      text-decoration: none;
    }
    button.danger { color: var(--color-midnight-ink); }
    button.danger:disabled { opacity: 0.4; }
    .library-tabs, .tabs {
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
      margin: 0 0 16px;
      padding: 0;
    }
    .tab, .subtab { margin: 0; }
    .tab.active, .subtab.active {
      color: var(--color-midnight-ink);
      font-weight: 600;
      background: var(--color-pale-lavender-tint);
      text-decoration: none;
      box-shadow: none;
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td {
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--color-stone-mist);
      vertical-align: top;
    }
    th {
      font-weight: 500;
      color: var(--color-smoke);
      font-size: 12px;
      text-transform: none;
      letter-spacing: 0;
    }
    tbody tr:hover td { background: var(--color-pale-lavender-tint); }
    .bar {
      height: 6px;
      background: var(--color-stone-mist);
      overflow: hidden;
      margin-top: 8px;
      border-radius: var(--radius-badges);
    }
    .bar > span {
      display: block;
      height: 100%;
      background: var(--color-deep-forest-teal);
      transition: width 0.3s;
    }
    .status-running { font-weight: 600; color: var(--color-midnight-ink); }
    .status-succeeded { font-weight: 500; color: var(--color-charcoal); }
    .status-failed { font-weight: 600; color: var(--color-midnight-ink); text-decoration: underline; }
    .status-cancelled { font-weight: 400; color: var(--color-smoke); }
    .status-cancelling { font-weight: 600; color: var(--color-charcoal); }
    .status-unknown { color: var(--color-smoke); }
    tr.is-running .bar > span { animation: barPulse 1.6s ease-in-out infinite; }
    @keyframes barPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
    .job-stage { display: block; font-size: 12px; margin-top: 4px; word-break: break-word; white-space: pre-wrap; color: var(--color-smoke); }
    .job-updated { font-size: 12px; color: var(--color-graphite-veil); }
    .videos { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px; }
    .videos .video-card img {
      width: 100%;
      aspect-ratio: 16/9;
      object-fit: cover;
      background: var(--color-stone-mist);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-images);
    }
    #authError {
      display: none;
      margin: 0 0 20px;
      padding: 16px 20px;
      border-radius: var(--radius-cards);
      background: var(--color-pale-lavender-tint);
      border: 1px solid var(--color-stone-mist);
      color: var(--color-charcoal);
    }
    #authError.visible { display: block; }
    code {
      font-family: ui-monospace, monospace;
      font-size: 13px;
      color: var(--color-charcoal);
    }
    pre { white-space: pre-wrap; word-break: break-word; }
    .panel { display: none; }
    .panel.active { display: block; }
    .obs-bar {
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      padding: 12px 50px;
      background: var(--color-white);
      color: var(--color-smoke);
      font-size: 11px;
      letter-spacing: 0.06em;
      border-top: 1px solid var(--color-stone-mist);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px 20px;
      z-index: 50;
    }
    .obs-bar strong { color: var(--color-midnight-ink); font-weight: 600; }
    .obs-bar.is-fetching .obs-activity-dot {
      background: var(--color-deep-forest-teal);
      animation: ui-pulse 1s ease-in-out infinite;
    }
    .obs-activity-dot {
      display: inline-block;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--color-graphite-veil);
      margin-right: 6px;
      vertical-align: middle;
    }
    .obs-version {
      margin-left: auto;
      color: var(--color-graphite-veil);
      font-family: ui-monospace, monospace;
      font-size: 11px;
      white-space: nowrap;
    }
    .obs-hit { color: var(--color-deep-forest-teal); font-weight: 600; }
    .obs-miss { color: var(--color-graphite-veil); }
    .list-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 0;
      border-bottom: 1px solid var(--color-stone-mist);
      cursor: pointer;
      gap: 20px;
    }
    .list-row:hover { background: var(--color-pale-lavender-tint); }
    .badges { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
    .badge {
      font-size: 11px;
      font-weight: 500;
      padding: 2px 8px;
      color: var(--color-deep-forest-teal);
      background: var(--color-pale-lavender-tint);
      border: none;
      border-radius: var(--radius-badges);
    }
    .detail {
      display: none;
      padding: 16px 0 12px;
      border-bottom: 1px solid var(--color-stone-mist);
      margin-bottom: 12px;
    }
    .detail.open { display: block; }
    .detail video {
      width: 100%;
      max-height: 420px;
      background: var(--color-midnight-ink);
      margin: 12px 0;
      border-radius: var(--radius-images);
    }
    .detail .desc {
      max-height: 200px;
      overflow: auto;
      font-size: 14px;
      white-space: pre-wrap;
      background: transparent;
      padding: 0;
      border: none;
      color: var(--color-smoke);
      font-family: var(--font-eb-garamond), Georgia, serif;
    }
    .asset-table { max-height: 400px; overflow: auto; }
    .modal {
      position: fixed;
      inset: 0;
      background: rgba(26, 26, 26, 0.35);
      backdrop-filter: blur(6px);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 100;
      padding: 20px;
    }
    .modal.open { display: flex; }
    .modal-inner {
      background: var(--color-cream-paper);
      max-width: min(960px, 100%);
      max-height: 90vh;
      overflow: auto;
      padding: var(--card-padding);
      position: relative;
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-cards);
      box-shadow: none;
    }
    .modal img {
      max-width: 100%;
      height: auto;
      display: block;
      border-radius: var(--radius-images);
    }
    .modal-close {
      position: absolute;
      top: 16px;
      right: 16px;
      background: transparent;
      color: var(--color-midnight-ink);
      border: none;
      width: auto;
      height: auto;
      cursor: pointer;
      font-size: 24px;
      line-height: 1;
      padding: 0;
    }
    .subtabs { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 20px; }
    .subtab { font-size: 14px; }
    .loading { color: var(--color-smoke); font-style: normal; }
    .loading-block {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      padding: 32px 16px;
      color: var(--color-smoke);
      font-size: 14px;
    }
    .spinner {
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid var(--color-stone-mist);
      border-top-color: var(--color-deep-forest-teal);
      border-radius: 50%;
      animation: ui-spin 0.65s linear infinite;
      flex-shrink: 0;
    }
    .spinner-md { width: 22px; height: 22px; border-width: 2px; }
    .spinner-btn { margin-right: 2px; }
    @keyframes ui-spin { to { transform: rotate(360deg); } }
    @keyframes ui-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
    @keyframes ui-shimmer {
      0% { background-position: 200% 0; }
      100% { background-position: -200% 0; }
    }
    button.is-loading, .nav-link.is-loading {
      cursor: wait;
      pointer-events: none;
    }
    button.is-loading {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }
    .main-tab.is-loading, .job-tab.is-loading, .tab.is-loading, .subtab.is-loading {
      cursor: wait;
      opacity: 0.7;
    }
    .page-boot {
      position: fixed;
      inset: 0;
      background: var(--color-cream-paper);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 200;
      transition: opacity 0.25s ease, visibility 0.25s ease;
    }
    .page-boot.is-hidden {
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
    }
    .page-boot-inner {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 12px;
      color: var(--color-smoke);
      font-size: 14px;
    }
    .stats-strip.is-loading .stat-value {
      color: transparent;
      background: linear-gradient(90deg, var(--color-stone-mist) 25%, #f5f5eb 50%, var(--color-stone-mist) 75%);
      background-size: 200% 100%;
      animation: ui-shimmer 1.2s ease-in-out infinite;
      border-radius: 4px;
      min-width: 1.75em;
      display: inline-block;
    }
    .job-table-wrap { position: relative; }
    .job-table-wrap.is-loading::after {
      content: '';
      position: absolute;
      inset: 0;
      background: rgba(255, 255, 235, 0.72);
      border-radius: var(--radius-small);
      pointer-events: none;
      z-index: 1;
    }
    .job-table-wrap.is-loading::before {
      content: '';
      position: absolute;
      top: 28px;
      left: 50%;
      margin-left: -11px;
      width: 22px;
      height: 22px;
      border: 2px solid var(--color-stone-mist);
      border-top-color: var(--color-deep-forest-teal);
      border-radius: 50%;
      animation: ui-spin 0.65s linear infinite;
      z-index: 2;
    }
    .table-empty {
      padding: 28px 12px;
      text-align: center;
      color: var(--color-smoke);
      font-size: 14px;
    }
    .list-row.is-loading { opacity: 0.55; pointer-events: none; }
    .modal-body-loading { min-height: 120px; display: flex; align-items: center; justify-content: center; }
    .cancel-confirm { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-top: 8px; }
    .cancel-confirm .warn { font-size: 12px; color: var(--color-smoke); }
    .job-table-wrap {
      max-height: min(75vh, 900px);
      overflow: auto;
      border: none;
      border-radius: 0;
      margin-top: 12px;
      background: transparent;
    }
    .job-table-wrap.expanded { max-height: none; }
    .job-table-wrap table { margin: 0; }
    .job-table-wrap thead th {
      position: sticky;
      top: 0;
      background: var(--color-cream-paper);
      z-index: 1;
    }
    .job-table-wrap td.job-progress { min-width: 12rem; max-width: 28rem; vertical-align: top; }
    .json-block {
      margin-top: 12px;
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      background: var(--color-cream-paper);
      overflow: hidden;
    }
    .json-block:has(pre:empty) { display: none; }
    .json-block-header {
      display: flex;
      justify-content: flex-end;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      border-bottom: 1px solid var(--color-stone-mist);
      background: var(--color-white);
    }
    .json-block pre {
      margin: 0;
      padding: 12px;
      max-height: 160px;
      overflow: auto;
      font-size: 12px;
      line-height: 1.4;
      background: transparent;
      color: var(--color-charcoal);
      font-family: ui-monospace, monospace;
      border-top: none;
    }
    .json-block.expanded pre { max-height: none; }
    button.copy-btn { font-size: 14px; }
    button.copy-btn.copied { color: var(--color-deep-forest-teal); font-weight: 600; }
    .job-toolbar {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
      margin-bottom: 12px;
    }
    .job-toolbar label {
      font-size: 13px;
      color: var(--color-smoke);
      margin: 0;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .job-toolbar select { width: auto; margin: 0; }
    .job-check-th, .job-check { width: 2.25rem; text-align: center; vertical-align: middle; }
    .job-check input, .job-check-th input { cursor: pointer; width: 1rem; height: 1rem; margin: 0; }
    .section-card { margin-bottom: 0; padding: 0; border: none; background: transparent; }
    a { color: var(--color-deep-forest-teal); text-decoration: none; }
    a:hover { text-decoration: underline; text-underline-offset: 2px; }
    @media (max-width: 720px) {
      .page { padding: 0 16px; }
      .obs-bar { padding: 10px 16px; }
      .create-grid { grid-template-columns: 1fr; }
      .form-row-2, .form-row-3 { grid-template-columns: 1fr; }
      .btn-primary, #runBtn, #extendBtn { width: 100%; }
      .main-nav { display: flex; width: 100%; }
      .main-tab { flex: 1; text-align: center; padding: 10px 8px; }
    }
  </style>
</head>
<body>
  <div id="pageBoot" class="page-boot" role="status" aria-live="polite" aria-busy="true">
    <div class="page-boot-inner">
      <span class="spinner spinner-md" aria-hidden="true"></span>
      <span>Loading dashboard…</span>
    </div>
  </div>
  <div class="page">
  <header class="site-header" aria-label="Dashboard">
    <p class="wordmark"><span class="wordmark-a">Music</span><span class="wordmark-b">Assembly</span></p>
    <div class="nav-actions">
      <button type="button" class="nav-link" id="refreshBtn" title="Reload jobs, inventory, and active tab">Refresh</button>
      <button type="button" class="nav-link" id="logoutBtn">Sign out</button>
    </div>
  </header>

  <div class="stats-strip" id="statsStrip" aria-label="Pipeline status">
    <div class="stat-chip"><span class="stat-label">Running</span><span class="stat-value" id="statRunning">0</span></div>
    <div class="stat-chip"><span class="stat-label">Ready backgrounds</span><span class="stat-value" id="statPostProcessed">—</span></div>
    <div class="stat-chip"><span class="stat-label">To extend</span><span class="stat-value" id="statExtendPending">—</span></div>
    <div class="stat-chip"><span class="stat-label">Tracks</span><span class="stat-value" id="statMusic">—</span></div>
    <div class="stat-chip"><span class="stat-label">Videos</span><span class="stat-value" id="statVideos">—</span></div>
  </div>

  <div id="authError" class="muted"></div>

  <nav class="main-nav" role="tablist" aria-label="Main sections">
    <button type="button" class="main-tab active" data-section="jobs">Jobs</button>
    <button type="button" class="main-tab" data-section="create">New run</button>
    <button type="button" class="main-tab" data-section="library">Library</button>
  </nav>

  <section id="sectionJobs" class="main-section active">
    <div class="card section-card">
      <nav class="job-nav" role="tablist" aria-label="Job type">
        <button type="button" class="job-tab active" data-job="assembly">Assembly</button>
        <button type="button" class="job-tab" data-job="extend">Extend</button>
      </nav>

      <div id="jobPanelAssembly" class="job-panel active">
        <div class="job-toolbar">
          <h2 style="margin:0;flex:1">Assembly jobs <span class="muted" id="assemblyJobCount" style="font-weight:400;font-size:13px"></span></h2>
          <label>Status
            <select id="jobFilter"><option value="">All</option><option>running</option><option>succeeded</option><option>failed</option></select>
          </label>
          <button type="button" class="secondary" id="expandAssemblyTable">Expand table</button>
          <button type="button" class="danger" id="cancelSelectedAssembly" disabled>Cancel selected</button>
        </div>
        <div class="job-table-wrap" id="assemblyTableWrap">
          <table><thead><tr><th class="job-check-th"><input type="checkbox" id="selectAllAssembly" title="Select all visible running jobs" aria-label="Select all visible running assembly jobs"></th><th>Execution</th><th>Status</th><th>Progress</th><th>Started</th><th></th></tr></thead><tbody id="jobsBody"></tbody></table>
        </div>
      </div>

      <div id="jobPanelExtend" class="job-panel">
        <div class="job-toolbar">
          <h2 style="margin:0;flex:1">Extend jobs <span class="muted" id="extendJobCount" style="font-weight:400;font-size:13px"></span></h2>
          <button type="button" class="secondary" id="expandExtendTable">Expand table</button>
          <button type="button" class="danger" id="cancelSelectedExtend" disabled>Cancel selected</button>
        </div>
        <div class="job-table-wrap" id="extendTableWrap">
          <table><thead><tr><th class="job-check-th"><input type="checkbox" id="selectAllExtend" title="Select all visible running jobs" aria-label="Select all visible running extend jobs"></th><th>Run</th><th>Status</th><th>Progress</th><th>Started</th><th></th></tr></thead><tbody id="extendBody"></tbody></table>
        </div>
      </div>
    </div>
  </section>

  <section id="sectionCreate" class="main-section">
    <div class="create-grid">
      <div class="card">
        <h2>Assemble video</h2>
        <p class="card-desc">Pick a channel and background folder, then start encoding on Cloud Run.</p>
        <div class="form-stack">
          <div>
            <label for="runChannel">YouTube channel</label>
            <select id="runChannel" required><option value="">Select channel…</option></select>
          </div>
          <div>
            <label for="runChannelCustom">Or new channel slug</label>
            <input id="runChannelCustom" placeholder="e.g. nappabeats"/>
            <p class="hint">Optional — overrides the dropdown above.</p>
          </div>
          <div>
            <label for="runImagesFolder">Background folder</label>
            <select id="runImagesFolder" required><option value="">Loading…</option></select>
          </div>
          <details class="advanced">
            <summary>Advanced options</summary>
            <div class="form-stack">
              <div class="checkbox-row">
                <input type="checkbox" id="runQueueYoutube" checked/>
                <label for="runQueueYoutube">Queue for YouTube upload when finished</label>
              </div>
              <div>
                <label for="runThumb">Thumbnail text</label>
                <input id="runThumb" value="OMYO"/>
              </div>
              <div class="form-row-3">
                <div>
                  <label for="runDuration">Duration (min)</label>
                  <input id="runDuration" type="number" value="90"/>
                </div>
                <div>
                  <label for="runVariance">Variance (min)</label>
                  <input id="runVariance" type="number" value="15"/>
                </div>
                <div>
                  <label for="runCount">Parallel jobs</label>
                  <select id="runCount">
                    <option value="1">1</option>
                    <option value="2">2</option>
                    <option value="3">3</option>
                    <option value="5">5</option>
                    <option value="10">10</option>
                  </select>
                </div>
              </div>
            </div>
          </details>
        </div>
        <div class="card-actions">
          <button id="runBtn" class="btn-primary">Start assembly</button>
          <div class="json-block">
            <div class="json-block-header">
              <button type="button" class="secondary expand-btn" data-expand-target="runResult">Expand</button>
              <button type="button" class="secondary copy-btn" data-copy-target="runResult">Copy</button>
            </div>
            <pre id="runResult" class="muted"></pre>
          </div>
        </div>
      </div>

      <div class="card">
        <h2>Extend backgrounds</h2>
        <p class="card-desc">Gemini extend from <code>pre-processed/</code> → <code>post-processed/</code>. <strong id="extendPending">…</strong> waiting.</p>
        <div class="form-stack">
          <div>
            <label for="extendLimit">Batch size</label>
            <select id="extendLimit">
              <option value="1">1 image</option>
              <option value="3">3 images</option>
              <option value="5">5 images</option>
              <option value="10">10 images</option>
              <option value="all">All pending</option>
            </select>
          </div>
        </div>
        <div class="card-actions">
          <button id="extendBtn" class="btn-secondary">Start extend</button>
          <div class="json-block">
            <div class="json-block-header">
              <button type="button" class="secondary expand-btn" data-expand-target="extendResult">Expand</button>
              <button type="button" class="secondary copy-btn" data-copy-target="extendResult">Copy</button>
            </div>
            <pre id="extendResult" class="muted"></pre>
          </div>
        </div>
      </div>
    </div>
    <pre id="inventory" class="muted" hidden aria-hidden="true"></pre>
  </section>

  <section id="sectionLibrary" class="main-section">
    <nav class="library-tabs tabs" role="tablist" aria-label="Library">
      <button type="button" class="tab active" data-tab="videos">Videos</button>
      <button type="button" class="tab" data-tab="assets">Backgrounds</button>
      <button type="button" class="tab" data-tab="obs">Debug</button>
    </nav>

    <div id="panelVideos" class="panel card active">
      <div class="job-toolbar" style="margin-top:0">
        <h2 style="margin:0;flex:1">Music videos</h2>
        <label>Channel
          <select id="videoChannel"><option value="">All channels</option></select>
        </label>
      </div>
      <p class="card-desc">Click a row to preview metadata and playback.</p>
      <div id="videoList"><p class="muted">Select Library → Videos to load.</p></div>
    </div>

    <div id="panelAssets" class="panel card">
      <h2>Background images</h2>
      <p class="card-desc">Click a filename to preview. Images load on demand.</p>
      <div class="subtabs" id="assetPools">
        <button type="button" class="subtab secondary active" data-pool="pre-processed">Pre-processed</button>
        <button type="button" class="subtab secondary" data-pool="post-processed">Post-processed</button>
        <button type="button" class="subtab secondary" data-pool="pre-used">Pre-used</button>
        <button type="button" class="subtab secondary" data-pool="post-used">Post-used</button>
      </div>
      <div id="assetList"><p class="muted">Select a pool above to load filenames.</p></div>
    </div>

    <div id="panelObs" class="panel card">
      <h2>Debug</h2>
      <p class="card-desc">API observability and raw inventory JSON.</p>
      <pre id="obsDetail" class="muted">Loading…</pre>
      <h3>Recent API calls</h3>
      <table><thead><tr><th>Time</th><th>Endpoint</th><th>ms</th><th>Cache</th></tr></thead><tbody id="obsFetches"></tbody></table>
    </div>
  </section>

  <div id="modal" class="modal" aria-hidden="true">
    <div class="modal-inner">
      <button type="button" class="modal-close" id="modalClose" aria-label="Close">×</button>
      <div id="modalBody"></div>
    </div>
  </div>

  </div>

  <div class="obs-bar" id="obsBar">
    <span class="obs-activity-dot" id="obsActivity" aria-hidden="true"></span>
    <span>Poll: <strong id="obsPoll">0</strong></span>
    <span>Last fetch: <strong id="obsLastMs">—</strong></span>
    <span>Cache: <span class="obs-hit" id="obsHits">0 hit</span> / <span class="obs-miss" id="obsMiss">0 miss</span></span>
    <span id="obsRunning" style="display:none">● jobs running</span>
    <span class="obs-version" id="obsVersion" title="API version and Cloud Run revision">v…</span>
  </div>

<script>
const ui = {
  assembly: new Map(),
  extend: new Map(),
  pollTimer: null,
  lastStatsAt: 0,
  jobFilter: '',
  tabsLoaded: { videos: false, assets: false, obs: false },
  assetPool: 'pre-processed',
  videoDetails: new Map(),
  videoChannels: new Map(),
  cancelPending: null,
  selectedJobs: new Set(),
};
const obs = { fetches: [], hits: 0, misses: 0, polls: 0, lastMs: null, lastError: null };
let apiInflight = 0;

function spinnerHtml(cls) {
  return '<span class="spinner' + (cls ? ' ' + cls : '') + '" aria-hidden="true"></span>';
}
function loadingBlockHtml(msg) {
  return '<div class="loading-block" role="status" aria-live="polite">' + spinnerHtml('spinner-md') + '<span>' + esc(msg) + '</span></div>';
}
function setBtnLoading(btn, loading, label) {
  if (!btn) return;
  if (loading) {
    if (btn.dataset.origHtml === undefined) btn.dataset.origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add('is-loading');
    btn.setAttribute('aria-busy', 'true');
    btn.innerHTML = spinnerHtml('spinner-btn') + '<span>' + esc(label || 'Working…') + '</span>';
  } else {
    btn.disabled = false;
    btn.classList.remove('is-loading');
    btn.removeAttribute('aria-busy');
    if (btn.dataset.origHtml !== undefined) {
      btn.innerHTML = btn.dataset.origHtml;
      delete btn.dataset.origHtml;
    }
  }
}
function setTabLoading(btn, loading) {
  if (!btn) return;
  btn.classList.toggle('is-loading', loading);
  btn.setAttribute('aria-busy', loading ? 'true' : 'false');
}
function setStatsLoading(loading) {
  document.getElementById('statsStrip')?.classList.toggle('is-loading', loading);
}
function setJobsLoading(loading) {
  document.getElementById('assemblyTableWrap')?.classList.toggle('is-loading', loading);
  document.getElementById('extendTableWrap')?.classList.toggle('is-loading', loading);
}
function hidePageBoot() {
  const boot = document.getElementById('pageBoot');
  if (!boot) return;
  boot.classList.add('is-hidden');
  boot.setAttribute('aria-busy', 'false');
}
function notifyApiActivity() {
  document.getElementById('obsBar')?.classList.toggle('is-fetching', apiInflight > 0);
}

function showAuthError(msg) {
  document.getElementById('authError').textContent = msg;
  document.getElementById('authError').classList.add('visible');
}
function clearAuthError() { document.getElementById('authError').classList.remove('visible'); }

async function api(path, opts={}) {
  apiInflight++;
  notifyApiActivity();
  try {
    const bust = path.includes('?') ? '&' : '?';
    const url = path + (path.includes('dashboard/snapshot') || path.includes('/progress')
      ? bust + '_=' + Date.now() : '');
    const t0 = performance.now();
    const r = await fetch(url, { ...opts, credentials: 'same-origin', headers: { 'Content-Type': 'application/json', ...(opts.headers||{}) } });
    const ms = Math.round(performance.now() - t0);
    const cache = r.headers.get('X-Cache') || (opts.expectJson === false ? '—' : '');
    if (cache === 'HIT') obs.hits++;
    if (cache === 'MISS') obs.misses++;
    obs.lastMs = ms;
    obs.fetches.unshift({ at: new Date().toLocaleTimeString(), path, ms, cache });
    if (obs.fetches.length > 25) obs.fetches.pop();
    renderObsBar();
    if (r.status === 401) { window.location.reload(); throw new Error('Session expired'); }
    if (!r.ok) { obs.lastError = path + ' ' + r.status; throw new Error(await r.text()); }
    clearAuthError();
    if (opts.expectJson === false) return r;
    return r.json();
  } finally {
    apiInflight--;
    notifyApiActivity();
  }
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
}
function copyFromPre(preId, btn) {
  const el = document.getElementById(preId);
  if (!el || !el.textContent.trim()) return;
  navigator.clipboard.writeText(el.textContent).then(() => {
    if (!btn) return;
    const prev = btn.textContent;
    btn.textContent = 'Copied';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = prev; btn.classList.remove('copied'); }, 1200);
  }).catch(() => {});
}
document.querySelectorAll('.copy-btn[data-copy-target]').forEach(btn => {
  btn.onclick = () => copyFromPre(btn.dataset.copyTarget, btn);
});
function toggleExpandBlock(targetId, btn) {
  const pre = document.getElementById(targetId);
  const block = pre?.closest('.json-block');
  if (!block) return;
  const expanded = block.classList.toggle('expanded');
  if (btn) btn.textContent = expanded ? 'Collapse' : 'Expand';
}
document.querySelectorAll('.expand-btn[data-expand-target]').forEach(btn => {
  btn.onclick = () => toggleExpandBlock(btn.dataset.expandTarget, btn);
});
function toggleTableExpand(wrapId, btnId) {
  const wrap = document.getElementById(wrapId);
  const btn = document.getElementById(btnId);
  if (!wrap || !btn) return;
  const expanded = wrap.classList.toggle('expanded');
  btn.textContent = expanded ? 'Collapse table' : 'Expand table';
}
document.getElementById('expandAssemblyTable').onclick = () => toggleTableExpand('assemblyTableWrap', 'expandAssemblyTable');
document.getElementById('expandExtendTable').onclick = () => toggleTableExpand('extendTableWrap', 'expandExtendTable');
function fmtTime(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleString(); } catch (_) { return iso; }
}
function fmtBytes(n) {
  if (n == null) return '';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  return (n/1048576).toFixed(1) + ' MB';
}
function cat() { return '__DEFAULT_CATEGORY__'; }
function runChannel() {
  const custom = document.getElementById('runChannelCustom').value.trim();
  if (custom) return custom;
  return document.getElementById('runChannel').value.trim();
}
function videoChannelFilter() {
  return document.getElementById('videoChannel').value.trim();
}

async function loadBackgroundFolders() {
  const el = document.getElementById('runImagesFolder');
  const keep = el.value;
  try {
    const d = await api('/v1/background-folders');
    const folders = d.folders || [];
    el.innerHTML = folders.length
      ? ''
      : '<option value="">No folders on R2</option>';
    for (const f of folders) {
      el.innerHTML += '<option value="' + esc(f) + '">' + esc(f) + '</option>';
    }
    const def = '__DEFAULT_CATEGORY__';
    if (keep && folders.includes(keep)) el.value = keep;
    else if (folders.includes(def)) el.value = def;
    else if (folders.length) el.value = folders[0];
  } catch (e) {
    console.warn('background-folders', e);
    el.innerHTML = '<option value="__DEFAULT_CATEGORY__">__DEFAULT_CATEGORY__</option>';
  }
}

async function loadChannelOptions() {
  try {
    const d = await api('/v1/channels?category=' + encodeURIComponent(cat()));
    const rows = (d.channel_details && d.channel_details.length)
      ? d.channel_details
      : (d.channels || []).map(id => ({ id, name: id }));
    for (const selId of ['runChannel', 'videoChannel']) {
      const el = document.getElementById(selId);
      const keep = el.value;
      const allOpt = selId === 'videoChannel'
        ? '<option value="">All channels</option>'
        : '<option value="">Select channel…</option>';
      el.innerHTML = allOpt;
      for (const ch of rows) {
        const id = ch.id || ch;
        const name = ch.name || id;
        const label = name === id ? id : (name + ' (' + id + ')');
        el.innerHTML += '<option value="' + esc(id) + '">' + esc(label) + '</option>';
      }
      if (keep) el.value = keep;
    }
  } catch (e) { console.warn('channels', e); }
}

function renderObsBar() {
  document.getElementById('obsPoll').textContent = obs.polls;
  document.getElementById('obsLastMs').textContent = obs.lastMs != null ? obs.lastMs + 'ms' : '—';
  document.getElementById('obsHits').textContent = obs.hits + ' hit';
  document.getElementById('obsMiss').textContent = obs.misses + ' miss';
}
async function loadVersionInfo() {
  try {
    const v = await api('/v1/version');
    const label = v.dashboard || ('v' + (v.version || '?') + ' · ' + (v.revision || 'local'));
    const el = document.getElementById('obsVersion');
    el.textContent = label;
    el.title = 'music-assembly-api ' + label + (v.build && v.build !== v.revision ? ' (build ' + v.build + ')' : '');
  } catch (_) {
    document.getElementById('obsVersion').textContent = 'v?';
  }
}
function renderObsPanel() {
  const tb = document.getElementById('obsFetches');
  tb.innerHTML = obs.fetches.map(f =>
    '<tr><td class="muted">' + esc(f.at) + '</td><td><code>' + esc(f.path) + '</code></td><td>' + f.ms + '</td><td>' + esc(f.cache || '—') + '</td></tr>'
  ).join('');
}
async function loadObservability() {
  const detail = document.getElementById('obsDetail');
  detail.innerHTML = loadingBlockHtml('Loading observability…');
  try {
    const d = await api('/v1/observability');
    detail.textContent = JSON.stringify(d, null, 2);
    renderObsPanel();
  } catch (e) {
    detail.textContent = String(e);
  }
}

function setStat(id, v) {
  const el = document.getElementById(id);
  if (el) el.textContent = (v == null || v === '') ? '—' : v;
}
function updateStatsStrip() {
  let running = 0;
  for (const [, tr] of ui.assembly) if (tr.classList.contains('is-running')) running++;
  for (const [, tr] of ui.extend) if (tr.classList.contains('is-running')) running++;
  setStat('statRunning', running);
}
function applyInventory(d) {
  const inv = d.inventory || {};
  document.getElementById('inventory').textContent = JSON.stringify(inv, null, 2);
  setStat('statPostProcessed', inv['post-processed']);
  setStat('statMusic', inv.music ?? inv['music']);
  setStat('statVideos', inv['music-video']);
  if (typeof d.extend_pending === 'number') {
    setStat('statExtendPending', d.extend_pending);
    document.getElementById('extendPending').textContent = d.extend_pending;
  }
}

async function refreshStats() {
  const d = await api('/v1/dashboard/stats?category=' + encodeURIComponent(cat()));
  applyInventory(d);
  ui.lastStatsAt = Date.now();
  return d;
}

function isCancellableStatus(st) {
  return st === 'running' || st === 'cancelling' || st === 'unknown';
}
function jobCheckboxHtml(row) {
  const id = row.execution_id;
  if (!isCancellableStatus(row.status || '')) {
    return '<td class="job-check muted">—</td>';
  }
  const checked = ui.selectedJobs.has(id) ? ' checked' : '';
  return '<td class="job-check"><input type="checkbox" class="job-select-cb" data-id="' + esc(id) + '"' + checked + ' aria-label="Select ' + esc(id) + '"></td>';
}
function selectedRunningInMap(map) {
  return [...ui.selectedJobs].filter(id => {
    const tr = map.get(id);
    return tr && tr.classList.contains('is-running');
  });
}
function updateBulkCancelButtons() {
  document.getElementById('cancelSelectedAssembly').disabled = selectedRunningInMap(ui.assembly).length === 0;
  document.getElementById('cancelSelectedExtend').disabled = selectedRunningInMap(ui.extend).length === 0;
}
function updateSelectAllCheckbox(selectAllId, map) {
  const el = document.getElementById(selectAllId);
  if (!el) return;
  let visible = 0;
  let selected = 0;
  for (const [id, tr] of map) {
    if (tr.style.display === 'none' || !tr.classList.contains('is-running')) continue;
    visible++;
    if (ui.selectedJobs.has(id)) selected++;
  }
  el.checked = visible > 0 && selected === visible;
  el.indeterminate = selected > 0 && selected < visible;
  el.disabled = visible === 0;
}
function bindJobCheckboxHandlers(root) {
  root.querySelectorAll('.job-select-cb').forEach(cb => {
    cb.onchange = () => {
      if (cb.checked) ui.selectedJobs.add(cb.dataset.id);
      else ui.selectedJobs.delete(cb.dataset.id);
      updateBulkCancelButtons();
      updateSelectAllCheckbox('selectAllAssembly', ui.assembly);
      updateSelectAllCheckbox('selectAllExtend', ui.extend);
    };
  });
}
function bindSelectAllCheckbox(selectAllId, map) {
  const el = document.getElementById(selectAllId);
  el.onchange = () => {
    const check = el.checked;
    for (const [id, tr] of map) {
      if (tr.style.display === 'none' || !tr.classList.contains('is-running')) continue;
      const cb = tr.querySelector('.job-select-cb');
      if (!cb) continue;
      cb.checked = check;
      if (check) ui.selectedJobs.add(id);
      else ui.selectedJobs.delete(id);
    }
    updateBulkCancelButtons();
    updateSelectAllCheckbox('selectAllAssembly', ui.assembly);
    updateSelectAllCheckbox('selectAllExtend', ui.extend);
  };
}
async function cancelSelectedJobs(map) {
  const ids = selectedRunningInMap(map);
  if (!ids.length) return;
  if (!confirm('Cancel ' + ids.length + ' running job(s)?')) return;
  const cancelBtn = map === ui.assembly
    ? document.getElementById('cancelSelectedAssembly')
    : document.getElementById('cancelSelectedExtend');
  setBtnLoading(cancelBtn, true, 'Cancelling…');
  try {
    const results = await Promise.allSettled(ids.map(id =>
      api('/v1/jobs/' + encodeURIComponent(id) + '/cancel', {
        method: 'POST',
        body: JSON.stringify({ confirm: true }),
      })
    ));
    const failed = results.filter(r => r.status === 'rejected').length;
    ids.forEach(id => ui.selectedJobs.delete(id));
    ui.cancelPending = null;
    await pollSnapshot({ includeStats: true });
    schedulePoll(1500);
    if (failed) alert(failed + ' cancel request(s) failed');
  } catch (e) {
    alert('Cancel failed: ' + e);
  }
  setBtnLoading(cancelBtn, false);
  updateBulkCancelButtons();
  updateSelectAllCheckbox('selectAllAssembly', ui.assembly);
  updateSelectAllCheckbox('selectAllExtend', ui.extend);
}
function jobActionsHtml(row) {
  const id = row.execution_id;
  const st = row.status || '';
  const active = isCancellableStatus(st);
  if (!active) return '<td class="job-actions muted">—</td>';
  if (ui.cancelPending === id) {
    return '<td class="job-actions"><div class="cancel-confirm">' +
      '<span class="warn">Cancel ' + esc(id) + '?</span>' +
      '<button type="button" class="danger cancel-confirm-btn" data-id="' + esc(id) + '">Yes, cancel</button>' +
      '<button type="button" class="secondary cancel-dismiss-btn">Keep running</button></div></td>';
  }
  return '<td class="job-actions"><button type="button" class="secondary cancel-start-btn" data-id="' + esc(id) + '">Cancel</button></td>';
}
function bindJobActionButtons(root) {
  root.querySelectorAll('.cancel-start-btn').forEach(btn => {
    btn.onclick = () => { ui.cancelPending = btn.dataset.id; rerenderJobTables(); };
  });
  root.querySelectorAll('.cancel-dismiss-btn').forEach(btn => {
    btn.onclick = () => { ui.cancelPending = null; rerenderJobTables(); };
  });
  root.querySelectorAll('.cancel-confirm-btn').forEach(btn => {
    btn.onclick = () => confirmCancelJob(btn.dataset.id);
  });
  bindJobCheckboxHandlers(root);
}
async function confirmCancelJob(executionId) {
  const btn = document.querySelector('.cancel-confirm-btn[data-id="' + executionId + '"]');
  setBtnLoading(btn, true, 'Cancelling…');
  try {
    const r = await api('/v1/jobs/' + encodeURIComponent(executionId) + '/cancel', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    });
    ui.cancelPending = null;
    await pollSnapshot({ includeStats: true });
    schedulePoll(1500);
    console.log('cancelled', r);
  } catch (e) {
    setBtnLoading(btn, false);
    alert('Cancel failed: ' + e);
  }
}
function rerenderJobTables() {
  for (const map of [ui.assembly, ui.extend]) {
    for (const tr of map.values()) {
      const id = tr.dataset.jobId;
      const status = tr.querySelector('.job-status')?.textContent || '';
      let actions = tr.querySelector('.job-actions');
      const html = jobActionsHtml({ execution_id: id, status });
      if (actions) actions.outerHTML = html;
      else tr.insertAdjacentHTML('beforeend', html);
      bindJobActionButtons(tr);
    }
  }
}

function upsertJobRow(tableId, map, row) {
  let tr = map.get(row.execution_id);
  const running = isCancellableStatus(row.status || '');
  if (!running) ui.selectedJobs.delete(row.execution_id);
  const gcpLine = row.gcp_execution_id && row.gcp_execution_id !== row.execution_id
    ? '<br/><span class="muted">' + esc(row.gcp_execution_id) + '</span>' : '';
  const updated = row.updated_at ? '<span class="job-updated">Updated ' + esc(fmtTime(row.updated_at)) + '</span>' : '';
  if (!tr) {
    tr = document.createElement('tr');
    tr.dataset.jobId = row.execution_id;
    tr.innerHTML =
      jobCheckboxHtml(row) +
      '<td class="job-id"><code>' + esc(row.execution_id) + '</code>' + gcpLine + '</td>' +
      '<td class="job-status status-' + esc(row.status) + '">' + esc(row.status) + '</td>' +
      '<td class="job-progress"><div class="bar"><span class="bar-fill"></span></div>' +
      '<span class="job-pct"></span><span class="job-stage"></span>' + updated + '</td>' +
      '<td class="job-started muted"></td>' +
      jobActionsHtml(row);
    map.set(row.execution_id, tr);
    document.getElementById(tableId).prepend(tr);
    bindJobActionButtons(tr);
  } else {
    const checkCell = tr.querySelector('.job-check');
    if (checkCell) checkCell.outerHTML = jobCheckboxHtml(row);
    tr.querySelector('.job-id').innerHTML = '<code>' + esc(row.execution_id) + '</code>' + gcpLine;
    const st = tr.querySelector('.job-status');
    st.className = 'job-status status-' + row.status;
    st.textContent = row.status;
    const actions = tr.querySelector('.job-actions');
    if (actions) actions.outerHTML = jobActionsHtml(row);
  }
  tr.classList.toggle('is-running', running);
  const pct = Number(row.pct) || 0;
  tr.querySelector('.bar-fill').style.width = Math.min(100, Math.max(0, pct)) + '%';
  tr.querySelector('.job-pct').textContent = pct.toFixed(0) + '% ';
  tr.querySelector('.job-stage').textContent = row.stage || (running ? 'Working…' : '');
  tr.querySelector('.job-started').textContent = fmtTime(row.created_at);
  const src = row.status_source && row.status_source !== 'r2' ? ' (' + row.status_source + ')' : '';
  const stCell = tr.querySelector('.job-status');
  if (stCell && src) stCell.title = 'Status from ' + row.status_source;
  tr.style.display = (!ui.jobFilter || row.status === ui.jobFilter) ? '' : 'none';
  bindJobActionButtons(tr);
}
function syncJobTable(tableId, map, rows) {
  const tb = document.getElementById(tableId);
  const countEl = document.getElementById(tableId === 'jobsBody' ? 'assemblyJobCount' : 'extendJobCount');
  if (countEl) {
    const running = rows.filter(r => isCancellableStatus(r.status || '')).length;
    countEl.textContent = rows.length
      ? '(' + rows.length + (running ? ', ' + running + ' running' : '') + ')'
      : '';
  }
  const ids = new Set(rows.map(r => r.execution_id));
  for (const [id, tr] of map) {
    if (!ids.has(id)) {
      tr.remove();
      map.delete(id);
      ui.selectedJobs.delete(id);
    }
  }
  for (const row of rows) upsertJobRow(tableId, map, row);
  for (let i = rows.length - 1; i >= 0; i--) {
    const tr = map.get(rows[i].execution_id);
    if (tr) tb.prepend(tr);
  }
  const wrapId = tableId === 'jobsBody' ? 'assemblyTableWrap' : 'extendTableWrap';
  const emptyId = tableId === 'jobsBody' ? 'assemblyTableEmpty' : 'extendTableEmpty';
  const wrap = document.getElementById(wrapId);
  let emptyEl = document.getElementById(emptyId);
  if (!rows.length) {
    if (!emptyEl && wrap) {
      emptyEl = document.createElement('div');
      emptyEl.id = emptyId;
      emptyEl.className = 'table-empty';
      emptyEl.textContent = tableId === 'jobsBody'
        ? 'No assembly jobs yet. Go to New run to start one.'
        : 'No extend jobs yet.';
      wrap.appendChild(emptyEl);
    }
  } else if (emptyEl) {
    emptyEl.remove();
  }
  updateBulkCancelButtons();
  updateSelectAllCheckbox('selectAllAssembly', ui.assembly);
  updateSelectAllCheckbox('selectAllExtend', ui.extend);
  updateStatsStrip();
}

async function loadVideoList() {
  const el = document.getElementById('videoList');
  el.innerHTML = loadingBlockHtml('Loading videos…');
  let url = '/v1/videos?summary=1';
  const chFilter = videoChannelFilter();
  if (chFilter) url += '&channel=' + encodeURIComponent(chFilter);
  const d = await api(url);
  ui.videoChannels.clear();
  if (!d.videos?.length) { el.innerHTML = '<p class="muted">No videos yet.</p>'; return; }
  el.innerHTML = '';
  for (const v of d.videos) {
    ui.videoChannels.set(v.id, v.channel || '');
    const wrap = document.createElement('div');
    wrap.className = 'video-item';
    wrap.dataset.videoId = v.id;
    const badges = [
      v.channel ? ('ch:' + v.channel) : null,
      v.has_video ? 'mp4' : null,
      v.has_thumbnail ? 'thumb' : null,
      v.has_title ? 'title' : null,
      v.has_description ? 'desc' : null,
    ].filter(Boolean).map(b => '<span class="badge">' + b + '</span>').join('');
    wrap.innerHTML =
      '<div class="list-row video-toggle">' +
        '<div><strong><code>' + esc(v.id) + '</code></strong><div class="badges">' + badges + '</div></div>' +
        '<span class="muted">' + esc(fmtTime(v.last_modified)) + '</span></div>' +
      '<div class="detail" id="detail-' + esc(v.id) + '"><p class="loading">Click row to load…</p></div>';
    wrap.querySelector('.video-toggle').onclick = () => toggleVideoDetail(v.id, wrap);
    el.appendChild(wrap);
  }
  ui.tabsLoaded.videos = true;
}

async function toggleVideoDetail(id, wrap) {
  const detail = wrap.querySelector('.detail');
  const open = detail.classList.toggle('open');
  if (!open) {
    const vid = detail.querySelector('video');
    if (vid) { vid.pause(); vid.removeAttribute('src'); vid.load(); }
    return;
  }
  if (ui.videoDetails.has(id)) return;
  const row = wrap.querySelector('.video-toggle');
  row?.classList.add('is-loading');
  detail.innerHTML = loadingBlockHtml('Loading metadata…');
  try {
    const ch = ui.videoChannels.get(id) || '';
    let url = '/v1/videos/' + encodeURIComponent(id);
    if (ch) url += '?channel=' + encodeURIComponent(ch);
    const v = await api(url);
    ui.videoDetails.set(id, v);
    const track = v.tracklist ? '<h4 style="font-size:.85rem;margin:.75rem 0 .25rem">Tracklist</h4><pre class="desc">' + esc(v.tracklist) + '</pre>' : '';
    detail.innerHTML =
      '<p><strong>' + esc(v.title || id) + '</strong></p>' +
      (v.description ? '<h4 style="font-size:.85rem;margin:.5rem 0 .25rem">Description</h4><div class="desc">' + esc(v.description) + '</div>' : '<p class="muted">No description file</p>') +
      track +
      (v.has_video
        ? '<p style="margin-top:.75rem"><button type="button" class="secondary play-btn">Load video preview</button></p>' +
          '<video controls preload="none" playsinline style="display:none"></video>'
        : '<p class="muted">No MP4 in this folder</p>') +
      '<p class="muted" style="margin-top:.5rem;font-size:.75rem">' + esc(v.r2_prefix) + '</p>';
    const playBtn = detail.querySelector('.play-btn');
  const videoEl = detail.querySelector('video');
    if (playBtn && videoEl) {
      playBtn.onclick = () => {
        setBtnLoading(playBtn, true, 'Loading video…');
        videoEl.style.display = 'block';
        videoEl.src = v.video_url;
        const onReady = () => {
          setBtnLoading(playBtn, false);
          playBtn.style.display = 'none';
          videoEl.play().catch(() => {});
          videoEl.removeEventListener('loadeddata', onReady);
          videoEl.removeEventListener('error', onErr);
        };
        const onErr = () => {
          setBtnLoading(playBtn, false);
          playBtn.textContent = 'Failed to load';
          videoEl.removeEventListener('loadeddata', onReady);
          videoEl.removeEventListener('error', onErr);
        };
        videoEl.addEventListener('loadeddata', onReady);
        videoEl.addEventListener('error', onErr);
      };
    }
  } catch (e) {
    detail.innerHTML = '<p class="muted">' + esc(String(e)) + '</p>';
  } finally {
    row?.classList.remove('is-loading');
  }
}

async function loadAssetList() {
  const el = document.getElementById('assetList');
  el.innerHTML = loadingBlockHtml('Loading ' + ui.assetPool + '…');
  const d = await api('/v1/assets?category=' + encodeURIComponent(cat()) + '&pool=' + encodeURIComponent(ui.assetPool));
  if (!d.items?.length) { el.innerHTML = '<p class="muted">No images in this pool.</p>'; return; }
  el.innerHTML = '<div class="asset-table">' + d.items.map(it =>
    '<div class="list-row asset-row" data-name="' + esc(it.name) + '">' +
      '<code>' + esc(it.name) + '</code>' +
      '<span class="muted">' + fmtBytes(it.size) + ' · ' + esc(fmtTime(it.modified)) + '</span></div>'
  ).join('') + '</div>';
  el.querySelectorAll('.asset-row').forEach(row => {
    row.onclick = () => openAssetModal(row.dataset.name);
  });
  ui.tabsLoaded.assets = true;
}

function openAssetModal(name) {
  const modal = document.getElementById('modal');
  const body = document.getElementById('modalBody');
  body.innerHTML = '<div class="modal-body-loading">' + loadingBlockHtml('Loading image…') + '</div><p><code>' + esc(name) + '</code></p>';
  modal.classList.add('open');
  modal.setAttribute('aria-busy', 'true');
  const img = new Image();
  img.alt = name;
  img.onload = () => {
    body.innerHTML = '<p><code>' + esc(name) + '</code></p>';
    body.appendChild(img);
    modal.setAttribute('aria-busy', 'false');
  };
  img.onerror = () => {
    body.innerHTML = '<p class="muted">Failed to load image</p>';
    modal.setAttribute('aria-busy', 'false');
  };
  img.src = '/v1/media/asset?category=' + encodeURIComponent(cat()) + '&pool=' + encodeURIComponent(ui.assetPool) + '&name=' + encodeURIComponent(name);
}
document.getElementById('modalClose').onclick = () => {
  document.getElementById('modal').classList.remove('open');
  document.getElementById('modalBody').innerHTML = '';
};
document.getElementById('modal').onclick = (e) => { if (e.target.id === 'modal') document.getElementById('modalClose').click(); };

function showMainSection(section) {
  document.querySelectorAll('.main-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.section === section);
  });
  document.querySelectorAll('.main-section').forEach(s => s.classList.remove('active'));
  const panelId = 'section' + section.charAt(0).toUpperCase() + section.slice(1);
  const el = document.getElementById(panelId);
  if (el) el.classList.add('active');
  if (section === 'library') {
    const activeTab = document.querySelector('#sectionLibrary .tab.active');
    loadLibraryTab(activeTab);
  }
}
async function loadLibraryTab(btn) {
  if (!btn) return;
  const tab = btn.dataset.tab || 'videos';
  const needsLoad = (tab === 'videos' && !ui.tabsLoaded.videos)
    || (tab === 'assets' && !ui.tabsLoaded.assets)
    || tab === 'obs';
  if (!needsLoad && tab !== 'obs') return;
  setTabLoading(btn, true);
  try {
    if (tab === 'videos' && !ui.tabsLoaded.videos) await loadVideoList();
    else if (tab === 'assets' && !ui.tabsLoaded.assets) await loadAssetList();
    else if (tab === 'obs') await loadObservability();
  } finally {
    setTabLoading(btn, false);
  }
}
document.querySelectorAll('.main-tab').forEach(btn => {
  btn.onclick = () => showMainSection(btn.dataset.section);
});
document.querySelectorAll('.job-tab').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.job-tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.job-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const panel = btn.dataset.job === 'extend' ? 'jobPanelExtend' : 'jobPanelAssembly';
    document.getElementById(panel).classList.add('active');
  };
});

document.querySelectorAll('#sectionLibrary .tab').forEach(btn => {
  btn.onclick = async () => {
    document.querySelectorAll('#sectionLibrary .tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('#sectionLibrary .panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    const panelId = tab === 'obs' ? 'panelObs' : 'panel' + tab.charAt(0).toUpperCase() + tab.slice(1);
    document.getElementById(panelId).classList.add('active');
    await loadLibraryTab(btn);
  };
});
document.querySelectorAll('#assetPools .subtab').forEach(btn => {
  btn.onclick = async () => {
    document.querySelectorAll('#assetPools .subtab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    ui.assetPool = btn.dataset.pool;
    ui.tabsLoaded.assets = false;
    setTabLoading(btn, true);
    try {
      await loadAssetList();
    } finally {
      setTabLoading(btn, false);
    }
  };
});

async function refreshRunningExtendProgress() {
  const ids = [...ui.extend.keys()].filter(id => {
    const tr = ui.extend.get(id);
    return tr && tr.classList.contains('is-running');
  });
  if (!ids.length) return;
  await Promise.all(ids.map(async (id) => {
    try {
      const p = await api('/v1/extend/jobs/' + encodeURIComponent(id) + '/progress');
      upsertJobRow('extendBody', ui.extend, {
        execution_id: id,
        status: p.status,
        pct: p.pct,
        stage: p.stage,
        updated_at: p.updated_at,
        created_at: p.created_at,
        status_source: 'r2',
      });
    } catch (e) { console.warn('extend progress poll', id, e); }
  }));
}

async function refreshRunningAssemblyProgress() {
  const ids = [...ui.assembly.keys()].filter(id => {
    const tr = ui.assembly.get(id);
    return tr && tr.classList.contains('is-running');
  });
  if (!ids.length) return;
  await Promise.all(ids.map(async (id) => {
    try {
      const p = await api('/v1/assembly/jobs/' + encodeURIComponent(id) + '/progress');
      upsertJobRow('jobsBody', ui.assembly, {
        execution_id: id,
        gcp_execution_id: p.gcp_execution_id,
        status: p.status,
        pct: p.pct,
        stage: p.stage,
        updated_at: p.updated_at,
        created_at: p.created_at,
        status_source: p.status_source,
      });
    } catch (e) { console.warn('progress poll', id, e); }
  }));
}

async function pollSnapshot({ includeStats, refresh }) {
  const q = '?category=' + encodeURIComponent(cat())
    + '&job_limit=100'
    + (includeStats ? '' : '&light=1')
    + (refresh ? '&refresh=1' : '');
  if (includeStats) setStatsLoading(true);
  try {
    const d = await api('/v1/dashboard/snapshot' + q);
    syncJobTable('jobsBody', ui.assembly, d.assembly_runs || []);
    syncJobTable('extendBody', ui.extend, d.extend_runs || []);
    if (d.has_running) {
      await Promise.all([refreshRunningAssemblyProgress(), refreshRunningExtendProgress()]);
    }
    if (includeStats) applyInventory(d);
    document.getElementById('obsRunning').style.display = d.has_running ? 'inline' : 'none';
    return !!d.has_running;
  } finally {
    if (includeStats) setStatsLoading(false);
  }
}
function schedulePoll(ms) { clearTimeout(ui.pollTimer); ui.pollTimer = setTimeout(runPollLoop, ms); }
async function runPollLoop() {
  obs.polls++;
  const needStats = (Date.now() - ui.lastStatsAt) > 45000;
  try {
    const hasRunning = await pollSnapshot({ includeStats: needStats });
    if (needStats) ui.lastStatsAt = Date.now();
    schedulePoll(hasRunning ? 1500 : 15000);
  } catch (e) {
    console.error(e);
    schedulePoll(20000);
  }
}

async function refreshAll() {
  const btn = document.getElementById('refreshBtn');
  setBtnLoading(btn, true, 'Refreshing…');
  try {
    ui.lastStatsAt = 0;
    ui.videoDetails.clear();
    const hasRunning = await pollSnapshot({ includeStats: true, refresh: true });
    const libraryOpen = document.getElementById('sectionLibrary').classList.contains('active');
    if (libraryOpen && document.getElementById('panelVideos').classList.contains('active')) {
      ui.tabsLoaded.videos = false;
      await loadVideoList();
    }
    if (libraryOpen && document.getElementById('panelAssets').classList.contains('active')) {
      ui.tabsLoaded.assets = false;
      await loadAssetList();
    }
    if (libraryOpen && document.getElementById('panelObs').classList.contains('active')) {
      await loadObservability();
    }
    schedulePoll(hasRunning ? 1500 : 15000);
  } catch (e) {
    console.error(e);
    showAuthError('Refresh failed. Check connection and try again.');
  }
  setBtnLoading(btn, false);
}
document.getElementById('refreshBtn').onclick = refreshAll;
document.getElementById('logoutBtn').onclick = async () => {
  const btn = document.getElementById('logoutBtn');
  setBtnLoading(btn, true, 'Signing out…');
  await fetch('/v1/dashboard/logout', { method: 'POST', credentials: 'same-origin' });
  window.location.reload();
};
document.getElementById('runBtn').onclick = async () => {
  const btn = document.getElementById('runBtn');
  const channel = runChannel();
  if (!channel) { alert('Select or enter a YouTube channel'); return; }
  const imagesFolder = document.getElementById('runImagesFolder').value.trim();
  if (!imagesFolder) { alert('Select a background folder'); return; }
  setBtnLoading(btn, true, 'Starting…');
  try {
    const r = await api('/v1/assembly/jobs', { method: 'POST', body: JSON.stringify({
      channel: channel,
      images_folder: imagesFolder,
      thumbnail_text: document.getElementById('runThumb').value,
      duration_min: parseInt(document.getElementById('runDuration').value, 10),
      variance_min: parseInt(document.getElementById('runVariance').value, 10),
      count: parseInt(document.getElementById('runCount').value, 10),
      queue_youtube: document.getElementById('runQueueYoutube').checked,
    })});
    document.getElementById('runResult').textContent = JSON.stringify(r, null, 2);
    ui.tabsLoaded.videos = false;
    ui.lastStatsAt = 0;
    await pollSnapshot({ includeStats: true });
    schedulePoll(3000);
    showMainSection('jobs');
    document.querySelector('.job-tab[data-job="assembly"]')?.click();
  } catch (e) { document.getElementById('runResult').textContent = String(e); }
  setBtnLoading(btn, false);
};
document.getElementById('extendBtn').onclick = async () => {
  const btn = document.getElementById('extendBtn');
  setBtnLoading(btn, true, 'Starting…');
  try {
    const lim = document.getElementById('extendLimit').value;
    const r = await api('/v1/extend/jobs', { method: 'POST', body: JSON.stringify({
      category: cat(),
      process_all: lim === 'all',
      limit: lim === 'all' ? null : parseInt(lim, 10),
    })});
    document.getElementById('extendResult').textContent = JSON.stringify(r, null, 2);
    ui.tabsLoaded.assets = false;
    ui.lastStatsAt = 0;
    await pollSnapshot({ includeStats: true });
    schedulePoll(3000);
    showMainSection('jobs');
    document.querySelector('.job-tab[data-job="extend"]')?.click();
  } catch (e) { document.getElementById('extendResult').textContent = String(e); }
  setBtnLoading(btn, false);
};
document.getElementById('jobFilter').onchange = () => {
  ui.jobFilter = document.getElementById('jobFilter').value;
  for (const tr of ui.assembly.values()) {
    const st = tr.querySelector('.job-status')?.textContent || '';
    tr.style.display = (!ui.jobFilter || st === ui.jobFilter) ? '' : 'none';
  }
  updateSelectAllCheckbox('selectAllAssembly', ui.assembly);
};
document.getElementById('cancelSelectedAssembly').onclick = () => cancelSelectedJobs(ui.assembly);
document.getElementById('cancelSelectedExtend').onclick = () => cancelSelectedJobs(ui.extend);
bindSelectAllCheckbox('selectAllAssembly', ui.assembly);
bindSelectAllCheckbox('selectAllExtend', ui.extend);
document.getElementById('videoChannel').addEventListener('change', async () => {
  ui.tabsLoaded.videos = false;
  ui.videoDetails.clear();
  if (document.getElementById('panelVideos').classList.contains('active')) {
    await loadVideoList();
  }
});

(async function init() {
  renderObsBar();
  setStatsLoading(true);
  setJobsLoading(true);
  try {
    await Promise.all([
      loadVersionInfo(),
      loadChannelOptions(),
      loadBackgroundFolders(),
      pollSnapshot({ includeStats: false }),
    ]);
    await refreshStats().catch(e => console.warn('stats', e));
  } catch (e) {
    console.error(e);
    showAuthError('Failed to load dashboard. Try Refresh.');
  } finally {
    setStatsLoading(false);
    setJobsLoading(false);
    hidePageBoot();
  }
  schedulePoll(15000);
})();
</script>
</body>
</html>
"""
)

install_openapi_docs(app)
