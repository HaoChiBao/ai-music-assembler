#!/usr/bin/env python3
"""Upload local pre-processed images to R2 under pre-processed/{category}/."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[1]
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".JPG", ".JPEG", ".PNG", ".WEBP"}


def main(argv: list[str] | None = None) -> int:
    import os
    import subprocess

    load_dotenv(find_dotenv(ROOT / ".env"))
    category = (argv[0] if argv else None) or os.environ.get("ASSEMBLY_CATEGORY", "korean")
    category = category.strip().strip("/")
    src = ROOT / "pre-processed"

    print("==> Creating R2 folder markers")
    init = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "r2_init_layout.py"), category],
        cwd=ROOT,
    )
    if init.returncode != 0:
        return init.returncode

    from music_assembler.r2_storage import r2_client, r2_config_from_env  # noqa: PLC0415

    cfg = r2_config_from_env(category=category)
    client = r2_client(cfg)
    files = sorted(p for p in src.iterdir() if p.is_file() and p.suffix in IMAGE_EXTENSIONS)
    if not files:
        print(f"error: no images in {src}", file=sys.stderr)
        return 1

    prefix = cfg.pre_processed_prefix
    print(f"==> Uploading {len(files)} image(s) to s3://{cfg.bucket}/{prefix}")
    for i, path in enumerate(files, 1):
        key = f"{prefix}{path.name}"
        client.upload_file(str(path), cfg.bucket, key)
        print(f"  [{i}/{len(files)}] {path.name}")

    resp = client.list_objects_v2(Bucket=cfg.bucket, Prefix=prefix)
    print(f"==> Done: {resp.get('KeyCount', 0)} object(s) under {prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
