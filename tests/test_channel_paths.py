"""Tests for YouTube channel output path helpers."""

from __future__ import annotations

import unittest

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

    def test_video_output_prefix_without_channel(self) -> None:
        self.assertEqual(video_output_prefix("korean"), "music-video/korean/")
        self.assertEqual(
            video_output_prefix("korean", "my-channel"),
            "music-video/korean/my-channel/",
        )

    def test_resolve_prefixes_with_channel(self) -> None:
        p = resolve_r2_assembly_prefixes(
            category="korean",
            music_folder=None,
            images_folder=None,
            output_folder=None,
            channel="night-drive",
        )
        self.assertEqual(p.channel, "night-drive")
        self.assertEqual(p.output_prefix, "music-video/korean/night-drive/")
        self.assertEqual(p.music_prefix, "music/korean/")


if __name__ == "__main__":
    unittest.main()
