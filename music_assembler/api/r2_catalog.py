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


def _parse_video_relative(rel: str) -> tuple[str | None, str] | None:
    """Return ``(channel, run_id)`` for a key relative to ``music-video/{category}/``."""
    if not rel or rel.endswith("/"):
        return None
    parts = rel.split("/")
    if parts[0].startswith("mv_"):
        return None, parts[0]
    if len(parts) >= 2 and parts[1].startswith("mv_"):
        return parts[0], parts[1]
    return None


def discover_channels(client, bucket: str, category: str) -> list[str]:
    """List channel subfolders under ``music-video/{category}/`` (excludes legacy ``mv_*``)."""
    seen: set[str] = set()
    prefix = f"music-video/{category.strip().strip('/')}/"
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            name = cp.get("Prefix", "").rstrip("/").split("/")[-1]
            if name and not name.startswith("mv_"):
                seen.add(name)
    return sorted(seen)


def _video_run_prefix(category: str, video_id: str, channel: str | None) -> str:
    ch = normalize_channel(channel) if channel else None
    return f"{video_output_prefix(category, ch)}{video_id}/"


def _scan_video_runs(
    client,
    bucket: str,
    *,
    category: str,
    channel: str | None = None,
) -> dict[tuple[str | None, str], dict[str, Any]]:
    """Index ``mv_*`` runs under ``music-video/{category}/`` (legacy + channel subfolders)."""
    base = f"music-video/{category.strip().strip('/')}/"
    channel_filter = normalize_channel(channel) if channel else None
    runs: dict[tuple[str | None, str], dict[str, Any]] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=base):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(base) :]
            parsed = _parse_video_relative(rel)
            if parsed is None:
                continue
            ch, run_id = parsed
            if channel_filter is not None and ch != channel_filter:
                continue
            entry = runs.setdefault(
                (ch, run_id),
                {
                    "id": run_id,
                    "category": category,
                    "channel": ch,
                    "prefix": f"{base}{run_id}/" if ch is None else f"{base}{ch}/{run_id}/",
                    "files": {},
                    "last_modified": None,
                },
            )
            filename = "/".join(rel.split("/")[2:]) if ch else "/".join(rel.split("/")[1:])
            if not filename:
                continue
            entry["files"][filename] = key
            lm = obj.get("LastModified")
            if lm and (entry["last_modified"] is None or lm > entry["last_modified"]):
                entry["last_modified"] = lm
    return runs


def _presign(client, bucket: str, key: str, *, expires: int = 3600) -> str | None:
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )
    except Exception:
        return None


def _media_query(category: str, video_id: str, channel: str | None) -> str:
    q = f"category={category}&video_id={video_id}"
    if channel:
        q += f"&channel={channel}"
    return q


def list_categories(client, bucket: str) -> list[str]:
    """Discover category subfolders under ``music-video/``."""
    seen: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="music-video/", Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            p = prefix.get("Prefix", "")
            # music-video/korean/
            parts = p.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "music-video":
                seen.add(parts[1])
    if not seen:
        seen.add(r2_config_from_env().category)
    return sorted(seen)


def category_inventory(client, bucket: str, category: str) -> dict[str, int]:
    """Count objects in each assembly prefix (6 R2 list scans)."""
    prefixes = {
        "music_mp3s": f"music/{category}/",
        "backgrounds_available": f"post-processed/{category}/",
        "backgrounds_in_flight": f"post-processed/{category}/in-flight/",
        "backgrounds_used": f"post-processed/{category}/used/",
        "pre_processed": f"pre-processed/{category}/",
        "music_videos": f"music-video/{category}/",
    }
    counts: dict[str, int] = {}
    for label, prefix in prefixes.items():
        n = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if _is_skipped_key(key):
                    continue
                if label == "backgrounds_available":
                    if "/used/" in key or "/in-flight/" in key:
                        continue
                if label == "pre_processed" and "/used/" in key:
                    continue
                n += 1
        counts[label] = n
    return counts


def list_video_summaries(
    client,
    bucket: str,
    *,
    category: str,
    channel: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List ``mv_*`` folders without reading title/description objects."""
    runs = _scan_video_runs(client, bucket, category=category, channel=channel)
    out: list[dict[str, Any]] = []
    for (_ch, run_id) in sorted(runs.keys(), key=lambda k: k[1], reverse=True)[:limit]:
        entry = runs[(_ch, run_id)]
        files = entry["files"]
        video_key = files.get(f"{run_id}_video.mp4")
        thumb_key = next((files[k] for k in files if k.endswith("_thumbnail.png")), None)
        out.append(
            {
                "id": run_id,
                "category": category,
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
    category: str,
    channel: str | None = None,
    limit: int = 50,
    stable_media_urls: bool = False,
    summary_only: bool = False,
) -> list[dict[str, Any]]:
    """List ``mv_*`` run folders under ``music-video/{category}/`` (and channel subfolders)."""
    if summary_only:
        return list_video_summaries(
            client, bucket, category=category, channel=channel, limit=limit
        )
    runs = _scan_video_runs(client, bucket, category=category, channel=channel)
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
        media_q = _media_query(category, run_id, _ch)
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
                "category": category,
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
    category: str,
    video_id: str,
    channel: str | None = None,
) -> dict[str, Any] | None:
    ch = normalize_channel(channel) if channel else None
    if ch is not None:
        prefixes = [_video_run_prefix(category, video_id, ch)]
    else:
        prefixes = [
            _video_run_prefix(category, video_id, None),
        ]
        for discovered in discover_channels(client, bucket, category):
            prefixes.append(_video_run_prefix(category, video_id, discovered))

    files: dict[str, str] = {}
    resolved_channel = ch
    resolved_prefix = ""
    for prefix in prefixes:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if _is_skipped_key(key):
                    continue
                name = key[len(prefix) :]
                if name:
                    files[name] = key
        if files:
            resolved_prefix = prefix
            if ch is None:
                rel = prefix[len(f"music-video/{category.strip().strip('/')}/") :]
                parts = rel.strip("/").split("/")
                resolved_channel = parts[0] if len(parts) == 2 else None
            break

    if not files:
        return None

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

    media_q = _media_query(category, video_id, resolved_channel)
    return {
        "id": video_id,
        "category": category,
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
    category: str,
    video_id: str,
    channel: str | None = None,
) -> str | None:
    row = get_video(client, bucket, category=category, video_id=video_id, channel=channel)
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
    category: str,
    video_id: str,
    channel: str | None = None,
) -> str | None:
    row = get_video(client, bucket, category=category, video_id=video_id, channel=channel)
    if not row:
        return None
    return next(
        (f["key"] for f in row.get("files", []) if f.get("name", "").endswith("_video.mp4")),
        None,
    )


def list_assets(
    client,
    bucket: str,
    *,
    category: str,
    pool: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """List image metadata for a pre/post-processed pool (no image bytes)."""
    if pool not in _ASSET_POOLS:
        raise ValueError(f"Unknown pool: {pool}")
    prefix = _ASSET_POOLS[pool](category)
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


def asset_object_key(category: str, pool: str, name: str) -> str:
    if pool not in _ASSET_POOLS:
        raise ValueError(f"Unknown pool: {pool}")
    if "/" in name or ".." in name:
        raise ValueError("Invalid asset name")
    return _ASSET_POOLS[pool](category) + name
