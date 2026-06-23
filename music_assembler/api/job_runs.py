"""List R2 job runs (assembly + extend) from ``jobs/*/meta.json``."""

from __future__ import annotations

import json
from typing import Any

from music_assembler.job_progress import read_progress_json


def list_r2_job_runs(
    client,
    bucket: str,
    *,
    id_prefix: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="jobs/", Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            run_id = prefix["Prefix"].strip("/").split("/")[-1]
            if not run_id.startswith(id_prefix):
                continue
            try:
                resp = client.get_object(Bucket=bucket, Key=f"jobs/{run_id}/meta.json")
                meta = json.loads(resp["Body"].read().decode("utf-8"))
            except client.exceptions.ClientError:
                meta = {"execution_id": run_id}
            progress = read_progress_json(client, bucket, run_id)
            runs.append({**meta, "progress": progress})
    runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return runs[:limit]


def load_r2_job_run(client, bucket: str, execution_id: str) -> dict[str, Any] | None:
    """Load one job folder (meta + progress) from R2."""
    try:
        resp = client.get_object(Bucket=bucket, Key=f"jobs/{execution_id}/meta.json")
        meta = json.loads(resp["Body"].read().decode("utf-8"))
    except client.exceptions.ClientError:
        return None
    progress = read_progress_json(client, bucket, execution_id)
    return {**meta, "progress": progress}