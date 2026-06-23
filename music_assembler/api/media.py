"""R2 media proxy with HTTP Range support for video seeking."""

from __future__ import annotations

import re
from typing import Any, Iterator

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _iter_body(body: Any, chunk_size: int = 1024 * 256) -> Iterator[bytes]:
    while True:
        chunk = body.read(chunk_size)
        if not chunk:
            break
        yield chunk


def stream_r2_object(
    client,
    bucket: str,
    key: str,
    request: Request,
    *,
    media_type: str,
    cache_control: str = "private, max-age=3600",
) -> Response:
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except client.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise HTTPException(status_code=404, detail="Object not found") from exc
        raise

    size = int(head["ContentLength"])
    range_header = request.headers.get("range")

    if not range_header:
        resp = client.get_object(Bucket=bucket, Key=key)
        return Response(
            content=resp["Body"].read(),
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(size),
                "Cache-Control": cache_control,
            },
        )

    match = _RANGE_RE.match(range_header.strip())
    if not match:
        raise HTTPException(status_code=416, detail="Invalid Range header")

    start_s, end_s = match.groups()
    start = int(start_s) if start_s else 0
    end = int(end_s) if end_s else size - 1
    if start >= size or start > end:
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers={"Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1)
    length = end - start + 1

    resp = client.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end}")
    return StreamingResponse(
        _iter_body(resp["Body"]),
        status_code=206,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
            "Cache-Control": cache_control,
        },
    )
