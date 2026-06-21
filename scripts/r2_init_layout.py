#!/usr/bin/env python3
"""Create category folder placeholders on Cloudflare R2."""

from __future__ import annotations

import os
import sys

import boto3
from botocore.config import Config


def main() -> int:
    bucket = os.environ.get("CLOUDFLARE_R2_BUCKET", "").strip()
    endpoint = os.environ.get("CLOUDFLARE_R2_ENDPOINT", "").strip()
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "").strip()
    category = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ASSEMBLY_CATEGORY", "korean")).strip("/")

    missing = [
        name
        for name, val in (
            ("CLOUDFLARE_R2_BUCKET", bucket),
            ("CLOUDFLARE_R2_ENDPOINT", endpoint),
            ("CLOUDFLARE_R2_ACCESS_KEY_ID", access_key),
            ("CLOUDFLARE_R2_SECRET_ACCESS_KEY", secret_key),
        )
        if not val
    ]
    if missing:
        print(f"error: set {', '.join(missing)} in .env", file=sys.stderr)
        return 1

    keys = [
        f"music/{category}/.gitkeep",
        f"post-processed/{category}/.gitkeep",
        f"post-processed/{category}/used/.gitkeep",
        f"music-video/{category}/.gitkeep",
    ]

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

    print(f"==> Creating R2 layout for category: {category}")
    for key in keys:
        print(f"  s3://{bucket}/{key}")
        client.put_object(Bucket=bucket, Key=key, Body=b"", ContentType="application/octet-stream")

    print("==> Done")
    for prefix in (f"music/{category}/", f"post-processed/{category}/", f"music-video/{category}/"):
        resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=10)
        print(f"  {prefix}: {resp.get('KeyCount', 0)} object(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
