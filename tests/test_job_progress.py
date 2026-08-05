"""Regression tests for R2 job metadata updates."""

from __future__ import annotations

import io
import json

from music_assembler.job_progress import (
    meta_key,
    patch_meta_gcp_execution_id,
    patch_meta_json,
    write_meta_json,
)


class _ClientError(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}


class _FakeR2:
    exceptions = type("Exceptions", (), {"ClientError": _ClientError})

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803
        del Bucket
        if Key not in self.objects:
            raise _ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs):  # noqa: N803
        del Bucket, kwargs
        self.objects[Key] = Body


def test_worker_metadata_patch_preserves_control_plane_fields():
    client = _FakeR2()

    write_meta_json(
        client,
        "bucket",
        "run-1",
        category="music",
        channel="channel",
        images_folder="custom-backgrounds",
        template_id="playlist_landscape",
        duration_min=90,
        variance_min=15,
        thumbnail_text="PLAYLIST",
        job_type="assembly",
    )
    patch_meta_gcp_execution_id(client, "bucket", "run-1", "gcp-123")

    patch_meta_json(
        client,
        "bucket",
        "run-1",
        category="music",
        images_folder="custom-backgrounds",
        claimed_background="background.png",
        channel="channel",
        template_id="playlist_landscape",
    )

    meta = json.loads(client.objects[meta_key("run-1")])
    assert meta["gcp_execution_id"] == "gcp-123"
    assert meta["duration_min"] == 90
    assert meta["variance_min"] == 15
    assert meta["thumbnail_text"] == "PLAYLIST"
    assert meta["job_type"] == "assembly"
    assert meta["category"] == "music"
    assert meta["images_folder"] == "custom-backgrounds"
    assert meta["claimed_background"] == "background.png"
