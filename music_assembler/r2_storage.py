"""Cloudflare R2 (S3-compatible) sync helpers for the assembly job."""

from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import boto3
    from botocore.config import Config
except ImportError:  # pragma: no cover - optional extra
    boto3 = None  # type: ignore[assignment]
    Config = None  # type: ignore[assignment,misc]

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP")
AUDIO_EXTENSIONS = (".mp3", ".MP3")

R2_ENV_VARS = (
    "CLOUDFLARE_R2_BUCKET",
    "CLOUDFLARE_R2_ENDPOINT",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
)


@dataclass(frozen=True)
class R2Config:
    bucket: str
    endpoint: str
    access_key_id: str
    secret_access_key: str
    category: str

    @property
    def music_prefix(self) -> str:
        return f"music/{self.category}/"

    @property
    def pre_processed_prefix(self) -> str:
        return f"pre-processed/{self.category}/"

    @property
    def used_pre_processed_prefix(self) -> str:
        return f"pre-processed/{self.category}/used/"

    @property
    def images_prefix(self) -> str:
        return f"post-processed/{self.category}/"

    @property
    def used_images_prefix(self) -> str:
        return f"post-processed/{self.category}/used/"

    @property
    def in_flight_images_prefix(self) -> str:
        return f"post-processed/{self.category}/in-flight/"

    @property
    def in_flight_pre_processed_prefix(self) -> str:
        return f"pre-processed/{self.category}/in-flight/"

    @property
    def output_prefix(self) -> str:
        return f"music-video/{self.category}/"


def _require_boto3() -> None:
    if boto3 is None or Config is None:
        print(
            "error: boto3 is required for R2 sync. Install with: pip install \".[r2]\"",
            file=sys.stderr,
        )
        raise SystemExit(1)


def r2_config_from_env(*, category: str | None = None) -> R2Config:
    """Load R2 settings from environment (call ``load_dotenv`` before this if using ``.env``)."""
    bucket = os.environ.get("CLOUDFLARE_R2_BUCKET", "").strip()
    endpoint = (
        os.environ.get("CLOUDFLARE_R2_ENDPOINT", "").strip()
        or os.environ.get("CLOUDFLARE_R2_ENDPOINT_URL", "").strip()
    )
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "").strip()
    cat = (category or os.environ.get("ASSEMBLY_CATEGORY", "korean")).strip().strip("/")

    missing = [
        name
        for name, val in zip(R2_ENV_VARS, (bucket, endpoint, access_key, secret_key), strict=True)
        if not val
    ]
    if missing:
        print(f"error: set {', '.join(missing)} in .env", file=sys.stderr)
        raise SystemExit(1)
    if not cat:
        print("error: set ASSEMBLY_CATEGORY or pass --category", file=sys.stderr)
        raise SystemExit(1)

    return R2Config(
        bucket=bucket,
        endpoint=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        category=cat,
    )


def r2_client(cfg: R2Config):
    _require_boto3()
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _normalize_prefix(prefix: str) -> str:
    return prefix if prefix.endswith("/") else f"{prefix}/"


def sync_prefix_to_dir(
    client,
    bucket: str,
    prefix: str,
    local_dir: Path,
    *,
    exclude_relative_prefixes: tuple[str, ...] = (),
) -> int:
    """Download every object under ``prefix`` into ``local_dir``. Returns file count."""
    prefix = _normalize_prefix(prefix)
    local_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix) :]
            if any(rel.startswith(ex) for ex in exclude_relative_prefixes):
                continue
            dest = local_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(dest))
            count += 1
    return count


def sync_dir_to_prefix(client, bucket: str, local_dir: Path, prefix: str) -> int:
    """Upload every file under ``local_dir`` to ``prefix``. Returns file count."""
    if not local_dir.is_dir():
        return 0
    prefix = _normalize_prefix(prefix)
    count = 0
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        key = f"{prefix}{rel}"
        client.upload_file(str(path), bucket, key)
        count += 1
    return count


def has_files_with_suffixes(directory: Path, suffixes: tuple[str, ...]) -> bool:
    if not directory.is_dir():
        return False
    return any(p.is_file() and p.suffix in suffixes for p in directory.iterdir())


def count_files_with_suffixes(directory: Path, suffixes: tuple[str, ...]) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.iterdir() if p.is_file() and p.suffix in suffixes)


def list_object_keys(
    client,
    bucket: str,
    prefix: str,
    *,
    exclude_relative_prefixes: tuple[str, ...] = (),
) -> list[str]:
    """List object keys under ``prefix`` (full S3 keys)."""
    prefix = _normalize_prefix(prefix)
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix) :]
            if any(rel.startswith(ex) for ex in exclude_relative_prefixes):
                continue
            keys.append(key)
    return sorted(keys)


def object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except client.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload_file(client, bucket: str, key: str, local_path: Path) -> None:
    client.upload_file(str(local_path), bucket, key)


def _is_image_key(key: str) -> bool:
    return any(key.lower().endswith(ext.lower()) for ext in IMAGE_EXTENSIONS)


def in_flight_key(images_prefix: str, execution_id: str, filename: str) -> str:
    return f"{_normalize_prefix(images_prefix)}in-flight/{execution_id}/{filename}"


def pre_processed_in_flight_key(
    pre_processed_prefix: str, execution_id: str, filename: str
) -> str:
    return f"{_normalize_prefix(pre_processed_prefix)}in-flight/{execution_id}/{filename}"


def list_in_flight_pre_processed_names(
    client, bucket: str, pre_processed_prefix: str
) -> set[str]:
    """Filenames reserved under ``pre-processed/{category}/in-flight/*/``."""
    prefix = f"{_normalize_prefix(pre_processed_prefix)}in-flight/"
    names: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix) :]
            if "/" not in rel:
                continue
            filename = rel.split("/", 1)[1]
            if _is_image_key(filename):
                names.add(filename)
    return names


def _post_processed_png_stems(client, bucket: str, images_prefix: str) -> set[str]:
    stems: set[str] = set()
    for key in list_object_keys(
        client,
        bucket,
        images_prefix,
        exclude_relative_prefixes=("used/", "in-flight/"),
    ):
        name = key.rsplit("/", 1)[-1]
        if name.startswith(".") or not name.lower().endswith(".png"):
            continue
        rel = key[len(_normalize_prefix(images_prefix)) :]
        if "/" in rel:
            continue
        stems.add(Path(name).stem)
    return stems


def list_claimable_pre_processed_keys(
    client,
    bucket: str,
    *,
    pre_processed_prefix: str,
    images_prefix: str,
    force: bool = False,
) -> list[str]:
    """Keys in the pre-processed pool available to claim (not used, in-flight, or already extended)."""
    pre_processed_prefix = _normalize_prefix(pre_processed_prefix)
    reserved = list_in_flight_pre_processed_names(client, bucket, pre_processed_prefix)
    existing_stems = set() if force else _post_processed_png_stems(client, bucket, images_prefix)
    keys: list[str] = []
    for key in list_object_keys(
        client,
        bucket,
        pre_processed_prefix,
        exclude_relative_prefixes=("used/", "in-flight/"),
    ):
        rel = key[len(pre_processed_prefix) :]
        if "/" in rel or rel.endswith(".gitkeep"):
            continue
        if not _is_image_key(rel):
            continue
        if rel in reserved:
            continue
        if not force and Path(rel).stem in existing_stems:
            continue
        keys.append(key)
    return keys


def claim_pre_processed_on_r2(
    client,
    bucket: str,
    *,
    pre_processed_prefix: str,
    images_prefix: str,
    execution_id: str,
    force: bool = False,
) -> str | None:
    """Atomically reserve one pre-processed photo for extend (copy → in-flight/, delete source).

    Returns the claimed filename, or ``None`` when nothing is available.
  Safe for parallel workers: races retry the next candidate.
    """
    available = list_claimable_pre_processed_keys(
        client,
        bucket,
        pre_processed_prefix=pre_processed_prefix,
        images_prefix=images_prefix,
        force=force,
    )
    if not available:
        return None
    random.shuffle(available)
    for src_key in available:
        filename = src_key.rsplit("/", 1)[-1]
        dest_key = pre_processed_in_flight_key(pre_processed_prefix, execution_id, filename)
        if not object_exists(client, bucket, src_key):
            continue
        try:
            copy_then_delete_object(client, bucket, src_key, dest_key)
        except client.exceptions.ClientError:
            continue
        if object_exists(client, bucket, dest_key):
            return filename
    return None


def release_pre_processed_claim(
    client,
    bucket: str,
    *,
    pre_processed_prefix: str,
    execution_id: str,
    filename: str,
) -> bool:
    """Return a claimed pre-processed photo to the pool after a failed extend."""
    src = pre_processed_in_flight_key(pre_processed_prefix, execution_id, filename)
    dest = f"{_normalize_prefix(pre_processed_prefix)}{filename}"
    if not object_exists(client, bucket, src):
        return object_exists(client, bucket, dest)
    try:
        copy_then_delete_object(client, bucket, src, dest)
        return True
    except client.exceptions.ClientError:
        return False


def retire_claimed_pre_processed_on_r2(
    client,
    bucket: str,
    *,
    pre_processed_prefix: str,
    used_pre_processed_prefix: str,
    execution_id: str,
    filename: str,
) -> bool:
    """Move a claimed pre-processed source from in-flight to ``used/`` after success."""
    in_flight = pre_processed_in_flight_key(pre_processed_prefix, execution_id, filename)
    dest_key = f"{_normalize_prefix(used_pre_processed_prefix)}{filename}"
    if not object_exists(client, bucket, in_flight):
        return object_exists(client, bucket, dest_key)
    if object_exists(client, bucket, dest_key):
        client.delete_object(Bucket=bucket, Key=in_flight)
    else:
        copy_then_delete_object(client, bucket, in_flight, dest_key)
    return object_exists(client, bucket, dest_key) and not object_exists(client, bucket, in_flight)


def list_in_flight_background_names(client, bucket: str, images_prefix: str) -> set[str]:
    """Filenames reserved under ``post-processed/{category}/in-flight/*/``."""
    prefix = f"{_normalize_prefix(images_prefix)}in-flight/"
    names: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix) :]
            if "/" not in rel:
                continue
            filename = rel.split("/", 1)[1]
            if _is_image_key(filename):
                names.add(filename)
    return names


def list_available_background_keys(client, bucket: str, images_prefix: str) -> list[str]:
    """Keys for backgrounds available to claim (not in ``used/`` or ``in-flight/``)."""
    images_prefix = _normalize_prefix(images_prefix)
    reserved = list_in_flight_background_names(client, bucket, images_prefix)
    keys: list[str] = []
    for key in list_object_keys(
        client,
        bucket,
        images_prefix,
        exclude_relative_prefixes=("used/", "in-flight/"),
    ):
        rel = key[len(images_prefix) :]
        if "/" in rel or rel.endswith(".gitkeep"):
            continue
        if not _is_image_key(rel):
            continue
        if rel in reserved:
            continue
        keys.append(key)
    return keys


def claim_background_on_r2(
    client,
    bucket: str,
    *,
    images_prefix: str,
    execution_id: str,
) -> str | None:
    """Atomically reserve one background for this job (copy → in-flight/, delete source).

    Returns the claimed filename, or ``None`` when every background is used or in-flight.
    Safe for parallel workers: races retry the next candidate.
    """
    available = list_available_background_keys(client, bucket, images_prefix)
    if not available:
        return None
    random.shuffle(available)
    for src_key in available:
        filename = src_key.rsplit("/", 1)[-1]
        dest_key = in_flight_key(images_prefix, execution_id, filename)
        if not object_exists(client, bucket, src_key):
            continue
        try:
            copy_then_delete_object(client, bucket, src_key, dest_key)
        except client.exceptions.ClientError:
            continue
        if object_exists(client, bucket, dest_key):
            return filename
    return None


def release_background_claim(
    client,
    bucket: str,
    *,
    images_prefix: str,
    execution_id: str,
    filename: str,
) -> bool:
    """Return a claimed background to the available pool after a failed encode."""
    src = in_flight_key(images_prefix, execution_id, filename)
    dest = f"{_normalize_prefix(images_prefix)}{filename}"
    if not object_exists(client, bucket, src):
        return object_exists(client, bucket, dest)
    try:
        copy_then_delete_object(client, bucket, src, dest)
        return True
    except client.exceptions.ClientError:
        return False


def retire_claimed_background_on_r2(
    client,
    bucket: str,
    *,
    images_prefix: str,
    used_images_prefix: str,
    execution_id: str,
    filename: str,
    local_used_path: Path | None = None,
) -> bool:
    """Move a claimed background from in-flight to ``used/`` (copy + delete)."""
    in_flight = in_flight_key(images_prefix, execution_id, filename)
    dest_key = f"{_normalize_prefix(used_images_prefix)}{filename}"
    if object_exists(client, bucket, in_flight):
        if object_exists(client, bucket, dest_key):
            client.delete_object(Bucket=bucket, Key=in_flight)
        else:
            copy_then_delete_object(client, bucket, in_flight, dest_key)
        return object_exists(client, bucket, dest_key) and not object_exists(
            client, bucket, in_flight
        )
    return retire_used_background_on_r2(
        client,
        bucket,
        images_prefix=images_prefix,
        used_images_prefix=used_images_prefix,
        filename=filename,
        local_used_path=local_used_path,
    )


def verify_background_retired_on_r2(
    client,
    bucket: str,
    *,
    images_prefix: str,
    used_images_prefix: str,
    filename: str,
    execution_id: str | None = None,
) -> dict[str, bool]:
    """Check pool / in-flight / used keys after a retire (for logs and tests)."""
    images_prefix = _normalize_prefix(images_prefix)
    used_images_prefix = _normalize_prefix(used_images_prefix)
    pool_key = f"{images_prefix}{filename}"
    used_key = f"{used_images_prefix}{filename}"
    in_flight_key_str = (
        in_flight_key(images_prefix, execution_id, filename) if execution_id else None
    )
    return {
        "in_pool": object_exists(client, bucket, pool_key),
        "in_flight": (
            object_exists(client, bucket, in_flight_key_str)
            if in_flight_key_str
            else False
        ),
        "in_used": object_exists(client, bucket, used_key),
    }


def copy_then_delete_object(client, bucket: str, src_key: str, dest_key: str) -> None:
    """Copy ``src_key`` to ``dest_key`` in the same bucket, then delete the source."""
    if src_key == dest_key:
        raise ValueError(f"refusing to copy object onto itself: {src_key}")
    client.copy_object(
        Bucket=bucket,
        Key=dest_key,
        CopySource={"Bucket": bucket, "Key": src_key},
        MetadataDirective="COPY",
    )
    client.delete_object(Bucket=bucket, Key=src_key)


def move_object(client, bucket: str, src_key: str, dest_key: str) -> None:
    """Alias for :func:`copy_then_delete_object` (R2 has no native rename)."""
    copy_then_delete_object(client, bucket, src_key, dest_key)


def retire_used_background_on_r2(
    client,
    bucket: str,
    *,
    images_prefix: str,
    used_images_prefix: str,
    filename: str,
    local_used_path: Path | None = None,
) -> bool:
    """Retire a background after a successful encode.

  ``post-processed/{category}/{file}`` → ``post-processed/{category}/used/{file}``

    Implemented as **copy to ``used/`` then delete the original** on R2 (see
    :func:`copy_then_delete_object`). If the source key is already gone, uploads
    from ``local_used_path`` when provided (local pipeline moves to ``used/`` first).
    """
    if not filename or "/" in filename or "\\" in filename or filename.strip() != filename:
        return False

    images_prefix = _normalize_prefix(images_prefix)
    used_images_prefix = _normalize_prefix(used_images_prefix)
    if not used_images_prefix.startswith(images_prefix):
        raise ValueError(
            f"used_images_prefix must be under images_prefix: {used_images_prefix!r} vs {images_prefix!r}"
        )

    src_key = f"{images_prefix}{filename}"
    dest_key = f"{used_images_prefix}{filename}"

    if object_exists(client, bucket, dest_key):
        if object_exists(client, bucket, src_key):
            copy_then_delete_object(client, bucket, src_key, dest_key)
        return True
    if object_exists(client, bucket, src_key):
        copy_then_delete_object(client, bucket, src_key, dest_key)
        return True
    if local_used_path is not None and local_used_path.is_file():
        upload_file(client, bucket, dest_key, local_used_path)
        return True
    return False
