"""Tests for atomic pre-processed claim helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from music_assembler.r2_storage import (
    R2Config,
    _pre_processed_claim_winner,
    claim_pre_processed_on_r2,
    list_claimable_pre_processed_keys,
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

    def test_claim_race_loser_deletes_copy_and_retries(self) -> None:
        cfg = _cfg()
        client = MagicMock()
        client.exceptions.ClientError = RuntimeError
        first = f"{cfg.pre_processed_prefix}a.jpg"
        second = f"{cfg.pre_processed_prefix}b.jpg"
        client.delete_object = MagicMock()

        with (
            patch(
                "music_assembler.r2_storage.list_claimable_pre_processed_keys",
                side_effect=[[first], [second]],
            ),
            patch("music_assembler.r2_storage.object_exists", return_value=True),
            patch("music_assembler.r2_storage.copy_then_delete_object"),
            patch(
                "music_assembler.r2_storage.list_in_flight_pre_processed_claims",
                side_effect=[
                    {
                        "a.jpg": [
                            (
                                "ext_a",
                                f"{cfg.pre_processed_prefix}in-flight/ext_a/a.jpg",
                            ),
                            (
                                "ext_z",
                                f"{cfg.pre_processed_prefix}in-flight/ext_z/a.jpg",
                            ),
                        ]
                    },
                    {
                        "b.jpg": [
                            (
                                "ext_z",
                                f"{cfg.pre_processed_prefix}in-flight/ext_z/b.jpg",
                            )
                        ]
                    },
                ],
            ),
        ):
            claimed = claim_pre_processed_on_r2(
                client,
                cfg.bucket,
                pre_processed_prefix=cfg.pre_processed_prefix,
                images_prefix=cfg.images_prefix,
                execution_id="ext_z",
            )

        self.assertEqual(claimed, "b.jpg")
        client.delete_object.assert_called_once_with(
            Bucket=cfg.bucket,
            Key=f"{cfg.pre_processed_prefix}in-flight/ext_z/a.jpg",
        )

    def test_claim_race_winner_keeps_copy(self) -> None:
        cfg = _cfg()
        client = MagicMock()
        client.exceptions.ClientError = RuntimeError
        src = f"{cfg.pre_processed_prefix}a.jpg"
        claims = [
            ("ext_a", f"{cfg.pre_processed_prefix}in-flight/ext_a/a.jpg"),
            ("ext_z", f"{cfg.pre_processed_prefix}in-flight/ext_z/a.jpg"),
        ]

        with (
            patch(
                "music_assembler.r2_storage.list_claimable_pre_processed_keys",
                return_value=[src],
            ),
            patch("music_assembler.r2_storage.object_exists", return_value=True),
            patch("music_assembler.r2_storage.copy_then_delete_object"),
            patch(
                "music_assembler.r2_storage.list_in_flight_pre_processed_claims",
                return_value={"a.jpg": claims},
            ),
        ):
            claimed = claim_pre_processed_on_r2(
                client,
                cfg.bucket,
                pre_processed_prefix=cfg.pre_processed_prefix,
                images_prefix=cfg.images_prefix,
                execution_id="ext_a",
            )

        self.assertEqual(claimed, "a.jpg")
        client.delete_object.assert_not_called()
        self.assertEqual(_pre_processed_claim_winner(claims), "ext_a")


if __name__ == "__main__":
    unittest.main()
