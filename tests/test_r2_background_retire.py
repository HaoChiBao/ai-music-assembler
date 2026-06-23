"""Tests for R2 background claim / retire (copy + delete)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

from music_assembler.r2_storage import (
    claim_background_on_r2,
    copy_then_delete_object,
    in_flight_key,
    retire_claimed_background_on_r2,
    retire_used_background_on_r2,
    verify_background_retired_on_r2,
)

BUCKET = "test-bucket"
IMAGES = "post-processed/korean/"
USED = "post-processed/korean/used/"
EXEC = "asm_20260623_test_abc123"
FILE = "bg_test.png"


class _FakeClient:
  """Minimal S3 client stub tracking object keys."""

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
      key = kwargs["Key"]
      self._keys.discard(key)


def _exists(client: _FakeClient, key: str) -> bool:
    from music_assembler.r2_storage import object_exists

    return object_exists(client, key)  # type: ignore[arg-type]


def _patch_exists(client: _FakeClient):
    import music_assembler.r2_storage as mod

    mod.object_exists = lambda _c, _b, key: key in client._keys  # type: ignore[assignment]
    mod.list_object_keys = lambda _c, _b, prefix, **kw: [  # type: ignore[assignment]
        k for k in client._keys if k.startswith(prefix) and "/" not in k[len(prefix) :]
    ]
    mod.list_in_flight_background_names = lambda *_a, **_k: set()  # type: ignore[assignment]


class TestCopyThenDelete(unittest.TestCase):
    def test_moves_object_to_dest_and_removes_source(self) -> None:
        client = _FakeClient({f"{IMAGES}{FILE}"})
        import music_assembler.r2_storage as mod

        mod.object_exists = lambda _c, _b, key: key in client._keys  # type: ignore[assignment]
        dest = f"{USED}{FILE}"
        copy_then_delete_object(client, BUCKET, f"{IMAGES}{FILE}", dest)  # type: ignore[arg-type]
        self.assertIn(dest, client._keys)
        self.assertNotIn(f"{IMAGES}{FILE}", client._keys)
        client.copy_object.assert_called_once()
        client.delete_object.assert_called_once()


class TestClaimAndRetire(unittest.TestCase):
    def test_dashboard_job_lifecycle(self) -> None:
        pool = f"{IMAGES}{FILE}"
        client = _FakeClient({pool})
        _patch_exists(client)

        claimed = claim_background_on_r2(
            client,  # type: ignore[arg-type]
            BUCKET,
            images_prefix=IMAGES,
            execution_id=EXEC,
        )
        self.assertEqual(claimed, FILE)
        flight = in_flight_key(IMAGES, EXEC, FILE)
        self.assertIn(flight, client._keys)
        self.assertNotIn(pool, client._keys)

        ok = retire_claimed_background_on_r2(
            client,  # type: ignore[arg-type]
            BUCKET,
            images_prefix=IMAGES,
            used_images_prefix=USED,
            execution_id=EXEC,
            filename=FILE,
        )
        self.assertTrue(ok)
        check = verify_background_retired_on_r2(
            client,  # type: ignore[arg-type]
            BUCKET,
            images_prefix=IMAGES,
            used_images_prefix=USED,
            filename=FILE,
            execution_id=EXEC,
        )
        self.assertTrue(check["in_used"])
        self.assertFalse(check["in_pool"])
        self.assertFalse(check["in_flight"])

    def test_retire_from_pool_without_claim(self) -> None:
        pool = f"{IMAGES}{FILE}"
        client = _FakeClient({pool})
        _patch_exists(client)

        ok = retire_used_background_on_r2(
            client,  # type: ignore[arg-type]
            BUCKET,
            images_prefix=IMAGES,
            used_images_prefix=USED,
            filename=FILE,
            local_used_path=None,
        )
        self.assertTrue(ok)
        self.assertIn(f"{USED}{FILE}", client._keys)
        self.assertNotIn(pool, client._keys)


if __name__ == "__main__":
    unittest.main()
