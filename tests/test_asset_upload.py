"""Tests for R2 asset upload helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

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

        def put_object(  # noqa: N803
            self, *, Bucket, Key, Body, ContentType, IfNoneMatch
        ):
            assert IfNoneMatch == "*"
            uploaded_keys.append(Key)

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


def test_concurrent_uploads_with_same_name_preserve_both_payloads():
    """Two non-overwrite uploads must not both report success for one object key."""

    class FakeError(Exception):
        def __init__(self, code: str):
            self.response = {"Error": {"Code": code}}

    class SynchronizedClient:
        exceptions = type("exceptions", (), {"ClientError": FakeError})()

        def __init__(self, conflict_status: int):
            self.objects: dict[str, bytes] = {}
            self.lock = Lock()
            self.first_upload_ready = Event()
            self.second_upload_done = Event()
            self.upload_count = 0
            self.conflict_status = conflict_status

        def head_object(self, *, Bucket, Key):  # noqa: N803
            with self.lock:
                if Key in self.objects:
                    return {}
            raise self.exceptions.ClientError("404")

        def put_object(  # noqa: N803
            self, *, Bucket, Key, Body, ContentType, IfNoneMatch
        ):
            assert IfNoneMatch == "*"
            with self.lock:
                upload_index = self.upload_count
                self.upload_count += 1
            if upload_index == 0:
                self.first_upload_ready.set()
                assert self.second_upload_done.wait(timeout=5)
            with self.lock:
                if Key in self.objects:
                    error = self.exceptions.ClientError(str(self.conflict_status))
                    error.response["ResponseMetadata"] = {
                        "HTTPStatusCode": self.conflict_status
                    }
                    raise error
                self.objects[Key] = Body
            if upload_index == 1:
                self.second_upload_done.set()

    for conflict_status in (409, 412):
        client = SynchronizedClient(conflict_status)

        def upload(payload: bytes):
            return asset_upload.upload_asset_files(
                client,
                "b",
                category="korean",
                pool="pre-processed",
                images_folder=None,
                files=[("same.png", payload)],
                overwrite=False,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(upload, b"first-request")
            assert client.first_upload_ready.wait(timeout=5)
            second = executor.submit(upload, b"second-request")
            results = [first.result(timeout=5), second.result(timeout=5)]

        assert all(result["count"] == 1 and not result["errors"] for result in results)
        assert len(client.objects) == 2
        assert set(client.objects.values()) == {b"first-request", b"second-request"}
        assert {result["uploaded"][0]["key"] for result in results} == {
            "pre-processed/korean/same.png",
            "pre-processed/korean/same_2.png",
        }


def test_overwrite_upload_remains_unconditional():
    uploaded: list[tuple[str, bytes]] = []

    class Client:
        def upload_file(self, path, bucket, key, ExtraArgs=None):  # noqa: N803
            uploaded.append((key, Path(path).read_bytes()))

    result = asset_upload.upload_asset_files(
        Client(),
        "b",
        category="korean",
        pool="pre-processed",
        images_folder=None,
        files=[("same.png", b"replacement")],
        overwrite=True,
    )

    assert result["uploaded"][0]["key"] == "pre-processed/korean/same.png"
    assert uploaded == [("pre-processed/korean/same.png", b"replacement")]


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
