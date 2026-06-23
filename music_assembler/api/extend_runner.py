"""Background extend-from-r2 runs triggered by the control API."""

from __future__ import annotations

import os
import threading
import traceback
from typing import Any

from music_assembler.api.job_cancel import make_extend_cancel_checker
from music_assembler.job_progress import write_progress_json
from music_assembler.extend_from_r2 import run_extend_from_r2
from music_assembler.r2_storage import r2_client, r2_config_from_env


def run_extend_job(
    execution_id: str,
    *,
    category: str,
    limit: int | None = 1,
    process_all: bool = False,
    force: bool = False,
    source_keys: list[str] | None = None,
) -> None:
    """Run extend-from-r2 and write progress to R2 ``jobs/{execution_id}/``."""
    cfg = r2_config_from_env(category=category)
    client = r2_client(cfg)
    bucket = cfg.bucket
    should_cancel = make_extend_cancel_checker(client, bucket, execution_id)
    progress_lock = threading.Lock()

    def on_progress(pct: float, stage: str, *, status: str = "running") -> None:
        if should_cancel():
            status = "cancelled"
            stage = "Cancelled"
        with progress_lock:
            write_progress_json(
                client,
                bucket,
                execution_id,
                pct=pct,
                stage=stage,
                category=category,
                status=status,
                extra={"job_type": "extend"},
            )

    on_progress(0, "Starting…")
    try:
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            raise RuntimeError("GEMINI_API_KEY is not configured on the API service")

        result: dict[str, Any] = run_extend_from_r2(
            category=category,
            limit=1,
            process_all=False,
            force=force,
            source_keys=source_keys,
            should_cancel=should_cancel,
            workers=1,
            on_progress=on_progress,
        )
        if result.get("cancelled"):
            on_progress(float(result.get("pct") or 0), "Cancelled", status="cancelled")
            return
        ok = int(result.get("ok", 0))
        failed = int(result.get("failed", 0))
        if ok == 0 and failed > 0:
            status = "failed"
            stage = f"Failed ({failed} image(s))"
        elif failed > 0:
            status = "succeeded"
            stage = f"Done — extended {ok}, failed {failed}"
        else:
            status = "succeeded"
            stage = f"Done — extended {ok} image(s)"
        on_progress(100, stage, status=status)
    except Exception as exc:
        write_progress_json(
            client,
            bucket,
            execution_id,
            pct=0,
            stage=str(exc),
            category=category,
            status="failed",
            extra={"job_type": "extend", "error": traceback.format_exc()},
        )