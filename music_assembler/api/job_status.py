"""Reconcile assembly job status from R2 progress + Cloud Run execution state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from music_assembler.api import gcp_jobs
from music_assembler.api.config import ApiSettings
from music_assembler.assemble_options import assembly_video_object_key, normalize_channel
from music_assembler.job_progress import patch_meta_gcp_execution_id, write_progress_json
from music_assembler.r2_storage import object_exists

_SYNC_PHASE_SEC = 300.0
_ENCODE_PHASE_SEC = 4500.0
# Worker writes real encode progress at ~12%+; below this is API heartbeat only.
_R2_TRUST_PCT = 12.0
_TERMINAL_R2 = frozenset({"succeeded", "failed", "cancelled"})


def _assembly_output_verified(
    client,
    bucket: str,
    run: dict[str, Any],
    prog: dict[str, Any] | None,
) -> bool:
    if not prog or prog.get("status") != "succeeded":
        return False
    channel = run.get("channel") or prog.get("channel")
    video_id = prog.get("video_id")
    if not channel or not video_id:
        return False
    ch = normalize_channel(str(channel))
    if not ch:
        return False
    return object_exists(
        client,
        bucket,
        assembly_video_object_key(ch, str(video_id)),
    )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _match_gcp_by_time(
    meta_created: str | None,
    gcp_rows: list[dict[str, Any]],
    *,
    max_delta_sec: float = 120.0,
    exclude: set[str] | None = None,
) -> dict[str, Any] | None:
    meta_dt = _parse_ts(meta_created)
    if meta_dt is None:
        return None
    taken = exclude or set()
    best: dict[str, Any] | None = None
    best_delta: float | None = None
    for row in gcp_rows:
        gcp_id = row.get("execution_id")
        if gcp_id in taken:
            continue
        gcp_dt = _parse_ts(row.get("create_time"))
        if gcp_dt is None:
            continue
        delta = abs((gcp_dt - meta_dt).total_seconds())
        if delta <= max_delta_sec and (best_delta is None or delta < best_delta):
            best = row
            best_delta = delta
    return best


def _estimate_running_pct(gcp_row: dict[str, Any]) -> tuple[float, str]:
    start_dt = _parse_ts(gcp_row.get("start_time") or gcp_row.get("create_time"))
    if start_dt is None:
        return 2.0, "Starting on Cloud Run…"
    elapsed = max(0.0, (datetime.now(timezone.utc) - start_dt).total_seconds())
    if elapsed < _SYNC_PHASE_SEC:
        pct = min(12.0, 2.0 + (elapsed / _SYNC_PHASE_SEC) * 10.0)
        return pct, "Syncing inputs from R2…"
    encode_elapsed = elapsed - _SYNC_PHASE_SEC
    pct = min(92.0, 12.0 + (encode_elapsed / _ENCODE_PHASE_SEC) * 80.0)
    return pct, "Encoding on Cloud Run…"


def _normalize_from_run(run: dict[str, Any]) -> dict[str, Any]:
    prog = run.get("progress") or {}
    status = prog.get("status") or "running"
    return {
        "execution_id": run.get("execution_id", ""),
        "gcp_execution_id": run.get("gcp_execution_id"),
        "category": run.get("category"),
        "status": status,
        "pct": float(prog.get("pct") or 0),
        "stage": prog.get("stage") or "",
        "updated_at": prog.get("updated_at"),
        "created_at": run.get("created_at"),
        "status_source": "r2" if prog else "default",
    }


def reconcile_assembly_runs(
    settings: ApiSettings,
    client,
    bucket: str,
    runs: list[dict[str, Any]],
    *,
    patch_r2: bool = True,
) -> list[dict[str, Any]]:
    """Merge fresh R2 ``progress.json`` with Cloud Run execution status."""
    if not runs:
        return []

    gcp_rows: list[dict[str, Any]] = []
    gcp_by_id: dict[str, dict[str, Any]] = {}
    try:
        gcp_rows = gcp_jobs.list_executions(settings, limit=50)
        gcp_by_id = {row["execution_id"]: row for row in gcp_rows}
    except Exception:
        pass

    linked_gcp: set[str] = {
        r["gcp_execution_id"] for r in runs if r.get("gcp_execution_id")
    }

    out: list[dict[str, Any]] = []
    for run in runs:
        row = _normalize_from_run(run)
        prog = run.get("progress")
        r2_status = (prog or {}).get("status")
        terminal = r2_status in _TERMINAL_R2
        needs_gcp = not terminal

        gcp_row: dict[str, Any] | None = None
        gcp_id = run.get("gcp_execution_id")
        if gcp_id and gcp_id in gcp_by_id:
            gcp_row = gcp_by_id[gcp_id]
        elif needs_gcp and gcp_rows:
            gcp_row = _match_gcp_by_time(
                run.get("created_at"),
                gcp_rows,
                exclude=linked_gcp,
            )
            if gcp_row:
                row["gcp_execution_id"] = gcp_row["execution_id"]
                linked_gcp.add(gcp_row["execution_id"])
                if patch_r2 and run.get("execution_id"):
                    patch_meta_gcp_execution_id(
                        client,
                        bucket,
                        run["execution_id"],
                        gcp_row["execution_id"],
                    )

        if gcp_row and needs_gcp:
            gcp_status = gcp_row.get("status")
            r2_pct = float(prog.get("pct") or 0) if prog else 0.0
            if gcp_status == "running":
                if prog is not None and r2_pct >= _R2_TRUST_PCT:
                    row["status_source"] = "r2"
                else:
                    est_pct, est_stage = _estimate_running_pct(gcp_row)
                    row["pct"] = max(r2_pct, est_pct)
                    if not row["stage"] or r2_pct < _R2_TRUST_PCT:
                        row["stage"] = est_stage if r2_pct < _R2_TRUST_PCT else row["stage"]
                    row["status"] = "running"
                    row["status_source"] = (
                        "r2" if prog is not None and r2_pct >= _R2_TRUST_PCT else "gcp_estimate"
                    )
            elif gcp_status in ("succeeded", "failed"):
                if r2_status == "failed":
                    row["status"] = "failed"
                    row["status_source"] = "r2"
                    row["stage"] = row["stage"] or (prog or {}).get("stage") or "Failed"
                elif gcp_status == "failed":
                    row["status"] = "failed"
                    row["status_source"] = "gcp"
                    row["stage"] = row["stage"] or "Failed on Cloud Run"
                elif _assembly_output_verified(client, bucket, run, prog):
                    row["status"] = "succeeded"
                    row["status_source"] = "r2"
                    row["pct"] = max(r2_pct, 100.0)
                    row["stage"] = row["stage"] or "Complete"
                elif prog is not None and r2_pct >= 100.0 and r2_status == "succeeded":
                    row["status"] = "failed"
                    row["status_source"] = "verify"
                    row["stage"] = "Cloud Run finished but output video missing on R2"
                elif prog is not None and r2_pct >= _R2_TRUST_PCT:
                    row["status"] = row["status"] or "running"
                    row["status_source"] = "r2"
                    row["stage"] = row["stage"] or (prog or {}).get("stage") or "Finishing…"
                else:
                    row["status"] = "running"
                    row["status_source"] = "gcp_wait_output"
                    row["stage"] = row["stage"] or "Cloud Run finished; waiting for R2 output…"
                row["updated_at"] = gcp_row.get("completion_time") or row.get("updated_at")
                if (
                    patch_r2
                    and run.get("execution_id")
                    and row["status"] in ("succeeded", "failed")
                    and row["status"] != r2_status
                ):
                    write_progress_json(
                        client,
                        bucket,
                        run["execution_id"],
                        pct=row["pct"],
                        stage=row["stage"],
                        category=run.get("category") or "korean",
                        status=row["status"],
                        extra={
                            k: prog[k]
                            for k in ("video_id", "channel", "job_type")
                            if prog and prog.get(k)
                        }
                        if prog
                        else None,
                    )

        elif needs_gcp and prog is None and gcp_row is None:
            row["status"] = "unknown"
            row["status_source"] = "none"
            row["stage"] = row["stage"] or "No progress on R2"

        out.append(row)
    return out


def reconcile_extend_runs(
    settings: ApiSettings,
    client,
    bucket: str,
    runs: list[dict[str, Any]],
    *,
    patch_r2: bool = True,
) -> list[dict[str, Any]]:
    """Merge R2 extend progress with Cloud Run execution status."""
    if not runs:
        return []

    gcp_rows: list[dict[str, Any]] = []
    gcp_by_id: dict[str, dict[str, Any]] = {}
    if settings.extend_use_gcp:
        try:
            gcp_rows = gcp_jobs.list_executions(
                settings,
                limit=50,
                job_resource=settings.extend_job_resource,
                job_name=settings.extend_job_name,
            )
            gcp_by_id = {row["execution_id"]: row for row in gcp_rows}
        except Exception:
            pass

    linked_gcp: set[str] = {
        r["gcp_execution_id"] for r in runs if r.get("gcp_execution_id")
    }

    out: list[dict[str, Any]] = []
    for run in runs:
        row = _normalize_from_run(run)
        prog = run.get("progress")
        terminal = row["status"] in ("succeeded", "failed", "cancelled")
        needs_gcp = not terminal

        gcp_row: dict[str, Any] | None = None
        gcp_id = run.get("gcp_execution_id")
        if gcp_id and gcp_id in gcp_by_id:
            gcp_row = gcp_by_id[gcp_id]
        elif needs_gcp and gcp_rows:
            gcp_row = _match_gcp_by_time(
                run.get("created_at"),
                gcp_rows,
                exclude=linked_gcp,
            )
            if gcp_row:
                row["gcp_execution_id"] = gcp_row["execution_id"]
                linked_gcp.add(gcp_row["execution_id"])
                if patch_r2 and run.get("execution_id"):
                    patch_meta_gcp_execution_id(
                        client,
                        bucket,
                        run["execution_id"],
                        gcp_row["execution_id"],
                    )

        if gcp_row and needs_gcp:
            gcp_status = gcp_row.get("status")
            r2_pct = float(prog.get("pct") or 0) if prog else 0.0
            if gcp_status == "running":
                if prog is not None and r2_pct >= _R2_TRUST_PCT:
                    row["status_source"] = "r2"
                else:
                    est_pct, est_stage = _estimate_running_pct(gcp_row)
                    row["pct"] = max(r2_pct, est_pct)
                    if not row["stage"] or r2_pct < _R2_TRUST_PCT:
                        row["stage"] = est_stage if r2_pct < _R2_TRUST_PCT else row["stage"]
                    row["status"] = "running"
                    row["status_source"] = (
                        "r2" if prog is not None and r2_pct >= _R2_TRUST_PCT else "gcp_estimate"
                    )
            elif gcp_status in ("succeeded", "failed"):
                row["status"] = gcp_status
                row["status_source"] = "gcp"
                if gcp_status == "succeeded":
                    row["pct"] = max(r2_pct, 100.0)
                    row["stage"] = row["stage"] or "Complete"
                else:
                    row["stage"] = row["stage"] or "Failed on Cloud Run"
                row["updated_at"] = gcp_row.get("completion_time") or row.get("updated_at")
                if patch_r2 and run.get("execution_id") and prog is None:
                    write_progress_json(
                        client,
                        bucket,
                        run["execution_id"],
                        pct=row["pct"],
                        stage=row["stage"],
                        category=run.get("category") or "korean",
                        status=gcp_status,
                        extra={"job_type": "extend"},
                    )

        elif needs_gcp and prog is None and gcp_row is None:
            row["status"] = "unknown"
            row["status_source"] = "none"
            row["stage"] = "No progress on R2"
        elif row["status"] == "cancelling":
            row["stage"] = row["stage"] or "Cancelling…"

        out.append(row)
    return out


def has_running_jobs(runs: list[dict[str, Any]]) -> bool:
    return any(r.get("status") in ("running", "cancelling") for r in runs)
