"""FastAPI application — Music Assembly control plane."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator

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
from music_assembler.api import assembly_schedule
from music_assembler.api import asset_upload
from music_assembler.api import r2_catalog
from music_assembler.api import uploader_client
from music_assembler.api.cache import dashboard_cache
from music_assembler.api.deploy_manifest import load_deploy_manifest
from music_assembler.api.media import stream_r2_object
from music_assembler.api.openapi_docs import install_openapi_docs
from music_assembler.api.progress_store import read_progress_json, write_meta_json, write_progress_json
from music_assembler.api.progress_store import patch_meta_gcp_execution_id
from music_assembler.extend_from_r2 import count_pending_r2_sources
from music_assembler.r2_storage import normalize_source_folder, r2_client, r2_config_from_env

app = FastAPI(
    title="Music Assembly API",
    description="Trigger Cloud Run assembly/extend jobs, monitor progress, browse R2 outputs.",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


def _normalize_images_folder(value: str) -> str:
    folder = value.strip().strip("/")
    if not folder or ".." in folder or "/" in folder or "\\" in folder:
        raise ValueError("images_folder must be a single folder name under post-processed/")
    return folder


def _assert_background_folder_exists(client, bucket: str, folder: str) -> None:
    known = r2_catalog.list_background_folders(client, bucket)
    if folder not in known:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown background folder {folder!r}. Available: {', '.join(known) or '(none)'}",
        )


def _assert_pre_processed_folder_exists(client, bucket: str, folder: str) -> None:
    known = r2_catalog.list_pre_processed_folders(client, bucket)
    if folder not in known:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown pre-processed folder {folder!r}. "
                f"Available: {', '.join(known) or '(none)'}"
            ),
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
    images_folder: str = Field(
        ...,
        description="R2 subfolder under post-processed/ for background stills (required).",
        examples=["korean"],
    )
    thumbnail_text: str | None = Field(default=None, description="Text burned into the thumbnail.", examples=["PLAYLIST"])
    duration_min: int | None = Field(default=None, ge=5, le=300, description="Target mix length in minutes.")
    variance_min: int | None = Field(default=None, ge=0, le=60, description="Random length variance (+/- minutes).")
    count: int = Field(default=1, ge=1, le=10, description="Parallel assembly jobs to start (one video each).")
    queue_youtube: bool = Field(
        default=True,
        description=(
            "After encode + R2 upload, register the video on the youtube-uploader pending queue "
            "(default on; set false to skip). Requires UPLOADER_API_* on the music-assemble worker."
        ),
    )
    upload_privacy: str = Field(
        default="private",
        description="YouTube privacy when queued (private | unlisted | public). Scheduled publish forces private until go-live.",
    )
    upload_schedule_publish: bool = Field(
        default=False,
        description=(
            "When true with publish_at, register with YouTube publishAt (and upload_at). "
            "When false with queue_youtube, register with upload_now + no_schedule so the "
            "uploader dispatches immediately using upload_privacy."
        ),
    )
    publish_at: str | None = Field(
        default=None,
        description="RFC3339 UTC go-live time. Also used as upload_at when upload_at is omitted.",
    )
    upload_at: str | None = Field(
        default=None,
        description="RFC3339 UTC queue pickup time for youtube-uploader. Defaults to publish_at when omitted.",
    )
    upload_tags: str = Field(default="", description="Comma-separated YouTube tags for the queued job.")
    upload_category_id: str = Field(default="10", description="YouTube category id (10 = Music).")
    upload_made_for_kids: bool = Field(default=False, description="YouTube madeForKids flag on the queued job.")

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        return normalize_channel(value)

    @field_validator("images_folder")
    @classmethod
    def _validate_images_folder(cls, value: str) -> str:
        return _normalize_images_folder(value)

    @field_validator("upload_privacy")
    @classmethod
    def _validate_start_upload_privacy(cls, value: str) -> str:
        raw = value.strip().lower()
        if raw not in assembly_schedule.VALID_UPLOAD_PRIVACY:
            raise ValueError(f"upload_privacy must be one of {assembly_schedule.VALID_UPLOAD_PRIVACY}")
        return raw


class StartExtendRequest(BaseModel):
    category: str | None = Field(default=None, description="R2 category (defaults to ASSEMBLY_CATEGORY).")
    source_folder: str = Field(
        ...,
        description="R2 subfolder under pre-processed/ to extend (required).",
        examples=["korean"],
    )
    limit: int | None = Field(
        default=1,
        ge=1,
        description="Images per batch when process_all is false (must be ≥ 1 and ≤ pending).",
    )
    process_all: bool = Field(default=False, description="Extend every pending pre-processed image.")
    force: bool = Field(default=False, description="Include images that would normally be skipped.")
    parallel: bool = Field(
        default=True,
        description="When limit>1, start one Cloud Run Job per image (recommended).",
    )

    @field_validator("source_folder")
    @classmethod
    def _validate_source_folder(cls, value: str) -> str:
        try:
            return normalize_source_folder(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class DashboardLoginRequest(BaseModel):
    password: str = Field(description="Value of ASSEMBLY_DASHBOARD_PASSWORD.")


class CancelJobRequest(BaseModel):
    confirm: bool = Field(default=False, description="Set true to cancel; false returns a preview only.")


class DaySlotRequest(BaseModel):
    enabled: bool = False
    assemble_at: str = assembly_schedule.DEFAULT_ASSEMBLE_AT
    upload_at: str | None = None


class ChannelScheduleRequest(BaseModel):
    enabled: bool = True
    timezone: str = "America/New_York"
    category: str | None = None
    images_folder: str = Field(
        ...,
        description="R2 subfolder under post-processed/ for background stills (required).",
        examples=["korean"],
    )
    duration_min: int = Field(default=90, ge=5, le=300)
    variance_min: int = Field(default=15, ge=0, le=60)
    thumbnail_text: str | None = None
    queue_youtube: bool = True
    upload_privacy: str = "private"
    upload_schedule_publish: bool = True
    upload_now: bool = Field(
        default=False,
        description=(
            "When true with queue_youtube, register with uploader upload_now after assembly. "
            "Mutually exclusive with upload_schedule_publish (timed publishAt)."
        ),
    )
    upload_tags: str = ""
    upload_category_id: str = "10"
    upload_made_for_kids: bool = False
    default_assemble_at: str = assembly_schedule.DEFAULT_ASSEMBLE_AT
    default_upload_at: str | None = None
    min_backgrounds: int = Field(default=1, ge=1, le=20)
    auto_extend: bool = True
    days: list[DaySlotRequest] = Field(default_factory=lambda: [DaySlotRequest() for _ in range(7)])
    apply_default_to_enabled_days: bool = False

    @field_validator("images_folder")
    @classmethod
    def _validate_schedule_images_folder(cls, value: str) -> str:
        return _normalize_images_folder(value)

    @field_validator("upload_privacy")
    @classmethod
    def _validate_upload_privacy(cls, value: str) -> str:
        raw = value.strip().lower()
        if raw not in assembly_schedule.VALID_UPLOAD_PRIVACY:
            raise ValueError(f"upload_privacy must be one of {assembly_schedule.VALID_UPLOAD_PRIVACY}")
        return raw

    @model_validator(mode="after")
    def _exclusive_youtube_upload_mode(self) -> ChannelScheduleRequest:
        if self.upload_now and self.upload_schedule_publish:
            # Prefer immediate dispatch when both are sent.
            self.upload_schedule_publish = False
        return self


def _schedule_from_request(channel: str, body: ChannelScheduleRequest) -> assembly_schedule.ChannelSchedule:
    if len(body.days) != 7:
        raise HTTPException(status_code=400, detail="days must contain exactly 7 entries (Sun–Sat)")
    upload_now = bool(body.queue_youtube and body.upload_now)
    upload_schedule_publish = bool(body.queue_youtube and body.upload_schedule_publish and not upload_now)
    sched = assembly_schedule.ChannelSchedule(
        channel=normalize_channel(channel),
        enabled=body.enabled,
        timezone=body.timezone.strip(),
        category=body.category,
        images_folder=body.images_folder,
        duration_min=body.duration_min,
        variance_min=body.variance_min,
        thumbnail_text=body.thumbnail_text,
        queue_youtube=body.queue_youtube,
        upload_privacy=body.upload_privacy,
        upload_schedule_publish=upload_schedule_publish,
        upload_now=upload_now,
        upload_tags=body.upload_tags.strip(),
        upload_category_id=body.upload_category_id.strip() or "10",
        upload_made_for_kids=body.upload_made_for_kids,
        default_assemble_at=body.default_assemble_at,
        default_upload_at=body.default_upload_at
        or assembly_schedule.upload_time_after_assemble(body.default_assemble_at),
        min_backgrounds=body.min_backgrounds,
        auto_extend=body.auto_extend,
        days=[assembly_schedule.DaySlot.from_dict(d.model_dump()) for d in body.days],
    )
    if body.apply_default_to_enabled_days:
        assembly_schedule.apply_default_times(
            sched,
            assemble_at=body.default_assemble_at,
            upload_at=body.default_upload_at,
        )
    assembly_schedule.ensure_schedule_upload_times(sched)
    return sched


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
        "deployed_at": (os.environ.get("ASSEMBLY_DEPLOYED_AT") or "").strip(),
    }


def _updates_payload() -> dict[str, Any]:
    info = _version_info()
    manifest = load_deploy_manifest()
    return {
        **info,
        "ref": manifest.get("ref"),
        "git_sha": manifest.get("git_sha"),
        "git_sha_short": manifest.get("git_sha_short") or info.get("build"),
        "generated_at": manifest.get("generated_at") or info.get("deployed_at") or None,
        "repo_url": manifest.get("repo_url"),
        "commits": manifest.get("commits") or [],
        "source": manifest.get("source", "file"),
    }


def _invalidate_category_cache(category: str) -> None:
    dashboard_cache.invalidate_prefix(_cache_key("stats", category))
    dashboard_cache.invalidate_prefix(_cache_key("videos", category))
    dashboard_cache.invalidate_prefix(_cache_key("assets", category))


def _invalidate_schedule_cache() -> None:
    dashboard_cache.invalidate_prefix(_cache_key("schedules", "overview"))


def _queue_extend_job_local(
    client,
    bucket: str,
    *,
    execution_id: str,
    category: str,
    source_folder: str,
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
        source_folder=source_folder,
    )
    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=0,
        stage=f"Queued locally — {label}…",
        category=category,
        status="running",
        extra={"job_type": "extend", "host": "local", "source_folder": source_folder},
    )
    threading.Thread(
        target=run_extend_job,
        kwargs={
            "execution_id": execution_id,
            "category": category,
            "source_folder": source_folder,
            "max_images": max_images,
            "force": force,
        },
        daemon=True,
    ).start()
    return {
        "execution_id": execution_id,
        "status": "running",
        "category": category,
        "source_folder": source_folder,
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
    source_folder: str,
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
            source_folder=source_folder,
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
        source_folder=source_folder,
    )
    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=0,
        stage=f"Queued on Cloud Run — {label}…",
        category=category,
        status="running",
        extra={"job_type": "extend", "source_folder": source_folder},
    )
    try:
        result = gcp_jobs.start_extend_job(
            settings,
            execution_id=execution_id,
            category=category,
            source_folder=source_folder,
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
        "source_folder": source_folder,
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


@app.get("/v1/updates")
def api_updates() -> dict[str, Any]:
    """Deployed build identity + commits baked into this Cloud Run revision."""
    return _updates_payload()


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
            "POST /v1/assets/upload",
            "GET /v1/observability",
            "GET /v1/categories",
            "GET /v1/categories/{category}/inventory",
            "GET /v1/background-folders",
            "GET /v1/pre-processed-folders",
            "GET /v1/channels",
            "GET /v1/updates",
            "GET /v1/version",
            "GET /v1/cron/assembly-health",
            "POST /v1/cron/assembly-health",
            "GET /v1/schedules",
            "GET /v1/schedules/overview",
            "GET /v1/schedules/{channel}",
            "PUT /v1/schedules/{channel}",
            "DELETE /v1/schedules/{channel}",
            "GET /v1/schedules/{channel}/status",
            "GET /v1/schedules/runs",
            "GET /v1/schedules/{channel}/runs",
            "DELETE /v1/schedules/runs/{slot_key}",
            "GET /v1/cron/run-schedules",
            "POST /v1/cron/run-schedules",
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
    _assert_background_folder_exists(client, bucket, body.images_folder)
    _invalidate_category_cache(category)
    if body.images_folder != category:
        _invalidate_category_cache(body.images_folder)

    publish_at = (body.publish_at or "").strip() or None
    upload_at = (body.upload_at or "").strip() or None
    upload_now = False
    if body.queue_youtube and body.upload_schedule_publish:
        if not publish_at and not upload_at:
            raise HTTPException(
                status_code=400,
                detail="publish_at or upload_at is required when upload_schedule_publish is true",
            )
    elif body.queue_youtube and not body.upload_schedule_publish:
        # Post immediately: uploader register with upload_now + no_schedule.
        publish_at = None
        upload_at = None
        upload_now = True
    elif not body.upload_schedule_publish:
        publish_at = None
        upload_at = None

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
                upload_privacy=body.upload_privacy if body.queue_youtube else None,
                publish_at=publish_at if body.queue_youtube else None,
                upload_at=upload_at if body.queue_youtube else None,
                upload_now=upload_now if body.queue_youtube else False,
                upload_tags=body.upload_tags.strip() or None if body.queue_youtube else None,
                upload_category_id=body.upload_category_id.strip() or None if body.queue_youtube else None,
                upload_made_for_kids=body.upload_made_for_kids if body.queue_youtube else None,
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


def _start_extend_for_schedule(
    client,
    bucket: str,
    settings: ApiSettings,
    *,
    execution_id: str,
    category: str,
    max_images: int,
    force: bool,
) -> dict[str, Any]:
    write_meta_json(
        client,
        bucket,
        execution_id,
        category=category,
        job_type="extend",
        limit=max_images,
    )
    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=0,
        stage="Scheduled auto-extend…",
        category=category,
        status="running",
        extra={"job_type": "extend", "source": "schedule"},
    )
    return gcp_jobs.start_extend_job(
        settings,
        execution_id=execution_id,
        category=category,
        max_images=max_images,
        force=force,
    )


@app.get("/v1/schedules")
def list_schedules(
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    client, bucket = _r2()
    schedules = assembly_schedule.list_schedules(client, bucket)
    return {"schedules": [s.to_dict() for s in schedules], "count": len(schedules)}


@app.get("/v1/schedules/overview")
def schedules_overview(
    upcoming_limit: int = Query(default=25, ge=1, le=100),
    runs_limit: int = Query(default=40, ge=1, le=200),
    refresh: bool = Query(default=False, description="Bypass overview cache"),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    """All channel schedules with upcoming slots and recent cron ledger entries."""
    cache_key = _cache_key("schedules", "overview", str(upcoming_limit), str(runs_limit))

    def load() -> dict[str, Any]:
        client, bucket = _r2()
        return assembly_schedule.schedules_overview(
            client,
            bucket,
            settings,
            upcoming_limit=upcoming_limit,
            runs_limit=runs_limit,
        )

    if refresh:
        data = load()
        hit = False
        dashboard_cache.set(cache_key, data, 30.0)
    else:
        data, hit = dashboard_cache.get_or_set(cache_key, 30.0, load)
    return JSONResponse(
        content={**data, "cache": {"hit": hit, "ttl_sec": 30}},
        headers={"X-Cache": "HIT" if hit else "MISS"},
    )


@app.get("/v1/schedules/{channel}")
def get_schedule(
    channel: str,
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    client, bucket = _r2()
    sched = assembly_schedule.get_schedule(client, bucket, normalize_channel(channel))
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return sched.to_dict()


@app.put("/v1/schedules/{channel}")
def put_schedule(
    channel: str,
    body: ChannelScheduleRequest,
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    try:
        sched = _schedule_from_request(channel, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    client, bucket = _r2()
    _assert_background_folder_exists(client, bucket, sched.images_folder or body.images_folder)
    saved = assembly_schedule.upsert_schedule(client, bucket, sched)
    _invalidate_schedule_cache()
    return saved.to_dict()


@app.delete("/v1/schedules/{channel}")
def remove_schedule(
    channel: str,
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    client, bucket = _r2()
    if not assembly_schedule.delete_schedule(client, bucket, normalize_channel(channel)):
        raise HTTPException(status_code=404, detail="Schedule not found")
    _invalidate_schedule_cache()
    return {"deleted": True, "channel": channel}


@app.get("/v1/schedules/{channel}/status")
def schedule_status(
    channel: str,
    include_resources: bool = Query(default=True, description="Include R2 inventory checks"),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    client, bucket = _r2()
    sched = assembly_schedule.get_schedule(client, bucket, normalize_channel(channel))
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    resources: dict[str, Any] | None = None
    if include_resources:
        resources = assembly_schedule.evaluate_resources(client, bucket, sched, settings)
    upcoming = assembly_schedule.preview_schedule(sched)
    out: dict[str, Any] = {
        "schedule": sched.to_dict(),
        "upcoming": upcoming,
    }
    if resources is not None:
        out["resources"] = resources
    return out


@app.get("/v1/schedules/runs")
def list_all_schedule_runs(
    channel: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    client, bucket = _r2()
    ch = normalize_channel(channel) if channel else None
    runs = assembly_schedule.list_schedule_runs(client, bucket, channel=ch, limit=limit)
    return {"runs": runs, "count": len(runs)}


@app.get("/v1/schedules/{channel}/runs")
def list_channel_schedule_runs(
    channel: str,
    limit: int = Query(default=50, ge=1, le=200),
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    client, bucket = _r2()
    ch = normalize_channel(channel)
    runs = assembly_schedule.list_schedule_runs(client, bucket, channel=ch, limit=limit)
    return {"runs": runs, "count": len(runs), "channel": ch}


@app.delete("/v1/schedules/runs/{slot_key:path}")
def clear_schedule_run(
    slot_key: str,
    _auth: None = Depends(require_api_auth),
) -> dict[str, Any]:
    """Remove ledger entry so a slot can fire again on the next cron tick."""
    client, bucket = _r2()
    if not assembly_schedule.delete_schedule_run(client, bucket, slot_key):
        raise HTTPException(status_code=404, detail="Schedule run not found")
    return {"deleted": True, "slot_key": slot_key}


@app.post("/v1/cron/run-schedules")
@app.get("/v1/cron/run-schedules")
def cron_run_schedules(
    dry_run: bool = Query(default=False),
    window_minutes: int = Query(default=assembly_schedule.DEFAULT_WINDOW_MINUTES, ge=5, le=60),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    """Evaluate per-channel schedules and start assembly jobs (Cloud Scheduler every 15m)."""
    client, bucket = _r2()

    def _extend(client, bucket, settings, *, execution_id, category, max_images, force):
        return _start_extend_for_schedule(
            client,
            bucket,
            settings,
            execution_id=execution_id,
            category=category,
            max_images=max_images,
            force=force,
        )

    return assembly_schedule.run_due_schedules(
        client,
        bucket,
        settings,
        window_minutes=window_minutes,
        dry_run=dry_run,
        new_execution_id=_new_execution_id,
        start_extend_fn=_extend,
    )


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
    images_folder: str | None = Query(default=None),
    _auth: None = Depends(require_api_auth),
) -> Response:
    """Proxy a single pre/post-processed image (loaded on demand from dashboard)."""
    client, bucket = _r2()
    folder: str | None = None
    if pool in ("post-processed", "post-used"):
        if not images_folder or not str(images_folder).strip():
            raise HTTPException(status_code=400, detail="images_folder is required for this pool")
        try:
            folder = _normalize_images_folder(images_folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        key = r2_catalog.asset_object_key(category, pool, name, images_folder=folder)
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
            "inventory": r2_catalog.dashboard_inventory(client, bucket, cat),
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
    images_folder: str | None = Query(
        default=None,
        description="Background pool under post-processed/ (required for post-processed and post-used pools).",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> JSONResponse:
    """List image filenames + metadata only (no bytes)."""
    cat = category or settings.default_category
    folder: str | None = None
    if pool in ("post-processed", "post-used"):
        if not images_folder or not str(images_folder).strip():
            raise HTTPException(
                status_code=400,
                detail="images_folder is required for post-processed and post-used asset pools",
            )
        try:
            folder = _normalize_images_folder(images_folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    cache_key = _cache_key("assets", cat, pool, folder or "", str(limit))

    def load() -> dict[str, Any]:
        client, bucket = _r2()
        items = r2_catalog.list_assets(
            client, bucket, category=cat, pool=pool, limit=limit, images_folder=folder
        )
        return {
            "category": cat,
            "images_folder": folder,
            "pool": pool,
            "items": items,
            "count": len(items),
        }

    data, hit = dashboard_cache.get_or_set(cache_key, 60.0, load)
    return JSONResponse(
        content={**data, "cache": {"hit": hit, "ttl_sec": 60}},
        headers={"X-Cache": "HIT" if hit else "MISS"},
    )


@app.post("/v1/assets/upload")
async def upload_assets(
    pool: str = Form(..., pattern="^(pre-processed|post-processed)$"),
    category: str | None = Form(default=None),
    images_folder: str | None = Form(default=None),
    overwrite: bool = Form(default=False),
    files: list[UploadFile] = File(...),
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    """Upload one or more images to a pre/post-processed pool on R2."""
    cat = (category or settings.default_category).strip()
    folder: str | None = None
    if pool == "post-processed":
        if not images_folder or not str(images_folder).strip():
            raise HTTPException(
                status_code=400,
                detail="images_folder is required when uploading to post-processed",
            )
        try:
            folder = _normalize_images_folder(images_folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not files:
        raise HTTPException(status_code=400, detail="No files attached")

    payloads: list[tuple[str, bytes]] = []
    for upload in files:
        if not upload.filename:
            continue
        data = await upload.read()
        payloads.append((upload.filename, data))

    client, bucket = _r2()
    try:
        result = asset_upload.upload_asset_files(
            client,
            bucket,
            category=cat,
            pool=pool,
            images_folder=folder,
            files=payloads,
            overwrite=overwrite,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _invalidate_category_cache(cat)
    if folder and folder != cat:
        _invalidate_category_cache(folder)
    return result


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
    effective_limit = min(job_limit, 50) if light else job_limit
    scan_extra_running = not light

    with ThreadPoolExecutor(max_workers=2) as pool:
        asm_future = pool.submit(
            job_runs.list_r2_job_runs,
            client,
            bucket,
            id_prefix="asm_",
            limit=effective_limit,
            scan_extra_running=scan_extra_running,
        )
        ext_future = pool.submit(
            job_runs.list_r2_job_runs,
            client,
            bucket,
            id_prefix="ext_",
            limit=effective_limit,
            scan_extra_running=scan_extra_running,
        )
        asm_raw = asm_future.result()
        ext_raw = ext_future.result()

    reconcile_asm_gcp = (not light) or job_status.runs_need_gcp_reconcile(asm_raw)
    reconcile_ext_gcp = (not light) or job_status.runs_need_gcp_reconcile(ext_raw)
    assembly_runs = job_status.reconcile_assembly_runs(
        settings, client, bucket, asm_raw, patch_r2=not light, reconcile_gcp=reconcile_asm_gcp
    )
    extend_runs = job_status.reconcile_extend_runs(
        settings, client, bucket, ext_raw, patch_r2=not light, reconcile_gcp=reconcile_ext_gcp
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
                "inventory": r2_catalog.dashboard_inventory(client, bucket, cat),
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
    source_folder = body.source_folder
    client, bucket = _r2()
    _assert_pre_processed_folder_exists(client, bucket, source_folder)
    _invalidate_category_cache(category)
    cfg = r2_config_from_env(category=category)
    pending = count_pending_r2_sources(
        client, cfg, force=body.force, source_folder=source_folder
    )
    if pending == 0:
        raise HTTPException(
            status_code=409,
            detail=f"No pending pre-processed images in pre-processed/{source_folder}/",
        )

    if not body.process_all:
        requested = body.limit or 1
        if requested > pending:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Requested {requested} images but only {pending} pending "
                    f"in pre-processed/{source_folder}/"
                ),
            )
        batch = requested
    else:
        batch = pending

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
                    source_folder=source_folder,
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
            "source_folder": source_folder,
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
        source_folder=source_folder,
        max_images=max_images,
        force=body.force,
        exclude_gcp_ids=assigned_gcp,
    )
    return {
        **job,
        "parallel": False,
        "pending": pending,
        "batch_size": batch,
        "source_folder": source_folder,
        "host": "cloud_run",
    }


@app.get("/v1/extend/pending")
def extend_pending(
    category: str | None = None,
    source_folder: str | None = None,
    force: bool = False,
    _auth: None = Depends(require_api_auth),
    settings: ApiSettings = Depends(_settings),
) -> dict[str, Any]:
    cat = category or settings.default_category
    client, bucket = _r2()
    folder: str | None = None
    if source_folder is not None and str(source_folder).strip():
        try:
            folder = normalize_source_folder(source_folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _assert_pre_processed_folder_exists(client, bucket, folder)
    cfg = r2_config_from_env(category=cat)
    pending = count_pending_r2_sources(
        client, cfg, force=force, source_folder=folder
    )
    return {"category": cat, "source_folder": folder, "pending": pending}


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


@app.get("/v1/pre-processed-folders")
def list_pre_processed_folders(_auth: None = Depends(require_api_auth)) -> dict[str, Any]:
    """Subfolders under ``pre-processed/`` — selectable source pools for extend."""
    client, bucket = _r2()
    folders = r2_catalog.list_pre_processed_folders(client, bucket)
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
    return _DASHBOARD_HTML.replace("__DEFAULT_CATEGORY__", settings.default_category).replace(
        "__DEFAULT_THUMBNAIL__",
        (os.environ.get("THUMBNAIL_TEXT") or "PLAYLIST").strip() or "PLAYLIST",
    )


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
      --section-gap: 48px;
      --card-padding: 28px;
      --element-gap: 16px;
      --radius-nav: 14px;
      --radius-cards: 32px;
      --radius-badges: 9999px;
      --radius-images: 12px;
      --radius-buttons: 14px;
      --radius-small: 8px;
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
      line-height: 1.4;
      background: var(--color-cream-paper);
      color: var(--color-midnight-ink);
      margin: 0;
      padding: 0 0 52px;
      -webkit-font-smoothing: antialiased;
    }
    .page {
      max-width: var(--page-max-width);
      margin: 0 auto;
      padding: 20px 32px 0;
    }
    h1, h2, h3 {
      font-family: var(--font-figtree);
      font-weight: 600;
      color: var(--color-midnight-ink);
      letter-spacing: -0.02em;
    }
    h2 {
      font-size: 17px;
      line-height: 1.3;
      margin: 0 0 6px;
    }
    h2::after { display: none; }
    h3 {
      font-size: 14px;
      line-height: 1.35;
      margin: 20px 0 8px;
      font-weight: 600;
      color: var(--color-smoke);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .panel-title {
      margin: 0;
      flex: 1;
      font-size: 16px;
      font-weight: 600;
      line-height: 1.3;
    }
    .panel-count {
      font-weight: 400;
      font-size: 13px;
      color: var(--color-smoke);
    }
    .card-desc {
      color: var(--color-smoke);
      font-size: 14px;
      line-height: 1.45;
      margin: 0 0 18px;
      max-width: 52ch;
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
      padding: 10px 16px;
      margin: 0 0 20px;
      background: var(--color-white);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-nav);
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
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr));
      gap: 10px;
      margin: 0 0 24px;
    }
    .stat-chip {
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 12px 14px;
      background: var(--color-white);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      font-size: 13px;
      min-height: 3.25rem;
    }
    .stat-label {
      color: var(--color-smoke);
      font-size: 12px;
      font-weight: 500;
      letter-spacing: 0.01em;
    }
    .stat-value {
      font-weight: 600;
      color: var(--color-midnight-ink);
      font-size: 18px;
      line-height: 1.2;
      font-variant-numeric: tabular-nums;
    }
    .main-nav {
      display: flex;
      gap: 4px;
      padding: 4px;
      margin: 0 0 24px;
      background: var(--color-white);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-nav);
    }
    .main-tab {
      background: transparent;
      color: var(--color-smoke);
      border: none;
      border-radius: calc(var(--radius-nav) - 4px);
      padding: 9px 18px;
      font-size: 14px;
      font-weight: 500;
      text-decoration: none;
      flex: 1;
      text-align: center;
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
      gap: 20px;
      margin: 0 0 18px;
      border-bottom: 1px solid var(--color-stone-mist);
    }
    .job-tab {
      background: transparent;
      color: var(--color-smoke);
      border: none;
      border-bottom: 3px solid transparent;
      border-radius: 0;
      padding: 0 0 8px;
      margin: 0 0 -1px;
      font-size: 14px;
      font-weight: 500;
      text-decoration: none;
    }
    .job-tab:hover { color: var(--color-midnight-ink); }
    .job-tab.active {
      color: var(--color-midnight-ink);
      font-weight: 600;
      border-bottom-color: var(--color-lavender-whisper);
      box-shadow: none;
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
    .run-publish-mode {
      margin: 0;
      padding: 10px 12px;
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .run-publish-mode legend {
      padding: 0 4px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--color-midnight-ink);
    }
    .run-youtube-timing {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .run-youtube-timing-label {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--color-midnight-ink);
    }
    .run-timing-seg {
      display: flex;
      flex-wrap: wrap;
      gap: 0;
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      overflow: hidden;
      background: var(--color-white);
    }
    .run-timing-seg button {
      flex: 1 1 auto;
      min-width: 7.5rem;
      margin: 0;
      border: none;
      border-radius: 0;
      border-right: 1px solid var(--color-stone-mist);
      background: transparent;
      color: var(--color-graphite-veil);
      font-size: 13px;
      font-weight: 500;
      padding: 10px 12px;
      cursor: pointer;
    }
    .run-timing-seg button:last-child { border-right: none; }
    .run-timing-seg button:hover { color: var(--color-midnight-ink); background: color-mix(in srgb, var(--color-cream-paper) 80%, var(--color-white)); }
    .run-timing-seg button.active {
      background: var(--color-midnight-ink);
      color: var(--color-white);
    }
    .run-youtube-block {
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      padding: 14px;
      background: color-mix(in srgb, var(--color-white) 70%, var(--color-cream-paper));
    }
    .run-youtube-title {
      margin: 0 0 4px;
      font-size: 14px;
      font-weight: 600;
      color: var(--color-midnight-ink);
    }
    .run-youtube-options[hidden],
    #runScheduleFieldsMain[hidden] { display: none !important; }
    .run-youtube-options { margin-top: 4px; }
    .schedule-page {
      display: flex;
      flex-direction: column;
      gap: 24px;
    }
    .schedule-page__header {
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--color-stone-mist);
    }
    .schedule-page__intro .panel-title { margin: 0 0 6px; }
    .schedule-page__intro .card-desc { margin: 0; }
    .schedule-page__toolbar {
      margin-bottom: 0;
      align-items: flex-end;
    }
    .schedule-page__toolbar label { min-width: min(280px, 100%); }
    .schedule-panel {
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-nav);
      background: var(--color-cream-paper);
      overflow: hidden;
    }
    .schedule-panel__head {
      padding: 18px 20px 0;
    }
    .schedule-panel__head .hint { margin: 6px 0 0; max-width: 62ch; }
    .schedule-panel__meta {
      margin: 0;
      padding: 14px 20px 0;
      font-size: 13px;
      color: var(--color-smoke);
      line-height: 1.45;
    }
    .schedule-panel__meta code {
      font-size: 12px;
      background: var(--color-white);
      padding: 2px 6px;
      border-radius: 4px;
      border: 1px solid var(--color-stone-mist);
    }
    .schedule-panel__stack {
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding: 16px 20px 20px;
    }
    .schedule-block {
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      background: var(--color-white);
      overflow: hidden;
    }
    .schedule-block__head {
      padding: 11px 16px;
      border-bottom: 1px solid var(--color-stone-mist);
      background: var(--color-pale-lavender-tint);
    }
    .schedule-block__title {
      margin: 0;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: var(--color-graphite-veil);
    }
    .schedule-block__body > .muted,
    .schedule-block__body > p.muted {
      margin: 0;
      padding: 16px;
      font-size: 13px;
    }
    .schedule-table-wrap {
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }
    .schedule-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    .schedule-table th,
    .schedule-table td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--color-stone-mist);
      text-align: left;
      vertical-align: top;
    }
    .schedule-table th {
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--color-graphite-veil);
      background: var(--color-white);
    }
    .schedule-table tr:last-child td { border-bottom: none; }
    .schedule-table tr.is-disabled { opacity: 0.55; }
    .schedule-table tr.is-clickable { cursor: pointer; }
    .schedule-table tr.is-clickable:hover td {
      background: var(--color-pale-lavender-tint);
    }
    .schedule-table .schedule-run-clear { white-space: nowrap; }
    .schedule-empty {
      padding: 32px 24px;
      border: 1px dashed var(--color-stone-mist);
      border-radius: var(--radius-nav);
      background: var(--color-pale-lavender-tint);
      text-align: center;
    }
    .schedule-empty-title {
      margin: 0 0 6px;
      font-size: 15px;
      font-weight: 600;
      color: var(--color-midnight-ink);
    }
    .schedule-empty .muted { margin: 0; font-size: 14px; }
    .schedule-empty-actions {
      margin-top: 16px;
      display: flex;
      justify-content: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .schedule-notice {
      padding: 14px 16px;
      border: 1px solid var(--color-stone-mist);
      border-left: 3px solid var(--color-midnight-ink);
      border-radius: var(--radius-buttons);
      background: color-mix(in srgb, var(--color-cream-paper) 70%, var(--color-white));
    }
    .schedule-notice[hidden] { display: none !important; }
    .schedule-notice-title {
      margin: 0 0 4px;
      font-size: 14px;
      font-weight: 600;
      color: var(--color-midnight-ink);
    }
    .schedule-notice .muted { margin: 0; font-size: 13px; line-height: 1.4; }
    .schedule-view[hidden] { display: none !important; }
    .schedule-editor-toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: flex-end;
      margin-bottom: 16px;
    }
    .schedule-editor-toolbar label { min-width: min(280px, 100%); margin: 0; }
    .schedule-subtabs {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 14px;
    }
    .schedule-editor {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .schedule-editor__head {
      padding-bottom: 14px;
      border-bottom: 1px solid var(--color-stone-mist);
    }
    .schedule-editor__title {
      margin: 0;
      font-size: 16px;
      font-weight: 600;
      color: var(--color-midnight-ink);
      letter-spacing: -0.01em;
    }
    .schedule-editor__head .hint { margin: 6px 0 0; }
    .schedule-editor__footer {
      display: flex;
      flex-direction: column;
      gap: 16px;
      padding-top: 4px;
      border-top: 1px solid var(--color-stone-mist);
    }
    .schedule-editor__actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }
    .schedule-editor__actions .btn-primary {
      width: auto;
      min-width: 11rem;
      padding: 12px 24px;
    }
    .schedule-panel--diagnostics {
      background: var(--color-white);
    }
    .schedule-panel--diagnostics .schedule-panel__head {
      padding: 14px 18px 0;
    }
    .schedule-panel--diagnostics .schedule-panel__stack {
      padding: 12px 18px 16px;
      gap: 8px;
    }
    .schedule-panel--diagnostics .schedule-diagnostic {
      border: none;
      border-radius: var(--radius-small);
      background: var(--color-cream-paper);
      padding: 0 12px;
    }
    .schedule-panel--diagnostics .schedule-diagnostic + .schedule-diagnostic {
      margin-top: 0;
    }
    .schedule-editor.is-schedule-disabled .schedule-section:not(.schedule-section--status) {
      opacity: 0.55;
      pointer-events: none;
    }
    .schedule-editor.is-upload-times-disabled .schedule-upload-time-control {
      opacity: 0.45;
      pointer-events: none;
    }
    .schedule-editor.is-upload-times-disabled .schedule-day-time-grid {
      grid-template-columns: 1fr;
    }
    .schedule-editor.is-upload-times-disabled .schedule-day-field--upload {
      display: none;
    }
    .schedule-upload-mode {
      margin: 0;
      padding: 12px 14px;
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .schedule-upload-mode legend {
      padding: 0 4px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--color-midnight-ink);
    }
    .schedule-upload-mode .hint { margin: 0 0 4px 24px; }
    .schedule-section {
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-nav);
      background: var(--color-white);
      overflow: hidden;
    }
    .schedule-section-head {
      padding: 16px 18px 0;
    }
    .schedule-section-head .hint {
      margin: 8px 0 0;
      max-width: 60ch;
    }
    .schedule-section-body {
      padding: 14px 18px 18px;
    }
    .schedule-section-body.form-stack { gap: 16px; }
    .schedule-section-title {
      margin: 0;
      font-size: 14px;
      font-weight: 600;
      color: var(--color-midnight-ink);
      letter-spacing: -0.01em;
    }
    .schedule-section--compact .schedule-section-body { padding-top: 14px; padding-bottom: 14px; }
    .schedule-day-bar {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 8px;
    }
    .schedule-day-times {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 12px;
      margin-top: 16px;
    }
    .schedule-day-times.is-empty {
      display: block;
      margin-top: 16px;
      padding: 14px 16px;
      border: 1px dashed var(--color-stone-mist);
      border-radius: var(--radius-small);
      background: var(--color-pale-lavender-tint);
    }
    .schedule-day-times.is-empty p { margin: 0; font-size: 13px; }
    .schedule-day-time-card {
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      padding: 14px;
      background: var(--color-pale-lavender-tint);
    }
    .schedule-day-time-card-head {
      font-size: 13px;
      font-weight: 600;
      color: var(--color-midnight-ink);
      margin-bottom: 12px;
    }
    .schedule-day-time-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .schedule-day-field {
      display: block;
      margin: 0;
      min-width: 0;
    }
    .schedule-day-field-label {
      display: block;
      margin-bottom: 6px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--color-graphite-veil);
    }
    .schedule-day-time-card .time-picker { margin-top: 0; }
    .schedule-day-time-card .time-picker-display { display: none; }
    .schedule-default-times {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .schedule-inline-action { margin-top: 0; }
    .schedule-actions {
      display: none;
    }
    .schedule-diagnostics {
      display: none;
    }
    .time-picker { margin-top: 4px; }
    .time-picker-row {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .time-picker-row select {
      width: auto;
      min-width: 68px;
      margin-top: 0;
      padding: 10px 12px;
      flex: 0 0 auto;
      font-size: 14px;
    }
    .time-picker-minute { min-width: 76px; }
    .time-picker-colon {
      font-weight: 600;
      color: var(--color-smoke);
      line-height: 1;
      user-select: none;
    }
    .time-picker-ampm {
      display: inline-flex;
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      overflow: hidden;
      background: var(--color-white);
    }
    button.time-picker-ampm-btn {
      padding: 10px 14px;
      font-size: 13px;
      font-weight: 600;
      color: var(--color-smoke);
      background: var(--color-white);
      border: none;
      border-right: 1px solid var(--color-stone-mist);
    }
    button.time-picker-ampm-btn:last-child { border-right: none; }
    button.time-picker-ampm-btn.is-active {
      background: var(--color-lavender-whisper);
      color: var(--color-midnight-ink);
    }
    button.time-picker-ampm-btn:hover:not(.is-active) {
      background: var(--color-pale-lavender-tint);
      color: var(--color-midnight-ink);
    }
    .time-picker-display {
      display: block;
      margin-top: 6px;
      font-size: 12px;
      color: var(--color-graphite-veil);
    }
    .schedule-summary {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr));
      gap: 14px 20px;
      margin: 0;
      padding: 0;
      border: none;
      background: transparent;
      font-size: 14px;
      line-height: 1.4;
    }
    .schedule-summary dt {
      margin: 0;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--color-graphite-veil);
    }
    .schedule-summary dd {
      margin: 4px 0 0;
      color: var(--color-midnight-ink);
      font-size: 14px;
    }
    .schedule-summary dd code {
      font-size: 13px;
      background: var(--color-pale-lavender-tint);
      padding: 2px 6px;
      border-radius: 4px;
    }
    .schedule-diagnostic {
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      background: var(--color-cream-paper);
      padding: 0 16px;
    }
    .schedule-diagnostic summary {
      cursor: pointer;
      padding: 12px 0;
      font-size: 13px;
      font-weight: 600;
      color: var(--color-midnight-ink);
      list-style: none;
    }
    .schedule-diagnostic summary::-webkit-details-marker { display: none; }
    .schedule-diagnostic[open] summary { border-bottom: 1px solid var(--color-stone-mist); margin-bottom: 0; }
    .schedule-diagnostic-body { padding: 12px 0 14px; }
    .schedule-diagnostic-body .schedule-table-wrap { margin: 0 -4px; }
    .schedule-diagnostic-body pre {
      margin: 0;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .schedule-days-grid { display: none; }
    .schedule-upcoming-groups {
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 12px;
    }
    .schedule-upcoming-group {
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-md);
      overflow: hidden;
      background: var(--color-pure-white);
    }
    .schedule-upcoming-group.is-disabled { opacity: 0.55; }
    .schedule-upcoming-group-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px 12px;
      padding: 10px 12px;
      background: var(--color-pale-lavender-tint);
      border-bottom: 1px solid var(--color-stone-mist);
    }
    .schedule-upcoming-group-head.is-clickable { cursor: pointer; }
    .schedule-upcoming-group-head.is-clickable:hover {
      background: var(--color-lavender-whisper);
    }
    .schedule-upcoming-group-meta {
      font-size: 12px;
      color: var(--color-smoke);
    }
    .schedule-upcoming-group .schedule-table { margin: 0; }
    .schedule-upcoming-group-empty {
      padding: 14px 16px;
      font-size: 13px;
      margin: 0;
    }
    .schedule-badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: var(--radius-badges);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.02em;
    }
    .schedule-badge--on {
      background: var(--color-lavender-whisper);
      color: var(--color-midnight-ink);
      border: 1px solid var(--color-midnight-ink);
    }
    .schedule-badge--off {
      background: var(--color-stone-mist);
      color: var(--color-smoke);
    }
    .schedule-badge--ready {
      background: rgba(3, 79, 70, 0.12);
      color: var(--color-deep-forest-teal);
    }
    .schedule-badge--blocked {
      background: rgba(95, 95, 89, 0.12);
      color: var(--color-smoke);
    }
    .asset-folder-label { display: block; margin: 0 0 12px; font-size: 14px; }
    .asset-folder-label select { margin-top: 6px; width: min(280px, 100%); }
    .card {
      background: var(--color-white);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-cards);
      box-shadow: none;
      padding: var(--card-padding);
      margin-bottom: 20px;
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
    button.schedule-day-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      min-height: 42px;
      padding: 10px 8px;
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      background: var(--color-white);
      color: var(--color-smoke);
      font-weight: 600;
      font-size: 13px;
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s, color 0.15s;
    }
    button.schedule-day-btn:hover {
      border-color: var(--color-midnight-ink);
      color: var(--color-midnight-ink);
    }
    button.schedule-day-btn.is-active {
      background: var(--color-lavender-whisper);
      border-color: var(--color-midnight-ink);
      color: var(--color-midnight-ink);
      box-shadow: inset 0 0 0 1px var(--color-midnight-ink);
    }
    .btn-primary, #runBtn {
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
    .btn-primary:hover, #runBtn:hover { background: var(--color-lavender-light); }
    button.btn-secondary, .btn-secondary {
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
      cursor: pointer;
    }
    button.btn-secondary:hover, .btn-secondary:hover { border-color: var(--color-midnight-ink); }
    button.btn-danger-outline, .btn-danger-outline {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: var(--color-white);
      color: var(--color-smoke);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      padding: 10px 18px;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
    }
    .btn-danger-outline.btn-sm { padding: 8px 14px; font-size: 13px; }
    button.btn-danger-outline:hover:not(:disabled), .btn-danger-outline:hover:not(:disabled) {
      color: #9b2c2c;
      border-color: #c53030;
      background: rgba(197, 48, 48, 0.06);
    }
    button.btn-sm, .btn-sm {
      padding: 8px 14px;
      font-size: 13px;
      font-weight: 500;
    }
    button.btn-ghost, .btn-ghost {
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
      margin: 0 0 20px;
      padding: 4px;
      background: var(--color-white);
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-nav);
    }
    .library-tabs .tab, .tabs .tab {
      flex: 1;
      text-align: center;
      border-radius: calc(var(--radius-nav) - 4px);
      padding: 9px 14px;
      min-width: 6rem;
    }
    .tab, .subtab { margin: 0; }
    .tab.active, .subtab.active {
      color: var(--color-white);
      font-weight: 600;
      background: var(--color-midnight-ink);
      text-decoration: none;
      box-shadow: none;
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td {
      text-align: left;
      padding: 11px 10px;
      border-bottom: 1px solid var(--color-stone-mist);
      vertical-align: middle;
    }
    th {
      font-weight: 500;
      color: var(--color-smoke);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    tbody tr:last-child td { border-bottom: none; }
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
      padding: 10px 32px;
      background: var(--color-white);
      color: var(--color-graphite-veil);
      font-size: 11px;
      letter-spacing: 0;
      border-top: 1px solid var(--color-stone-mist);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 18px;
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
      cursor: pointer;
      background: none;
      border: none;
      padding: 0;
    }
    .obs-version:hover { color: var(--color-midnight-ink); text-decoration: underline; }
    .updates-meta {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 12px 20px;
      margin: 16px 0 24px;
    }
    .updates-meta dt {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--color-graphite-veil);
      margin: 0;
    }
    .updates-meta dd {
      margin: 2px 0 0;
      font-family: ui-monospace, monospace;
      font-size: 13px;
      color: var(--color-midnight-ink);
      word-break: break-all;
    }
    .updates-hint {
      margin: 0 0 16px;
      color: var(--color-smoke);
      font-size: 13px;
      max-width: 52rem;
    }
    .updates-commits {
      width: 100%;
      border-collapse: collapse;
    }
    .updates-commits th {
      text-align: left;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--color-graphite-veil);
      padding: 8px 10px 8px 0;
      border-bottom: 1px solid var(--color-stone-mist);
    }
    .updates-commits td {
      padding: 10px 10px 10px 0;
      border-bottom: 1px solid var(--color-stone-mist);
      vertical-align: top;
      font-size: 13px;
    }
    .updates-commits code {
      font-size: 12px;
    }
    .updates-commits a { color: var(--color-deep-forest-teal); }
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
      line-height: 1.45;
      white-space: pre-wrap;
      background: transparent;
      padding: 0;
      border: none;
      color: var(--color-smoke);
      font-family: var(--font-figtree);
    }
    .detail-heading {
      font-size: 12px;
      font-weight: 600;
      margin: 16px 0 6px;
      color: var(--color-smoke);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .detail-meta {
      margin-top: 12px;
      font-size: 12px;
      color: var(--color-graphite-veil);
      word-break: break-all;
    }
    .asset-table { max-height: 400px; overflow: auto; }
    .asset-upload {
      margin: 0 0 16px;
      padding: 14px 16px;
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-md);
      background: rgba(255,255,255,0.45);
    }
    .asset-upload-row {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: flex-end;
    }
    .asset-upload-row label { display: flex; flex-direction: column; gap: 6px; font-size: 14px; }
    .asset-upload-row input[type=file] { max-width: 280px; font-size: 13px; }
    .asset-upload-hint { margin: 8px 0 0; font-size: 13px; color: var(--color-smoke); }
    .asset-upload-status { margin-top: 10px; font-size: 14px; }
    .asset-upload-status.ok { color: var(--color-deep-forest-teal); }
    .asset-upload-status.err { color: #9b2c2c; }
    .asset-upload-progress {
      display: none;
      margin-top: 12px;
    }
    .asset-upload-progress.active { display: block; }
    .asset-upload-progress .bar {
      height: 10px;
      margin-top: 6px;
    }
    .asset-upload-progress-meta {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 13px;
      color: var(--color-smoke);
    }
    .asset-upload-progress-pct {
      font-variant-numeric: tabular-nums;
      color: var(--color-midnight-ink);
      font-weight: 600;
    }
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
      background: var(--color-white);
      z-index: 1;
      box-shadow: 0 1px 0 var(--color-stone-mist);
    }
    .job-table-wrap td.job-progress { min-width: 12rem; max-width: 28rem; vertical-align: top; }
    .json-block {
      display: none;
      margin-top: 12px;
      border: 1px solid var(--color-stone-mist);
      border-radius: var(--radius-buttons);
      background: var(--color-cream-paper);
      overflow: hidden;
    }
    .json-block.has-content { display: block; }
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
      gap: 10px 14px;
      flex-wrap: wrap;
      align-items: center;
      margin-bottom: 14px;
    }
    .job-toolbar--flush { margin-top: 0; }
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
    .section-card { margin-bottom: 0; }
    a { color: var(--color-deep-forest-teal); text-decoration: none; }
    a:hover { text-decoration: underline; text-underline-offset: 2px; }
    @media (max-width: 720px) {
      .page { padding: 0 16px; }
      .obs-bar { padding: 10px 16px; }
      .create-grid { grid-template-columns: 1fr; }
      .form-row-2, .form-row-3 { grid-template-columns: 1fr; }
      .schedule-day-bar { grid-template-columns: repeat(4, 1fr); }
      .schedule-day-time-grid { grid-template-columns: 1fr; }
      .schedule-default-times { grid-template-columns: 1fr; }
      .schedule-summary { grid-template-columns: 1fr; }
      .schedule-page__toolbar { align-items: stretch; }
      .schedule-page__toolbar label { width: 100%; }
      .schedule-editor__actions .btn-primary,
      .schedule-editor__actions .btn-danger-outline { width: 100%; min-width: 0; }
      .btn-primary, #runBtn { width: 100%; }
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
    <div class="stat-chip"><span class="stat-label">Active jobs</span><span class="stat-value" id="statRunning">0</span></div>
    <div class="stat-chip"><span class="stat-label">Backgrounds ready</span><span class="stat-value" id="statPostProcessed">—</span></div>
    <div class="stat-chip"><span class="stat-label">To extend</span><span class="stat-value" id="statExtendPending">—</span></div>
    <div class="stat-chip"><span class="stat-label">Tracks</span><span class="stat-value" id="statMusic">—</span></div>
    <div class="stat-chip"><span class="stat-label">Videos</span><span class="stat-value" id="statVideos">—</span></div>
  </div>

  <div id="authError" class="muted"></div>

  <nav class="main-nav" role="tablist" aria-label="Main sections">
    <button type="button" class="main-tab active" data-section="jobs">Jobs</button>
    <button type="button" class="main-tab" data-section="create">New run</button>
    <button type="button" class="main-tab" data-section="library">Library</button>
    <button type="button" class="main-tab" data-section="schedule">Schedule</button>
    <button type="button" class="main-tab" data-section="updates">Updates</button>
  </nav>

  <section id="sectionJobs" class="main-section active">
    <div class="card section-card">
      <nav class="job-nav" role="tablist" aria-label="Job type">
        <button type="button" class="job-tab active" data-job="assembly">Assembly</button>
        <button type="button" class="job-tab" data-job="extend">Extend</button>
      </nav>

      <div id="jobPanelAssembly" class="job-panel active">
        <div class="job-toolbar">
          <h2 class="panel-title">Assembly jobs <span class="panel-count" id="assemblyJobCount"></span></h2>
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
          <h2 class="panel-title">Extend jobs <span class="panel-count" id="extendJobCount"></span></h2>
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
        <p class="card-desc">Encode a playlist video on Cloud Run.</p>
        <div class="form-stack">
          <div>
            <label for="runChannel">YouTube channel</label>
            <select id="runChannel" required><option value="">Select channel…</option></select>
          </div>
          <div>
            <label for="runImagesFolder">Background folder</label>
            <select id="runImagesFolder" required><option value="">Loading…</option></select>
            <p class="hint">Pool under <code>post-processed/{folder}/</code> on R2 — each video claims one random still from this folder.</p>
          </div>
          <div class="run-youtube-timing" id="runYoutubeTiming">
            <span class="run-youtube-timing-label">YouTube after assembly</span>
            <div class="run-timing-seg" role="group" aria-label="YouTube upload timing">
              <button type="button" class="active" data-timing="immediate" id="runTimingImmediate">Upload immediately</button>
              <button type="button" data-timing="schedule" id="runTimingSchedule">Schedule…</button>
              <button type="button" data-timing="skip" id="runTimingSkip">Skip upload</button>
            </div>
            <p class="hint" id="runTimingHint">Dispatches to youtube-uploader with <code>upload_now</code> as soon as the video is ready.</p>
            <div id="runScheduleFieldsMain" class="form-stack" hidden>
              <div class="form-row-2">
                <div>
                  <label for="runPublishAt">Goes live (local time)</label>
                  <input id="runPublishAt" type="datetime-local"/>
                  <p class="hint">YouTube publishAt — video stays private until this time.</p>
                </div>
                <div>
                  <label for="runUploadAt">Upload to YouTube (optional)</label>
                  <input id="runUploadAt" type="datetime-local"/>
                  <p class="hint">Queue pickup time. Leave blank to upload at go-live time.</p>
                </div>
              </div>
            </div>
          </div>
          <details class="advanced">
            <summary>More options</summary>
            <div class="form-stack">
              <div>
                <label for="runChannelCustom">Custom channel slug</label>
                <input id="runChannelCustom" placeholder="e.g. nappabeats"/>
                <p class="hint">Overrides the dropdown when set.</p>
              </div>

              <div class="run-youtube-block">
                <h3 class="run-youtube-title">YouTube upload details</h3>
                <p class="hint">Privacy, tags, and kids flag. Timing is set above.</p>
                <div id="runYoutubeOptions" class="form-stack run-youtube-options">
                  <div class="form-row-3">
                    <div>
                      <label for="runUploadPrivacy">Privacy</label>
                      <select id="runUploadPrivacy">
                        <option value="private" selected>Private (recommended for scheduled publish)</option>
                        <option value="unlisted">Unlisted</option>
                        <option value="public">Public</option>
                      </select>
                    </div>
                    <div>
                      <label for="runUploadCategory">Category ID</label>
                      <input id="runUploadCategory" value="10" placeholder="10 = Music"/>
                    </div>
                    <div>
                      <label for="runUploadTags">Tags (comma-separated)</label>
                      <input id="runUploadTags" placeholder="lofi, chill, study"/>
                    </div>
                  </div>
                  <div class="checkbox-row">
                    <input type="checkbox" id="runUploadMadeForKids"/>
                    <label for="runUploadMadeForKids">Made for kids</label>
                  </div>
                </div>
              </div>

              <div>
                <label for="runThumb">Thumbnail text</label>
                <input id="runThumb" value="PLAYLIST"/>
              </div>
              <div class="form-row-3">
                <div>
                  <label for="runDuration">Duration (min)</label>
                  <input id="runDuration" type="number" min="5" max="300" step="1" value="90"/>
                </div>
                <div>
                  <label for="runVariance">Variance (min)</label>
                  <input id="runVariance" type="number" min="0" max="60" step="1" value="15"/>
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
        <p class="card-desc"><strong id="extendPending">…</strong> images waiting in <code id="extendPendingPath">pre-processed/</code>.</p>
        <div class="form-stack">
          <div>
            <label for="extendSourceFolder">Pre-processed folder</label>
            <select id="extendSourceFolder" required><option value="">Loading…</option></select>
          </div>
          <div>
            <label for="extendLimit">Batch size</label>
            <input id="extendLimit" type="number" min="1" step="1" value="1"/>
          </div>
          <div class="checkbox-row">
            <input type="checkbox" id="extendProcessAll"/>
            <label for="extendProcessAll">Process all pending in folder</label>
          </div>
        </div>
        <div class="card-actions">
          <button id="extendBtn" class="btn-ghost">Start extend</button>
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
      <button type="button" class="tab" data-tab="obs">System</button>
    </nav>

    <div id="panelVideos" class="panel card active">
      <div class="job-toolbar job-toolbar--flush">
        <h2 class="panel-title">Music videos</h2>
        <label>Channel
          <select id="videoChannel"><option value="">All channels</option></select>
        </label>
      </div>
      <p class="card-desc">Open a row to preview metadata and playback.</p>
      <div id="videoList"><p class="muted">Loading videos…</p></div>
    </div>

    <div id="panelAssets" class="panel card">
      <h2>Background images</h2>
      <p class="card-desc">Browse R2 pools by folder. Upload hundreds of images in batches to Cloudflare R2 (jpg, png, webp).</p>
      <label class="asset-folder-label" id="assetFolderWrap" hidden>Background folder
        <select id="assetImagesFolder"><option value="">Loading…</option></select>
      </label>
      <div class="subtabs" id="assetPools">
        <button type="button" class="subtab secondary active" data-pool="pre-processed">Pre-processed</button>
        <button type="button" class="subtab secondary" data-pool="post-processed">Post-processed</button>
        <button type="button" class="subtab secondary" data-pool="pre-used">Pre-used</button>
        <button type="button" class="subtab secondary" data-pool="post-used">Post-used</button>
      </div>
      <div id="assetUploadWrap" class="asset-upload">
        <div class="asset-upload-row">
          <label>Images
            <input type="file" id="assetUploadInput" multiple accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"/>
          </label>
          <label id="assetUploadFolderWrap" hidden>Upload folder
            <input type="text" id="assetUploadFolder" placeholder="e.g. korean" maxlength="128"/>
          </label>
          <div class="checkbox-row">
            <input type="checkbox" id="assetUploadOverwrite"/>
            <label for="assetUploadOverwrite">Replace same filename</label>
          </div>
          <button type="button" class="btn-primary" id="assetUploadBtn">Upload to R2</button>
        </div>
        <p class="asset-upload-hint" id="assetUploadHint">Uploads to <code>pre-processed/{category}/</code> for extend.</p>
        <div id="assetUploadProgress" class="asset-upload-progress" aria-hidden="true">
          <div class="asset-upload-progress-meta">
            <span id="assetUploadProgressLabel">Preparing…</span>
            <span class="asset-upload-progress-pct" id="assetUploadProgressPct">0%</span>
          </div>
          <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="assetUploadProgressBar">
            <span id="assetUploadProgressFill" style="width:0%"></span>
          </div>
        </div>
        <div id="assetUploadStatus" class="asset-upload-status" aria-live="polite"></div>
      </div>
      <div id="assetList"><p class="muted">Select a pool above to load filenames.</p></div>
    </div>

    <div id="panelObs" class="panel card">
      <h2>System</h2>
      <p class="card-desc">API cache stats and raw inventory.</p>
      <pre id="obsDetail" class="muted">Loading…</pre>
      <h3>Recent requests</h3>
      <table><thead><tr><th>Time</th><th>Endpoint</th><th>ms</th><th>Cache</th></tr></thead><tbody id="obsFetches"></tbody></table>
    </div>
  </section>

  <section id="sectionSchedule" class="main-section">
    <div class="card section-card schedule-page">
      <header class="schedule-page__header">
        <div class="schedule-page__intro">
          <h2 class="panel-title">Assembly schedule</h2>
          <p class="card-desc">Set weekly assembly and YouTube upload times per channel. Cron runs every 15 minutes.</p>
          <nav class="schedule-subtabs job-nav" role="tablist" aria-label="Schedule views">
            <button type="button" class="job-tab schedule-subtab active" data-schedule-tab="overview" id="scheduleTabOverview">Overview</button>
            <button type="button" class="job-tab schedule-subtab" data-schedule-tab="create" id="scheduleTabCreate">Create / edit</button>
          </nav>
        </div>
        <div class="schedule-page__toolbar job-toolbar job-toolbar--flush">
          <button type="button" class="btn-secondary btn-sm" id="scheduleReload">Reload</button>
        </div>
      </header>

      <div id="scheduleViewOverview" class="schedule-view">
      <div class="schedule-panel" id="scheduleOverviewPanel">
        <header class="schedule-panel__head">
          <h3 class="schedule-section-title">Scheduled jobs</h3>
          <p class="hint">All saved weekly schedules and what the cron dispatcher will fire next.</p>
        </header>
        <p class="schedule-panel__meta" id="scheduleCronMeta">Loading…</p>
        <div class="schedule-panel__stack">
          <section class="schedule-block">
            <header class="schedule-block__head">
              <h4 class="schedule-block__title">All channels</h4>
            </header>
            <div class="schedule-block__body" id="scheduleOverviewChannels">
              <p class="muted">Loading schedules…</p>
            </div>
          </section>
          <section class="schedule-block">
            <header class="schedule-block__head">
              <h4 class="schedule-block__title">Upcoming by channel</h4>
            </header>
            <div class="schedule-block__body" id="scheduleOverviewUpcoming">
              <p class="muted">—</p>
            </div>
          </section>
          <section class="schedule-block">
            <header class="schedule-block__head">
              <h4 class="schedule-block__title">Recent cron runs</h4>
            </header>
            <div class="schedule-block__body" id="scheduleOverviewRuns">
              <p class="muted">—</p>
            </div>
          </section>
        </div>
      </div>
      </div>

      <div id="scheduleViewEditor" class="schedule-view" hidden>
      <div class="schedule-editor-toolbar">
        <label>Channel
          <select id="scheduleChannel"><option value="">Select channel…</option></select>
        </label>
      </div>

      <div id="scheduleEmpty" class="schedule-empty">
        <p class="schedule-empty-title">Choose a channel</p>
        <p class="muted">Pick a YouTube channel to create a new weekly schedule or edit an existing one.</p>
      </div>

      <div id="scheduleNewBanner" class="schedule-notice" hidden>
        <p class="schedule-notice-title">No schedule yet for <code id="scheduleNewChannelLabel">channel</code></p>
        <p class="muted">There isn’t a saved schedule for this channel. Configure the form below and save to create one — defaults are already filled in.</p>
      </div>

      <div id="scheduleEditor" class="schedule-editor" hidden>
        <header class="schedule-editor__head">
          <h3 class="schedule-editor__title" id="scheduleEditorTitle">Channel schedule</h3>
          <p class="hint" id="scheduleEditorHint">Configure weekly cadence, defaults, and YouTube upload behavior.</p>
        </header>

        <section class="schedule-section schedule-section--status">
          <div class="schedule-section-head">
            <h3 class="schedule-section-title">Basics</h3>
            <p class="hint">Enable scheduling and set timezone and background pool for this channel.</p>
          </div>
          <div class="schedule-section-body form-stack">
            <div class="checkbox-row">
              <input type="checkbox" id="scheduleEnabled" checked/>
              <label for="scheduleEnabled">Schedule enabled</label>
            </div>
            <div class="form-row-2">
              <div>
                <label for="scheduleTimezone">Timezone</label>
                <input id="scheduleTimezone" value="America/New_York" placeholder="America/New_York"/>
              </div>
              <div>
                <label for="scheduleImagesFolder">Background folder</label>
                <select id="scheduleImagesFolder" required><option value="">Loading…</option></select>
              </div>
            </div>
          </div>
        </section>

        <section class="schedule-section">
          <div class="schedule-section-head">
            <h3 class="schedule-section-title">Weekly cadence</h3>
            <p class="hint" id="scheduleCadenceHint">Toggle days on (lavender). Assembly and upload run at the times you set for each enabled day.</p>
          </div>
          <div class="schedule-section-body">
            <div class="schedule-day-bar" id="scheduleDayBar" role="group" aria-label="Weekly schedule days">
              <button type="button" class="schedule-day-btn" data-day="0" aria-pressed="false" title="Sunday">Sun</button>
              <button type="button" class="schedule-day-btn" data-day="1" aria-pressed="false" title="Monday">Mon</button>
              <button type="button" class="schedule-day-btn" data-day="2" aria-pressed="false" title="Tuesday">Tue</button>
              <button type="button" class="schedule-day-btn" data-day="3" aria-pressed="false" title="Wednesday">Wed</button>
              <button type="button" class="schedule-day-btn" data-day="4" aria-pressed="false" title="Thursday">Thu</button>
              <button type="button" class="schedule-day-btn" data-day="5" aria-pressed="false" title="Friday">Fri</button>
              <button type="button" class="schedule-day-btn" data-day="6" aria-pressed="false" title="Saturday">Sat</button>
            </div>
            <div class="schedule-day-times" id="scheduleDayTimes"></div>
            <div class="schedule-days-grid" id="scheduleDaysGrid" hidden aria-hidden="true"></div>
          </div>
        </section>

        <section class="schedule-section">
          <div class="schedule-section-head">
            <h3 class="schedule-section-title">Defaults &amp; video</h3>
            <p class="hint" id="scheduleDefaultsHint">Default upload is 1 hour after assemble. Apply defaults to all enabled days, then tweak per day above.</p>
          </div>
          <div class="schedule-section-body form-stack">
            <div class="schedule-default-times">
              <div>
                <label for="scheduleDefaultAssemble">Default assemble time</label>
                <input type="hidden" id="scheduleDefaultAssemble" value="11:00"/>
              </div>
              <div class="schedule-upload-time-control" id="scheduleDefaultUploadWrap">
                <label for="scheduleDefaultUpload">Default upload time</label>
                <input type="hidden" id="scheduleDefaultUpload" value="12:00"/>
              </div>
            </div>
            <div class="schedule-inline-action">
              <button type="button" class="btn-secondary btn-sm" id="scheduleApplyDefault">Apply defaults to enabled days</button>
            </div>
            <div class="form-row-3">
              <div>
                <label for="scheduleThumb">Thumbnail text</label>
                <input id="scheduleThumb" value="__DEFAULT_THUMBNAIL__"/>
              </div>
              <div>
                <label for="scheduleDuration">Duration (min)</label>
                <input id="scheduleDuration" type="number" min="5" max="300" value="90"/>
              </div>
              <div>
                <label for="scheduleVariance">Variance (min)</label>
                <input id="scheduleVariance" type="number" min="0" max="60" value="15"/>
              </div>
            </div>
            <div class="checkbox-row">
              <input type="checkbox" id="scheduleAutoExtend" checked/>
              <label for="scheduleAutoExtend">Auto-extend when backgrounds are low</label>
            </div>
          </div>
        </section>

        <section class="schedule-section">
          <div class="schedule-section-head">
            <h3 class="schedule-section-title">YouTube upload</h3>
            <p class="hint">After assembly finishes, register the video with youtube-uploader. Choose immediate dispatch, a timed go-live, or queue-only.</p>
          </div>
          <div class="schedule-section-body form-stack">
            <div class="checkbox-row">
              <input type="checkbox" id="scheduleQueueYoutube" checked/>
              <label for="scheduleQueueYoutube">Queue for YouTube after assembly</label>
            </div>
            <div id="scheduleYoutubeOptions" class="form-stack run-youtube-options">
              <div class="form-row-3">
                <div>
                  <label for="scheduleUploadPrivacy">Privacy</label>
                  <select id="scheduleUploadPrivacy">
                    <option value="private">Private (recommended for scheduled publish)</option>
                    <option value="unlisted">Unlisted</option>
                    <option value="public">Public</option>
                  </select>
                </div>
                <div>
                  <label for="scheduleUploadCategory">Category ID</label>
                  <input id="scheduleUploadCategory" value="10" placeholder="10 = Music"/>
                </div>
                <div>
                  <label for="scheduleUploadTags">Tags (comma-separated)</label>
                  <input id="scheduleUploadTags" placeholder="lofi, chill, study"/>
                </div>
              </div>
              <fieldset class="schedule-upload-mode" id="scheduleUploadMode">
                <legend>Upload timing</legend>
                <div class="checkbox-row">
                  <input type="radio" name="scheduleUploadMode" id="scheduleModeImmediate" value="immediate"/>
                  <label for="scheduleModeImmediate">Upload immediately after assembly</label>
                </div>
                <p class="hint">Dispatches with <code>upload_now</code> as soon as the video is ready. Day upload times are unused.</p>
                <div class="checkbox-row">
                  <input type="radio" name="scheduleUploadMode" id="scheduleModeScheduled" value="scheduled" checked/>
                  <label for="scheduleModeScheduled">Auto-upload &amp; publish at the day’s upload time</label>
                </div>
                <p class="hint">Arms Cloud Scheduler for that day’s upload time (YouTube publishAt). Late assembly defaults to 5 minutes after ready.</p>
                <div class="checkbox-row">
                  <input type="radio" name="scheduleUploadMode" id="scheduleModeQueueOnly" value="queue_only"/>
                  <label for="scheduleModeQueueOnly">Queue only — don’t auto-dispatch</label>
                </div>
                <p class="hint">Registers on the uploader queue for a later manual <code>/runs</code> (or other dispatch).</p>
              </fieldset>
              <div class="checkbox-row">
                <input type="checkbox" id="scheduleUploadMadeForKids"/>
                <label for="scheduleUploadMadeForKids">Made for kids</label>
              </div>
            </div>
          </div>
        </section>

        <section class="schedule-section">
          <div class="schedule-section-head">
            <h3 class="schedule-section-title">Preview</h3>
            <p class="hint">Summary of this channel’s saved schedule before you save.</p>
          </div>
          <div class="schedule-section-body">
            <dl class="schedule-summary" id="scheduleSummary"></dl>
          </div>
        </section>

        <footer class="schedule-editor__footer">
          <div class="schedule-editor__actions">
            <button type="button" class="btn-primary" id="scheduleSave">Save schedule</button>
            <button type="button" class="btn-danger-outline btn-sm" id="scheduleDelete">Delete schedule</button>
          </div>

          <div class="schedule-panel schedule-panel--diagnostics">
            <header class="schedule-panel__head">
              <h3 class="schedule-section-title">Diagnostics</h3>
              <p class="hint">Resource readiness, upcoming slots, and cron run history for this channel.</p>
            </header>
            <div class="schedule-panel__stack">
              <details class="schedule-diagnostic">
                <summary>Resource check</summary>
                <div class="schedule-diagnostic-body">
                  <pre id="scheduleResources" class="muted">—</pre>
                </div>
              </details>
              <details class="schedule-diagnostic" open>
                <summary>Upcoming slots</summary>
                <div class="schedule-diagnostic-body" id="scheduleUpcoming"><p class="muted">—</p></div>
              </details>
              <details class="schedule-diagnostic" open>
                <summary>Run history</summary>
                <div class="schedule-diagnostic-body">
                  <p class="hint">Clear a row to allow that slot to fire again on the next cron tick.</p>
                  <div id="scheduleRunHistory"><p class="muted">—</p></div>
                </div>
              </details>
            </div>
          </div>
        </footer>
      </div>
      </div>
    </div>
  </section>

  <section id="sectionUpdates" class="main-section">
    <div class="card section-card">
      <h2 class="panel-title">Hosted site updates</h2>
      <p class="card-desc">What is running on this Cloud Run revision — compare the build SHA to <code>main</code> on GitHub to confirm you are up to date.</p>
      <dl class="updates-meta" id="updatesMeta">
        <div><dt>Version</dt><dd id="updVersion">—</dd></div>
        <div><dt>Build</dt><dd id="updBuild">—</dd></div>
        <div><dt>Revision</dt><dd id="updRevision">—</dd></div>
        <div><dt>Branch</dt><dd id="updRef">—</dd></div>
        <div><dt>Deployed</dt><dd id="updDeployed">—</dd></div>
      </dl>
      <p class="updates-hint" id="updatesHint">Loading…</p>
      <h3 class="schedule-section-title">Commits in this build</h3>
      <div class="job-table-wrap">
        <table class="updates-commits">
          <thead><tr><th>SHA</th><th>Date</th><th>Subject</th></tr></thead>
          <tbody id="updatesCommits"><tr><td colspan="3" class="muted">Loading…</td></tr></tbody>
        </table>
      </div>
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
    <button type="button" class="obs-version" id="obsVersion" title="Open Updates — API version and Cloud Run revision">v…</button>
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
function showResultBlock(preId, content) {
  const pre = document.getElementById(preId);
  if (!pre) return;
  const text = String(content ?? '');
  pre.textContent = text;
  pre.closest('.json-block')?.classList.toggle('has-content', !!text.trim());
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

async function populateBackgroundFolderSelect(selectId, selected) {
  const el = document.getElementById(selectId);
  if (!el) return;
  const keep = selected || el.value;
  try {
    const d = await api('/v1/background-folders');
    const folders = d.folders || [];
    el.innerHTML = folders.length
      ? '<option value="">Select folder…</option>'
      : '<option value="">No folders on R2</option>';
    for (const f of folders) {
      el.innerHTML += '<option value="' + esc(f) + '">' + esc(f) + '</option>';
    }
    const def = '__DEFAULT_CATEGORY__';
    if (keep && folders.includes(keep)) el.value = keep;
    else if (folders.includes(def)) el.value = def;
    else if (folders.length === 1) el.value = folders[0];
  } catch (e) {
    console.warn('background-folders', selectId, e);
    el.innerHTML = '<option value="__DEFAULT_CATEGORY__">__DEFAULT_CATEGORY__</option>';
  }
}

async function populatePreProcessedFolderSelect(selectId, selected) {
  const el = document.getElementById(selectId);
  if (!el) return;
  const keep = selected || el.value;
  try {
    const d = await api('/v1/pre-processed-folders');
    const folders = d.folders || [];
    el.innerHTML = folders.length
      ? '<option value="">Select folder…</option>'
      : '<option value="">No folders on R2</option>';
    for (const f of folders) {
      el.innerHTML += '<option value="' + esc(f) + '">' + esc(f) + '</option>';
    }
    const def = '__DEFAULT_CATEGORY__';
    if (keep && folders.includes(keep)) el.value = keep;
    else if (folders.includes(def)) el.value = def;
    else if (folders.length === 1) el.value = folders[0];
  } catch (e) {
    console.warn('pre-processed-folders', selectId, e);
    el.innerHTML = '<option value="">Failed to load folders</option>';
  }
}

async function refreshExtendPending() {
  const folder = document.getElementById('extendSourceFolder')?.value?.trim() || '';
  const pathEl = document.getElementById('extendPendingPath');
  const pendingEl = document.getElementById('extendPending');
  if (pathEl) {
    pathEl.textContent = folder ? ('pre-processed/' + folder + '/') : 'pre-processed/';
  }
  if (!folder) {
    if (pendingEl) pendingEl.textContent = '—';
    return;
  }
  try {
    const d = await api(
      '/v1/extend/pending?category=' + encodeURIComponent(cat())
      + '&source_folder=' + encodeURIComponent(folder)
    );
    if (pendingEl) pendingEl.textContent = d.pending;
    setStat('statExtendPending', d.pending);
  } catch (e) {
    console.warn('extend pending', e);
    if (pendingEl) pendingEl.textContent = '?';
  }
}

function syncExtendLimitEnabled() {
  const all = document.getElementById('extendProcessAll')?.checked;
  const lim = document.getElementById('extendLimit');
  if (lim) lim.disabled = !!all;
}

async function loadBackgroundFolders() {
  await Promise.all([
    populateBackgroundFolderSelect('runImagesFolder'),
    populateBackgroundFolderSelect('scheduleImagesFolder'),
    populateBackgroundFolderSelect('assetImagesFolder'),
    populatePreProcessedFolderSelect('extendSourceFolder'),
  ]);
  syncExtendLimitEnabled();
  await refreshExtendPending();
}

function assetPoolUsesBackgroundFolder(pool) {
  return pool === 'post-processed' || pool === 'post-used';
}

function assetPoolAllowsUpload(pool) {
  return pool === 'pre-processed' || pool === 'post-processed';
}

function selectedAssetImagesFolder() {
  const el = document.getElementById('assetImagesFolder');
  return el ? el.value.trim() : '';
}

function uploadTargetImagesFolder() {
  const manual = document.getElementById('assetUploadFolder')?.value.trim();
  if (manual) return manual;
  return selectedAssetImagesFolder();
}

function syncAssetFolderVisibility() {
  const wrap = document.getElementById('assetFolderWrap');
  if (wrap) wrap.hidden = !assetPoolUsesBackgroundFolder(ui.assetPool);
  const uploadWrap = document.getElementById('assetUploadWrap');
  if (uploadWrap) uploadWrap.hidden = !assetPoolAllowsUpload(ui.assetPool);
  const folderWrap = document.getElementById('assetUploadFolderWrap');
  if (folderWrap) folderWrap.hidden = ui.assetPool !== 'post-processed';
  const hint = document.getElementById('assetUploadHint');
  if (hint) {
    if (ui.assetPool === 'post-processed') {
      hint.innerHTML = 'Uploads to <code>post-processed/{folder}/</code>. Pick a folder above or type a new one.';
    } else if (ui.assetPool === 'pre-processed') {
      hint.innerHTML = 'Uploads to <code>pre-processed/' + esc(cat()) + '/</code> for extend.';
    }
  }
}

async function loadChannelOptions() {
  try {
    const d = await api('/v1/channels?category=' + encodeURIComponent(cat()));
    const rows = (d.channel_details && d.channel_details.length)
      ? d.channel_details
      : (d.channels || []).map(id => ({ id, name: id }));
    for (const selId of ['runChannel', 'videoChannel', 'scheduleChannel']) {
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
    el.title = 'Open Updates — music-assembly-api ' + label + (v.build && v.build !== v.revision ? ' (build ' + v.build + ')' : '');
  } catch (_) {
    document.getElementById('obsVersion').textContent = 'v?';
  }
}
async function loadUpdatesPanel() {
  const tbody = document.getElementById('updatesCommits');
  const hint = document.getElementById('updatesHint');
  tbody.innerHTML = '<tr><td colspan="3">' + loadingBlockHtml('Loading update log…') + '</td></tr>';
  try {
    const u = await api('/v1/updates');
    document.getElementById('updVersion').textContent = u.version || '—';
    document.getElementById('updBuild').textContent = u.build || u.git_sha_short || '—';
    document.getElementById('updRevision').textContent = u.revision || '—';
    document.getElementById('updRef').textContent = u.ref || '—';
    document.getElementById('updDeployed').textContent = u.generated_at || u.deployed_at || '—';
    const repo = (u.repo_url || 'https://github.com/HaoChiBao/ai-music-assembler').replace(/\/$/, '');
    const build = u.build || u.git_sha_short || '';
    const tip = (u.commits && u.commits[0] && u.commits[0].short) || u.git_sha_short || '';
    if (u.source === 'missing' || !(u.commits && u.commits.length)) {
      hint.textContent = 'No deploy manifest in this image (local or pre-CI build). After merging to main, the Deploy dashboard job bakes recent commits into /v1/updates.';
    } else if (build && tip && build === tip) {
      hint.innerHTML = 'This revision matches tip commit <code>' + esc(tip) + '</code>. On GitHub, confirm <a href="' + esc(repo) + '/commits/main" target="_blank" rel="noopener">main</a> starts with the same SHA.';
    } else {
      hint.innerHTML = 'Live build <code>' + esc(build || '?') + '</code>. Commit list is from the image manifest' + (tip ? ' (tip <code>' + esc(tip) + '</code>)' : '') + '.';
    }
    const rows = (u.commits || []).map(c => {
      const sha = esc(c.short || (c.sha || '').slice(0, 7) || '?');
      const shaLink = c.sha
        ? '<a href="' + esc(repo) + '/commit/' + esc(c.sha) + '" target="_blank" rel="noopener"><code>' + sha + '</code></a>'
        : '<code>' + sha + '</code>';
      let subject = esc(c.subject || '');
      if (c.pr) {
        subject += ' <a href="' + esc(repo) + '/pull/' + esc(String(c.pr)) + '" target="_blank" rel="noopener">#' + esc(String(c.pr)) + '</a>';
      }
      return '<tr><td>' + shaLink + '</td><td class="muted">' + esc(c.date || '—') + '</td><td>' + subject + '</td></tr>';
    });
    tbody.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="3" class="muted">No commits recorded.</td></tr>';
  } catch (e) {
    hint.textContent = String(e);
    tbody.innerHTML = '<tr><td colspan="3" class="muted">Failed to load.</td></tr>';
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
async function applyInventory(d) {
  const inv = d.inventory || {};
  document.getElementById('inventory').textContent = JSON.stringify(inv, null, 2);
  setStat(
    'statPostProcessed',
    inv.backgrounds_ready ?? inv['post-processed'] ?? inv.backgrounds_available
  );
  setStat('statMusic', inv.music_mp3s ?? inv.music ?? inv['music']);
  setStat('statVideos', inv.music_videos ?? inv['music-video']);
  const folder = document.getElementById('extendSourceFolder')?.value?.trim();
  if (folder) {
    await refreshExtendPending();
  } else if (typeof d.extend_pending === 'number') {
    setStat('statExtendPending', d.extend_pending);
    document.getElementById('extendPending').textContent = d.extend_pending;
  }
}

async function refreshStats() {
  const d = await api('/v1/dashboard/stats?category=' + encodeURIComponent(cat()));
  await applyInventory(d);
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
    const track = v.tracklist ? '<h4 class="detail-heading">Tracklist</h4><pre class="desc">' + esc(v.tracklist) + '</pre>' : '';
    detail.innerHTML =
      '<p><strong>' + esc(v.title || id) + '</strong></p>' +
      (v.description ? '<h4 class="detail-heading">Description</h4><div class="desc">' + esc(v.description) + '</div>' : '<p class="muted">No description file</p>') +
      track +
      (v.has_video
        ? '<p style="margin-top:12px"><button type="button" class="secondary play-btn">Load video preview</button></p>' +
          '<video controls preload="none" playsinline style="display:none"></video>'
        : '<p class="muted">No MP4 in this folder</p>') +
      '<p class="detail-meta">' + esc(v.r2_prefix) + '</p>';
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
  syncAssetFolderVisibility();
  el.innerHTML = loadingBlockHtml('Loading ' + ui.assetPool + '…');
  let q = '/v1/assets?category=' + encodeURIComponent(cat()) + '&pool=' + encodeURIComponent(ui.assetPool);
  if (assetPoolUsesBackgroundFolder(ui.assetPool)) {
    const folder = selectedAssetImagesFolder();
    if (!folder) {
      el.innerHTML = '<p class="muted">Select a background folder above.</p>';
      return;
    }
    q += '&images_folder=' + encodeURIComponent(folder);
  }
  const d = await api(q);
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

/** Cloud Run HTTP/1 max request body is 32 MiB — pack by estimated wire size under that. */
const ASSET_UPLOAD_MAX_FILES = 50;
const ASSET_UPLOAD_MAX_FILE_BYTES = 20 * 1024 * 1024;
const ASSET_UPLOAD_MAX_REQUEST_BYTES = 28 * 1024 * 1024;
const ASSET_UPLOAD_FORM_OVERHEAD = 4096;
const ASSET_UPLOAD_PART_OVERHEAD = 512;

function setAssetUploadProgress(pct, label) {
  const wrap = document.getElementById('assetUploadProgress');
  const fill = document.getElementById('assetUploadProgressFill');
  const bar = document.getElementById('assetUploadProgressBar');
  const pctEl = document.getElementById('assetUploadProgressPct');
  const labelEl = document.getElementById('assetUploadProgressLabel');
  if (!wrap) return;
  const clamped = Math.max(0, Math.min(100, Math.round(pct)));
  wrap.classList.add('active');
  wrap.setAttribute('aria-hidden', 'false');
  if (fill) fill.style.width = clamped + '%';
  if (bar) bar.setAttribute('aria-valuenow', String(clamped));
  if (pctEl) pctEl.textContent = clamped + '%';
  if (labelEl && label != null) labelEl.textContent = label;
}

function hideAssetUploadProgress() {
  const wrap = document.getElementById('assetUploadProgress');
  if (!wrap) return;
  wrap.classList.remove('active');
  wrap.setAttribute('aria-hidden', 'true');
}

function estimateAssetUploadWireBytes(files) {
  let total = ASSET_UPLOAD_FORM_OVERHEAD;
  for (const file of files) {
    total += file.size + ASSET_UPLOAD_PART_OVERHEAD + (file.name ? file.name.length : 0);
  }
  return total;
}

function packAssetUploadBatches(fileList) {
  const batches = [];
  const skipped = [];
  let current = [];
  for (const file of fileList) {
    if (file.size > ASSET_UPLOAD_MAX_FILE_BYTES) {
      skipped.push({
        name: file.name,
        error: 'File exceeds ' + (ASSET_UPLOAD_MAX_FILE_BYTES / (1024 * 1024)) + ' MB limit',
      });
      continue;
    }
    const next = current.concat(file);
    const wouldExceed =
      current.length > 0 &&
      (current.length >= ASSET_UPLOAD_MAX_FILES ||
        estimateAssetUploadWireBytes(next) > ASSET_UPLOAD_MAX_REQUEST_BYTES);
    if (wouldExceed) {
      batches.push(current);
      current = [];
    }
    current.push(file);
  }
  if (current.length) batches.push(current);
  return { batches, skipped };
}

function formatUploadError(status, text) {
  const raw = (text || '').trim();
  if (status === 413 || /Request Entity Too Large/i.test(raw)) {
    return 'Request too large for Cloud Run (HTTP 413). Files should be auto-batched under 28 MB — retry, or upload fewer at once.';
  }
  if (raw.startsWith('{')) {
    try {
      const j = JSON.parse(raw);
      if (typeof j.detail === 'string') return j.detail;
      if (Array.isArray(j.detail)) {
        return j.detail.map(function (d) {
          return d.msg || (typeof d === 'string' ? d : JSON.stringify(d));
        }).join('; ');
      }
      if (typeof j.message === 'string') return j.message;
    } catch (_) { /* fall through */ }
  }
  if (/<\s*html/i.test(raw)) {
    const title = raw.match(/<title>([^<]+)<\/title>/i);
    return ((title && title[1].trim()) || 'Upload failed') + ' (HTTP ' + status + ')';
  }
  return raw || ('Upload failed (HTTP ' + status + ')');
}

function postAssetUploadBatch(fd, onProgress) {
  return new Promise(function (resolve, reject) {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/v1/assets/upload');
    xhr.withCredentials = true;
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
    };
    xhr.onload = function () {
      const text = xhr.responseText || '';
      if (xhr.status === 401) {
        const err = new Error('Session expired');
        err.status = 401;
        reject(err);
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        const err = new Error(formatUploadError(xhr.status, text));
        err.status = xhr.status;
        err.body = text;
        reject(err);
        return;
      }
      try {
        resolve(JSON.parse(text));
      } catch (_) {
        reject(new Error('Invalid upload response'));
      }
    };
    xhr.onerror = function () { reject(new Error('Network error during upload')); };
    xhr.onabort = function () { reject(new Error('Upload aborted')); };
    xhr.send(fd);
  });
}

function buildAssetUploadFormData(batch, pool, imagesFolder, overwrite) {
  const fd = new FormData();
  fd.append('pool', pool);
  fd.append('category', cat());
  if (imagesFolder) fd.append('images_folder', imagesFolder);
  if (overwrite) fd.append('overwrite', 'true');
  for (const file of batch) fd.append('files', file);
  return fd;
}

function mergeUploadResults(a, b) {
  return {
    count: (a.count || 0) + (b.count || 0),
    errors: [].concat(a.errors || [], b.errors || []),
    images_folder: b.images_folder || a.images_folder || null,
    uploaded: [].concat(a.uploaded || [], b.uploaded || []),
  };
}

async function uploadAssetBatchWithRetry(batch, pool, imagesFolder, overwrite, onProgress) {
  try {
    return await postAssetUploadBatch(
      buildAssetUploadFormData(batch, pool, imagesFolder, overwrite),
      onProgress
    );
  } catch (e) {
    if (e && e.status === 413 && batch.length > 1) {
      const mid = Math.ceil(batch.length / 2);
      const first = batch.slice(0, mid);
      const second = batch.slice(mid);
      let firstResult = { count: 0, errors: [], uploaded: [], images_folder: null };
      try {
        firstResult = await uploadAssetBatchWithRetry(first, pool, imagesFolder, overwrite, function (loaded, total) {
          const ratio = total > 0 ? loaded / total : 0;
          onProgress(ratio * estimateAssetUploadWireBytes(first), estimateAssetUploadWireBytes(batch));
        });
        const secondResult = await uploadAssetBatchWithRetry(second, pool, imagesFolder, overwrite, function (loaded, total) {
          const ratio = total > 0 ? loaded / total : 0;
          const firstDone = estimateAssetUploadWireBytes(first);
          onProgress(firstDone + ratio * estimateAssetUploadWireBytes(second), estimateAssetUploadWireBytes(batch));
        });
        return mergeUploadResults(firstResult, secondResult);
      } catch (splitErr) {
        const nested = splitErr.partialResult;
        if (firstResult && firstResult.count) {
          splitErr.partialResult = nested
            ? mergeUploadResults(firstResult, nested)
            : firstResult;
        }
        throw splitErr;
      }
    }
    throw e;
  }
}

async function uploadAssetFiles() {
  const input = document.getElementById('assetUploadInput');
  const statusEl = document.getElementById('assetUploadStatus');
  const btn = document.getElementById('assetUploadBtn');
  if (!input?.files?.length) {
    if (statusEl) {
      statusEl.className = 'asset-upload-status err';
      statusEl.textContent = 'Choose one or more images first.';
    }
    return;
  }
  const uploadPool = ui.assetPool;
  if (!assetPoolAllowsUpload(uploadPool)) {
    if (statusEl) {
      statusEl.className = 'asset-upload-status err';
      statusEl.textContent = 'Upload is only available for pre-processed and post-processed pools.';
    }
    return;
  }

  let imagesFolder = null;
  if (uploadPool === 'post-processed') {
    imagesFolder = uploadTargetImagesFolder();
    if (!imagesFolder) {
      if (statusEl) {
        statusEl.className = 'asset-upload-status err';
        statusEl.textContent = 'Select or type a background folder for post-processed uploads.';
      }
      return;
    }
  }
  const overwrite = !!document.getElementById('assetUploadOverwrite')?.checked;
  const { batches, skipped } = packAssetUploadBatches(Array.from(input.files));
  if (!batches.length) {
    if (statusEl) {
      statusEl.className = 'asset-upload-status err';
      statusEl.textContent = skipped.length
        ? 'No files under the 20 MB limit. ' + skipped.length + ' skipped.'
        : 'Choose one or more images first.';
    }
    return;
  }

  const totalFiles = batches.reduce(function (n, b) { return n + b.length; }, 0);
  const totalBytes = batches.reduce(function (n, b) {
    return n + b.reduce(function (s, f) { return s + f.size; }, 0);
  }, 0);
  let uploadedCount = 0;
  let allErrors = skipped.slice();
  let completedBytes = 0;
  let filesCompleted = 0;
  let lastFolder = imagesFolder;

  setBtnLoading(btn, true, 'Uploading…');
  if (statusEl) {
    statusEl.className = 'asset-upload-status';
    statusEl.textContent = '';
  }
  setAssetUploadProgress(0, 'Uploading 0 / ' + totalFiles + '…');

  try {
    for (let i = 0; i < batches.length; i++) {
      const batch = batches[i];
      const batchBytes = batch.reduce(function (s, f) { return s + f.size; }, 0);
      setAssetUploadProgress(
        totalBytes > 0 ? (completedBytes / totalBytes) * 100 : 0,
        'Uploading ' + filesCompleted + ' / ' + totalFiles +
          ' · batch ' + (i + 1) + '/' + batches.length
      );

      const t0 = performance.now();
      const d = await uploadAssetBatchWithRetry(
        batch,
        uploadPool,
        imagesFolder,
        overwrite,
        function (loaded, total) {
          const ratio = total > 0 ? loaded / total : 0;
          const overallBytes = completedBytes + ratio * batchBytes;
          const pct = totalBytes > 0 ? (overallBytes / totalBytes) * 100 : 0;
          const approxDone = filesCompleted + Math.min(batch.length, Math.floor(ratio * batch.length));
          setAssetUploadProgress(
            pct,
            'Uploading ' + approxDone + ' / ' + totalFiles +
              ' · batch ' + (i + 1) + '/' + batches.length
          );
        }
      );
      const ms = Math.round(performance.now() - t0);
      obs.fetches.unshift({ at: new Date().toLocaleTimeString(), path: '/v1/assets/upload', ms, cache: '—' });
      if (obs.fetches.length > 25) obs.fetches.pop();
      renderObsBar();

      uploadedCount += d.count || 0;
      if (d.errors && d.errors.length) allErrors = allErrors.concat(d.errors);
      if (d.images_folder) lastFolder = d.images_folder;
      completedBytes += batchBytes;
      filesCompleted += batch.length;
      setAssetUploadProgress(
        totalBytes > 0 ? (completedBytes / totalBytes) * 100 : 100,
        'Uploaded ' + filesCompleted + ' / ' + totalFiles
      );
    }

    const errCount = allErrors.length;
    const msg = 'Uploaded ' + uploadedCount + ' image(s) to R2 in ' + batches.length + ' batch(es).'
      + (errCount ? ' ' + errCount + ' failed/skipped.' : '');
    if (statusEl) {
      statusEl.className = 'asset-upload-status' + (errCount && !uploadedCount ? ' err' : ' ok');
      statusEl.textContent = msg;
    }
    setAssetUploadProgress(100, 'Done — ' + uploadedCount + ' uploaded');
    input.value = '';
    ui.tabsLoaded.assets = false;
    if (ui.assetPool === 'post-processed' && lastFolder) {
      await populateBackgroundFolderSelect('assetImagesFolder', lastFolder);
      const folderInput = document.getElementById('assetUploadFolder');
      if (folderInput) folderInput.value = lastFolder;
    }
    await refreshStats();
    await loadAssetList();
  } catch (e) {
    if (String(e && e.message || e).includes('Session expired')) {
      window.location.reload();
      return;
    }
    if (e && e.partialResult) {
      uploadedCount += e.partialResult.count || 0;
      if (e.partialResult.errors && e.partialResult.errors.length) {
        allErrors = allErrors.concat(e.partialResult.errors);
      }
      if (e.partialResult.images_folder) lastFolder = e.partialResult.images_folder;
    }
    const pct = totalBytes > 0 ? (completedBytes / totalBytes) * 100 : 0;
    setAssetUploadProgress(pct, 'Failed after ' + uploadedCount + ' uploaded');
    if (statusEl) {
      statusEl.className = 'asset-upload-status err';
      const partial = uploadedCount
        ? 'Uploaded ' + uploadedCount + ' before failure. '
        : '';
      statusEl.textContent = partial + String(e && e.message || e);
    }
    if (uploadedCount) {
      ui.tabsLoaded.assets = false;
      try {
        await refreshStats();
        await loadAssetList();
      } catch (_) { /* keep error message */ }
    }
  } finally {
    setBtnLoading(btn, false);
  }
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
  img.src = '/v1/media/asset?category=' + encodeURIComponent(cat()) + '&pool=' + encodeURIComponent(ui.assetPool) + '&name=' + encodeURIComponent(name)
    + (assetPoolUsesBackgroundFolder(ui.assetPool) && selectedAssetImagesFolder()
      ? '&images_folder=' + encodeURIComponent(selectedAssetImagesFolder()) : '');
}
document.getElementById('modalClose').onclick = () => {
  document.getElementById('modal').classList.remove('open');
  document.getElementById('modalBody').innerHTML = '';
};
document.getElementById('modal').onclick = (e) => { if (e.target.id === 'modal') document.getElementById('modalClose').click(); };

const SCHEDULE_DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const SCHEDULE_DAY_ABBR = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DEFAULT_ASSEMBLE_TIME = '11:00';

function timeInputValue(hhmm) {
  if (!hhmm) return '';
  const parts = String(hhmm).split(':');
  if (parts.length < 2) return '';
  return parts[0].padStart(2, '0') + ':' + parts[1].padStart(2, '0');
}
function timeFromInput(value) {
  if (!value) return null;
  const [h, m] = value.split(':');
  return String(parseInt(h, 10)).padStart(2, '0') + ':' + String(parseInt(m, 10)).padStart(2, '0');
}
function uploadTimeAfterAssemble(assembleHhmm, offsetMin = 60) {
  if (!assembleHhmm) return null;
  const parts = String(assembleHhmm).split(':');
  if (parts.length < 2) return null;
  let total = parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10) + offsetMin;
  total = ((total % (24 * 60)) + 24 * 60) % (24 * 60);
  return String(Math.floor(total / 60)).padStart(2, '0') + ':' + String(total % 60).padStart(2, '0');
}
function hhmmToParts(hhmm) {
  const normalized = timeInputValue(hhmm) || DEFAULT_ASSEMBLE_TIME;
  let h24 = parseInt(normalized.split(':')[0], 10);
  const minute = parseInt(normalized.split(':')[1], 10);
  const ampm = h24 >= 12 ? 'PM' : 'AM';
  let hour12 = h24 % 12;
  if (hour12 === 0) hour12 = 12;
  return { hour12, minute, ampm };
}
function partsToHhmm(hour12, minute, ampm) {
  let h = parseInt(hour12, 10);
  const m = parseInt(minute, 10);
  if (ampm === 'AM') {
    if (h === 12) h = 0;
  } else if (h !== 12) {
    h += 12;
  }
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
}
function formatTimeDisplay(hhmm) {
  const { hour12, minute, ampm } = hhmmToParts(hhmm);
  return hour12 + ':' + String(minute).padStart(2, '0') + ' ' + ampm;
}
function buildTimePickerMarkup(hiddenInput) {
  const val = timeInputValue(hiddenInput.value) || DEFAULT_ASSEMBLE_TIME;
  const { hour12, minute, ampm } = hhmmToParts(val);
  const hourOpts = Array.from({ length: 12 }, (_, i) => {
    const h = i + 1;
    return '<option value="' + h + '"' + (h === hour12 ? ' selected' : '') + '>' + h + '</option>';
  }).join('');
  const minOpts = Array.from({ length: 60 }, (_, i) => {
    const v = String(i).padStart(2, '0');
    return '<option value="' + v + '"' + (i === minute ? ' selected' : '') + '>' + v + '</option>';
  }).join('');
  return '<div class="time-picker" data-target="' + esc(hiddenInput.id || '') + '">'
    + '<div class="time-picker-row">'
    + '<select class="time-picker-hour" aria-label="Hour">' + hourOpts + '</select>'
    + '<span class="time-picker-colon" aria-hidden="true">:</span>'
    + '<select class="time-picker-minute" aria-label="Minute">' + minOpts + '</select>'
    + '<div class="time-picker-ampm" role="group" aria-label="AM or PM">'
    + '<button type="button" class="time-picker-ampm-btn' + (ampm === 'AM' ? ' is-active' : '') + '" data-ampm="AM">AM</button>'
    + '<button type="button" class="time-picker-ampm-btn' + (ampm === 'PM' ? ' is-active' : '') + '" data-ampm="PM">PM</button>'
    + '</div></div>'
    + '<span class="time-picker-display">' + esc(formatTimeDisplay(val)) + '</span>'
    + '</div>';
}
function syncTimePickerFromHidden(picker, hidden) {
  if (!picker || !hidden) return;
  const { hour12, minute, ampm } = hhmmToParts(hidden.value);
  const hourEl = picker.querySelector('.time-picker-hour');
  const minEl = picker.querySelector('.time-picker-minute');
  if (hourEl) hourEl.value = String(hour12);
  if (minEl) minEl.value = String(minute).padStart(2, '0');
  picker.querySelectorAll('.time-picker-ampm-btn').forEach(btn => {
    btn.classList.toggle('is-active', btn.dataset.ampm === ampm);
  });
  const disp = picker.querySelector('.time-picker-display');
  if (disp) disp.textContent = formatTimeDisplay(hidden.value);
}
function bindTimePickerEvents(picker, hidden, onChange) {
  const sync = () => {
    const hour12 = parseInt(picker.querySelector('.time-picker-hour').value, 10);
    const minute = picker.querySelector('.time-picker-minute').value;
    const ampmBtn = picker.querySelector('.time-picker-ampm-btn.is-active');
    const ampm = ampmBtn ? ampmBtn.dataset.ampm : 'AM';
    hidden.value = partsToHhmm(hour12, minute, ampm);
    const disp = picker.querySelector('.time-picker-display');
    if (disp) disp.textContent = formatTimeDisplay(hidden.value);
    if (onChange) onChange(hidden.value);
    hidden.dispatchEvent(new Event('change', { bubbles: true }));
  };
  picker.querySelector('.time-picker-hour').addEventListener('change', sync);
  picker.querySelector('.time-picker-minute').addEventListener('change', sync);
  picker.querySelectorAll('.time-picker-ampm-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      picker.querySelectorAll('.time-picker-ampm-btn').forEach(b => b.classList.remove('is-active'));
      btn.classList.add('is-active');
      sync();
    });
  });
}
function mountTimePickerForHidden(hiddenInput, onChange) {
  if (!hiddenInput || hiddenInput.dataset.timePickerMounted) return null;
  hiddenInput.type = 'hidden';
  hiddenInput.dataset.timePickerMounted = '1';
  const wrap = document.createElement('div');
  wrap.innerHTML = buildTimePickerMarkup(hiddenInput);
  const picker = wrap.firstElementChild;
  hiddenInput.parentNode.insertBefore(picker, hiddenInput);
  bindTimePickerEvents(picker, hiddenInput, onChange);
  return picker;
}
function setHiddenTimeValue(hidden, hhmm) {
  if (!hidden) return;
  hidden.value = timeInputValue(hhmm) || DEFAULT_ASSEMBLE_TIME;
  const picker = hidden.previousElementSibling;
  if (picker && picker.classList.contains('time-picker')) {
    syncTimePickerFromHidden(picker, hidden);
  }
}
function readScheduleTime(hiddenId) {
  const hidden = document.getElementById(hiddenId);
  return timeFromInput(hidden?.value) || DEFAULT_ASSEMBLE_TIME;
}
function defaultScheduleDays() {
  return SCHEDULE_DAY_NAMES.map(() => ({
    enabled: false,
    assemble_at: DEFAULT_ASSEMBLE_TIME,
    upload_at: uploadTimeAfterAssemble(DEFAULT_ASSEMBLE_TIME),
  }));
}
let scheduleDayBarBound = false;
function bindScheduleDayBar() {
  if (scheduleDayBarBound) return;
  const bar = document.getElementById('scheduleDayBar');
  if (!bar) return;
  scheduleDayBarBound = true;
  bar.querySelectorAll('.schedule-day-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const i = parseInt(btn.dataset.day, 10);
      const next = !btn.classList.contains('is-active');
      btn.classList.toggle('is-active', next);
      btn.setAttribute('aria-pressed', next ? 'true' : 'false');
      const hidden = document.querySelector('.schedule-day-enabled[data-day="' + i + '"]');
      if (hidden) hidden.checked = next;
      if (next) {
        const assembleHidden = document.querySelector('.schedule-day-assemble[data-day="' + i + '"]');
        const uploadHidden = document.querySelector('.schedule-day-upload[data-day="' + i + '"]');
        const defaultAssemble = timeFromInput(document.getElementById('scheduleDefaultAssemble')?.value) || DEFAULT_ASSEMBLE_TIME;
        if (assembleHidden && !assembleHidden.value) {
          assembleHidden.value = timeInputValue(defaultAssemble);
        }
        if (uploadHidden && !uploadHidden.value) {
          uploadHidden.value = timeInputValue(
            uploadTimeAfterAssemble(assembleHidden?.value || defaultAssemble)
          );
        }
      }
      renderScheduleDayTimes(collectScheduleDays());
      updateScheduleSummary();
    });
  });
}
function renderScheduleDays(days) {
  const rows = (days && days.length === 7) ? days : defaultScheduleDays();
  const bar = document.getElementById('scheduleDayBar');
  const times = document.getElementById('scheduleDayTimes');
  const grid = document.getElementById('scheduleDaysGrid');
  if (!bar || !times) return;

  bindScheduleDayBar();
  bar.querySelectorAll('.schedule-day-btn').forEach(btn => {
    const i = parseInt(btn.dataset.day, 10);
    const day = rows[i] || defaultScheduleDays()[i];
    const on = !!day.enabled;
    btn.classList.toggle('is-active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });

  if (grid) {
    grid.innerHTML = rows.map((day, i) =>
      '<input type="checkbox" class="schedule-day-enabled" data-day="' + i + '"' + (day.enabled ? ' checked' : '') + ' hidden>'
      + '<input type="hidden" class="schedule-day-assemble" data-day="' + i + '" value="' + esc(timeInputValue(day.assemble_at || DEFAULT_ASSEMBLE_TIME)) + '">'
      + '<input type="hidden" class="schedule-day-upload" data-day="' + i + '" value="' + esc(timeInputValue(day.upload_at || uploadTimeAfterAssemble(day.assemble_at || DEFAULT_ASSEMBLE_TIME) || '')) + '">'
    ).join('');
  }

  renderScheduleDayTimes(rows);
}

function renderScheduleDayTimes(days) {
  const times = document.getElementById('scheduleDayTimes');
  if (!times) return;
  const enabled = days.map((d, i) => ({ day: d, i })).filter(x => x.day.enabled);
  if (!enabled.length) {
    times.classList.add('is-empty');
    times.innerHTML = '<p class="muted">Enable at least one day to set assemble and upload times.</p>';
    return;
  }
  times.classList.remove('is-empty');
  times.innerHTML = enabled.map(({ day, i }) => {
    const assembleVal = timeInputValue(day.assemble_at || DEFAULT_ASSEMBLE_TIME);
    const uploadVal = timeInputValue(day.upload_at || uploadTimeAfterAssemble(assembleVal) || '');
    return '<div class="schedule-day-time-card" data-day="' + i + '">'
      + '<div class="schedule-day-time-card-head">' + esc(SCHEDULE_DAY_NAMES[i]) + '</div>'
      + '<div class="schedule-day-time-grid">'
      + '<label class="schedule-day-field"><span class="schedule-day-field-label">Assemble</span>'
      + '<input type="hidden" class="schedule-day-assemble-visible" data-day="' + i + '" value="' + esc(assembleVal) + '"></label>'
      + '<label class="schedule-day-field schedule-day-field--upload schedule-upload-time-control"><span class="schedule-day-field-label">Upload</span>'
      + '<input type="hidden" class="schedule-day-upload-visible" data-day="' + i + '" value="' + esc(uploadVal) + '"></label>'
      + '</div></div>';
  }).join('');

  times.querySelectorAll('input.schedule-day-assemble-visible').forEach(hidden => {
    mountTimePickerForHidden(hidden, () => {
      const i = hidden.dataset.day;
      const assembleEl = document.querySelector('.schedule-day-assemble[data-day="' + i + '"]');
      if (assembleEl) assembleEl.value = hidden.value;
      updateScheduleSummary();
    });
  });
  times.querySelectorAll('input.schedule-day-upload-visible').forEach(hidden => {
    mountTimePickerForHidden(hidden, () => {
      const i = hidden.dataset.day;
      const uploadEl = document.querySelector('.schedule-day-upload[data-day="' + i + '"]');
      if (uploadEl) uploadEl.value = hidden.value;
      updateScheduleSummary();
    });
  });
  syncScheduleUploadTimingUi();
}
function collectScheduleDays() {
  return SCHEDULE_DAY_NAMES.map((_, i) => {
    const btn = document.querySelector('.schedule-day-btn[data-day="' + i + '"]');
    const enabled = btn ? btn.classList.contains('is-active') : false;
    const assembleVis = document.querySelector('.schedule-day-assemble-visible[data-day="' + i + '"]');
    const uploadVis = document.querySelector('.schedule-day-upload-visible[data-day="' + i + '"]');
    const assembleHidden = document.querySelector('.schedule-day-assemble[data-day="' + i + '"]');
    const uploadHidden = document.querySelector('.schedule-day-upload[data-day="' + i + '"]');
    const assemble_at = timeFromInput(assembleVis?.value || assembleHidden?.value) || DEFAULT_ASSEMBLE_TIME;
    const uploadRaw = uploadVis?.value || uploadHidden?.value;
    const upload_at = uploadRaw ? timeFromInput(uploadRaw) : uploadTimeAfterAssemble(assemble_at);
    return { enabled, assemble_at, upload_at };
  });
}
function scheduleUploadMode() {
  const selected = document.querySelector('input[name="scheduleUploadMode"]:checked');
  return selected?.value || 'scheduled';
}
function setScheduleUploadMode(mode) {
  const value = mode === 'immediate' || mode === 'queue_only' ? mode : 'scheduled';
  const el = document.querySelector('input[name="scheduleUploadMode"][value="' + value + '"]');
  if (el) el.checked = true;
}
function readScheduleUploadSettings() {
  const mode = scheduleUploadMode();
  return {
    queue_youtube: document.getElementById('scheduleQueueYoutube')?.checked !== false,
    upload_privacy: document.getElementById('scheduleUploadPrivacy')?.value || 'private',
    upload_schedule_publish: mode === 'scheduled',
    upload_now: mode === 'immediate',
    upload_tags: document.getElementById('scheduleUploadTags')?.value.trim() || '',
    upload_category_id: document.getElementById('scheduleUploadCategory')?.value.trim() || '10',
    upload_made_for_kids: document.getElementById('scheduleUploadMadeForKids')?.checked || false,
  };
}
function readScheduleVideoSettings() {
  const thumb = document.getElementById('scheduleThumb')?.value.trim() || '';
  let duration = parseInt(document.getElementById('scheduleDuration')?.value, 10);
  let variance = parseInt(document.getElementById('scheduleVariance')?.value, 10);
  if (!Number.isFinite(duration)) duration = 90;
  if (!Number.isFinite(variance)) variance = 15;
  return {
    thumbnail_text: thumb || null,
    duration_min: Math.min(300, Math.max(5, duration)),
    variance_min: Math.min(60, Math.max(0, variance)),
  };
}
function updateScheduleSummary() {
  const el = document.getElementById('scheduleSummary');
  if (!el) return;
  const channel = document.getElementById('scheduleChannel')?.value.trim() || '—';
  const folder = document.getElementById('scheduleImagesFolder')?.value.trim() || '—';
  const tz = document.getElementById('scheduleTimezone')?.value.trim() || 'America/New_York';
  const video = readScheduleVideoSettings();
  const upload = readScheduleUploadSettings();
  const useUploadTimes = upload.queue_youtube && upload.upload_schedule_publish;
  const enabledDays = collectScheduleDays()
    .map((d, i) => (d.enabled
      ? SCHEDULE_DAY_ABBR[i] + ' ' + formatTimeDisplay(d.assemble_at)
        + (useUploadTimes ? ' → ' + formatTimeDisplay(d.upload_at) : '')
      : null))
    .filter(Boolean);
  let youtubeLabel = 'Off';
  if (upload.queue_youtube) {
    if (upload.upload_now) youtubeLabel = esc(upload.upload_privacy) + ' · upload immediately after assemble';
    else if (upload.upload_schedule_publish) youtubeLabel = esc(upload.upload_privacy) + ' · auto-upload at day upload time';
    else youtubeLabel = esc(upload.upload_privacy) + ' · queue only (manual upload)';
  }
  el.innerHTML =
    '<dt>Channel</dt><dd><code>' + esc(channel) + '</code></dd>'
    + '<dt>Background pool</dt><dd><code>post-processed/' + esc(folder) + '/</code></dd>'
    + '<dt>Music</dt><dd><code>music/' + esc(cat()) + '/</code></dd>'
    + '<dt>Video length</dt><dd>' + esc(String(video.duration_min)) + ' min ± ' + esc(String(video.variance_min)) + ' min</dd>'
    + '<dt>Thumbnail</dt><dd>' + esc(video.thumbnail_text || '(none)') + '</dd>'
    + '<dt>YouTube</dt><dd>' + youtubeLabel + '</dd>'
    + '<dt>Timezone</dt><dd>' + esc(tz) + '</dd>'
    + '<dt>Active days</dt><dd>' + (enabledDays.length ? esc(enabledDays.join(' · ')) : 'None — toggle days above') + '</dd>';
}
function syncScheduleUploadTimingUi() {
  const queueOn = document.getElementById('scheduleQueueYoutube')?.checked !== false;
  const mode = scheduleUploadMode();
  const disableUploadTimes = !queueOn || mode !== 'scheduled';
  const editor = document.getElementById('scheduleEditor');
  if (editor) editor.classList.toggle('is-upload-times-disabled', disableUploadTimes);
  const cadenceHint = document.getElementById('scheduleCadenceHint');
  const defaultsHint = document.getElementById('scheduleDefaultsHint');
  if (cadenceHint) {
    if (!queueOn) {
      cadenceHint.textContent = 'Toggle days on (lavender). Assembly runs at the times you set for each enabled day.';
    } else if (mode === 'immediate') {
      cadenceHint.textContent = 'Toggle days on (lavender). Assembly runs at each day’s time; YouTube upload starts as soon as encode finishes.';
    } else if (mode === 'queue_only') {
      cadenceHint.textContent = 'Toggle days on (lavender). Assembly runs at each day’s time; YouTube stays queued until you dispatch manually.';
    } else {
      cadenceHint.textContent = 'Toggle days on (lavender). Assembly and upload run at the times you set for each enabled day.';
    }
  }
  if (defaultsHint) {
    defaultsHint.textContent = disableUploadTimes
      ? 'Set a default assemble time, apply it to enabled days, then tweak per day above. Upload times are unused for this YouTube mode.'
      : 'Default upload is 1 hour after assemble. Apply defaults to all enabled days, then tweak per day above.';
  }
  document.querySelectorAll('.schedule-day-upload-visible, #scheduleDefaultUpload').forEach((el) => {
    el.disabled = disableUploadTimes;
  });
  document.querySelectorAll('.schedule-upload-time-control .time-picker button, .schedule-upload-time-control .time-picker select, .schedule-upload-time-control .time-picker input').forEach((el) => {
    el.disabled = disableUploadTimes;
  });
}
function syncScheduleYoutubeOptions() {
  const queueOn = document.getElementById('scheduleQueueYoutube')?.checked !== false;
  const opts = document.getElementById('scheduleYoutubeOptions');
  if (opts) opts.hidden = !queueOn;
  syncScheduleUploadTimingUi();
  updateScheduleSummary();
}
function syncScheduleEnabledState() {
  const editor = document.getElementById('scheduleEditor');
  const on = document.getElementById('scheduleEnabled')?.checked !== false;
  if (editor) editor.classList.toggle('is-schedule-disabled', !on);
}
function fillScheduleForm(data) {
  document.getElementById('scheduleEnabled').checked = data.enabled !== false;
  document.getElementById('scheduleTimezone').value = data.timezone || 'America/New_York';
  const folder = data.images_folder || '';
  populateBackgroundFolderSelect('scheduleImagesFolder', folder).then(() => {
    if (folder) document.getElementById('scheduleImagesFolder').value = folder;
    updateScheduleSummary();
  });
  setHiddenTimeValue(document.getElementById('scheduleDefaultAssemble'), data.default_assemble_at || DEFAULT_ASSEMBLE_TIME);
  setHiddenTimeValue(
    document.getElementById('scheduleDefaultUpload'),
    data.default_upload_at || uploadTimeAfterAssemble(data.default_assemble_at || DEFAULT_ASSEMBLE_TIME) || ''
  );
  document.getElementById('scheduleThumb').value = data.thumbnail_text || '__DEFAULT_THUMBNAIL__';
  document.getElementById('scheduleDuration').value = data.duration_min ?? 90;
  document.getElementById('scheduleVariance').value = data.variance_min ?? 15;
  document.getElementById('scheduleQueueYoutube').checked = data.queue_youtube !== false;
  document.getElementById('scheduleUploadPrivacy').value = data.upload_privacy || 'private';
  if (data.upload_now) setScheduleUploadMode('immediate');
  else if (data.upload_schedule_publish === false) setScheduleUploadMode('queue_only');
  else setScheduleUploadMode('scheduled');
  document.getElementById('scheduleUploadTags').value = data.upload_tags || '';
  document.getElementById('scheduleUploadCategory').value = data.upload_category_id || '10';
  document.getElementById('scheduleUploadMadeForKids').checked = !!data.upload_made_for_kids;
  document.getElementById('scheduleAutoExtend').checked = data.auto_extend !== false;
  syncScheduleEnabledState();
  syncScheduleYoutubeOptions();
  renderScheduleDays(data.days || defaultScheduleDays());
  updateScheduleSummary();
}
async function loadScheduleRunHistory(channel) {
  const el = document.getElementById('scheduleRunHistory');
  if (!el || !channel) {
    if (el) el.innerHTML = '<p class="muted">—</p>';
    return;
  }
  try {
    const data = await api('/v1/schedules/' + encodeURIComponent(channel) + '/runs?limit=30');
    renderScheduleRunHistoryRows(data.runs || [], el, channel);
  } catch (e) {
    el.innerHTML = '<p class="muted">' + esc(String(e)) + '</p>';
  }
}
function renderScheduleStatus(status) {
  const res = status.resources;
  const sched = status.schedule || {};
  const resEl = document.getElementById('scheduleResources');
  if (resEl) {
    if (!res) {
      resEl.textContent = 'Loading resource check…';
    } else {
      const blockers = (res.blockers || []).join(', ') || 'none';
      resEl.textContent = JSON.stringify({
        images_folder: res.images_folder,
        ready: res.ready,
        backgrounds_available: res.backgrounds_available,
        min_backgrounds: res.min_backgrounds,
        extend_pending: res.extend_pending,
        music_tracks: res.music_tracks,
        blockers: blockers,
        duration_min: sched.duration_min,
        variance_min: sched.variance_min,
        thumbnail_text: sched.thumbnail_text,
      }, null, 2);
    }
  }
  const upcoming = status.upcoming || [];
  const el = document.getElementById('scheduleUpcoming');
  if (!el) return;
  if (!upcoming.length) {
    el.innerHTML = '<p class="muted">No upcoming enabled slots in the next two weeks.</p>';
    return;
  }
  el.innerHTML = renderUpcomingSlotsTable(upcoming);
}
async function loadScheduleEditorDiagnostics(channel) {
  if (!channel) return;
  const resEl = document.getElementById('scheduleResources');
  const upEl = document.getElementById('scheduleUpcoming');
  if (resEl) resEl.textContent = 'Loading…';
  if (upEl) upEl.innerHTML = '<p class="muted">Loading…</p>';
  const statusUrl = '/v1/schedules/' + encodeURIComponent(channel) + '/status?include_resources=1';
  const runsUrl = '/v1/schedules/' + encodeURIComponent(channel) + '/runs?limit=30';
  const [statusResult, runsResult] = await Promise.allSettled([
    api(statusUrl),
    api(runsUrl),
  ]);
  if (statusResult.status === 'fulfilled') {
    renderScheduleStatus(statusResult.value);
  } else {
    if (resEl) resEl.textContent = String(statusResult.reason);
    if (upEl) upEl.innerHTML = '<p class="muted">—</p>';
  }
  if (runsResult.status === 'fulfilled') {
    renderScheduleRunHistoryRows(runsResult.value.runs || [], document.getElementById('scheduleRunHistory'), channel);
  } else {
    const histEl = document.getElementById('scheduleRunHistory');
    if (histEl) histEl.innerHTML = '<p class="muted">' + esc(String(runsResult.reason)) + '</p>';
  }
}
function renderScheduleRunHistoryRows(runs, el, channel) {
  if (!el) return;
  if (!runs.length) {
    el.innerHTML = '<p class="muted">No scheduled runs yet (cron must call <code>/v1/cron/run-schedules</code>).</p>';
    return;
  }
  el.innerHTML = wrapScheduleTable('<table class="schedule-table"><thead><tr><th>Slot</th><th>Status</th><th>Execution</th><th>When</th><th></th></tr></thead><tbody>'
    + runs.map(r => {
      const slot = esc(r.slot_key || '—');
      const status = esc(r.status || '—');
      const execId = r.execution_id ? '<code>' + esc(r.execution_id) + '</code>' : '—';
      const when = esc(r.updated_at || r.created_at || '—');
      const clearBtn = scheduleRunClearButton(r.slot_key);
      return '<tr><td><code>' + slot + '</code></td><td>' + status + '</td><td>' + execId + '</td><td>' + when + '</td><td>' + clearBtn + '</td></tr>';
    }).join('')
    + '</tbody></table>');
  bindScheduleRunClearButtons(el, async () => {
    await loadScheduleEditorDiagnostics(channel);
    await loadScheduleOverview();
  });
}
function scheduleStatusBadge(enabled) {
  return enabled
    ? '<span class="schedule-badge schedule-badge--on">On</span>'
    : '<span class="schedule-badge schedule-badge--off">Off</span>';
}
function resourcesBadge(ready, blockers) {
  if (ready) return '<span class="schedule-badge schedule-badge--ready">Ready</span>';
  const hint = (blockers || []).slice(0, 2).join(', ') || 'Blocked';
  return '<span class="schedule-badge schedule-badge--blocked" title="' + esc(hint) + '">Blocked</span>';
}
function wrapScheduleTable(html) {
  if (!html) return '';
  return '<div class="schedule-table-wrap">' + html + '</div>';
}
function renderUpcomingSlotsTable(slots, { includeSlotKey = false } = {}) {
  if (!slots?.length) return '';
  const slotKeyCol = includeSlotKey ? '<th>Slot key</th>' : '';
  const rows = slots.map(s => {
    const slotKeyCell = includeSlotKey
      ? '<td><code>' + esc(s.slot_key) + '</code></td>'
      : '';
    return '<tr><td>' + esc(s.day_name) + '</td><td>'
      + esc(formatTimeDisplay(s.assemble_at)) + '</td><td>'
      + esc(s.upload_at ? formatTimeDisplay(s.upload_at) : '—') + '</td><td>'
      + esc(s.at_local) + '</td>' + slotKeyCell + '</tr>';
  }).join('');
  return wrapScheduleTable('<table class="schedule-table"><thead><tr>'
    + '<th>Day</th><th>Assemble</th><th>Upload</th><th>Local time</th>' + slotKeyCol
    + '</tr></thead><tbody>' + rows + '</tbody></table>');
}
function scheduleRunClearButton(slotKey) {
  return slotKey
    ? '<button type="button" class="btn-secondary btn-sm schedule-run-clear" data-slot="' + esc(slotKey) + '">Clear</button>'
    : '';
}
function renderUpcomingByChannel(channels) {
  if (!channels?.length) {
    return '<p class="muted">No schedules saved yet.</p>';
  }
  return '<div class="schedule-upcoming-groups">'
    + channels.map(ch => {
      const slots = ch.upcoming || [];
      const groupClass = (ch.enabled ? '' : 'is-disabled ') + 'schedule-upcoming-group';
      const headClass = 'schedule-upcoming-group-head is-clickable';
      const activeDays = ch.active_days?.length
        ? esc(ch.active_days.join(' · '))
        : 'No active days';
      const body = slots.length
        ? renderUpcomingSlotsTable(slots, { includeSlotKey: true })
        : '<p class="schedule-upcoming-group-empty muted">No upcoming enabled slots in the next two weeks.</p>';
      return '<section class="' + groupClass.trim() + '">'
        + '<div class="' + headClass + '" data-channel="' + esc(ch.channel) + '">'
        + '<code>' + esc(ch.channel) + '</code>'
        + scheduleStatusBadge(ch.enabled)
        + '<span class="schedule-upcoming-group-meta">' + esc(ch.timezone) + ' · ' + activeDays + '</span>'
        + '</div>'
        + body
        + '</section>';
    }).join('')
    + '</div>';
}
function bindScheduleChannelJump(container, selector) {
  if (!container) return;
  container.querySelectorAll(selector || '[data-channel]').forEach(el => {
    el.onclick = () => {
      const ch = el.dataset.channel;
      if (!ch) return;
      openScheduleEditorForChannel(ch);
    };
  });
}
function bindScheduleRunClearButtons(container, onCleared) {
  if (!container) return;
  container.querySelectorAll('.schedule-run-clear').forEach(btn => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm('Clear this run record? The slot can fire again on the next cron tick.')) return;
      try {
        await api('/v1/schedules/runs/' + encodeURIComponent(btn.dataset.slot), { method: 'DELETE' });
        if (onCleared) await onCleared();
      } catch (e) { alert(String(e)); }
    };
  });
}
async function loadScheduleOverview(refresh) {
  const meta = document.getElementById('scheduleCronMeta');
  const chEl = document.getElementById('scheduleOverviewChannels');
  const upEl = document.getElementById('scheduleOverviewUpcoming');
  const runsEl = document.getElementById('scheduleOverviewRuns');
  if (!meta || !chEl || !upEl || !runsEl) return;
  if (refresh) {
    meta.textContent = 'Refreshing…';
  }
  try {
    const overviewPath = refresh ? '/v1/schedules/overview?refresh=1' : '/v1/schedules/overview';
    const data = await api(overviewPath);
    const cron = data.cron || {};
    const count = data.channel_count || 0;
    meta.innerHTML = 'Cloud Scheduler polls <code>' + esc(cron.endpoint || '/v1/cron/run-schedules')
      + '</code> every ' + esc(String(cron.poll_minutes || 15)) + ' min · '
      + esc(String(count)) + ' channel' + (count === 1 ? '' : 's') + ' configured · '
      + esc(String(cron.match_window_minutes || 15)) + ' min match window';

    const channels = data.channels || [];
    if (!channels.length) {
      chEl.innerHTML = '<div class="schedule-empty" style="margin:16px;border:none">'
        + '<p class="schedule-empty-title">No schedules yet</p>'
        + '<p class="muted">Create a weekly schedule for a YouTube channel to start automatic assembly.</p>'
        + '<div class="schedule-empty-actions">'
        + '<button type="button" class="btn-primary btn-sm" id="scheduleOverviewCreateBtn">Create a schedule</button>'
        + '</div></div>';
      document.getElementById('scheduleOverviewCreateBtn')?.addEventListener('click', () => showScheduleSubtab('create'));
    } else {
      chEl.innerHTML = wrapScheduleTable('<table class="schedule-table"><thead><tr>'
        + '<th>Channel</th><th>Status</th><th>Timezone</th><th>Active days</th><th>Next assemble</th><th>Resources</th><th>YouTube</th>'
        + '</tr></thead><tbody>'
        + channels.map(row => {
          const next = row.next_slot;
          const nextTxt = next
            ? esc(next.day_name) + ' ' + esc(formatTimeDisplay(next.assemble_at))
              + '<br><span class="muted">' + esc(next.at_local) + '</span>'
            : '<span class="muted">—</span>';
          const yt = row.queue_youtube
            ? esc(row.upload_privacy) + (row.upload_now
              ? ' · upload after assemble'
              : (row.upload_schedule_publish ? ' · auto @ upload time' : ' · queue only'))
            : 'Off';
          const rowClass = ((row.enabled ? '' : 'is-disabled ') + 'is-clickable').trim();
          return '<tr class="' + rowClass + '" data-channel="' + esc(row.channel) + '">'
            + '<td><code>' + esc(row.channel) + '</code><br><span class="muted">post-processed/'
            + esc(row.images_folder || '—') + '/</span></td>'
            + '<td>' + scheduleStatusBadge(row.enabled) + '</td>'
            + '<td>' + esc(row.timezone) + '</td>'
            + '<td>' + (row.active_days?.length ? esc(row.active_days.join(' · ')) : '<span class="muted">None</span>') + '</td>'
            + '<td>' + nextTxt + '</td>'
            + '<td>' + resourcesBadge(row.resources_ready, row.blockers) + '</td>'
            + '<td>' + esc(yt) + '</td>'
            + '</tr>';
        }).join('')
        + '</tbody></table>');
      bindScheduleChannelJump(chEl, 'tr[data-channel]');
    }

    upEl.innerHTML = renderUpcomingByChannel(channels);
    bindScheduleChannelJump(upEl, '.schedule-upcoming-group-head');

    const runs = data.recent_runs || [];
    if (!runs.length) {
      runsEl.innerHTML = '<p class="muted">No cron dispatches recorded yet.</p>';
    } else {
      runsEl.innerHTML = wrapScheduleTable('<table class="schedule-table"><thead><tr>'
        + '<th>Channel</th><th>Slot</th><th>Status</th><th>Execution</th><th>When</th><th></th>'
        + '</tr></thead><tbody>'
        + runs.map(r => {
          const slot = esc(r.slot_key || '—');
          const channel = r.channel ? esc(r.channel) : esc(String(r.slot_key || '').split(':')[0] || '—');
          const status = esc(r.status || '—');
          const execId = r.execution_id ? '<code>' + esc(r.execution_id) + '</code>' : '—';
          const when = esc(r.updated_at || r.created_at || '—');
          const clearBtn = scheduleRunClearButton(r.slot_key);
          return '<tr><td><code>' + channel + '</code></td><td><code>' + slot + '</code></td><td>' + status
            + '</td><td>' + execId + '</td><td>' + when + '</td><td>' + clearBtn + '</td></tr>';
        }).join('')
        + '</tbody></table>');
      bindScheduleRunClearButtons(runsEl, async () => {
        await loadScheduleOverview();
        const ch = document.getElementById('scheduleChannel').value.trim();
        if (ch) await loadScheduleEditorDiagnostics(ch);
      });
    }
  } catch (e) {
    meta.textContent = String(e);
    chEl.innerHTML = '<p class="muted">' + esc(String(e)) + '</p>';
    upEl.innerHTML = '<p class="muted">—</p>';
    runsEl.innerHTML = '<p class="muted">—</p>';
  }
}
async function loadScheduleEditor(channel) {
  const empty = document.getElementById('scheduleEmpty');
  const editor = document.getElementById('scheduleEditor');
  const banner = document.getElementById('scheduleNewBanner');
  const hint = document.getElementById('scheduleEditorHint');
  if (!channel) {
    empty.hidden = false;
    editor.hidden = true;
    if (banner) banner.hidden = true;
    const titleEl = document.getElementById('scheduleEditorTitle');
    if (titleEl) titleEl.textContent = 'Channel schedule';
    if (hint) hint.textContent = 'Configure weekly cadence, defaults, and YouTube upload behavior.';
    const emptyTitle = empty?.querySelector('.schedule-empty-title');
    const emptyMuted = empty?.querySelector('.muted');
    if (emptyTitle) emptyTitle.textContent = 'Choose a channel';
    if (emptyMuted) emptyMuted.textContent = 'Pick a YouTube channel to create a new weekly schedule or edit an existing one.';
    return;
  }
  empty.hidden = true;
  editor.hidden = false;
  const titleEl = document.getElementById('scheduleEditorTitle');
  if (titleEl) titleEl.textContent = channel;
  let data;
  let isNew = false;
  try {
    data = await api('/v1/schedules/' + encodeURIComponent(channel));
  } catch (e) {
    if (isScheduleNotFound(e)) {
      isNew = true;
      data = defaultNewSchedule(channel);
    } else {
      console.error('schedule editor', e);
      empty.hidden = false;
      empty.querySelector('.schedule-empty-title').textContent = 'Couldn’t load schedule';
      empty.querySelector('.muted').textContent = String(e);
      editor.hidden = true;
      if (banner) banner.hidden = true;
      return;
    }
  }
  if (banner) {
    banner.hidden = !isNew;
    const label = document.getElementById('scheduleNewChannelLabel');
    if (label) label.textContent = channel;
  }
  if (hint) {
    hint.textContent = isNew
      ? 'Creating a new weekly schedule for this channel. Save when you’re ready.'
      : 'Editing the saved weekly schedule for this channel.';
  }
  const saveBtn = document.getElementById('scheduleSave');
  if (saveBtn) saveBtn.textContent = isNew ? 'Create schedule' : 'Save schedule';
  const deleteBtn = document.getElementById('scheduleDelete');
  if (deleteBtn) deleteBtn.hidden = isNew;
  fillScheduleForm(data);
  updateScheduleSummary();
  if (!isNew) loadScheduleEditorDiagnostics(channel);
  else {
    const resEl = document.getElementById('scheduleResources');
    const upEl = document.getElementById('scheduleUpcoming');
    const histEl = document.getElementById('scheduleRunHistory');
    if (resEl) resEl.textContent = 'Save the schedule to run resource checks.';
    if (upEl) upEl.innerHTML = '<p class="muted">Save the schedule to preview upcoming slots.</p>';
    if (histEl) histEl.innerHTML = '<p class="muted">No run history yet.</p>';
  }
}
function isScheduleNotFound(err) {
  const msg = String(err || '');
  return msg.includes('Schedule not found') || /\b404\b/.test(msg);
}
function defaultNewSchedule(channel) {
  return {
    channel,
    enabled: true,
    timezone: 'America/New_York',
    default_assemble_at: DEFAULT_ASSEMBLE_TIME,
    default_upload_at: '12:00',
    duration_min: 90,
    variance_min: 15,
    thumbnail_text: '__DEFAULT_THUMBNAIL__',
    queue_youtube: true,
    upload_schedule_publish: true,
    upload_now: false,
    upload_privacy: 'private',
    upload_category_id: '10',
    auto_extend: true,
    days: defaultScheduleDays(),
  };
}
function showScheduleSubtab(tab) {
  const overview = tab !== 'create';
  const overviewEl = document.getElementById('scheduleViewOverview');
  const editorEl = document.getElementById('scheduleViewEditor');
  if (overviewEl) overviewEl.hidden = !overview;
  if (editorEl) editorEl.hidden = overview;
  document.querySelectorAll('.schedule-subtab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.scheduleTab === (overview ? 'overview' : 'create'));
  });
  if (overview) {
    loadScheduleOverview();
  }
}
function openScheduleEditorForChannel(channel) {
  const sel = document.getElementById('scheduleChannel');
  if (sel && channel) sel.value = channel;
  showScheduleSubtab('create');
  loadScheduleEditor(channel);
  document.getElementById('scheduleViewEditor')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
async function saveSchedule() {
  const channel = document.getElementById('scheduleChannel').value.trim();
  if (!channel) { alert('Select a channel'); return; }
  const imagesFolder = document.getElementById('scheduleImagesFolder').value.trim();
  if (!imagesFolder) { alert('Select a background folder'); return; }
  const btn = document.getElementById('scheduleSave');
  setBtnLoading(btn, true, 'Saving…');
  try {
    const video = readScheduleVideoSettings();
    const upload = readScheduleUploadSettings();
    const body = {
      enabled: document.getElementById('scheduleEnabled').checked,
      timezone: document.getElementById('scheduleTimezone').value.trim() || 'America/New_York',
      images_folder: imagesFolder,
      duration_min: video.duration_min,
      variance_min: video.variance_min,
      thumbnail_text: video.thumbnail_text,
      queue_youtube: upload.queue_youtube,
      upload_privacy: upload.upload_privacy,
      upload_schedule_publish: upload.upload_schedule_publish,
      upload_now: upload.upload_now,
      upload_tags: upload.upload_tags,
      upload_category_id: upload.upload_category_id,
      upload_made_for_kids: upload.upload_made_for_kids,
      default_assemble_at: readScheduleTime('scheduleDefaultAssemble'),
      default_upload_at: timeFromInput(document.getElementById('scheduleDefaultUpload').value)
        || uploadTimeAfterAssemble(readScheduleTime('scheduleDefaultAssemble')),
      auto_extend: document.getElementById('scheduleAutoExtend').checked,
      days: collectScheduleDays(),
      apply_default_to_enabled_days: false,
    };
    await api('/v1/schedules/' + encodeURIComponent(channel), { method: 'PUT', body: JSON.stringify(body) });
    await loadScheduleEditor(channel);
    await loadScheduleOverview();
  } catch (e) {
    alert('Save failed: ' + e);
  }
  setBtnLoading(btn, false);
}

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
  if (section === 'schedule') {
    showScheduleSubtab('overview');
  }
  if (section === 'updates') {
    loadUpdatesPanel();
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
document.getElementById('obsVersion').onclick = () => showMainSection('updates');
document.querySelectorAll('#sectionJobs .job-tab[data-job]').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('#sectionJobs .job-tab[data-job]').forEach(b => b.classList.remove('active'));
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
    syncAssetFolderVisibility();
    setTabLoading(btn, true);
    try {
      await loadAssetList();
    } finally {
      setTabLoading(btn, false);
    }
  };
});
document.getElementById('assetImagesFolder')?.addEventListener('change', async () => {
  ui.tabsLoaded.assets = false;
  if (document.getElementById('panelAssets').classList.contains('active')) {
    await loadAssetList();
  }
});
document.getElementById('assetUploadBtn')?.addEventListener('click', uploadAssetFiles);

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
    + '&job_limit=50'
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
    if (includeStats) await applyInventory(d);
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
  const timing = runYoutubeTiming();
  const queueYoutube = timing !== 'skip';
  const schedulePublish = timing === 'schedule';
  let publishAt = null;
  let uploadAt = null;
  if (queueYoutube && schedulePublish) {
    publishAt = localDatetimeToRfc3339(document.getElementById('runPublishAt').value);
    uploadAt = localDatetimeToRfc3339(document.getElementById('runUploadAt').value);
    if (!publishAt && !uploadAt) {
      alert('Pick a go-live or upload date & time, or choose “Upload immediately”');
      return;
    }
    if (!publishAt && uploadAt) publishAt = uploadAt;
  }
  let durationMin = parseInt(document.getElementById('runDuration').value, 10);
  let varianceMin = parseInt(document.getElementById('runVariance').value, 10);
  if (!Number.isFinite(durationMin)) durationMin = 90;
  if (!Number.isFinite(varianceMin)) varianceMin = 15;
  durationMin = Math.min(300, Math.max(5, durationMin));
  varianceMin = Math.min(60, Math.max(0, varianceMin));
  setBtnLoading(btn, true, 'Starting…');
  try {
    const payload = {
      channel: channel,
      images_folder: imagesFolder,
      thumbnail_text: document.getElementById('runThumb').value,
      duration_min: durationMin,
      variance_min: varianceMin,
      count: parseInt(document.getElementById('runCount').value, 10),
      queue_youtube: queueYoutube,
    };
    if (queueYoutube) {
      payload.upload_privacy = document.getElementById('runUploadPrivacy').value || 'private';
      payload.upload_schedule_publish = schedulePublish;
      payload.upload_tags = document.getElementById('runUploadTags').value || '';
      payload.upload_category_id = document.getElementById('runUploadCategory').value || '10';
      payload.upload_made_for_kids = document.getElementById('runUploadMadeForKids').checked;
      if (schedulePublish) {
        if (publishAt) payload.publish_at = publishAt;
        if (uploadAt) payload.upload_at = uploadAt;
      }
    }
    const r = await api('/v1/assembly/jobs', { method: 'POST', body: JSON.stringify(payload)});
    showResultBlock('runResult', JSON.stringify(r, null, 2));
    ui.tabsLoaded.videos = false;
    ui.lastStatsAt = 0;
    await pollSnapshot({ includeStats: true });
    schedulePoll(3000);
    showMainSection('jobs');
    document.querySelector('.job-tab[data-job="assembly"]')?.click();
  } catch (e) { showResultBlock('runResult', String(e)); }
  setBtnLoading(btn, false);
};
function localDatetimeToRfc3339(value) {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function defaultLocalDatetimeValue(hoursAhead) {
  const d = new Date(Date.now() + (hoursAhead || 24) * 3600 * 1000);
  d.setMinutes(0, 0, 0);
  const pad = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
    + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function runYoutubeTiming() {
  const active = document.querySelector('.run-timing-seg button.active');
  return active?.dataset?.timing || 'immediate';
}

function syncRunYoutubeOptions() {
  const timing = runYoutubeTiming();
  const queueOn = timing !== 'skip';
  const scheduleOn = timing === 'schedule';
  const opts = document.getElementById('runYoutubeOptions');
  const fields = document.getElementById('runScheduleFieldsMain');
  const hint = document.getElementById('runTimingHint');
  if (opts) opts.hidden = !queueOn;
  if (fields) fields.hidden = !scheduleOn;
  if (hint) {
    if (timing === 'immediate') {
      hint.innerHTML = 'Dispatches to youtube-uploader with <code>upload_now</code> as soon as the video is ready.';
    } else if (timing === 'schedule') {
      hint.textContent = 'Queues for upload and YouTube go-live at the times you pick below.';
    } else {
      hint.textContent = 'Assembly only — no YouTube register after encode.';
    }
  }
  if (scheduleOn) {
    const pub = document.getElementById('runPublishAt');
    if (pub && !pub.value) pub.value = defaultLocalDatetimeValue(24);
  }
}

document.querySelectorAll('.run-timing-seg button').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.run-timing-seg button').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    syncRunYoutubeOptions();
  });
});
syncRunYoutubeOptions();
document.getElementById('extendSourceFolder')?.addEventListener('change', () => {
  refreshExtendPending();
});
document.getElementById('extendProcessAll')?.addEventListener('change', syncExtendLimitEnabled);
document.getElementById('extendBtn').onclick = async () => {
  const btn = document.getElementById('extendBtn');
  const sourceFolder = document.getElementById('extendSourceFolder').value.trim();
  if (!sourceFolder) { alert('Select a pre-processed folder'); return; }
  const processAll = document.getElementById('extendProcessAll').checked;
  let limit = null;
  if (!processAll) {
    limit = parseInt(document.getElementById('extendLimit').value, 10);
    if (!Number.isFinite(limit) || limit < 1) {
      alert('Batch size must be a number of 1 or greater');
      return;
    }
    const pendingText = document.getElementById('extendPending').textContent;
    const pending = parseInt(pendingText, 10);
    if (Number.isFinite(pending) && limit > pending) {
      alert('Requested ' + limit + ' images but only ' + pending + ' pending in pre-processed/' + sourceFolder + '/');
      return;
    }
  }
  setBtnLoading(btn, true, 'Starting…');
  try {
    const r = await api('/v1/extend/jobs', { method: 'POST', body: JSON.stringify({
      category: cat(),
      source_folder: sourceFolder,
      process_all: processAll,
      limit: processAll ? null : limit,
    })});
    showResultBlock('extendResult', JSON.stringify(r, null, 2));
    ui.tabsLoaded.assets = false;
    ui.lastStatsAt = 0;
    await pollSnapshot({ includeStats: true });
    await refreshExtendPending();
    schedulePoll(3000);
    showMainSection('jobs');
    document.querySelector('.job-tab[data-job="extend"]')?.click();
  } catch (e) { showResultBlock('extendResult', String(e)); }
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
bindScheduleDayBar();
renderScheduleDayTimes(defaultScheduleDays());
mountTimePickerForHidden(document.getElementById('scheduleDefaultAssemble'), () => {
  const assemble = readScheduleTime('scheduleDefaultAssemble');
  setHiddenTimeValue(document.getElementById('scheduleDefaultUpload'), uploadTimeAfterAssemble(assemble));
  updateScheduleSummary();
});
mountTimePickerForHidden(document.getElementById('scheduleDefaultUpload'), () => updateScheduleSummary());
document.getElementById('scheduleEnabled')?.addEventListener('change', () => {
  syncScheduleEnabledState();
  updateScheduleSummary();
});
document.querySelectorAll('.schedule-subtab').forEach((btn) => {
  btn.addEventListener('click', () => showScheduleSubtab(btn.dataset.scheduleTab || 'overview'));
});
document.getElementById('scheduleChannel').addEventListener('change', () => loadScheduleEditor(document.getElementById('scheduleChannel').value.trim()));
document.getElementById('scheduleReload').onclick = () => {
  const overviewHidden = document.getElementById('scheduleViewOverview')?.hidden;
  if (!overviewHidden) {
    loadScheduleOverview(true);
  } else {
    const ch = document.getElementById('scheduleChannel').value.trim();
    if (ch) loadScheduleEditor(ch);
  }
};
document.getElementById('scheduleSave').onclick = saveSchedule;
['scheduleThumb', 'scheduleDuration', 'scheduleVariance', 'scheduleImagesFolder', 'scheduleTimezone', 'scheduleQueueYoutube'].forEach(id => {
  document.getElementById(id)?.addEventListener('change', updateScheduleSummary);
  document.getElementById(id)?.addEventListener('input', updateScheduleSummary);
});
document.getElementById('scheduleApplyDefault').onclick = () => {
  const assemble = readScheduleTime('scheduleDefaultAssemble');
  const upload = timeFromInput(document.getElementById('scheduleDefaultUpload').value)
    || uploadTimeAfterAssemble(assemble);
  document.querySelectorAll('.schedule-day-btn.is-active').forEach(btn => {
    const i = btn.dataset.day;
    const assembleVis = document.querySelector('.schedule-day-assemble-visible[data-day="' + i + '"]');
    const uploadVis = document.querySelector('.schedule-day-upload-visible[data-day="' + i + '"]');
    const assembleHidden = document.querySelector('.schedule-day-assemble[data-day="' + i + '"]');
    const uploadHidden = document.querySelector('.schedule-day-upload[data-day="' + i + '"]');
    setHiddenTimeValue(assembleVis, assemble);
    setHiddenTimeValue(uploadVis, upload);
    if (assembleHidden) assembleHidden.value = timeInputValue(assemble);
    if (uploadHidden) uploadHidden.value = timeInputValue(upload);
  });
  updateScheduleSummary();
};
['scheduleUploadPrivacy', 'scheduleUploadTags', 'scheduleUploadCategory', 'scheduleUploadMadeForKids', 'scheduleQueueYoutube'].forEach(id => {
  document.getElementById(id)?.addEventListener('change', () => {
    if (id === 'scheduleQueueYoutube') syncScheduleYoutubeOptions();
    else updateScheduleSummary();
  });
});
document.querySelectorAll('input[name="scheduleUploadMode"]').forEach((el) => {
  el.addEventListener('change', () => {
    syncScheduleUploadTimingUi();
    updateScheduleSummary();
  });
});
document.getElementById('scheduleQueueYoutube')?.addEventListener('change', syncScheduleYoutubeOptions);
document.getElementById('scheduleDelete').onclick = async () => {
  const channel = document.getElementById('scheduleChannel').value.trim();
  if (!channel || !confirm('Delete schedule for ' + channel + '?')) return;
  try {
    await api('/v1/schedules/' + encodeURIComponent(channel), { method: 'DELETE' });
    document.getElementById('scheduleChannel').value = '';
    await loadScheduleEditor('');
    await loadScheduleOverview();
    showScheduleSubtab('overview');
  } catch (e) { alert(String(e)); }
};

(async function init() {
  renderObsBar();
  setStatsLoading(true);
  setJobsLoading(true);
  const bootTimeoutMs = 12000;
  try {
    await Promise.race([
      Promise.all([
        loadVersionInfo(),
        loadChannelOptions(),
        loadBackgroundFolders(),
        pollSnapshot({ includeStats: false }),
      ]),
      new Promise(resolve => setTimeout(resolve, bootTimeoutMs)),
    ]);
  } catch (e) {
    console.error(e);
    showAuthError('Failed to load dashboard. Try Refresh.');
  } finally {
    setStatsLoading(false);
    setJobsLoading(false);
    hidePageBoot();
  }
  refreshStats().catch(e => console.warn('stats', e));
  schedulePoll(15000);
})();
</script>
</body>
</html>
"""
)

install_openapi_docs(app)
