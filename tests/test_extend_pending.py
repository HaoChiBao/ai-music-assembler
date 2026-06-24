"""Tests for extend pending source discovery (no per-image HEAD requests)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from music_assembler.extend_from_r2 import pending_r2_sources
from music_assembler.r2_storage import R2Config


def _cfg() -> R2Config:
    return R2Config(
        bucket="b",
        endpoint="https://example.com",
        access_key_id="k",
        secret_access_key="s",
        category="korean",
    )


class PendingR2SourcesTests(unittest.TestCase):
    def test_skips_existing_post_processed_without_head_per_source(self) -> None:
        cfg = _cfg()
        client = MagicMock()
        pre = [
            f"pre-processed/korean/a.jpg",
            f"pre-processed/korean/b.jpg",
            f"pre-processed/korean/c.jpg",
        ]
        post = [
            f"post-processed/korean/a.png",
        ]

        def fake_list(_client, bucket, pre_processed_prefix, images_prefix, force=False):
            if pre_processed_prefix == cfg.pre_processed_prefix:
                return [pre[1], pre[2]]
            return []

        with unittest.mock.patch(
            "music_assembler.extend_from_r2.list_claimable_pre_processed_keys",
            side_effect=fake_list,
        ) as list_mock:
            pending = pending_r2_sources(client, cfg, force=False)

        self.assertEqual(pending, [pre[1], pre[2]])
        self.assertEqual(list_mock.call_count, 1)
        client.head_object.assert_not_called()

    def test_force_includes_all_images(self) -> None:
        cfg = _cfg()
        client = MagicMock()
        pre = [f"pre-processed/korean/a.jpg"]

        with unittest.mock.patch(
            "music_assembler.extend_from_r2.list_claimable_pre_processed_keys",
            return_value=pre,
        ):
            pending = pending_r2_sources(client, cfg, force=True)

        self.assertEqual(pending, pre)


if __name__ == "__main__":
    unittest.main()
