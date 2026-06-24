"""Tests for YouTube channel output path helpers."""

from __future__ import annotations

import unittest

from music_assembler.api.r2_catalog import _parse_music_video_key
from music_assembler.assemble_options import (
    normalize_channel,
    resolve_r2_assembly_prefixes,
    video_output_prefix,
)


class TestChannelPaths(unittest.TestCase):
    def test_normalize_channel_slug(self) -> None:
        self.assertEqual(normalize_channel("Lofi Beats"), "lofi-beats")
        self.assertEqual(normalize_channel("channel_a"), "channel_a")
        self.assertIsNone(normalize_channel(""))
        self.assertIsNone(normalize_channel(None))

    def test_normalize_rejects_invalid(self) -> None:
        with self.assertRaises(ValueError):
            normalize_channel("../evil")

    def test_video_output_prefix(self) -> None:
        self.assertEqual(video_output_prefix("nappabeats"), "music-video/nappabeats/")

    def test_video_output_prefix_requires_channel(self) -> None:
        with self.assertRaises(ValueError):
            video_output_prefix("")

    def test_resolve_prefixes_with_channel(self) -> None:
        p = resolve_r2_assembly_prefixes(
            category="korean",
            music_folder=None,
            images_folder=None,
            output_folder=None,
            channel="night-drive",
        )
        self.assertEqual(p.channel, "night-drive")
        self.assertEqual(p.output_prefix, "music-video/night-drive/")
        self.assertEqual(p.music_prefix, "music/korean/")
        self.assertEqual(p.images_prefix, "post-processed/korean/")

    def test_resolve_prefixes_images_folder_override(self) -> None:
        p = resolve_r2_assembly_prefixes(
            category="korean",
            music_folder=None,
            images_folder="japanese",
            output_folder=None,
            channel="nappabeats",
        )
        self.assertEqual(p.music_prefix, "music/korean/")
        self.assertEqual(p.images_prefix, "post-processed/japanese/")
        self.assertEqual(p.images_folder, "japanese")

    def test_resolve_prefixes_requires_channel(self) -> None:
        with self.assertRaises(SystemExit):
            resolve_r2_assembly_prefixes(
                category="korean",
                music_folder=None,
                images_folder=None,
                output_folder=None,
                channel=None,
            )

    def test_parse_music_video_key_current_layout(self) -> None:
        parsed = _parse_music_video_key("music-video/nappabeats/mv_20260101_120000/mv_20260101_120000_video.mp4")
        self.assertEqual(parsed, ("nappabeats", "mv_20260101_120000", "mv_20260101_120000_video.mp4"))

    def test_parse_music_video_key_legacy_nested(self) -> None:
        parsed = _parse_music_video_key(
            "music-video/korean/nappabeats/mv_20260101_120000/mv_20260101_120000_video.mp4"
        )
        self.assertEqual(parsed, ("nappabeats", "mv_20260101_120000", "mv_20260101_120000_video.mp4"))

    def test_parse_music_video_key_legacy_flat(self) -> None:
        parsed = _parse_music_video_key("music-video/korean/mv_20260101_120000/mv_20260101_120000_video.mp4")
        self.assertEqual(parsed, ("korean", "mv_20260101_120000", "mv_20260101_120000_video.mp4"))


if __name__ == "__main__":
    unittest.main()
