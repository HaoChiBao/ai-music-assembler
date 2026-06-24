"""R2 job progress blobs — shared by the Cloud Run worker and the control API."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

PROGRESS_PREFIX = "jobs/"


def progress_key(execution_id: str) -> str:
    return f"{PROGRESS_PREFIX}{execution_id}/progress.json"


def meta_key(execution_id: str) -> str:
    return f"{PROGRESS_PREFIX}{execution_id}/meta.json"


def write_progress_json(
    client,
    bucket: str,
    execution_id: str,
    *,
    pct: float,
    stage: str,
    category: str,
    status: str = "running",
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "execution_id": execution_id,
        "category": category,
        "pct": round(float(pct), 1),
        "stage": stage,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        client.put_object(
            Bucket=bucket,
            Key=progress_key(execution_id),
            Body=body,
            ContentType="application/json",
            CacheControl="no-cache",
        )
    except Exception as exc:
        print(
            f"warning: failed to write progress for {execution_id}: {exc}",
            file=sys.stderr,
        )
        raise


def read_progress_json(client, bucket: str, execution_id: str) -> dict[str, Any] | None:
    key = progress_key(execution_id)
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except client.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def read_meta_json(client, bucket: str, execution_id: str) -> dict[str, Any] | None:
    key = meta_key(execution_id)
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except client.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def patch_meta_gcp_execution_id(
    client,
    bucket: str,
    execution_id: str,
    gcp_execution_id: str,
) -> None:
    meta = read_meta_json(client, bucket, execution_id) or {"execution_id": execution_id}
    if meta.get("gcp_execution_id") == gcp_execution_id:
        return
    meta["gcp_execution_id"] = gcp_execution_id
    client.put_object(
        Bucket=bucket,
        Key=meta_key(execution_id),
        Body=json.dumps(meta).encode("utf-8"),
        ContentType="application/json",
    )


def write_meta_json(
    client,
    bucket: str,
    execution_id: str,
    *,
    category: str,
    duration_min: int | None = None,
    variance_min: int | None = None,
    thumbnail_text: str | None = None,
    gcp_execution_id: str | None = None,
    claimed_background: str | None = None,
    job_type: str | None = None,
    limit: int | None = None,
    process_all: bool | None = None,
    channel: str | None = None,
    images_folder: str | None = None,
) -> None:
    existing = read_meta_json(client, bucket, execution_id)
    created_at = (existing or {}).get("created_at") or datetime.now(timezone.utc).isoformat()
    payload = {
        "execution_id": execution_id,
        "category": category,
        "channel": channel,
        "images_folder": images_folder,
        "duration_min": duration_min,
        "variance_min": variance_min,
        "thumbnail_text": thumbnail_text,
        "gcp_execution_id": gcp_execution_id,
        "claimed_background": claimed_background,
        "job_type": job_type,
        "limit": limit,
        "process_all": process_all,
        "created_at": created_at,
    }
    client.put_object(
        Bucket=bucket,
        Key=meta_key(execution_id),
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )
