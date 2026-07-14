"""Inventory / dashboard background counting."""

from __future__ import annotations

from music_assembler.api.r2_catalog import count_ready_backgrounds, dashboard_inventory


class _Client:
    class exceptions:
        ClientError = Exception

    def __init__(self, keys: list[str]):
        self._keys = keys

    def get_paginator(self, _name: str):
        keys = self._keys

        class Paginator:
            def paginate(self, **kwargs):
                prefix = kwargs.get("Prefix", "")
                yield {
                    "Contents": [
                        {"Key": k, "Size": 1} for k in keys if k.startswith(prefix)
                    ]
                }

        return Paginator()


def test_count_ready_backgrounds_sums_all_folders():
    client = _Client(
        [
            "post-processed/korean/a.png",
            "post-processed/korean/b.jpg",
            "post-processed/korean/used/old.png",
            "post-processed/korean/in-flight/job/x.png",
            "post-processed/japanese/c.webp",
            "post-processed/japanese/notes.txt",
            "post-processed/japanese/nested/deep.png",
        ]
    )
    assert count_ready_backgrounds(client, "b") == 3
    assert count_ready_backgrounds(client, "b", folder="korean") == 2
    assert count_ready_backgrounds(client, "b", folder="japanese") == 1


def test_dashboard_inventory_aliases():
    client = _Client(
        [
            "post-processed/korean/a.png",
            "post-processed/japanese/b.png",
            "music/korean/track1.mp3",
            "music/korean/readme.txt",
            "music-video/ch/mv_1/mv_1_video.mp4",
            "music-video/ch/mv_1/mv_1_title.txt",
        ]
    )
    inv = dashboard_inventory(client, "b", "korean")
    assert inv["backgrounds_ready"] == 2
    assert inv["post-processed"] == 2
    assert inv["backgrounds_available"] == 1
    assert inv["music_mp3s"] == 1
    assert inv["music"] == 1
    assert inv["music_videos"] == 1
    assert inv["music-video"] == 1
