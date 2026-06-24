"""Background extend runs triggered by the control API (local / in-process fallback)."""

from __future__ import annotations

import os
import threading
import traceback
from typing import Any

from music_assembler.api.job_cancel import make_extend_cancel_checker
from music_assembler.job_progress import write_progress_json
from music_assembler.extend_from_r2 import run_extend_cloud_worker
from music_assembler.r2_storage import r2_client, r2_config_from_env


def run_extend_job(
    execution_id: str,
    *,
    category: str,
    max_images: int | None = 1,
    force: bool = False,
) -> None:
    """Run extend worker loop in-process and write progress to R2 ``jobs/{execution_id}/``."""
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
                extra={"job_type": "extend", "host": "local"},
            )

    on_progress(0, "Starting locally…")
    try:
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            raise RuntimeError("GEMINI_API_KEY is not configured")

        result: dict[str, Any] = run_extend_cloud_worker(
            execution_id,
            category=category,
            max_images=max_images,
            force=force,
            should_cancel=should_cancel,
            on_progress=lambda pct, stage: on_progress(pct, stage),
        )
        if result.get("cancelled"):
            on_progress(0, "Cancelled", status="cancelled")
            return
        if result.get("empty"):
            on_progress(100, "No pending images", status="succeeded")
            return
        ok = int(result.get("ok", 0))
        failed = int(result.get("failed", 0))
        if ok == 0 and failed > 0:
            on_progress(100, f"Failed ({failed} image(s))", status="failed")
            return
        stage = f"Done — extended {ok} image(s)"
        if failed:
            stage += f", failed {failed}"
        on_progress(100, stage, status="succeeded")
    except Exception as exc:
        write_progress_json(
            client,
            bucket,
            execution_id,
            pct=0,
            stage=str(exc),
            category=category,
            status="failed",
            extra={"job_type": "extend", "host": "local", "error": traceback.format_exc()},
        )
