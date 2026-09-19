"""Tests for R2 asset upload helpers."""

from __future__ import annotations

import asyncio

import pytest

from music_assembler.api import asset_upload


def test_sanitize_upload_filename():
    assert asset_upload.sanitize_upload_filename("My Photo.JPG") == "My_Photo.jpg"
    assert asset_upload.sanitize_upload_filename("  test.webp  ") == "test.webp"


def test_sanitize_rejects_bad_extension():
    with pytest.raises(ValueError, match="Unsupported"):
        asset_upload.sanitize_upload_filename("video.mp4")


def test_sanitize_rejects_path_traversal():
    with pytest.raises(ValueError, match="Invalid"):
        asset_upload.sanitize_upload_filename("../evil.png")


def test_resolve_upload_key_unique():
    seen: set[str] = set()

    class FakeError(Exception):
        def __init__(self, code: str):
            self.response = {"Error": {"Code": code}}

    class Client:
        exceptions = type("exceptions", (), {"ClientError": FakeError})()

        def head_object(self, *, Bucket, Key):  # noqa: N803
            if Key in seen:
                return {}
            raise Client.exceptions.ClientError("404")

    client = Client()
    key1 = asset_upload.resolve_upload_key(
        client,
        "bucket",
        category="korean",
        pool="pre-processed",
        filename="a.jpg",
        images_folder=None,
        overwrite=False,
    )
    seen.add(key1)
    key2 = asset_upload.resolve_upload_key(
        client,
        "bucket",
        category="korean",
        pool="pre-processed",
        filename="a.jpg",
        images_folder=None,
        overwrite=False,
    )
    assert key1 == "pre-processed/korean/a.jpg"
    assert key2 == "pre-processed/korean/a_2.jpg"


def test_upload_asset_files_mock():
    uploaded_keys: list[str] = []

    class FakeError(Exception):
        def __init__(self, code: str):
            self.response = {"Error": {"Code": code}}

    class Client:
        exceptions = type("exceptions", (), {"ClientError": FakeError})()

        def head_object(self, *, Bucket, Key):  # noqa: N803
            if Key in uploaded_keys:
                return {}
            raise Client.exceptions.ClientError("404")

        def upload_file(self, path, bucket, key, ExtraArgs=None):  # noqa: N803
            uploaded_keys.append(key)

    client = Client()
    result = asset_upload.upload_asset_files(
        client,
        "b",
        category="korean",
        pool="pre-processed",
        images_folder=None,
        files=[("one.png", b"abc"), ("two.jpg", b"def")],
    )
    assert result["count"] == 2
    assert len(uploaded_keys) == 2
    assert uploaded_keys[0].startswith("pre-processed/korean/")


def test_upload_rejects_used_pool():
    with pytest.raises(ValueError, match="not allowed"):
        asset_upload.upload_asset_files(
            object(),
            "b",
            category="korean",
            pool="pre-used",
            images_folder=None,
            files=[("a.png", b"x")],
        )


def test_streamed_upload_bounds_each_file_and_continues(monkeypatch):
    monkeypatch.setattr(asset_upload, "MAX_BYTES_PER_FILE", 8)
    monkeypatch.setattr(asset_upload, "READ_CHUNK_BYTES", 4)
    uploaded_keys: list[str] = []

    class FakeError(Exception):
        def __init__(self, code: str):
            self.response = {"Error": {"Code": code}}

    class Client:
        exceptions = type("exceptions", (), {"ClientError": FakeError})()

        def head_object(self, *, Bucket, Key):  # noqa: N803
            raise self.exceptions.ClientError("404")

        def upload_file(self, path, bucket, key, ExtraArgs=None):  # noqa: N803
            uploaded_keys.append(key)

    class Upload:
        def __init__(self, filename: str, size: int):
            self.filename = filename
            self.remaining = size
            self.bytes_read = 0

        async def read(self, size: int) -> bytes:
            assert size == asset_upload.READ_CHUNK_BYTES
            if self.remaining == 0:
                return b""
            count = min(size, self.remaining)
            self.remaining -= count
            self.bytes_read += count
            return b"x" * count

    too_large = Upload("huge.png", 20)
    valid = Upload("safe.png", 3)
    result = asyncio.run(
        asset_upload.upload_streamed_asset_files(
            Client(),
            "bucket",
            category="korean",
            pool="pre-processed",
            images_folder=None,
            uploads=[too_large, valid],
        )
    )

    assert too_large.bytes_read == 12
    assert too_large.remaining == 8
    assert result["count"] == 1
    assert result["uploaded"][0]["name"] == "safe.png"
    assert result["errors"][0]["name"] == "huge.png"
    assert result["errors"][0]["error"].startswith("File exceeds")
    assert uploaded_keys == ["pre-processed/korean/safe.png"]
