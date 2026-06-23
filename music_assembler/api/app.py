"""FastAPI application — Music Assembly control plane."""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from music_assembler import __version__

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
from music_assembler.api import r2_catalog
from music_assembler.api.cache import dashboard_cache
from music_assembler.api.media import stream_r2_object
from music_assembler.api.progress_store import read_progress_json, write_meta_json, write_progress_json
from music_assembler.api.progress_store import patch_meta_gcp_execution_id
from music_assembler.extend_from_r2 import count_pending_r2_sources, pending_r2_sources
from music_assembler.r2_storage import r2_client, r2_config_from_env

app = FastAPI(
    title="Music Assembly API",
    description="Trigger Cloud Run assembly jobs, monitor progress, browse R2 outputs.",
    version="0.1.0",
)


class StartJobRequest(BaseModel):
    category: str | None = None
    channel: str | None = None
    thumbnail_text: str | None = None
    duration_min: int | None = Field(default=None, ge=15, le=240)
    variance_min: int | None = Field(default=None, ge=0, le=60)
    count: int = Field(default=1, ge=1, le=10)

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        return normalize_channel(value)


class StartExtendRequest(BaseModel):
    category: str | None = None
    limit: int | None = Field(default=1, ge=1, le=20)
    process_all: bool = False
    force: bool = False
    parallel: bool = True


class DashboardLoginRequest(BaseModel):
    password: str


class CancelJobRequest(BaseModel):
    confirm: bool = False


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


def _queue_extend_job(
    background_tasks: BackgroundTasks | None,
    client,
    bucket: str,
    *,
    execution_id: str,
    category: str,
    source_keys: list[str],
    force: bool,
    image_name: str | None = None,
    spawn_thread: bool = False,
) -> dict[str, Any]:
    stage = f"Queued — {image_name or source_keys[0].rsplit('/', 1)[-1]}…"
    write_meta_json(
        client,
        bucket,
        execution_id,
        category=category,
        job_type="extend",
        limit=1,
        process_all=False,
    )
    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=0,
        stage=stage,
        category=category,
        status="running",
        extra={"job_type": "extend", "source_key": source_keys[0] if source_keys else None},
    )
    task_kwargs = dict(
        execution_id=execution_id,
        category=category,
        limit=1,
        process_all=False,
        force=force,
        source_keys=source_keys,
    )
    if spawn_thread:
        threading.Thread(target=run_extend_job, kwargs=task_kwargs, daemon=True).start()
    elif background_tasks is not None:
        background_tasks.add_task(run_extend_job, **task_kwargs)
    else:
        threading.Thread(target=run_extend_job, kwargs=task_kwargs, daemon=True).start()
    return {
        "execution_id": execution_id,
        "status": "running",
        "category": category,
        "source_key": source_keys[0] if source_keys else None,
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
            "GET /v1/channels",
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
    videos = r2_catalog.list_videos(client, bucket, category=cat, limit=5)
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
    channel = body.channel
    client, bucket = _r2()
    _invalidate_category_cache(category)

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
                thumbnail_text=body.thumbnail_text,
                duration_min=body.duration_min,
                variance_min=body.variance_min,
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


@app.get("/v1/media/thumbnail")
def media_thumbnail(
    category: str,
    video_id: str,
    channel: str | None = None,
    _auth: None = Depends(require_api_auth),
) -> Response:
    """Stable thumbnail URL for the dashboard (avoids presigned URL churn)."""
    client, bucket = _r2()
    ch = normalize_channel(channel) if channel else None
    key = r2_catalog.find_thumbnail_key(
        client, bucket, category=category, video_id=video_id, channel=ch
    )
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
    category: str,
    video_id: str,
    channel: str | None = None,
    _auth: None = Depends(require_api_auth),
) -> Response:
    """Stream MP4 with Range support for in-browser preview."""
    client, bucket = _r2()
    ch = normalize_channel(channel) if channel else None
    key = r2_catalog.find_video_key(
        client, bucket, category=category, video_id=video_id, channel=ch
    )
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
    job_limit: int = Query(default=25, ge=1, le=100),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    """Poll endpoint for jobs. Use /v1/dashboard/stats and tab loaders for the rest."""
    cat = category or settings.default_category
    client, bucket = _r2()

    asm_raw = job_runs.list_r2_job_runs(client, bucket, id_prefix="asm_", limit=job_limit)
    ext_raw = job_runs.list_r2_job_runs(client, bucket, id_prefix="ext_", limit=job_limit)
    assembly_runs = job_status.reconcile_assembly_runs(
        settings, client, bucket, asm_raw, patch_r2=True
    )
    extend_runs = job_status.reconcile_extend_runs(ext_raw)
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
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not configured on this API service",
        )
    category = (body.category or settings.default_category).strip()
    client, bucket = _r2()
    _invalidate_category_cache(category)
    cfg = r2_config_from_env(category=category)
    pending_keys = pending_r2_sources(client, cfg, force=body.force)
    pending = len(pending_keys)
    if pending == 0:
        raise HTTPException(status_code=409, detail="No pending pre-processed images on R2")

    batch = pending if body.process_all else min(body.limit or 1, pending)
    batch_keys = pending_keys[:batch]

    if body.parallel and len(batch_keys) > 1:
        jobs: list[dict[str, Any]] = []
        for key in batch_keys:
            execution_id = _new_extend_id()
            jobs.append(
                _queue_extend_job(
                    background_tasks,
                    client,
                    bucket,
                    execution_id=execution_id,
                    category=category,
                    source_keys=[key],
                    force=body.force,
                    image_name=Path(key).name,
                    spawn_thread=True,
                )
            )
        return {
            "parallel": True,
            "jobs": jobs,
            "batch_size": len(jobs),
            "category": category,
            "pending": pending,
        }

    execution_id = _new_extend_id()
    job = _queue_extend_job(
        background_tasks,
        client,
        bucket,
        execution_id=execution_id,
        category=category,
        source_keys=batch_keys,
        force=body.force,
        image_name=(
            f"{len(batch_keys)} image(s)"
            if len(batch_keys) != 1
            else Path(batch_keys[0]).name
        ),
    )
    return {
        **job,
        "parallel": False,
        "pending": pending,
        "batch_size": len(batch_keys),
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
    """Configured + R2-discovered YouTube channel folders for a category."""
    cat = category or settings.default_category
    client, bucket = _r2()
    discovered = r2_catalog.discover_channels(client, bucket, cat)
    merged = sorted(set(settings.configured_channels) | set(discovered))
    return {
        "category": cat,
        "channels": merged,
        "configured": list(settings.configured_channels),
        "discovered": discovered,
    }


@app.get("/v1/videos")
def list_videos(
    category: str | None = None,
    channel: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    summary: bool = Query(default=True, description="Metadata only — no title/description reads"),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    cat = category or settings.default_category
    ch = normalize_channel(channel) if channel else None
    cache_key = _cache_key("videos", cat, ch or "all", "summary" if summary else "full", str(limit))

    def load() -> dict[str, Any]:
        client, bucket = _r2()
        videos = r2_catalog.list_videos(
            client,
            bucket,
            category=cat,
            channel=ch,
            limit=limit,
            stable_media_urls=True,
            summary_only=summary,
        )
        return {"category": cat, "channel": ch, "videos": videos, "count": len(videos), "summary": summary}

    data, hit = dashboard_cache.get_or_set(cache_key, 60.0 if summary else 30.0, load)
    return JSONResponse(
        content={**data, "cache": {"hit": hit}},
        headers={"X-Cache": "HIT" if hit else "MISS"},
    )


@app.get("/v1/videos/{video_id}")
def get_video(
    video_id: str,
    category: str | None = None,
    channel: str | None = None,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    cat = category or settings.default_category
    ch = normalize_channel(channel) if channel else None
    client, bucket = _r2()
    row = r2_catalog.get_video(client, bucket, category=cat, video_id=video_id, channel=ch)
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


@app.get("/", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    settings: ApiSettings = Depends(_settings),
) -> str:
    if not has_dashboard_session(request, settings):
        return _LOGIN_HTML
    return _DASHBOARD_HTML


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Music Assembly</title>
  <style>
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      background: #000;
      color: #fff;
      font-family: system-ui, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .box { text-align: center; width: min(320px, 90vw); }
    h1 { font-size: 1.25rem; font-weight: 500; margin: 0 0 1.5rem; letter-spacing: 0.02em; }
    input {
      width: 100%;
      background: #000;
      color: #fff;
      border: 1px solid #fff;
      padding: 0.75rem 1rem;
      font: inherit;
      margin-bottom: 1rem;
    }
    input:focus { outline: 1px solid #fff; outline-offset: 2px; }
    button {
      width: 100%;
      background: #fff;
      color: #000;
      border: none;
      padding: 0.75rem 1rem;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    #err { color: #fff; font-size: 0.85rem; margin-top: 1rem; min-height: 1.2em; opacity: 0.85; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Enter password</h1>
    <form id="loginForm">
      <input type="password" id="password" autocomplete="current-password" autofocus />
      <button type="submit" id="submitBtn">Continue</button>
    </form>
    <p id="err"></p>
  </div>
  <script>
    document.getElementById('loginForm').onsubmit = async (e) => {
      e.preventDefault();
      const btn = document.getElementById('submitBtn');
      const err = document.getElementById('err');
      btn.disabled = true;
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
          return;
        }
        window.location.reload();
      } catch (_) {
        err.textContent = 'Connection error';
        btn.disabled = false;
      }
    };
  </script>
</body>
</html>
"""


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Music Assembly</title>
  <style>
    :root { --bg:#fff; --card:#fff; --text:#000; --muted:#666; --border:#000; --bar-bg:#e5e5e5; }
    * { box-sizing:border-box; }
    body { font-family: system-ui, sans-serif; background: var(--bg); color: var(--text); margin:0 auto; padding:1.5rem; max-width:1200px; width:100%; box-sizing:border-box; }
    h1, h2 { font-weight:600; }
    h1 { margin:0 0 .5rem; font-size:1.5rem; }
    h2 { margin:0 0 .75rem; font-size:1.1rem; }
    .muted { color: var(--muted); font-size:.9rem; }
    .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:1rem; margin:1rem 0; }
    .card { background:var(--card); border:1px solid var(--border); padding:1rem; }
    button, select, input { background:#fff; color:var(--text); border:1px solid var(--border); padding:.5rem .75rem; font:inherit; }
    button { cursor:pointer; background:#000; color:#fff; border-color:#000; font-weight:600; }
    button:hover { background:#333; }
    button:disabled { opacity:.4; cursor:not-allowed; }
    button.secondary { background:#fff; color:#000; border:1px solid #000; }
    button.secondary:hover { background:#f0f0f0; }
    table { width:100%; border-collapse:collapse; font-size:.85rem; }
    th, td { text-align:left; padding:.5rem; border-bottom:1px solid #ccc; }
    th { border-bottom:2px solid #000; font-weight:600; }
    .bar { height:6px; background:var(--bar-bg); overflow:hidden; margin-top:.25rem; border:1px solid #ccc; }
    .bar > span { display:block; height:100%; background:#000; transition:width .3s; }
    .status-running { font-weight:600; }
    .status-succeeded { font-weight:600; }
    .status-failed { font-weight:600; text-decoration:underline; }
    .status-cancelled { font-weight:600; color:var(--muted); }
    .status-cancelling { font-weight:600; }
    .status-unknown { color:var(--muted); }
    tr.is-running .bar > span { animation: barPulse 1.6s ease-in-out infinite; }
    @keyframes barPulse { 0%,100% { opacity:1; } 50% { opacity:0.45; } }
    .job-stage { display:block; font-size:.78rem; margin-top:.2rem; max-width:320px; word-break:break-word; }
    .job-updated { font-size:.72rem; color:var(--muted); }
    .videos { display:grid; grid-template-columns: repeat(auto-fill, minmax(200px,1fr)); gap:1rem; }
    .videos .video-card img { width:100%; aspect-ratio:16/9; object-fit:cover; background:#eee; border:1px solid #ccc; }
    #authError { display:none; margin:.75rem 0; padding:.75rem 1rem; border:1px solid #000; background:#f5f5f5; }
    #authError.visible { display:block; }
    .topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; flex-wrap:wrap; }
    .logout { background:#fff; color:#000; border:1px solid #000; font-size:.85rem; padding:.35rem .75rem; }
    code { background:#f0f0f0; padding:.1em .35em; font-size:.9em; }
    pre { white-space:pre-wrap; word-break:break-word; }
    .tabs { display:flex; gap:.5rem; flex-wrap:wrap; margin:1rem 0; border-bottom:2px solid #000; padding-bottom:.5rem; }
    .tab { background:#fff; color:#000; border:1px solid #000; padding:.4rem .9rem; font-size:.9rem; }
    .tab.active { background:#000; color:#fff; }
    .panel { display:none; }
    .panel.active { display:block; }
    .obs-bar { position:sticky; bottom:0; margin:1.5rem -1.5rem -1.5rem; padding:.6rem 1.5rem; background:#111; color:#eee; font-size:.75rem; border-top:1px solid #333; display:flex; flex-wrap:wrap; align-items:center; gap:.25rem 1rem; }
    .obs-bar span { margin-right:0; }
    .obs-version { margin-left:auto; color:#aaa; font-family: ui-monospace, monospace; font-size:.7rem; white-space:nowrap; }
    .obs-hit { color:#8f8; }
    .obs-miss { color:#fa8; }
    .list-row { display:flex; justify-content:space-between; align-items:center; padding:.5rem 0; border-bottom:1px solid #ddd; cursor:pointer; gap:.5rem; }
    .list-row:hover { background:#f8f8f8; }
    .badges { display:flex; gap:.35rem; flex-wrap:wrap; }
    .badge { font-size:.7rem; border:1px solid #999; padding:.1rem .4rem; border-radius:2px; }
    .detail { display:none; padding:.75rem 0 .5rem; border-bottom:2px solid #000; margin-bottom:.5rem; }
    .detail.open { display:block; }
    .detail video { width:100%; max-height:420px; background:#000; margin:.5rem 0; }
    .detail .desc { max-height:200px; overflow:auto; font-size:.85rem; white-space:pre-wrap; background:#f5f5f5; padding:.5rem; border:1px solid #ddd; }
    .asset-table { max-height:400px; overflow:auto; }
    .modal { position:fixed; inset:0; background:rgba(0,0,0,.85); display:none; align-items:center; justify-content:center; z-index:100; padding:1rem; }
    .modal.open { display:flex; }
    .modal-inner { background:#fff; max-width:min(960px,100%); max-height:90vh; overflow:auto; padding:1rem; position:relative; }
    .modal img { max-width:100%; height:auto; display:block; }
    .modal-close { position:absolute; top:.5rem; right:.5rem; background:#000; color:#fff; border:none; width:2rem; height:2rem; cursor:pointer; }
    .subtabs { display:flex; gap:.35rem; flex-wrap:wrap; margin-bottom:.75rem; }
    .subtab { font-size:.8rem; padding:.25rem .6rem; }
    .subtab.active { background:#000; color:#fff; }
    .loading { color:var(--muted); font-style:italic; }
    .cancel-confirm { display:flex; gap:.35rem; flex-wrap:wrap; align-items:center; margin-top:.35rem; }
    .cancel-confirm .warn { font-size:.78rem; color:#900; }
    button.danger { background:#900; border-color:#900; }
    button.danger:hover { background:#b00; }
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>Music Assembly</h1>
      <p class="muted">Trigger jobs, track progress, browse outputs on R2.</p>
    </div>
    <div style="display:flex;gap:.5rem;align-items:flex-start">
      <button type="button" class="secondary" id="refreshBtn" title="Reload jobs, inventory, and active tab">Refresh</button>
      <button type="button" class="logout" id="logoutBtn">Sign out</button>
    </div>
  </div>
  <div id="authError" class="muted"></div>

  <div class="grid">
    <div class="card">
      <h2>Run assembly</h2>
      <p class="muted">Starts <code>music-assemble</code> on Cloud Run.</p>
      <p><label>Category <input id="runCategory" value="korean"/></label></p>
      <p><label>YouTube channel
        <select id="runChannel"><option value="">(legacy root — no subfolder)</option></select>
      </label></p>
      <p><label>Or new channel slug <input id="runChannelCustom" placeholder="e.g. lofi-beats"/></label></p>
      <p class="muted">Finished videos upload to <code>music-video/{category}/{channel}/mv_*/</code>. Parallel jobs share the same channel.</p>
      <p><label>Thumbnail text <input id="runThumb" value="OMYO"/></label></p>
      <p><label>Duration (min) <input id="runDuration" type="number" value="90"/></label></p>
      <p><label>Variance (min) <input id="runVariance" type="number" value="15"/></label></p>
      <p><label>Parallel jobs
        <select id="runCount">
          <option value="1">1 video</option>
          <option value="2">2 videos</option>
          <option value="3">3 videos</option>
          <option value="5">5 videos</option>
          <option value="10">10 videos</option>
        </select>
      </label></p>
      <p class="muted">Each job claims a unique background from <code>post-processed/</code>. Jobs with no image left exit immediately.</p>
      <button id="runBtn">Start job</button>
      <pre id="runResult" class="muted"></pre>
    </div>
    <div class="card">
      <h2>Extend backgrounds</h2>
      <p class="muted">Pull from <code>pre-processed/</code>, Gemini extend → <code>post-processed/</code>.</p>
      <p class="muted">Batch &gt;1 starts one parallel job per image (faster, isolated progress).</p>
      <p class="muted">Pending: <strong id="extendPending">…</strong></p>
      <p><label>Batch size
        <select id="extendLimit">
          <option value="1">1 image</option>
          <option value="3">3 images</option>
          <option value="5">5 images</option>
          <option value="10">10 images</option>
          <option value="all">All pending</option>
        </select>
      </label></p>
      <button id="extendBtn" class="secondary">Start extend</button>
      <pre id="extendResult" class="muted"></pre>
    </div>
    <div class="card">
      <h2>Inventory</h2>
      <pre id="inventory" class="muted">Loading…</pre>
    </div>
  </div>

  <div class="card">
    <h2>Assembly jobs</h2>
    <label>Filter <select id="jobFilter"><option value="">All</option><option>running</option><option>succeeded</option><option>failed</option></select></label>
    <table><thead><tr><th>Execution</th><th>Status</th><th>Progress</th><th>Started</th><th></th></tr></thead><tbody id="jobsBody"></tbody></table>
  </div>

  <div class="card">
    <h2>Extend jobs</h2>
    <table><thead><tr><th>Run</th><th>Status</th><th>Progress</th><th>Started</th><th></th></tr></thead><tbody id="extendBody"></tbody></table>
  </div>

  <nav class="tabs" role="tablist">
    <button type="button" class="tab" data-tab="videos">Music videos</button>
    <button type="button" class="tab" data-tab="assets">Background images</button>
    <button type="button" class="tab" data-tab="obs">Observability</button>
  </nav>

  <div id="panelVideos" class="panel card">
    <h2>Music videos</h2>
    <p class="muted">List loads on demand. Expand a row for title, description, and video preview.</p>
    <p><label>Channel filter
      <select id="videoChannel"><option value="">All channels</option></select>
    </label></p>
    <div id="videoList"><p class="loading">Open this tab to load…</p></div>
  </div>

  <div id="panelAssets" class="panel card">
    <h2>Background images</h2>
    <p class="muted">Filenames only until you click — then the image loads.</p>
    <div class="subtabs" id="assetPools">
      <button type="button" class="subtab secondary active" data-pool="pre-processed">Pre-processed</button>
      <button type="button" class="subtab secondary" data-pool="post-processed">Post-processed</button>
      <button type="button" class="subtab secondary" data-pool="pre-used">Pre-used</button>
      <button type="button" class="subtab secondary" data-pool="post-used">Post-used</button>
    </div>
    <div id="assetList"><p class="loading">Open this tab to load…</p></div>
  </div>

  <div id="panelObs" class="panel card">
    <h2>Observability</h2>
    <pre id="obsDetail" class="muted">Loading…</pre>
    <h3 style="font-size:.95rem">Recent API calls</h3>
    <table><thead><tr><th>Time</th><th>Endpoint</th><th>ms</th><th>Cache</th></tr></thead><tbody id="obsFetches"></tbody></table>
  </div>

  <div id="modal" class="modal" aria-hidden="true">
    <div class="modal-inner">
      <button type="button" class="modal-close" id="modalClose" aria-label="Close">×</button>
      <div id="modalBody"></div>
    </div>
  </div>

  <div class="obs-bar" id="obsBar">
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
};
const obs = { fetches: [], hits: 0, misses: 0, polls: 0, lastMs: null, lastError: null };

function showAuthError(msg) {
  document.getElementById('authError').textContent = msg;
  document.getElementById('authError').classList.add('visible');
}
function clearAuthError() { document.getElementById('authError').classList.remove('visible'); }

async function api(path, opts={}) {
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
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
}
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
function cat() { return document.getElementById('runCategory').value.trim() || 'korean'; }
function runChannel() {
  const custom = document.getElementById('runChannelCustom').value.trim();
  if (custom) return custom;
  return document.getElementById('runChannel').value.trim();
}
function videoChannelFilter() {
  return document.getElementById('videoChannel').value.trim();
}

async function loadChannelOptions() {
  try {
    const d = await api('/v1/channels?category=' + encodeURIComponent(cat()));
    for (const selId of ['runChannel', 'videoChannel']) {
      const el = document.getElementById(selId);
      const keep = el.value;
      const allOpt = selId === 'videoChannel'
        ? '<option value="">All channels</option>'
        : '<option value="">(legacy root — no subfolder)</option>';
      el.innerHTML = allOpt;
      for (const ch of d.channels || []) {
        el.innerHTML += '<option value="' + esc(ch) + '">' + esc(ch) + '</option>';
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
function renderObsPanel() {
  const tb = document.getElementById('obsFetches');
  tb.innerHTML = obs.fetches.map(f =>
    '<tr><td class="muted">' + esc(f.at) + '</td><td><code>' + esc(f.path) + '</code></td><td>' + f.ms + '</td><td>' + esc(f.cache || '—') + '</td></tr>'
  ).join('');
}
async function loadObservability() {
  try {
    const d = await api('/v1/observability');
    document.getElementById('obsDetail').textContent = JSON.stringify(d, null, 2);
    renderObsPanel();
  } catch (e) {
    document.getElementById('obsDetail').textContent = String(e);
  }
}

function applyInventory(d) {
  if (d.inventory) document.getElementById('inventory').textContent = JSON.stringify(d.inventory, null, 2);
  if (typeof d.extend_pending === 'number') {
    document.getElementById('extendPending').textContent = d.extend_pending + ' in pre-processed/';
  }
}

async function refreshStats() {
  const d = await api('/v1/dashboard/stats?category=' + encodeURIComponent(cat()));
  applyInventory(d);
  ui.lastStatsAt = Date.now();
  return d;
}

function jobActionsHtml(row) {
  const id = row.execution_id;
  const st = row.status || '';
  const active = st === 'running' || st === 'cancelling' || st === 'unknown';
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
}
async function confirmCancelJob(executionId) {
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
  const running = row.status === 'running' || row.status === 'cancelling' || row.status === 'unknown';
  const gcpLine = row.gcp_execution_id && row.gcp_execution_id !== row.execution_id
    ? '<br/><span class="muted">' + esc(row.gcp_execution_id) + '</span>' : '';
  const updated = row.updated_at ? '<span class="job-updated">Updated ' + esc(fmtTime(row.updated_at)) + '</span>' : '';
  if (!tr) {
    tr = document.createElement('tr');
    tr.dataset.jobId = row.execution_id;
    tr.innerHTML =
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
  const ids = new Set(rows.map(r => r.execution_id));
  for (const [id, tr] of map) { if (!ids.has(id)) { tr.remove(); map.delete(id); } }
  for (const row of rows) upsertJobRow(tableId, map, row);
  for (let i = rows.length - 1; i >= 0; i--) {
    const tr = map.get(rows[i].execution_id);
    if (tr) tb.prepend(tr);
  }
}

async function loadVideoList() {
  const el = document.getElementById('videoList');
  el.innerHTML = '<p class="loading">Loading video list…</p>';
  let url = '/v1/videos?category=' + encodeURIComponent(cat()) + '&summary=1';
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
  detail.innerHTML = '<p class="loading">Loading metadata…</p>';
  try {
    const ch = ui.videoChannels.get(id) || '';
    let url = '/v1/videos/' + encodeURIComponent(id) + '?category=' + encodeURIComponent(cat());
    if (ch) url += '&channel=' + encodeURIComponent(ch);
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
        videoEl.style.display = 'block';
        videoEl.src = v.video_url;
        playBtn.style.display = 'none';
        videoEl.play().catch(() => {});
      };
    }
  } catch (e) {
    detail.innerHTML = '<p class="muted">' + esc(String(e)) + '</p>';
  }
}

async function loadAssetList() {
  const el = document.getElementById('assetList');
  el.innerHTML = '<p class="loading">Loading ' + esc(ui.assetPool) + '…</p>';
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
  body.innerHTML = '<p class="loading">Loading image…</p><p><code>' + esc(name) + '</code></p>';
  modal.classList.add('open');
  const img = new Image();
  img.alt = name;
  img.onload = () => { body.innerHTML = '<p><code>' + esc(name) + '</code></p>'; body.appendChild(img); };
  img.onerror = () => { body.innerHTML = '<p class="muted">Failed to load image</p>'; };
  img.src = '/v1/media/asset?category=' + encodeURIComponent(cat()) + '&pool=' + encodeURIComponent(ui.assetPool) + '&name=' + encodeURIComponent(name);
}
document.getElementById('modalClose').onclick = () => {
  document.getElementById('modal').classList.remove('open');
  document.getElementById('modalBody').innerHTML = '';
};
document.getElementById('modal').onclick = (e) => { if (e.target.id === 'modal') document.getElementById('modalClose').click(); };

document.querySelectorAll('.tab').forEach(btn => {
  btn.onclick = async () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    const panelId = tab === 'obs' ? 'panelObs' : 'panel' + tab.charAt(0).toUpperCase() + tab.slice(1);
    document.getElementById(panelId).classList.add('active');
    if (tab === 'videos' && !ui.tabsLoaded.videos) await loadVideoList();
    if (tab === 'assets' && !ui.tabsLoaded.assets) await loadAssetList();
    if (tab === 'obs') await loadObservability();
  };
});
document.querySelectorAll('#assetPools .subtab').forEach(btn => {
  btn.onclick = async () => {
    document.querySelectorAll('#assetPools .subtab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    ui.assetPool = btn.dataset.pool;
    ui.tabsLoaded.assets = false;
    await loadAssetList();
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
    + (includeStats ? '' : '&light=1')
    + (refresh ? '&refresh=1' : '');
  const d = await api('/v1/dashboard/snapshot' + q);
  syncJobTable('jobsBody', ui.assembly, d.assembly_runs || []);
  syncJobTable('extendBody', ui.extend, d.extend_runs || []);
  if (d.has_running) {
    await Promise.all([refreshRunningAssemblyProgress(), refreshRunningExtendProgress()]);
  }
  if (includeStats) applyInventory(d);
  document.getElementById('obsRunning').style.display = d.has_running ? 'inline' : 'none';
  return !!d.has_running;
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
  btn.disabled = true;
  try {
    ui.lastStatsAt = 0;
    ui.videoDetails.clear();
    const hasRunning = await pollSnapshot({ includeStats: true, refresh: true });
    if (document.getElementById('panelVideos').classList.contains('active')) {
      ui.tabsLoaded.videos = false;
      await loadVideoList();
    }
    if (document.getElementById('panelAssets').classList.contains('active')) {
      ui.tabsLoaded.assets = false;
      await loadAssetList();
    }
    if (document.getElementById('panelObs').classList.contains('active')) {
      await loadObservability();
    }
    schedulePoll(hasRunning ? 1500 : 15000);
  } catch (e) {
    console.error(e);
  }
  btn.disabled = false;
}
document.getElementById('refreshBtn').onclick = refreshAll;
document.getElementById('logoutBtn').onclick = async () => {
  await fetch('/v1/dashboard/logout', { method: 'POST', credentials: 'same-origin' });
  window.location.reload();
};
document.getElementById('runBtn').onclick = async () => {
  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  try {
    const r = await api('/v1/assembly/jobs', { method: 'POST', body: JSON.stringify({
      category: cat(),
      channel: runChannel() || null,
      thumbnail_text: document.getElementById('runThumb').value,
      duration_min: parseInt(document.getElementById('runDuration').value, 10),
      variance_min: parseInt(document.getElementById('runVariance').value, 10),
      count: parseInt(document.getElementById('runCount').value, 10),
    })});
    document.getElementById('runResult').textContent = JSON.stringify(r, null, 2);
    ui.tabsLoaded.videos = false;
    ui.lastStatsAt = 0;
    await pollSnapshot({ includeStats: true });
    schedulePoll(3000);
  } catch (e) { document.getElementById('runResult').textContent = String(e); }
  btn.disabled = false;
};
document.getElementById('extendBtn').onclick = async () => {
  const btn = document.getElementById('extendBtn');
  btn.disabled = true;
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
  } catch (e) { document.getElementById('extendResult').textContent = String(e); }
  btn.disabled = false;
};
document.getElementById('jobFilter').onchange = () => {
  ui.jobFilter = document.getElementById('jobFilter').value;
  for (const tr of ui.assembly.values()) {
    const st = tr.querySelector('.job-status')?.textContent || '';
    tr.style.display = (!ui.jobFilter || st === ui.jobFilter) ? '' : 'none';
  }
};
document.getElementById('runCategory').addEventListener('change', () => {
  ui.tabsLoaded = { videos: false, assets: false, obs: false };
  ui.videoDetails.clear();
  ui.videoChannels.clear();
  ui.lastStatsAt = 0;
  loadChannelOptions();
});
document.getElementById('videoChannel').addEventListener('change', () => {
  ui.tabsLoaded.videos = false;
  ui.videoDetails.clear();
  if (document.getElementById('panelVideos').classList.contains('active')) loadVideoList();
});

(async function init() {
  renderObsBar();
  try {
    await loadChannelOptions();
    await pollSnapshot({ includeStats: true });
  } catch (e) { console.error(e); }
  schedulePoll(15000);
})();
</script>
</body>
</html>
"""
