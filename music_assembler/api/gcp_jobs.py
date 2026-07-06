"""Cloud Run Jobs — start executions and read status."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from music_assembler.api.config import ApiSettings
from music_assembler.api.gcp_credentials import get_gcp_credentials

try:
    from google.api_core import exceptions as gcp_exceptions
    from google.cloud import run_v2
except ImportError:  # pragma: no cover
    gcp_exceptions = None  # type: ignore[assignment,misc]
    run_v2 = None  # type: ignore[assignment,misc]

_EXECUTION_RE = re.compile(
    r"projects/(?P<project>[^/]+)/locations/(?P<region>[^/]+)/jobs/(?P<job>[^/]+)/executions/(?P<exec>[^/]+)"
)


_GCP_CREDS_HINT = (
    "GCP credentials not configured. For local dev, set ASSEMBLY_GCP_SA_* in .env "
    "(see .env.example). Required: ASSEMBLY_GCP_SA_PRIVATE_KEY and "
    "ASSEMBLY_GCP_SA_CLIENT_EMAIL."
)


def _require_client():
    if run_v2 is None:
        raise RuntimeError(
            "google-cloud-run is required for the assembly API. "
            'Install with: pip install ".[api]"'
        )
    try:
        credentials = get_gcp_credentials()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(_GCP_CREDS_HINT) from exc
    return (
        run_v2.JobsClient(credentials=credentials),
        run_v2.ExecutionsClient(credentials=credentials),
    )


def _execution_status(execution: Any) -> str:
    for cond in getattr(execution, "conditions", []) or []:
        if getattr(cond, "type_", None) == "Completed":
            state = getattr(cond, "state", None)
            if state == run_v2.Condition.State.CONDITION_SUCCEEDED:
                return "succeeded"
            if state == run_v2.Condition.State.CONDITION_FAILED:
                return "failed"
            if state == run_v2.Condition.State.CONDITION_RECONCILING:
                return "running"
    return "running"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ts(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def execution_to_dict(execution: Any, *, job_name: str) -> dict[str, Any]:
    name = execution.name
    match = _EXECUTION_RE.search(name)
    short_id = match.group("exec") if match else name.rsplit("/", 1)[-1]
    return {
        "execution_id": short_id,
        "name": name,
        "job_name": job_name,
        "status": _execution_status(execution),
        "create_time": _ts(getattr(execution, "create_time", None)),
        "start_time": _ts(getattr(execution, "start_time", None)),
        "completion_time": _ts(getattr(execution, "completion_time", None)),
        "log_uri": (
            f"https://console.cloud.google.com/run/jobs/executions/details/"
            f"{getattr(execution, 'name', '').split('/')[-3]}/{short_id}"
            if name
            else None
        ),
    }


def _wrap_gcp_error(exc: Exception) -> RuntimeError:
    return RuntimeError(str(exc))


def _pick_new_execution(
    executions_client: Any,
    *,
    parent: str,
    job_name: str,
    started_after: datetime,
    exclude: set[str],
) -> dict[str, Any] | None:
    """Find the GCP execution created for this ``run_job`` call (parallel-safe)."""
    cutoff = started_after - timedelta(seconds=10)
    try:
        pages = executions_client.list_executions(parent=parent)
    except Exception as exc:
        if gcp_exceptions and isinstance(exc, gcp_exceptions.GoogleAPIError):
            raise _wrap_gcp_error(exc) from exc
        raise
    for execution in pages:
        row = execution_to_dict(execution, job_name=job_name)
        if row["execution_id"] in exclude:
            continue
        created = _parse_ts(row.get("create_time"))
        if created is not None and created >= cutoff:
            return row
    return None


def _run_cloud_job(
    settings: ApiSettings,
    *,
    job_resource: str,
    job_name: str,
    env: list[Any],
    execution_id: str,
    exclude_gcp_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Start a Cloud Run Job execution and return the linked GCP execution row."""
    jobs_client, executions_client = _require_client()
    exclude = exclude_gcp_ids or set()
    started_after = datetime.now(timezone.utc)
    request = run_v2.RunJobRequest(
        name=job_resource,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(env=env)
            ]
        ),
    )
    try:
        jobs_client.run_job(request=request)
    except Exception as exc:
        raise RuntimeError(f"RunJob failed: {exc}") from exc
    picked: dict[str, Any] | None = None
    for _ in range(12):
        try:
            picked = _pick_new_execution(
                executions_client,
                parent=job_resource,
                job_name=job_name,
                started_after=started_after,
                exclude=exclude,
            )
        except RuntimeError:
            break
        if picked is not None:
            break
        time.sleep(0.4)
    if picked is None:
        return {"execution_id": execution_id, "status": "starting", "name": None}
    data = dict(picked)
    data["api_execution_id"] = execution_id
    data["gcp_execution_id"] = data["execution_id"]
    data["execution_id"] = execution_id
    return data


def start_assembly_job(
    settings: ApiSettings,
    *,
    execution_id: str,
    category: str,
    channel: str | None = None,
    images_folder: str | None = None,
    thumbnail_text: str | None = None,
    duration_min: int | None = None,
    variance_min: int | None = None,
    queue_youtube: bool = True,
    upload_privacy: str | None = None,
    publish_at: str | None = None,
    upload_tags: str | None = None,
    upload_category_id: str | None = None,
    upload_made_for_kids: bool | None = None,
    exclude_gcp_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Start ``music-assemble`` with env overrides for this run."""
    env = [
        run_v2.EnvVar(name="ASSEMBLY_EXECUTION_ID", value=execution_id),
        run_v2.EnvVar(name="ASSEMBLY_CATEGORY", value=category),
    ]
    if images_folder and images_folder.strip():
        env.append(run_v2.EnvVar(name="ASSEMBLY_IMAGES_FOLDER", value=images_folder.strip()))
    if channel:
        env.append(run_v2.EnvVar(name="ASSEMBLY_CHANNEL", value=channel))
    if thumbnail_text:
        env.append(run_v2.EnvVar(name="THUMBNAIL_TEXT", value=thumbnail_text))
    if duration_min is not None:
        env.append(run_v2.EnvVar(name="ASSEMBLY_DURATION_MIN", value=str(duration_min)))
    if variance_min is not None:
        env.append(run_v2.EnvVar(name="ASSEMBLY_VARIANCE_MIN", value=str(variance_min)))
    env.append(
        run_v2.EnvVar(
            name="ASSEMBLY_QUEUE_YOUTUBE",
            value="true" if queue_youtube else "false",
        )
    )
    if upload_privacy:
        env.append(run_v2.EnvVar(name="ASSEMBLY_UPLOAD_PRIVACY", value=upload_privacy))
    if publish_at:
        env.append(run_v2.EnvVar(name="ASSEMBLY_PUBLISH_AT", value=publish_at))
    if upload_tags:
        env.append(run_v2.EnvVar(name="ASSEMBLY_UPLOAD_TAGS", value=upload_tags))
    if upload_category_id:
        env.append(run_v2.EnvVar(name="ASSEMBLY_UPLOAD_CATEGORY_ID", value=upload_category_id))
    if upload_made_for_kids is not None:
        env.append(
            run_v2.EnvVar(
                name="ASSEMBLY_UPLOAD_MADE_FOR_KIDS",
                value="true" if upload_made_for_kids else "false",
            )
        )
    return _run_cloud_job(
        settings,
        job_resource=settings.job_resource,
        job_name=settings.assembly_job_name,
        env=env,
        execution_id=execution_id,
        exclude_gcp_ids=exclude_gcp_ids,
    )


def start_extend_job(
    settings: ApiSettings,
    *,
    execution_id: str,
    category: str,
    max_images: int | None = None,
    force: bool = False,
    exclude_gcp_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Start ``music-extend`` with env overrides for this run."""
    env = [
        run_v2.EnvVar(name="EXTEND_EXECUTION_ID", value=execution_id),
        run_v2.EnvVar(name="ASSEMBLY_CATEGORY", value=category),
    ]
    if max_images is not None:
        env.append(run_v2.EnvVar(name="EXTEND_MAX_IMAGES", value=str(max_images)))
    if force:
        env.append(run_v2.EnvVar(name="EXTEND_FORCE", value="true"))
    return _run_cloud_job(
        settings,
        job_resource=settings.extend_job_resource,
        job_name=settings.extend_job_name,
        env=env,
        execution_id=execution_id,
        exclude_gcp_ids=exclude_gcp_ids,
    )


def list_executions(
    settings: ApiSettings,
    *,
    limit: int = 25,
    status: str | None = None,
    job_resource: str | None = None,
    job_name: str | None = None,
) -> list[dict[str, Any]]:
    resource = job_resource or settings.job_resource
    name = job_name or settings.assembly_job_name
    _, executions_client = _require_client()
    out: list[dict[str, Any]] = []
    try:
        pages = executions_client.list_executions(parent=resource)
    except Exception as exc:
        if gcp_exceptions and isinstance(exc, gcp_exceptions.GoogleAPIError):
            raise _wrap_gcp_error(exc) from exc
        raise
    for execution in pages:
        row = execution_to_dict(execution, job_name=name)
        if status and row["status"] != status:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def cancel_execution(
    settings: ApiSettings,
    execution_id: str,
    *,
    job_resource: str | None = None,
) -> dict[str, Any]:
    """Cancel a running Cloud Run Job execution."""
    resource = job_resource or settings.job_resource
    _, executions_client = _require_client()
    full = (
        f"{resource}/executions/{execution_id}"
        if "/" not in execution_id
        else execution_id
    )
    try:
        executions_client.cancel_execution(name=full)
    except Exception as exc:
        raise RuntimeError(f"CancelExecution failed: {exc}") from exc
    return {"execution_id": execution_id, "status": "cancelled"}


def get_execution(
    settings: ApiSettings,
    execution_id: str,
    *,
    job_resource: str | None = None,
    job_name: str | None = None,
) -> dict[str, Any] | None:
    resource = job_resource or settings.job_resource
    name = job_name or settings.assembly_job_name
    _, executions_client = _require_client()
    full = (
        f"{resource}/executions/{execution_id}"
        if "/" not in execution_id
        else execution_id
    )
    try:
        execution = executions_client.get_execution(name=full)
    except Exception:
        for row in list_executions(
            settings, limit=50, job_resource=resource, job_name=name
        ):
            if row["execution_id"] == execution_id or row["execution_id"].endswith(
                execution_id
            ):
                return row
        return None
    return execution_to_dict(execution, job_name=name)
