"""Client for the youtube-uploader control API (channels + upload queue)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from music_assembler.api.config import ApiSettings

logger = logging.getLogger(__name__)


def r2_object_uri(bucket: str, key: str) -> str:
    """S3-style URI for an R2 object (youtube-uploader reads via boto3)."""
    return f"s3://{bucket}/{key.lstrip('/')}"


def uploader_credentials_from_env() -> tuple[str | None, str | None]:
    """``(UPLOADER_API_URL, UPLOADER_API_KEY)`` for workers and scripts."""
    url = os.environ.get("UPLOADER_API_URL", "").strip() or None
    key = os.environ.get("UPLOADER_API_KEY", "").strip() or None
    return url, key


def resolve_queue_youtube(cli_value: bool | None) -> bool:
    """CLI ``--queue-youtube`` / ``--no-queue-youtube`` with ``ASSEMBLY_QUEUE_YOUTUBE`` fallback.

    Default is **on** unless ``ASSEMBLY_QUEUE_YOUTUBE=false`` (or CLI ``--no-queue-youtube``).
    """
    if cli_value is not None:
        return cli_value
    raw = os.environ.get("ASSEMBLY_QUEUE_YOUTUBE", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return True


def fetch_uploader_channels(settings: ApiSettings) -> list[dict[str, Any]]:
    """Return channel rows from ``GET /v1/channels`` on the uploader API."""
    base = (settings.uploader_api_url or "").strip().rstrip("/")
    key = (settings.uploader_api_key or "").strip()
    if not base or not key:
        return []

    url = f"{base}/v1/channels"
    req = urllib.request.Request(
        url,
        headers={"X-API-Key": key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("uploader channels HTTP %s: %s", exc.code, url)
        return []
    except Exception as exc:
        logger.warning("uploader channels failed: %s", exc)
        return []

    rows: list[dict[str, Any]] = []
    for item in payload.get("channels") or []:
        if not isinstance(item, dict):
            continue
        channel_id = (item.get("id") or item.get("channel_ref") or "").strip()
        if not channel_id:
            continue
        auth = item.get("auth") if isinstance(item.get("auth"), dict) else {}
        rows.append(
            {
                "id": channel_id,
                "name": (item.get("name") or channel_id).strip(),
                "youtube_channel_id": item.get("youtube_channel_id"),
                "custom_url": item.get("custom_url"),
                "auth_valid": bool(auth.get("valid")),
                "source": "uploader",
            }
        )
    return rows


def merge_channel_list(
    *,
    uploader_channels: list[dict[str, Any]],
    configured: tuple[str, ...],
    discovered: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Merge uploader, config, and R2 channels; return ids + detail rows sorted by name."""
    details_by_id: dict[str, dict[str, Any]] = {}

    for row in uploader_channels:
        details_by_id[row["id"]] = dict(row)

    for channel_id in configured:
        if channel_id not in details_by_id:
            details_by_id[channel_id] = {
                "id": channel_id,
                "name": channel_id,
                "source": "configured",
            }

    for channel_id in discovered:
        if channel_id not in details_by_id:
            details_by_id[channel_id] = {
                "id": channel_id,
                "name": channel_id,
                "source": "r2",
            }

    channel_details = sorted(
        details_by_id.values(),
        key=lambda row: ((row.get("name") or row["id"]).lower(), row["id"]),
    )
    return [row["id"] for row in channel_details], channel_details


def register_youtube_upload(
    *,
    api_url: str,
    api_key: str,
    channel: str,
    title: str,
    video_uri: str,
    description: str = "",
    thumbnail_uri: str = "",
    job_id: str | None = None,
    tags: list[str] | None = None,
    privacy: str | None = None,
    publish_at: str | None = None,
    category_id: str | None = None,
    made_for_kids: bool | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    """Register a finished video with ``POST /v1/channels/{channel}/jobs/register``."""
    base = api_url.strip().rstrip("/")
    channel_ref = channel.strip()
    if not base or not api_key.strip():
        raise ValueError("UPLOADER_API_URL and UPLOADER_API_KEY are required")
    if not channel_ref:
        raise ValueError("channel is required to register a YouTube upload job")
    if not title.strip():
        raise ValueError("title is required to register a YouTube upload job")
    if not video_uri.strip():
        raise ValueError("video_uri is required to register a YouTube upload job")

    payload: dict[str, Any] = {
        "title": title.strip(),
        "video_uri": video_uri.strip(),
    }
    if description.strip():
        payload["description"] = description.strip()
    if thumbnail_uri.strip():
        payload["thumbnail_uri"] = thumbnail_uri.strip()
    if job_id:
        payload["job_id"] = job_id
    if tags:
        payload["tags"] = tags
    if privacy:
        payload["privacy"] = privacy.strip().lower()
    if publish_at:
        payload["publish_at"] = publish_at.strip()
    if category_id:
        payload["category_id"] = str(category_id).strip()
    if made_for_kids is not None:
        payload["made_for_kids"] = bool(made_for_kids)

    url = f"{base}/v1/channels/{channel_ref}/jobs/register"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "X-API-Key": api_key.strip(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"uploader register HTTP {exc.code}: {detail}") from exc
