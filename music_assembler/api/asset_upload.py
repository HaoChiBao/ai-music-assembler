"""Upload background images to R2 asset pools."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from music_assembler.api.r2_catalog import asset_object_key
from music_assembler.r2_storage import IMAGE_EXTENSIONS, object_exists

UPLOAD_POOLS = frozenset({"pre-processed", "post-processed"})
MAX_FILES_PER_REQUEST = 50
MAX_BYTES_PER_FILE = 20 * 1024 * 1024

_IMAGE_SUFFIXES = {ext.lower() for ext in IMAGE_EXTENSIONS}
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def sanitize_upload_filename(name: str) -> str:
    """Return a safe flat filename with a supported image extension."""
    raw = name.strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("Invalid filename")
    base = Path(raw).name
    suffix = Path(base).suffix.lower()
    if suffix not in _IMAGE_SUFFIXES:
        raise ValueError(f"Unsupported image type {suffix!r} (use jpg, png, or webp)")
    stem = Path(base).stem
    safe_stem = re.sub(r"[^\w.\-]+", "_", stem, flags=re.ASCII).strip("._-")
    if not safe_stem:
        safe_stem = "image"
    return f"{safe_stem}{suffix}"


def content_type_for_filename(name: str) -> str:
    return _CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def resolve_upload_key(
    client,
    bucket: str,
    *,
    category: str,
    pool: str,
    filename: str,
    images_folder: str | None,
    overwrite: bool,
) -> str:
    """Pick the R2 object key; suffix ``_2``, ``_3``, … when not overwriting."""
    name = sanitize_upload_filename(filename)
    key = asset_object_key(category, pool, name, images_folder=images_folder)
    if overwrite or not object_exists(client, bucket, key):
        return key
    stem = Path(name).stem
    suffix = Path(name).suffix
    prefix = key[: -len(name)]
    for i in range(2, 1000):
        candidate_name = f"{stem}_{i}{suffix}"
        candidate_key = f"{prefix}{candidate_name}"
        if not object_exists(client, bucket, candidate_key):
            return candidate_key
    raise ValueError(f"Could not find a free name for {name!r}")


def upload_asset_files(
    client,
    bucket: str,
    *,
    category: str,
    pool: str,
    images_folder: str | None,
    files: list[tuple[str, bytes]],
    overwrite: bool = False,
) -> dict[str, Any]:
    """Upload ``(filename, bytes)`` pairs to an asset pool. Returns summary dict."""
    if pool not in UPLOAD_POOLS:
        raise ValueError(f"Upload not allowed for pool {pool!r}")
    if not files:
        raise ValueError("No files to upload")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise ValueError(f"At most {MAX_FILES_PER_REQUEST} files per request")

    uploaded: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for original_name, data in files:
        if len(data) > MAX_BYTES_PER_FILE:
            errors.append(
                {
                    "name": original_name,
                    "error": f"File exceeds {MAX_BYTES_PER_FILE // (1024 * 1024)} MB limit",
                }
            )
            continue
        try:
            key = resolve_upload_key(
                client,
                bucket,
                category=category,
                pool=pool,
                filename=original_name,
                images_folder=images_folder,
                overwrite=overwrite,
            )
            name = key.rsplit("/", 1)[-1]
            local_path = Path(f"/tmp/r2-upload-{name}")
            local_path.write_bytes(data)
            try:
                client.upload_file(
                    str(local_path),
                    bucket,
                    key,
                    ExtraArgs={"ContentType": content_type_for_filename(name)},
                )
            finally:
                local_path.unlink(missing_ok=True)
            uploaded.append({"name": name, "key": key, "size": len(data)})
        except ValueError as exc:
            errors.append({"name": original_name, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — collect per-file failures
            errors.append({"name": original_name, "error": str(exc)})

    if not uploaded and errors:
        raise ValueError(errors[0]["error"])

    return {
        "category": category,
        "pool": pool,
        "images_folder": images_folder,
        "uploaded": uploaded,
        "errors": errors,
        "count": len(uploaded),
    }
