"""OpenAPI metadata for ``/docs`` (Swagger UI) and ``/redoc``.

FastAPI builds the interactive API reference automatically from this module.
No separate documentation route is required.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from music_assembler import __version__

APP_DESCRIPTION = """
Music Assembly **control plane** — queue Cloud Run jobs, poll R2 progress, and browse outputs.

## Authentication

Most ``/v1/*`` routes accept **either**:

- **`X-API-Key`** header (scripts, curl, automation) when ``ASSEMBLY_API_KEY`` is set, or
- **Dashboard session cookie** after ``POST /v1/dashboard/login`` (browser UI).

Public routes: ``GET /health``, ``GET /v1/version``, ``GET /v1/capabilities``, ``POST /v1/dashboard/login``.

## Job IDs

| Prefix | Worker | Storage |
|--------|--------|---------|
| ``asm_*`` | ``music-assemble`` Cloud Run Job | ``jobs/{id}/meta.json`` + ``progress.json`` on R2 |
| ``ext_*`` | ``music-extend`` Cloud Run Job | same layout under ``jobs/ext_*`` |

Progress is written to R2 by workers; the API reconciles with GCP execution state when needed.

## Typical flows

**Start assembly (1 video):**
```bash
curl -X POST "$BASE/v1/assembly/jobs" \\
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \\
  -d '{"category":"korean","channel":"nappabeats","count":1}'
```

**Poll dashboard jobs:**
```bash
curl "$BASE/v1/dashboard/snapshot?category=korean&light=1" -H "X-API-Key: $KEY"
```

**Start extend batch (parallel — one Cloud Run job per image):**
```bash
curl -X POST "$BASE/v1/extend/jobs" \\
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \\
  -d '{"category":"korean","limit":5,"parallel":true}'
```

## Interactive docs

- **Swagger UI:** ``/docs``
- **ReDoc:** ``/redoc``
- **OpenAPI JSON:** ``/openapi.json``
"""

OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "Meta",
        "description": "Health, version, and capability discovery. No API key required.",
    },
    {
        "name": "Dashboard",
        "description": "Browser login and aggregated poll endpoints used by the web UI.",
    },
    {
        "name": "Assembly",
        "description": "Start and monitor ``music-assemble`` Cloud Run Jobs (music video pipeline).",
    },
    {
        "name": "Extend",
        "description": "Start and monitor ``music-extend`` Cloud Run Jobs (Gemini background extension).",
    },
    {
        "name": "Jobs",
        "description": "Cross-cutting job actions (cancel) for both assembly and extend runs.",
    },
    {
        "name": "Catalog",
        "description": "R2 inventory — categories, channels, videos, and background image pools.",
    },
    {
        "name": "Media",
        "description": "Authenticated proxies for thumbnails, MP4 previews, and pool images.",
    },
    {
        "name": "Observability",
        "description": "Internal cache and service stats for debugging.",
    },
]

# Keys: ``"{method} {path}"`` matching FastAPI route paths.
ENDPOINT_DOCS: dict[str, dict[str, Any]] = {
    "GET /health": {
        "tags": ["Meta"],
        "summary": "Liveness probe",
        "description": (
            "Returns service name and version. Used by Cloud Run health checks and uptime monitors. "
            "**No authentication.**"
        ),
        "response_example": {
            "status": "ok",
            "service": "music-assembly-api",
            "version": "0.1.1",
            "revision": "music-assembly-api-00017-8jq",
        },
    },
    "GET /v1/version": {
        "tags": ["Meta"],
        "summary": "Build and revision info",
        "description": (
            "Returns package version, Cloud Run revision (``K_REVISION``), and optional build id "
            "(``ASSEMBLY_BUILD_ID``). Shown in the dashboard footer. **No authentication.**"
        ),
        "response_example": {
            "version": "0.1.1",
            "revision": "music-assembly-api-00017-8jq",
            "build": "v0.1.2-job-logs-fix",
            "dashboard": "v0.1.1 · music-assembly-api-00017-8jq",
        },
    },
    "GET /v1/capabilities": {
        "tags": ["Meta"],
        "summary": "Configured features and endpoint index",
        "description": (
            "Machine-readable summary of GCP project/region, job names, default category, auth modes, "
            "and a flat list of API paths. Useful for clients discovering what this deployment supports. "
            "**No authentication.**"
        ),
        "response_example": {
            "service": "music-assembly-api",
            "version": "0.1.1",
            "gcp_project": "youtube-uploader-499603",
            "gcp_region": "northamerica-northeast2",
            "assembly_job": "music-assemble",
            "extend_job": "music-extend",
            "default_category": "korean",
            "configured_channels": ["lofi-beats"],
            "auth": {"api": "X-API-Key", "dashboard": "password+cookie"},
            "endpoints": ["POST /v1/assembly/jobs", "GET /v1/dashboard/snapshot"],
        },
    },
    "POST /v1/dashboard/login": {
        "tags": ["Dashboard"],
        "summary": "Unlock web dashboard",
        "description": (
            "Validates ``ASSEMBLY_DASHBOARD_PASSWORD`` and sets an **httpOnly session cookie** "
            "so subsequent browser requests to ``/v1/*`` work without ``X-API-Key``. "
            "If dashboard password is not configured, returns ``dashboard_auth: disabled``."
        ),
        "request_example": {"password": "your-dashboard-password"},
        "response_example": {"ok": True},
    },
    "POST /v1/dashboard/logout": {
        "tags": ["Dashboard"],
        "summary": "Clear dashboard session",
        "description": "Clears the session cookie. Requires an existing session or API key.",
        "response_example": {"ok": True},
    },
    "GET /v1/dashboard": {
        "tags": ["Dashboard"],
        "summary": "Legacy dashboard summary",
        "description": (
            "Returns recent GCP assembly executions (limit 10), recent videos (limit 5), and category "
            "inventory. Prefer ``GET /v1/dashboard/snapshot`` for the current web UI."
        ),
        "response_example": {
            "category": "korean",
            "recent_jobs": [
                {
                    "execution_id": "music-assemble-abc12",
                    "status": "running",
                    "job_name": "music-assemble",
                }
            ],
            "recent_videos": [{"id": "mv_20260623_120000", "channel": "lofi-beats"}],
            "inventory": {"pre-processed": 42, "post-processed": 18, "music-video": 3},
        },
    },
    "GET /v1/dashboard/stats": {
        "tags": ["Dashboard"],
        "summary": "Cached inventory and extend pending count",
        "description": (
            "Lightweight stats for the dashboard cards: per-pool image counts and how many "
            "``pre-processed/`` images are waiting for extend. Cached ~45s; pass ``refresh=true`` "
            "to bypass. Response includes ``X-Cache: HIT|MISS`` header."
        ),
        "response_example": {
            "category": "korean",
            "inventory": {
                "pre-processed": 42,
                "post-processed": 18,
                "pre-used": 100,
                "post-used": 95,
                "music-video": 12,
            },
            "extend_pending": 42,
            "cache": {"hit": True, "ttl_sec": 45},
        },
    },
    "GET /v1/dashboard/snapshot": {
        "tags": ["Dashboard"],
        "summary": "Poll assembly + extend job tables",
        "description": (
            "Primary dashboard poll endpoint. Lists up to ``job_limit`` recent assembly and extend "
            "runs from R2, reconciled with GCP status. Still-running jobs outside the window are "
            "always included.\n\n"
            "- ``light=true`` — jobs only (skip inventory stats; faster).\n"
            "- ``refresh=true`` — bypass stats cache and force GCP reconcile.\n\n"
            "When ``has_running`` is true, the UI also polls per-job progress endpoints."
        ),
        "response_example": {
            "category": "korean",
            "assembly_runs": [
                {
                    "execution_id": "asm_20260623_120000_a1b2c3d4",
                    "status": "running",
                    "pct": 34.5,
                    "stage": "Encoding on Cloud Run…",
                    "status_source": "gcp",
                }
            ],
            "extend_runs": [
                {
                    "execution_id": "ext_20260623_120100_e5f6g7h8",
                    "status": "running",
                    "pct": 55.0,
                    "stage": "Gemini extend 1/1: photo.jpg",
                    "status_source": "r2",
                }
            ],
            "has_running": True,
        },
    },
    "POST /v1/assembly/jobs": {
        "tags": ["Assembly"],
        "summary": "Start music-assemble Cloud Run Job(s)",
        "description": (
            "Queues one or more assembly runs. For each job:\n"
            "1. Writes ``jobs/asm_*/meta.json`` and ``progress.json`` on R2.\n"
            "2. Starts a ``music-assemble`` GCP execution with env vars (category for music, "
            "``images_folder`` for ``post-processed/`` backgrounds, channel for output path, etc.).\n"
            "3. Each parallel job claims a unique ``post-processed/{images_folder}/`` background.\n\n"
            "Set ``count`` > 1 to render multiple videos in parallel (separate Cloud Run executions).\n\n"
            "By default ``queue_youtube`` is **true**: each finished video is registered on the "
            "youtube-uploader pending queue after R2 upload (worker needs ``UPLOADER_API_URL`` + "
            "``UPLOADER_API_KEY``). Set ``queue_youtube: false`` to skip.\n\n"
            "Optional YouTube flags: ``upload_privacy``, ``upload_schedule_publish``, ``publish_at`` / "
            "``upload_at`` (RFC3339 UTC), ``upload_tags``, ``upload_category_id``, ``upload_made_for_kids``. "
            "When ``upload_schedule_publish`` is false, the worker registers with uploader "
            "``upload_now`` + ``no_schedule`` so upload starts as soon as assembly finishes."
        ),
        "request_example": {
            "category": "korean",
            "channel": "lofi-beats",
            "images_folder": "korean",
            "template_id": "playlist_landscape",
            "thumbnail_text": "OMYO",
            "duration_min": 90,
            "variance_min": 15,
            "count": 1,
            "queue_youtube": True,
            "upload_privacy": "private",
            "upload_schedule_publish": True,
            "publish_at": "2026-08-01T16:00:00Z",
        },
        "response_example": {
            "api_execution_id": "asm_20260623_120000_a1b2c3d4",
            "gcp_execution_id": "music-assemble-xyz99",
            "execution_id": "music-assemble-xyz99",
            "status": "running",
            "jobs": [{"api_execution_id": "asm_20260623_120000_a1b2c3d4"}],
            "count": 1,
        },
    },
    "GET /v1/assembly/jobs": {
        "tags": ["Assembly"],
        "summary": "List GCP assembly executions",
        "description": (
            "Lists recent ``music-assemble`` Cloud Run Job executions directly from GCP "
            "(not R2). Optional ``status`` filter: ``running``, ``succeeded``, ``failed``."
        ),
        "response_example": {
            "jobs": [
                {
                    "execution_id": "music-assemble-xyz99",
                    "job_name": "music-assemble",
                    "status": "running",
                    "create_time": "2026-06-23T12:00:00Z",
                    "log_uri": "https://console.cloud.google.com/run/jobs/...",
                }
            ],
            "count": 1,
        },
    },
    "GET /v1/assembly/jobs/{execution_id}": {
        "tags": ["Assembly"],
        "summary": "Get one assembly run",
        "description": (
            "For ``asm_*`` ids, returns R2 progress. For raw GCP execution ids, returns GCP row "
            "plus optional R2 progress if linked."
        ),
        "response_example": {
            "execution_id": "asm_20260623_120000_a1b2c3d4",
            "status": "running",
            "pct": 45.0,
            "stage": "Mixing audio…",
            "updated_at": "2026-06-23T12:05:00+00:00",
        },
    },
    "GET /v1/assembly/jobs/{execution_id}/progress": {
        "tags": ["Assembly"],
        "summary": "Poll assembly progress (reconciled)",
        "description": (
            "Returns a single normalized row merging R2 ``progress.json`` with GCP execution state. "
            "Used by the dashboard for running assembly jobs. Prefer this over raw ``GET .../jobs/{id}`` "
            "for live updates."
        ),
        "response_example": {
            "execution_id": "asm_20260623_120000_a1b2c3d4",
            "gcp_execution_id": "music-assemble-xyz99",
            "status": "running",
            "pct": 45.0,
            "stage": "Encoding on Cloud Run…",
            "status_source": "gcp",
            "updated_at": "2026-06-23T12:05:00+00:00",
        },
    },
    "GET /v1/assembly/runs": {
        "tags": ["Assembly"],
        "summary": "List assembly runs from R2",
        "description": (
            "Lists ``jobs/asm_*/`` folders on R2 (meta + progress), newest first. Includes still-running "
            "jobs beyond ``limit`` when applicable."
        ),
        "response_example": {
            "runs": [
                {
                    "execution_id": "asm_20260623_120000_a1b2c3d4",
                    "category": "korean",
                    "channel": "lofi-beats",
                    "created_at": "2026-06-23T12:00:00+00:00",
                    "progress": {"pct": 45.0, "stage": "Encoding…", "status": "running"},
                }
            ],
            "count": 1,
        },
    },
    "POST /v1/extend/jobs": {
        "tags": ["Extend"],
        "summary": "Start music-extend Cloud Run Job(s)",
        "description": (
            "Pulls images from ``pre-processed/{category}/``, runs Gemini extend, uploads to "
            "``post-processed/``. By default runs on GCP (``music-extend`` job), not in-process.\n\n"
            "- ``parallel=true`` + ``limit`` > 1 → one Cloud Run execution per image (faster, isolated).\n"
            "- ``process_all=true`` → drain entire pending pool.\n"
            "- Returns **409** if no pending images.\n\n"
            "Workers atomically claim images so parallel jobs never extend the same file."
        ),
        "request_example": {
            "category": "korean",
            "limit": 5,
            "process_all": False,
            "force": False,
            "parallel": True,
        },
        "response_example": {
            "parallel": True,
            "batch_size": 5,
            "category": "korean",
            "pending": 42,
            "host": "cloud_run",
            "jobs": [
                {
                    "execution_id": "ext_20260623_120100_e5f6g7h8",
                    "gcp_execution_id": "music-extend-abc12",
                    "max_images": 1,
                }
            ],
        },
    },
    "GET /v1/extend/pending": {
        "tags": ["Extend"],
        "summary": "Count extendable pre-processed images",
        "description": (
            "Returns how many images in ``pre-processed/{category}/`` are available for extend "
            "(excludes in-flight and used). ``force=true`` includes images that would otherwise be skipped."
        ),
        "response_example": {"category": "korean", "pending": 42},
    },
    "GET /v1/extend/runs": {
        "tags": ["Extend"],
        "summary": "List extend runs from R2",
        "description": "Same as assembly runs but for ``jobs/ext_*/`` prefixes.",
        "response_example": {
            "runs": [
                {
                    "execution_id": "ext_20260623_120100_e5f6g7h8",
                    "category": "korean",
                    "job_type": "extend",
                    "progress": {"pct": 100.0, "status": "succeeded", "stage": "Complete"},
                }
            ],
            "count": 1,
        },
    },
    "GET /v1/extend/jobs/{execution_id}/progress": {
        "tags": ["Extend"],
        "summary": "Poll extend progress",
        "description": "Reads ``jobs/{execution_id}/progress.json`` from R2. Used for live extend job rows.",
        "response_example": {
            "execution_id": "ext_20260623_120100_e5f6g7h8",
            "pct": 55.0,
            "stage": "Gemini extend 1/1: pinterest-1.jpg",
            "status": "running",
            "updated_at": "2026-06-23T12:01:30+00:00",
        },
    },
    "GET /v1/jobs/{execution_id}/cancel": {
        "tags": ["Jobs"],
        "summary": "Preview cancel (GET)",
        "description": (
            "Returns what would happen if you cancel this job (GCP execution id, current status). "
            "Does not cancel. Use ``POST`` with ``confirm: true`` to actually cancel."
        ),
        "response_example": {
            "found": True,
            "execution_id": "ext_20260623_120100_e5f6g7h8",
            "status": "running",
            "cancellable": True,
            "message": "POST with confirm=true to cancel",
        },
    },
    "POST /v1/jobs/{execution_id}/cancel": {
        "tags": ["Jobs"],
        "summary": "Cancel a running job",
        "description": (
            "Cancels GCP execution for assembly or extend. With ``confirm: false`` (default), behaves "
            "like the GET preview. With ``confirm: true``, cancels the Cloud Run execution and updates "
            "R2 progress to ``cancelled``."
        ),
        "request_example": {"confirm": True},
        "response_example": {
            "found": True,
            "execution_id": "ext_20260623_120100_e5f6g7h8",
            "cancelled": True,
            "gcp_execution_id": "music-extend-abc12",
        },
    },
    "GET /v1/categories": {
        "tags": ["Catalog"],
        "summary": "List R2 category folders",
        "description": "Discovers top-level categories from R2 keys (e.g. ``korean``, ``jazz``).",
        "response_example": {"categories": ["korean", "jazz"]},
    },
    "GET /v1/background-folders": {
        "tags": ["Catalog"],
        "summary": "List post-processed background folders",
        "description": (
            "Discovers subfolders under ``post-processed/`` on R2. Use as ``images_folder`` when "
            "starting assembly jobs to pick which background pool to claim from."
        ),
        "response_example": {"folders": ["korean", "japanese"], "count": 2},
    },
    "GET /v1/categories/{category}/inventory": {
        "tags": ["Catalog"],
        "summary": "Object counts per pool",
        "description": (
            "Counts objects under ``pre-processed``, ``post-processed``, ``music-video``, and used pools "
            "for one category."
        ),
        "response_example": {
            "category": "korean",
            "counts": {
                "pre-processed": 42,
                "post-processed": 18,
                "pre-used": 100,
                "post-used": 95,
                "music-video": 12,
            },
        },
    },
    "GET /v1/channels": {
        "tags": ["Catalog"],
        "summary": "List YouTube channel slugs for assembly",
        "description": (
            "Returns channel slugs for ``POST /v1/assembly/jobs`` ``channel`` field.\n\n"
            "When ``UPLOADER_API_URL`` and ``UPLOADER_API_KEY`` are set, fetches all configured "
            "YouTube channels from the **youtube-uploader** service and merges with "
            "``ASSEMBLY_CHANNELS`` and R2 ``music-video/{channel}/`` folders.\n\n"
            "Use ``channel_details[].id`` as the request value; ``name`` is for display."
        ),
        "response_example": {
            "category": "korean",
            "channels": ["nappabeats", "sapporobeats"],
            "channel_details": [
                {
                    "id": "nappabeats",
                    "name": "NappaBeats",
                    "custom_url": "@nappabeats",
                    "auth_valid": True,
                    "source": "uploader",
                }
            ],
            "configured": [],
            "discovered": [],
            "uploader": {"configured": True, "count": 5},
        },
    },
    "GET /v1/videos": {
        "tags": ["Catalog"],
        "summary": "List finished music videos",
        "description": (
            "Lists ``music-video/{channel}/mv_*`` folders. "
            "``summary=true`` (default) returns flags only (has mp4, thumb, title) without reading "
            "large text files. Cached ~60s."
        ),
        "response_example": {
            "category": "korean",
            "channel": "lofi-beats",
            "summary": True,
            "count": 2,
            "videos": [
                {
                    "id": "mv_20260623_120000",
                    "channel": "lofi-beats",
                    "has_video": True,
                    "has_thumbnail": True,
                    "last_modified": "2026-06-23T12:30:00+00:00",
                }
            ],
            "cache": {"hit": False},
        },
    },
    "GET /v1/videos/{video_id}": {
        "tags": ["Catalog"],
        "summary": "Get video metadata",
        "description": (
            "Full metadata for one ``mv_*`` folder: title, description, tracklist, and stable "
            "``/v1/media/*`` URLs for thumbnail and MP4."
        ),
        "response_example": {
            "id": "mv_20260623_120000",
            "category": "korean",
            "channel": "lofi-beats",
            "title": "Late Night Lofi Mix",
            "description": "90 minutes of chill beats…",
            "has_video": True,
            "video_url": "/v1/media/video?channel=nappabeats&video_id=mv_20260623_120000",
            "thumbnail_url": "/v1/media/thumbnail?channel=nappabeats&video_id=mv_20260623_120000",
            "r2_prefix": "music-video/nappabeats/mv_20260623_120000/",
        },
    },
    "GET /v1/assets": {
        "tags": ["Catalog"],
        "summary": "List background images (metadata only)",
        "description": (
            "Lists filenames in a pool: ``pre-processed``, ``post-processed``, ``pre-used``, or "
            "``post-used``. Does not return image bytes — use ``GET /v1/media/asset`` per file."
        ),
        "response_example": {
            "category": "korean",
            "pool": "pre-processed",
            "count": 2,
            "items": [
                {"name": "01-pinterest-1.jpg", "size": 245760, "modified": "2026-06-20T10:00:00+00:00"}
            ],
            "cache": {"hit": True, "ttl_sec": 60},
        },
    },
    "POST /v1/assets/upload": {
        "tags": ["Catalog"],
        "summary": "Upload images to R2",
        "description": (
            "Multipart upload of one or more images to ``pre-processed/{category}/`` or "
            "``post-processed/{images_folder}/`` on Cloudflare R2. Supports jpg, png, webp "
            "(max 50 files and ~28 MiB estimated request size per batch to stay under Cloud Run's "
            "32 MiB HTTP/1 limit; 20 MB per file). The dashboard auto-batches larger selections and "
            "retries with smaller chunks if a batch still hits HTTP 413. "
            "Duplicate names get ``_2``, ``_3`` suffixes unless ``overwrite`` is true."
        ),
        "response_example": {
            "category": "korean",
            "pool": "pre-processed",
            "images_folder": None,
            "count": 2,
            "uploaded": [
                {"name": "photo.jpg", "key": "pre-processed/korean/photo.jpg", "size": 120000}
            ],
            "errors": [],
        },
    },
    "GET /v1/media/thumbnail": {
        "tags": ["Media"],
        "summary": "Stream video thumbnail",
        "description": (
            "Returns PNG/JPEG bytes for a video's thumbnail. Stable URL for embedding in the dashboard "
            "(no expiring presigned URLs). **Response is binary**, not JSON."
        ),
        "response_example": "(binary image/png body)",
    },
    "GET /v1/media/video": {
        "tags": ["Media"],
        "summary": "Stream MP4 with Range support",
        "description": (
            "Proxies the video MP4 from R2 with **HTTP Range** headers for in-browser seeking. "
            "**Response is binary** ``video/mp4``."
        ),
        "response_example": "(binary video/mp4 body; supports Range: bytes=…)",
    },
    "GET /v1/media/asset": {
        "tags": ["Media"],
        "summary": "Stream one pool image",
        "description": (
            "Loads a single file from ``{pool}/{category}/{name}`` on R2. Used when clicking a row "
            "in the Background images tab."
        ),
        "response_example": "(binary image/jpeg or image/png body)",
    },
    "GET /v1/observability": {
        "tags": ["Observability"],
        "summary": "Cache and service stats",
        "description": "In-memory dashboard cache statistics for debugging slow loads.",
        "response_example": {
            "service": "music-assembly-api",
            "cache": {"entries": 4, "hits": 120, "misses": 15},
        },
    },
}


def _patch_operation(operation: dict[str, Any], meta: dict[str, Any]) -> None:
    if meta.get("summary"):
        operation["summary"] = meta["summary"]
    if meta.get("description"):
        operation["description"] = meta["description"]
    if meta.get("tags"):
        operation["tags"] = meta["tags"]

    example = meta.get("response_example")
    if example is not None:
        responses = operation.setdefault("responses", {})
        ok = responses.setdefault("200", {"description": "Successful response"})
        if isinstance(example, str):
            ok["description"] = example
        else:
            ok.setdefault("content", {})["application/json"] = {"example": example}

    req_example = meta.get("request_example")
    if req_example is not None:
        body = operation.setdefault("requestBody", {})
        content = body.setdefault("content", {})
        content.setdefault("application/json", {})["example"] = req_example


def install_openapi_docs(app: FastAPI) -> None:
    """Attach rich OpenAPI metadata so ``/docs`` and ``/redoc`` are fully described."""

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version or __version__,
            description=APP_DESCRIPTION,
            routes=app.routes,
            tags=OPENAPI_TAGS,
        )

        schema.setdefault("components", {}).setdefault(
            "securitySchemes",
            {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": (
                        "Set ``ASSEMBLY_API_KEY`` on the server. "
                        "Alternatively use a dashboard session cookie after login."
                    ),
                }
            },
        )

        public_paths = {
            "/health",
            "/v1/version",
            "/v1/capabilities",
            "/v1/dashboard/login",
            "/openapi.json",
            "/docs",
            "/redoc",
        }

        paths = schema.get("paths", {})
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not path or not methods:
                continue
            if getattr(route, "include_in_schema", True) is False:
                continue

            for method in methods:
                if method in ("HEAD", "OPTIONS"):
                    continue
                key = f"{method} {path}"
                meta = ENDPOINT_DOCS.get(key)
                path_item = paths.get(path)
                if not path_item:
                    continue
                operation = path_item.get(method.lower())
                if not operation:
                    continue
                if meta:
                    _patch_operation(operation, meta)
                if path not in public_paths and path.startswith("/v1/"):
                    operation.setdefault("security", [{"ApiKeyAuth": []}])

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
