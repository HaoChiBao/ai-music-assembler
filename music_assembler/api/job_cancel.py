"""Request cancellation for assembly and extend Cloud Run jobs."""

from __future__ import annotations

from typing import Any

from music_assembler.api.config import ApiSettings
from music_assembler.api import gcp_jobs
from music_assembler.job_progress import read_meta_json, read_progress_json, write_progress_json

_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _job_type(execution_id: str, meta: dict[str, Any] | None) -> str:
    if meta and meta.get("job_type"):
        return str(meta["job_type"])
    if execution_id.startswith("ext_"):
        return "extend"
    return "assembly"


def cancel_job_preview(
    client,
    bucket: str,
    execution_id: str,
) -> dict[str, Any]:
    """Return job details for a two-step cancel confirmation in the dashboard."""
    meta = read_meta_json(client, bucket, execution_id)
    if meta is None:
        return {"found": False, "execution_id": execution_id}
    progress = read_progress_json(client, bucket, execution_id)
    status = (progress or {}).get("status", "running")
    job_type = _job_type(execution_id, meta)
    return {
        "found": True,
        "execution_id": execution_id,
        "job_type": job_type,
        "category": meta.get("category"),
        "status": status,
        "stage": (progress or {}).get("stage", ""),
        "can_cancel": status not in _TERMINAL,
        "message": (
            f"Cancel {job_type} job {execution_id}?"
            if status not in _TERMINAL
            else f"Job is already {status}."
        ),
    }


def cancel_job(
    client,
    bucket: str,
    execution_id: str,
    settings: ApiSettings,
) -> dict[str, Any]:
    """Mark a job cancelled on R2 and stop GCP assembly executions when linked."""
    meta = read_meta_json(client, bucket, execution_id)
    if meta is None:
        return {"found": False, "execution_id": execution_id}

    progress = read_progress_json(client, bucket, execution_id)
    status = (progress or {}).get("status", "running")
    if status in _TERMINAL:
        return {
            "found": True,
            "execution_id": execution_id,
            "status": status,
            "already_terminal": True,
        }

    job_type = _job_type(execution_id, meta)
    category = meta.get("category") or settings.default_category
    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=float((progress or {}).get("pct") or 0),
        stage="Cancellation requested…",
        category=category,
        status="cancelling",
        extra={"job_type": job_type},
    )

    gcp_cancelled = False
    gcp_error: str | None = None
    gcp_id = meta.get("gcp_execution_id")
    if gcp_id:
        job_resource = (
            settings.extend_job_resource
            if job_type == "extend"
            else settings.job_resource
        )
        try:
            gcp_jobs.cancel_execution(settings, gcp_id, job_resource=job_resource)
            gcp_cancelled = True
        except Exception as exc:
            gcp_error = str(exc)

    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=float((progress or {}).get("pct") or 0),
        stage="Cancelled",
        category=category,
        status="cancelled",
        extra={"job_type": job_type, "gcp_cancelled": gcp_cancelled},
    )
    out: dict[str, Any] = {
        "found": True,
        "execution_id": execution_id,
        "job_type": job_type,
        "status": "cancelled",
        "gcp_cancelled": gcp_cancelled,
    }
    if gcp_error:
        out["gcp_cancel_error"] = gcp_error
    return out


def make_extend_cancel_checker(client, bucket: str, execution_id: str):
    """Return a callable that reads cancel state from R2 (cached after first hit)."""
    flagged = False

    def should_cancel() -> bool:
        nonlocal flagged
        if flagged:
            return True
        prog = read_progress_json(client, bucket, execution_id)
        if prog and prog.get("status") in ("cancelling", "cancelled"):
            flagged = True
            return True
        return False

    return should_cancel
