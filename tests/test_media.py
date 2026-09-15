"""R2 media proxy streaming behavior."""

from __future__ import annotations

import asyncio
from io import BytesIO

from starlette.requests import Request
from starlette.responses import StreamingResponse

from music_assembler.api.media import stream_r2_object


class _TrackedBody:
    def __init__(self, data: bytes) -> None:
        self._stream = BytesIO(data)
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("media body must never be read into memory all at once")
        return self._stream.read(size)

    def close(self) -> None:
        self.closed = True
        self._stream.close()


class _R2Client:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.body: _TrackedBody | None = None
        self.requested_range: str | None = None

    def head_object(self, **_kwargs):
        return {"ContentLength": len(self.data)}

    def get_object(self, **kwargs):
        self.requested_range = kwargs.get("Range")
        data = self.data
        if self.requested_range:
            bounds = self.requested_range.removeprefix("bytes=").split("-", 1)
            start, end = (int(value) for value in bounds)
            data = data[start : end + 1]
        self.body = _TrackedBody(data)
        return {"Body": self.body}


def _request(*, range_header: str | None = None) -> Request:
    headers = [] if range_header is None else [(b"range", range_header.encode("ascii"))]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


async def _consume(response: StreamingResponse) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


def test_full_object_response_streams_without_unbounded_read():
    payload = b"x" * (1024 * 1024 + 17)
    client = _R2Client(payload)

    response = stream_r2_object(
        client,
        "bucket",
        "music-video/channel/video.mp4",
        _request(),
        media_type="video/mp4",
    )

    assert isinstance(response, StreamingResponse)
    assert client.body is not None
    assert client.body.read_sizes == []
    assert asyncio.run(_consume(response)) == payload
    assert client.body.read_sizes
    assert all(size == 1024 * 256 for size in client.body.read_sizes)
    assert client.body.closed is True
    assert response.headers["content-length"] == str(len(payload))


def test_range_response_streams_and_closes_body():
    payload = b"0123456789"
    client = _R2Client(payload)

    response = stream_r2_object(
        client,
        "bucket",
        "music-video/channel/video.mp4",
        _request(range_header="bytes=2-5"),
        media_type="video/mp4",
    )

    assert isinstance(response, StreamingResponse)
    assert response.status_code == 206
    assert asyncio.run(_consume(response)) == b"2345"
    assert client.requested_range == "bytes=2-5"
    assert client.body is not None and client.body.closed is True
