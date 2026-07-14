"""List music videos and category inventory from R2."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from music_assembler.assemble_options import normalize_channel, video_output_prefix
from music_assembler.r2_storage import IMAGE_EXTENSIONS, r2_client, r2_config_from_env

_ASSET_POOLS = {
    "pre-processed": lambda cat: f"pre-processed/{cat}/",
    "pre-used": lambda cat: f"pre-processed/{cat}/used/",
    "post-processed": lambda cat: f"post-processed/{cat}/",
    "post-used": lambda cat: f"post-processed/{cat}/used/",
}


def _iso_ts(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _is_skipped_key(key: str) -> bool:
    return key.endswith("/") or key.endswith(".gitkeep")


def _image_name(key: str, prefix: str) -> str | None:
    rel = key[len(prefix) :]
    if not rel or "/" in rel:
        return None
    if not any(rel.lower().endswith(ext.lower()) for ext in IMAGE_EXTENSIONS):
        return None
    return rel


def _read_text_object(client, bucket: str, key: str) -> str | None:
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read().decode("utf-8").strip()
    except client.exceptions.ClientError:
        return None


def _parse_music_video_key(key: str) -> tuple[str | None, str, str] | None:
    """Parse an R2 key into ``(channel, run_id, filename)``.

    Supports:
    - ``music-video/{channel}/mv_*/…`` (current layout)
    - ``music-video/{category}/mv_*/…`` (legacy flat category folder)
    - ``music-video/{category}/{channel}/mv_*/…`` (legacy nested layout)
    """
    if not key.startswith("music-video/") or _is_skipped_key(key):
        return None
    parts = key.split("/")
    if len(parts) < 3:
        return None
    seg1, seg2 = parts[1], parts[2]
    if seg1.startswith("mv_"):
        filename = "/".join(parts[2:])
        return None, seg1, filename
    if seg2.startswith("mv_"):
        filename = "/".join(parts[3:])
        return seg1, seg2, filename
    if len(parts) >= 4 and parts[3].startswith("mv_"):
        filename = "/".join(parts[4:])
        return parts[2], parts[3], filename
    return None


def discover_video_channels(client, bucket: str) -> list[str]:
    """List YouTube channel folders directly under ``music-video/``."""
    seen: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="music-video/", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            name = cp.get("Prefix", "").rstrip("/").split("/")[-1]
            if name and not name.startswith("mv_"):
                seen.add(name)
    return sorted(seen)


def discover_channels(client, bucket: str, category: str | None = None) -> list[str]:
    """List channel folders under ``music-video/`` (``category`` ignored — kept for API compat)."""
    _ = category
    return discover_video_channels(client, bucket)


def _video_run_prefix(channel: str, video_id: str) -> str:
    ch = normalize_channel(channel)
    if not ch:
        raise ValueError("channel is required")
    return f"{video_output_prefix(ch)}{video_id}/"


def _scan_video_runs(
    client,
    bucket: str,
    *,
    channel: str | None = None,
) -> dict[tuple[str | None, str], dict[str, Any]]:
    """Index ``mv_*`` runs under ``music-video/`` (current + legacy layouts)."""
    channel_filter = normalize_channel(channel) if channel else None
    runs: dict[tuple[str | None, str], dict[str, Any]] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="music-video/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parsed = _parse_music_video_key(key)
            if parsed is None:
                continue
            ch, run_id, filename = parsed
            if channel_filter is not None and ch != channel_filter:
                continue
            if not filename:
                continue
            prefix = (
                f"music-video/{ch}/{run_id}/"
                if ch
                else f"music-video/{run_id}/"
            )
            entry = runs.setdefault(
                (ch, run_id),
                {
                    "id": run_id,
                    "channel": ch,
                    "prefix": prefix,
                    "files": {},
                    "last_modified": None,
                },
            )
            entry["files"][filename] = key
            lm = obj.get("LastModified")
            if lm and (entry["last_modified"] is None or lm > entry["last_modified"]):
                entry["last_modified"] = lm
    return runs


def _media_query(channel: str | None, video_id: str) -> str:
    q = f"video_id={video_id}"
    if channel:
        q = f"channel={channel}&" + q
    return q


def _presign(client, bucket: str, key: str, *, expires: int = 3600) -> str | None:
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )
    except Exception:
        return None


def list_categories(client, bucket: str) -> list[str]:
    """Discover genre/category subfolders under ``music/`` (inputs, not video output)."""
    seen: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="music/", Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            p = prefix.get("Prefix", "")
            parts = p.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "music":
                seen.add(parts[1])
    if not seen:
        seen.add(r2_config_from_env().category)
    return sorted(seen)


def list_background_folders(client, bucket: str) -> list[str]:
    """Discover subfolders under ``post-processed/`` (assembly background pools)."""
    seen: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="post-processed/", Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            p = prefix.get("Prefix", "")
            parts = p.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "post-processed":
                seen.add(parts[1])
    if not seen:
        seen.add(r2_config_from_env().category)
    return sorted(seen)


def _is_image_filename(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext.lower()) for ext in IMAGE_EXTENSIONS)


def count_ready_backgrounds(
    client,
    bucket: str,
    *,
    folder: str | None = None,
) -> int:
    """Count claimable background images under ``post-processed/``.

    When ``folder`` is set, only that subfolder is scanned. Otherwise every
    subfolder is included. Files in ``used/`` or ``in-flight/`` are excluded;
    only images sitting directly in a pool folder count (same shape as claimable).
    """
    prefix = f"post-processed/{folder.strip().strip('/')}/" if folder else "post-processed/"
    n = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if _is_skipped_key(key):
                continue
            if "/used/" in key or "/in-flight/" in key:
                continue
            parts = key.split("/")
            # post-processed/{folder}/{filename}
            if len(parts) != 3 or parts[0] != "post-processed":
                continue
            if not _is_image_filename(parts[2]):
                continue
            n += 1
    return n


def category_inventory(client, bucket: str, category: str) -> dict[str, int]:
    """Count objects in each assembly prefix (6 R2 list scans)."""
    prefixes = {
        "music_mp3s": f"music/{category}/",
        "backgrounds_available": f"post-processed/{category}/",
        "backgrounds_in_flight": f"post-processed/{category}/in-flight/",
        "backgrounds_used": f"post-processed/{category}/used/",
        "pre_processed": f"pre-processed/{category}/",
        "music_videos": "music-video/",
    }
    counts: dict[str, int] = {}
    for label, prefix in prefixes.items():
        if label == "backgrounds_available":
            counts[label] = count_ready_backgrounds(client, bucket, folder=category)
            continue
        n = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if _is_skipped_key(key):
                    continue
                if label == "pre_processed" and "/used/" in key:
                    continue
                if label == "music_mp3s":
                    name = key[len(prefix) :]
                    if not name or "/" in name or not name.lower().endswith(".mp3"):
                        continue
                if label == "music_videos":
                    # Count finished video runs (…/mv_*_video.mp4), not every artifact.
                    if not key.endswith("_video.mp4"):
                        continue
                n += 1
        counts[label] = n
    return counts


def dashboard_inventory(client, bucket: str, category: str) -> dict[str, int]:
    """Category inventory plus dashboard chip aliases (all-folder backgrounds ready)."""
    inv = category_inventory(client, bucket, category)
    backgrounds_ready = count_ready_backgrounds(client, bucket)
    return {
        **inv,
        "backgrounds_ready": backgrounds_ready,
        "post-processed": backgrounds_ready,
        "music": inv.get("music_mp3s", 0),
        "music-video": inv.get("music_videos", 0),
    }


def list_video_summaries(
    client,
    bucket: str,
    *,
    channel: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List ``mv_*`` folders without reading title/description objects."""
    runs = _scan_video_runs(client, bucket, channel=channel)
    out: list[dict[str, Any]] = []
    for (_ch, run_id) in sorted(runs.keys(), key=lambda k: k[1], reverse=True)[:limit]:
        entry = runs[(_ch, run_id)]
        files = entry["files"]
        video_key = files.get(f"{run_id}_video.mp4")
        thumb_key = next((files[k] for k in files if k.endswith("_thumbnail.png")), None)
        out.append(
            {
                "id": run_id,
                "channel": _ch,
                "has_video": video_key is not None,
                "has_thumbnail": thumb_key is not None,
                "has_title": f"{run_id}_title.txt" in files,
                "has_description": f"{run_id}_description.txt" in files,
                "has_tracklist": f"{run_id}_tracklist.txt" in files,
                "file_count": len(files),
                "last_modified": _iso_ts(entry["last_modified"]),
                "r2_prefix": entry["prefix"],
            }
        )
    return out


def list_videos(
    client,
    bucket: str,
    *,
    channel: str | None = None,
    limit: int = 50,
    stable_media_urls: bool = False,
    summary_only: bool = False,
) -> list[dict[str, Any]]:
    """List ``mv_*`` run folders under ``music-video/{channel}/``."""
    if summary_only:
        return list_video_summaries(client, bucket, channel=channel, limit=limit)
    runs = _scan_video_runs(client, bucket, channel=channel)
    out: list[dict[str, Any]] = []
    for (_ch, run_id) in sorted(runs.keys(), key=lambda k: k[1], reverse=True)[:limit]:
        entry = runs[(_ch, run_id)]
        files = entry["files"]
        title = _read_text_object(client, bucket, files.get(f"{run_id}_title.txt", ""))
        description = _read_text_object(
            client, bucket, files.get(f"{run_id}_description.txt", "")
        )
        video_key = files.get(f"{run_id}_video.mp4")
        thumb_key = next(
            (files[k] for k in files if k.endswith("_thumbnail.png")),
            None,
        )
        media_q = _media_query(_ch, run_id)
        if stable_media_urls and thumb_key:
            thumb_url = f"/v1/media/thumbnail?{media_q}"
        else:
            thumb_url = _presign(client, bucket, thumb_key) if thumb_key else None
        video_url = (
            f"/v1/media/video?{media_q}"
            if stable_media_urls and video_key
            else (_presign(client, bucket, video_key) if video_key else None)
        )
        out.append(
            {
                "id": run_id,
                "channel": _ch,
                "title": title,
                "description": description,
                "description_preview": (description or "")[:200] if description else None,
                "has_video": video_key is not None,
                "has_thumbnail": thumb_key is not None,
                "video_url": video_url,
                "thumbnail_url": thumb_url,
                "r2_prefix": entry["prefix"],
            }
        )
    return out


def get_video(
    client,
    bucket: str,
    *,
    video_id: str,
    channel: str | None = None,
) -> dict[str, Any] | None:
    ch = normalize_channel(channel) if channel else None
    runs = _scan_video_runs(client, bucket, channel=ch)
    entry = runs.get((ch, video_id))
    if entry is None and ch is None:
        for (run_ch, run_id), candidate in runs.items():
            if run_id == video_id:
                entry = candidate
                ch = run_ch
                break
    if entry is None:
        return None

    files = entry["files"]
    resolved_prefix = entry["prefix"]
    resolved_channel = ch or entry.get("channel")

    title = _read_text_object(client, bucket, files.get(f"{video_id}_title.txt", ""))
    description = _read_text_object(
        client, bucket, files.get(f"{video_id}_description.txt", "")
    )
    tracklist = _read_text_object(client, bucket, files.get(f"{video_id}_tracklist.txt", ""))
    video_key = files.get(f"{video_id}_video.mp4")
    thumb_key = next((files[k] for k in files if k.endswith("_thumbnail.png")), None)

    file_rows = []
    for name, key in sorted(files.items()):
        kind = "other"
        if name.endswith(".mp4"):
            kind = "video"
        elif name.endswith("_thumbnail.png"):
            kind = "thumbnail"
        elif name.endswith(".txt"):
            kind = "text"
        elif name.endswith(".mp3"):
            kind = "audio"
        elif name.endswith(".png"):
            kind = "image"
        file_rows.append({"name": name, "key": key, "kind": kind, "size": None})

    media_q = _media_query(resolved_channel, video_id)
    return {
        "id": video_id,
        "channel": resolved_channel,
        "title": title,
        "description": description,
        "tracklist": tracklist,
        "has_video": video_key is not None,
        "has_thumbnail": thumb_key is not None,
        "video_url": f"/v1/media/video?{media_q}",
        "thumbnail_url": f"/v1/media/thumbnail?{media_q}",
        "r2_prefix": resolved_prefix,
        "files": file_rows,
    }


def find_thumbnail_key(
    client,
    bucket: str,
    *,
    video_id: str,
    channel: str | None = None,
) -> str | None:
    row = get_video(client, bucket, video_id=video_id, channel=channel)
    if not row:
        return None
    return next(
        (f["key"] for f in row.get("files", []) if f.get("name", "").endswith("_thumbnail.png")),
        None,
    )


def find_video_key(
    client,
    bucket: str,
    *,
    video_id: str,
    channel: str | None = None,
) -> str | None:
    row = get_video(client, bucket, video_id=video_id, channel=channel)
    if not row:
        return None
    return next(
        (f["key"] for f in row.get("files", []) if f.get("name", "").endswith("_video.mp4")),
        None,
    )


def _asset_folder(category: str, pool: str, images_folder: str | None = None) -> str:
    """Folder segment for asset pools — ``images_folder`` for post-processed backgrounds."""
    if pool in ("post-processed", "post-used") and images_folder and str(images_folder).strip():
        return str(images_folder).strip().strip("/")
    return category


def list_assets(
    client,
    bucket: str,
    *,
    category: str,
    pool: str,
    limit: int = 500,
    images_folder: str | None = None,
) -> list[dict[str, Any]]:
    """List image metadata for a pre/post-processed pool (no image bytes)."""
    if pool not in _ASSET_POOLS:
        raise ValueError(f"Unknown pool: {pool}")
    folder = _asset_folder(category, pool, images_folder)
    prefix = _ASSET_POOLS[pool](folder)
    rows: list[dict[str, Any]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if _is_skipped_key(key):
                continue
            name = _image_name(key, prefix)
            if not name:
                continue
            rows.append(
                {
                    "name": name,
                    "size": obj.get("Size"),
                    "modified": _iso_ts(obj.get("LastModified")),
                    "pool": pool,
                }
            )
    rows.sort(key=lambda r: r.get("modified") or "", reverse=True)
    return rows[:limit]


def asset_object_key(
    category: str,
    pool: str,
    name: str,
    *,
    images_folder: str | None = None,
) -> str:
    if pool not in _ASSET_POOLS:
        raise ValueError(f"Unknown pool: {pool}")
    if "/" in name or ".." in name:
        raise ValueError("Invalid asset name")
    folder = _asset_folder(category, pool, images_folder)
    return _ASSET_POOLS[pool](folder) + name
