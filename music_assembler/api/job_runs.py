"""List R2 job runs (assembly + extend) from ``jobs/*/meta.json``."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from music_assembler.job_progress import read_progress_json


def _load_run(client, bucket: str, run_id: str) -> dict[str, Any]:
    try:
        resp = client.get_object(Bucket=bucket, Key=f"jobs/{run_id}/meta.json")
        meta = json.loads(resp["Body"].read().decode("utf-8"))
    except client.exceptions.ClientError:
        meta = {"execution_id": run_id}
    progress = read_progress_json(client, bucket, run_id)
    return {**meta, "progress": progress}


def list_r2_job_runs(
    client,
    bucket: str,
    *,
    id_prefix: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Return the most recent job runs for ``id_prefix`` (parallel R2 reads).

    Always includes any still-running jobs even when they fall outside the
    ``limit`` window so long batches stay visible on the dashboard.
    """
    run_ids: list[str] = []
    list_prefix = f"jobs/{id_prefix}"
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix, Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            run_id = prefix["Prefix"].strip("/").split("/")[-1]
            if run_id.startswith(id_prefix):
                run_ids.append(run_id)

    run_ids.sort(reverse=True)
    recent = run_ids[: max(limit, 0)]
    extra_running: list[str] = []
    if len(run_ids) > len(recent):
        tail = run_ids[len(recent) : len(recent) + 200]
        workers = min(8, len(tail)) or 1

        def _is_active(run_id: str) -> bool:
            prog = read_progress_json(client, bucket, run_id)
            return bool(prog and prog.get("status") in ("running", "cancelling"))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for run_id, active in zip(tail, pool.map(_is_active, tail)):
                if active:
                    extra_running.append(run_id)

    to_load = list(dict.fromkeys(recent + extra_running))
    if not to_load:
        return []

    workers = min(8, len(to_load))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        runs = list(pool.map(lambda rid: _load_run(client, bucket, rid), to_load))

    runs.sort(key=lambda r: r.get("created_at") or r.get("execution_id", ""), reverse=True)
    return runs


def load_r2_job_run(client, bucket: str, execution_id: str) -> dict[str, Any] | None:
    """Load one job folder (meta + progress) from R2."""
    try:
        resp = client.get_object(Bucket=bucket, Key=f"jobs/{execution_id}/meta.json")
        meta = json.loads(resp["Body"].read().decode("utf-8"))
    except client.exceptions.ClientError:
        return None
    progress = read_progress_json(client, bucket, execution_id)
    return {**meta, "progress": progress}
