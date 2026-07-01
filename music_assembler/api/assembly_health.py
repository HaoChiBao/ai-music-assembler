"""Audit recent assembly jobs — detect false successes and missing outputs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from music_assembler.api import gcp_jobs
from music_assembler.api.config import ApiSettings
from music_assembler.assemble_options import assembly_video_object_key, normalize_channel
from music_assembler.job_progress import write_progress_json
from music_assembler.r2_storage import list_in_flight_background_claims, object_exists


def _video_id_for_run(run: dict[str, Any]) -> str | None:
    prog = run.get("progress") or {}
    video_id = prog.get("video_id")
    if isinstance(video_id, str) and video_id.strip():
        return video_id.strip()
    return None


def _channel_for_run(run: dict[str, Any]) -> str | None:
    ch = run.get("channel") or (run.get("progress") or {}).get("channel")
    if isinstance(ch, str) and ch.strip():
        return normalize_channel(ch)
    return None


def assembly_output_exists(client, bucket: str, *, channel: str, video_id: str) -> bool:
    key = assembly_video_object_key(channel, video_id)
    return object_exists(client, bucket, key)


def verify_assembly_run_output(
    client,
    bucket: str,
    run: dict[str, Any],
) -> dict[str, Any]:
    """Return verification details for one assembly run."""
    execution_id = run.get("execution_id", "")
    prog = run.get("progress") or {}
    status = prog.get("status")
    channel = _channel_for_run(run)
    video_id = _video_id_for_run(run)
    duplicate_claims: list[str] = []

    images_folder = run.get("images_folder") or run.get("category")
    if images_folder and run.get("claimed_background"):
        prefix = f"post-processed/{images_folder}/"
        claims = list_in_flight_background_claims(client, bucket, prefix).get(
            run["claimed_background"], []
        )
        if len(claims) > 1:
            duplicate_claims = [row[0] for row in claims]

    output_ok = False
    missing_reason: str | None = None
    if status == "succeeded":
        if not channel:
            missing_reason = "missing channel in job meta"
        elif not video_id:
            missing_reason = "missing video_id in progress"
        elif not assembly_output_exists(client, bucket, channel=channel, video_id=video_id):
            missing_reason = f"video not found on R2: {video_id}"
        else:
            output_ok = True
    elif status in ("running", "cancelling"):
        missing_reason = "job still running"
    elif status == "failed":
        missing_reason = prog.get("stage") or "job failed"
    else:
        missing_reason = "no terminal status"

    healthy = status == "succeeded" and output_ok and not duplicate_claims
    return {
        "execution_id": execution_id,
        "status": status,
        "channel": channel,
        "video_id": video_id,
        "gcp_execution_id": run.get("gcp_execution_id"),
        "claimed_background": run.get("claimed_background"),
        "duplicate_in_flight_claims": duplicate_claims,
        "output_ok": output_ok,
        "healthy": healthy,
        "issue": None if healthy else (missing_reason or "unhealthy"),
    }


def audit_recent_assemblies(
    settings: ApiSettings,
    client,
    bucket: str,
    runs: list[dict[str, Any]],
    *,
    repair: bool = False,
) -> dict[str, Any]:
    """Check recent assembly runs for missing outputs and stale duplicate claims."""
    checked: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    gcp_by_id: dict[str, dict[str, Any]] = {}
    try:
        gcp_by_id = {
            row["execution_id"]: row
            for row in gcp_jobs.list_executions(settings, limit=50)
        }
    except Exception:
        pass

    for run in runs:
        row = verify_assembly_run_output(client, bucket, run)
        gcp_id = run.get("gcp_execution_id")
        if gcp_id and gcp_id in gcp_by_id:
            row["gcp_status"] = gcp_by_id[gcp_id].get("status")
        checked.append(row)
        if not row["healthy"]:
            issues.append(row)
            if repair and run.get("execution_id"):
                prog = run.get("progress") or {}
                if prog.get("status") == "succeeded" and not row["output_ok"]:
                    write_progress_json(
                        client,
                        bucket,
                        run["execution_id"],
                        pct=float(prog.get("pct") or 0),
                        stage=row["issue"] or "Missing output video on R2",
                        category=run.get("category") or "korean",
                        status="failed",
                        extra={
                            k: prog[k]
                            for k in ("video_id", "channel", "job_type")
                            if prog.get(k)
                        },
                    )
                    row["repaired"] = True

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checked": len(checked),
        "healthy": sum(1 for row in checked if row["healthy"]),
        "issues": issues,
        "runs": checked,
    }
