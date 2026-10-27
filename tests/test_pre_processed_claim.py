"""Tests for atomic pre-processed claim helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from music_assembler.r2_storage import (
    R2Config,
    claim_pre_processed_on_r2,
    list_claimable_pre_processed_keys,
    retire_claimed_pre_processed_on_r2,
)


def _cfg() -> R2Config:
    return R2Config(
        bucket="b",
        endpoint="https://example.com",
        access_key_id="k",
        secret_access_key="s",
        category="korean",
    )


class PreProcessedClaimTests(unittest.TestCase):
    def test_list_claimable_skips_in_flight_and_existing_png(self) -> None:
        cfg = _cfg()
        client = MagicMock()

        def fake_list(_client, bucket, prefix, exclude_relative_prefixes=()):
            if prefix == cfg.pre_processed_prefix:
                return [
                    f"pre-processed/korean/a.jpg",
                    f"pre-processed/korean/b.jpg",
                    f"pre-processed/korean/in-flight/exec/a.jpg",
                ]
            if prefix == cfg.images_prefix:
                return [f"post-processed/korean/a.png"]
            if prefix == f"{cfg.pre_processed_prefix}in-flight/":
                return [f"pre-processed/korean/in-flight/exec/a.jpg"]
            return []

        with patch(
            "music_assembler.r2_storage.list_object_keys", side_effect=fake_list
        ):
            keys = list_claimable_pre_processed_keys(
                client,
                cfg.bucket,
                pre_processed_prefix=cfg.pre_processed_prefix,
                images_prefix=cfg.images_prefix,
                force=False,
            )

        self.assertEqual(keys, ["pre-processed/korean/b.jpg"])

    def test_claim_returns_none_when_empty(self) -> None:
        cfg = _cfg()
        client = MagicMock()
        with patch(
            "music_assembler.r2_storage.list_claimable_pre_processed_keys",
            return_value=[],
        ):
            claimed = claim_pre_processed_on_r2(
                client,
                cfg.bucket,
                pre_processed_prefix=cfg.pre_processed_prefix,
                images_prefix=cfg.images_prefix,
                execution_id="ext_test",
            )
        self.assertIsNone(claimed)

    def test_retire_overwrites_stale_used_source(self) -> None:
        cfg = _cfg()
        client = MagicMock()
        in_flight = f"{cfg.pre_processed_prefix}in-flight/ext_test/photo.jpg"
        used = f"{cfg.used_pre_processed_prefix}photo.jpg"

        with patch(
            "music_assembler.r2_storage.object_exists",
            side_effect=[True, True, False],
        ), patch(
            "music_assembler.r2_storage.copy_then_delete_object",
        ) as move:
            retired = retire_claimed_pre_processed_on_r2(
                client,
                cfg.bucket,
                pre_processed_prefix=cfg.pre_processed_prefix,
                used_pre_processed_prefix=cfg.used_pre_processed_prefix,
                execution_id="ext_test",
                filename="photo.jpg",
            )

        self.assertTrue(retired)
        move.assert_called_once_with(client, cfg.bucket, in_flight, used)
        client.delete_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
