"""Tests for safe R2 prefix downloads."""

from __future__ import annotations

from pathlib import Path

import pytest

from music_assembler.r2_storage import sync_prefix_to_dir


class _Paginator:
    def __init__(self, keys: list[str]):
        self.keys = keys

    def paginate(self, **kwargs):
        return [{"Contents": [{"Key": key} for key in self.keys]}]


class _FakeClient:
    def __init__(self, keys: list[str]):
        self.keys = keys
        self.downloads: list[tuple[str, Path]] = []

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return _Paginator(self.keys)

    def download_file(self, bucket: str, key: str, destination: str) -> None:
        path = Path(destination)
        path.write_text(key, encoding="utf-8")
        self.downloads.append((key, path))


def test_sync_prefix_rejects_unsafe_object_keys(tmp_path):
    prefix = "music/korean/"
    unsafe_keys = [
        "music/koreanish/sibling.mp3",
        prefix + "../../escaped.mp3",
        prefix + str(tmp_path / "absolute.mp3"),
    ]

    for index, key in enumerate(unsafe_keys):
        local_dir = tmp_path / f"download-{index}"
        client = _FakeClient([key])

        with pytest.raises(ValueError):
            sync_prefix_to_dir(client, "bucket", prefix, local_dir)

        assert client.downloads == []


def test_sync_prefix_ignores_directory_marker(tmp_path):
    prefix = "music/korean/"
    client = _FakeClient([prefix])

    count = sync_prefix_to_dir(client, "bucket", prefix, tmp_path / "download")

    assert count == 0
    assert client.downloads == []


def test_sync_prefix_rejects_symlink_escape(tmp_path):
    prefix = "music/korean/"
    local_dir = tmp_path / "download"
    outside = tmp_path / "outside"
    outside.mkdir()
    local_dir.mkdir()
    (local_dir / "linked").symlink_to(outside, target_is_directory=True)
    client = _FakeClient([prefix + "linked/escaped.mp3"])

    with pytest.raises(ValueError, match="resolves outside destination"):
        sync_prefix_to_dir(client, "bucket", prefix, local_dir)

    assert client.downloads == []
    assert not (outside / "escaped.mp3").exists()


def test_sync_prefix_downloads_valid_nested_key(tmp_path):
    prefix = "music/korean/"
    local_dir = tmp_path / "download"
    key = prefix + "albums/2026/normal.mp3"
    client = _FakeClient([key])

    count = sync_prefix_to_dir(client, "bucket", prefix, local_dir)

    destination = local_dir / "albums/2026/normal.mp3"
    assert count == 1
    assert client.downloads == [(key, destination)]
    assert destination.read_text(encoding="utf-8") == key
