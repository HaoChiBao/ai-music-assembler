"""Tests for parallel assembly fixes (unique basenames, claim races, health audit)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from music_assembler.api.assembly_health import verify_assembly_run_output
from music_assembler.assemble_options import unique_output_basename
from music_assembler.r2_storage import (
    _background_claim_winner,
    claim_background_on_r2,
    in_flight_key,
)

BUCKET = "test-bucket"
IMAGES = "post-processed/korean/"
FILE = "bg_test.png"


class TestUniqueOutputBasename(unittest.TestCase):
    def test_includes_execution_suffix(self) -> None:
        a = unique_output_basename("asm_20260630_201722_e278a4ed")
        b = unique_output_basename("asm_20260630_201722_abcdef12")
        self.assertTrue(a.startswith("mv_"))
        self.assertIn("e278a4ed", a)
        self.assertIn("abcdef12", b)
        self.assertNotEqual(a, b)


class TestBackgroundClaimRace(unittest.TestCase):
    def test_deterministic_winner_prefers_lexicographic_execution_id(self) -> None:
        claims = [
            ("asm_z", in_flight_key(IMAGES, "asm_z", FILE)),
            ("asm_a", in_flight_key(IMAGES, "asm_a", FILE)),
        ]
        self.assertEqual(_background_claim_winner(claims), "asm_a")

    def test_second_worker_does_not_keep_duplicate_after_first_claimed(self) -> None:
        pool = f"{IMAGES}{FILE}"
        client = _FakeClient({pool})
        _patch_exists(client)

        first = claim_background_on_r2(
            client,  # type: ignore[arg-type]
            BUCKET,
            images_prefix=IMAGES,
            execution_id="asm_first",
        )
        second = claim_background_on_r2(
            client,  # type: ignore[arg-type]
            BUCKET,
            images_prefix=IMAGES,
            execution_id="asm_second",
        )
        self.assertEqual(first, FILE)
        self.assertIsNone(second)


class _FakeClient:
    def __init__(self, keys: set[str]) -> None:
        self._keys = set(keys)
        self.copy_object = MagicMock(side_effect=self._copy)
        self.delete_object = MagicMock(side_effect=self._delete)
        self.exceptions = type("E", (), {"ClientError": Exception})()

    def _copy(self, **kwargs) -> None:
        dest = kwargs["Key"]
        src = kwargs["CopySource"]["Key"]
        if src not in self._keys:
            raise self.exceptions.ClientError("NoSuchKey")
        self._keys.add(dest)

    def _delete(self, **kwargs) -> None:
        self._keys.discard(kwargs["Key"])


def _patch_exists(client: _FakeClient):
    import music_assembler.r2_storage as mod

    mod.object_exists = lambda _c, _b, key: key in client._keys  # type: ignore[assignment]
    mod.list_object_keys = lambda _c, _b, prefix, **kw: [  # type: ignore[assignment]
        k for k in client._keys if k.startswith(prefix) and "/" not in k[len(prefix) :]
    ]

    def _claims(_c, _b, images_prefix: str):
        prefix = f"{images_prefix}in-flight/"
        out: dict[str, list[tuple[str, str]]] = {}
        for key in client._keys:
            if not key.startswith(prefix):
                continue
            rel = key[len(prefix) :]
            parts = rel.split("/", 1)
            if len(parts) != 2:
                continue
            out.setdefault(parts[1], []).append((parts[0], key))
        for rows in out.values():
            rows.sort(key=lambda row: row[0])
        return out

    mod.list_in_flight_background_claims = _claims  # type: ignore[assignment]


class TestAssemblyHealth(unittest.TestCase):
    def test_flags_succeeded_without_video(self) -> None:
        import music_assembler.api.assembly_health as health

        health.object_exists = lambda *_a, **_k: False  # type: ignore[assignment]
        run = {
            "execution_id": "asm_test",
            "channel": "listen-omyo",
            "progress": {"status": "succeeded", "video_id": "mv_20260630_201906"},
        }
        row = verify_assembly_run_output(MagicMock(), "bucket", run)
        self.assertFalse(row["healthy"])
        self.assertIn("not found", row["issue"] or "")


if __name__ == "__main__":
    unittest.main()
