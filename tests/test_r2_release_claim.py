"""Regression tests for failed R2 claim releases."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from music_assembler.r2_storage import (
    in_flight_key,
    pre_processed_in_flight_key,
    release_background_claim,
    release_pre_processed_claim,
)

BUCKET = "bucket"
FILENAME = "asset.png"


class _ClientError(Exception):
    def __init__(self, code: str = "NoSuchKey"):
        self.response = {"Error": {"Code": code}}


class _FakeClient:
    class exceptions:
        ClientError = _ClientError

    def __init__(
        self,
        objects: dict[str, bytes],
        *,
        fail_copy: bool = False,
        fail_delete: bool = False,
    ):
        self.objects = dict(objects)
        self.fail_copy = fail_copy
        self.fail_delete = fail_delete
        self.copies: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    def head_object(self, *, Bucket: str, Key: str):
        if Key not in self.objects:
            raise _ClientError()
        return {"ContentLength": len(self.objects[Key])}

    def copy_object(self, *, Bucket: str, Key: str, CopySource, MetadataDirective: str):
        if self.fail_copy:
            raise _ClientError("CopyFailed")
        source = CopySource["Key"]
        self.objects[Key] = self.objects[source]
        self.copies.append((source, Key))

    def delete_object(self, *, Bucket: str, Key: str):
        if self.fail_delete:
            raise _ClientError("DeleteFailed")
        self.objects.pop(Key, None)
        self.deletes.append(Key)


@dataclass(frozen=True)
class _ReleaseCase:
    name: str
    prefix: str
    execution_id: str

    @property
    def source(self) -> str:
        if self.name == "pre_processed":
            return pre_processed_in_flight_key(self.prefix, self.execution_id, FILENAME)
        return in_flight_key(self.prefix, self.execution_id, FILENAME)

    @property
    def destination(self) -> str:
        return f"{self.prefix}{FILENAME}"

    def release(self, client: _FakeClient) -> bool:
        if self.name == "pre_processed":
            return release_pre_processed_claim(
                client,
                BUCKET,
                pre_processed_prefix=self.prefix,
                execution_id=self.execution_id,
                filename=FILENAME,
            )
        return release_background_claim(
            client,
            BUCKET,
            images_prefix=self.prefix,
            execution_id=self.execution_id,
            filename=FILENAME,
        )


CASES = [
    _ReleaseCase("pre_processed", "pre-processed/korean/", "ext-old"),
    _ReleaseCase("background", "post-processed/korean/", "asm-old"),
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_release_preserves_fresh_destination_and_deletes_stale_claim(case):
    client = _FakeClient(
        {
            case.source: b"stale-in-flight",
            case.destination: b"fresh-reupload",
        }
    )

    assert case.release(client) is True

    assert client.objects[case.destination] == b"fresh-reupload"
    assert case.source not in client.objects
    assert client.copies == []
    assert client.deletes == [case.source]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_release_restores_claim_when_destination_is_absent(case):
    client = _FakeClient({case.source: b"claimed-bytes"})

    assert case.release(client) is True

    assert client.objects[case.destination] == b"claimed-bytes"
    assert case.source not in client.objects
    assert client.copies == [(case.source, case.destination)]
    assert client.deletes == [case.source]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_release_returns_false_when_stale_claim_delete_fails(case):
    client = _FakeClient(
        {
            case.source: b"stale-in-flight",
            case.destination: b"fresh-reupload",
        },
        fail_delete=True,
    )

    assert case.release(client) is False

    assert client.objects[case.destination] == b"fresh-reupload"
    assert client.objects[case.source] == b"stale-in-flight"
    assert client.copies == []


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_release_returns_false_when_restore_copy_fails(case):
    client = _FakeClient({case.source: b"claimed-bytes"}, fail_copy=True)

    assert case.release(client) is False

    assert case.destination not in client.objects
    assert client.objects[case.source] == b"claimed-bytes"
    assert client.deletes == []


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_release_returns_false_when_restore_delete_fails(case):
    client = _FakeClient({case.source: b"claimed-bytes"}, fail_delete=True)

    assert case.release(client) is False

    assert client.objects[case.destination] == b"claimed-bytes"
    assert client.objects[case.source] == b"claimed-bytes"
    assert client.copies == [(case.source, case.destination)]
    assert client.deletes == []
