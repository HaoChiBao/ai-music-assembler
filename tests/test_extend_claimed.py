"""Focused failure-boundary tests for extending a claimed R2 source."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import MagicMock, patch

from music_assembler.extend_from_r2 import (
    extend_one_claimed_on_r2,
    run_extend_cloud_worker,
)
from music_assembler.r2_storage import R2Config


def _cfg() -> R2Config:
    return R2Config(
        bucket="b",
        endpoint="https://example.com",
        access_key_id="k",
        secret_access_key="s",
        category="korean",
    )


def _settings() -> dict[str, object]:
    return {
        "retries": 0,
        "backoff": 0,
        "model": "test-model",
        "prompt": "test prompt",
        "aspect": "16:9",
        "img_size": "1K",
        "out_w": None,
    }


class ExtendOneClaimedFailureTests(unittest.TestCase):
    def test_download_exception_releases_claim_and_returns_failure(self) -> None:
        client = MagicMock()
        client.download_file.side_effect = RuntimeError("download failed")
        with TemporaryDirectory() as tmp, patch(
            "music_assembler.extend_from_r2.extend_one_with_retry"
        ) as extend, patch(
            "music_assembler.extend_from_r2.release_pre_processed_claim",
            return_value=True,
        ) as release:
            result = extend_one_claimed_on_r2(
                client=client,
                cfg=_cfg(),
                execution_id="ext_test",
                filename="photo.jpg",
                work_dir=Path(tmp),
                gemini_client=MagicMock(),
                gemini_settings=_settings(),
            )

        self.assertEqual(result, (False, "input preparation failed: download failed"))
        release.assert_called_once()
        extend.assert_not_called()

    def test_upload_exception_releases_claim_and_returns_failure(self) -> None:
        client = MagicMock()
        with TemporaryDirectory() as tmp, patch(
            "music_assembler.extend_from_r2.extend_one_with_retry"
        ), patch(
            "music_assembler.extend_from_r2.upload_file",
            side_effect=RuntimeError("upload failed"),
        ), patch(
            "music_assembler.extend_from_r2.release_pre_processed_claim",
            return_value=True,
        ) as release, patch(
            "music_assembler.extend_from_r2.retire_claimed_pre_processed_on_r2"
        ) as retire:
            result = extend_one_claimed_on_r2(
                client=client,
                cfg=_cfg(),
                execution_id="ext_test",
                filename="photo.jpg",
                work_dir=Path(tmp),
                gemini_client=MagicMock(),
                gemini_settings=_settings(),
            )

        self.assertEqual(result, (False, "upload failed: upload failed"))
        release.assert_called_once()
        retire.assert_not_called()
        client.delete_object.assert_not_called()

    def test_false_retirement_releases_claim_and_returns_failure(self) -> None:
        client = MagicMock()
        with TemporaryDirectory() as tmp, patch(
            "music_assembler.extend_from_r2.extend_one_with_retry"
        ), patch(
            "music_assembler.extend_from_r2.upload_file"
        ) as upload, patch(
            "music_assembler.extend_from_r2.retire_claimed_pre_processed_on_r2",
            return_value=False,
        ) as retire, patch(
            "music_assembler.extend_from_r2.release_pre_processed_claim",
            return_value=False,
        ) as release:
            result = extend_one_claimed_on_r2(
                client=client,
                cfg=_cfg(),
                execution_id="ext_test",
                filename="photo.jpg",
                work_dir=Path(tmp),
                gemini_client=MagicMock(),
                gemini_settings=_settings(),
            )

        self.assertFalse(result[0])
        self.assertIn("retire", result[1] or "")
        self.assertIn("claim release did not complete", result[1] or "")
        upload.assert_called_once()
        retire.assert_called_once()
        release.assert_called_once()
        client.delete_object.assert_not_called()

    def test_retirement_exception_releases_claim_and_returns_failure(self) -> None:
        client = MagicMock()
        with TemporaryDirectory() as tmp, patch(
            "music_assembler.extend_from_r2.extend_one_with_retry"
        ), patch(
            "music_assembler.extend_from_r2.upload_file"
        ) as upload, patch(
            "music_assembler.extend_from_r2.retire_claimed_pre_processed_on_r2",
            side_effect=RuntimeError("R2 finalize unavailable"),
        ) as retire, patch(
            "music_assembler.extend_from_r2.release_pre_processed_claim",
            return_value=True,
        ) as release:
            result = extend_one_claimed_on_r2(
                client=client,
                cfg=_cfg(),
                execution_id="ext_test",
                filename="photo.jpg",
                work_dir=Path(tmp),
                gemini_client=MagicMock(),
                gemini_settings=_settings(),
            )

        self.assertEqual(result, (False, "retire failed: R2 finalize unavailable"))
        upload.assert_called_once()
        retire.assert_called_once()
        release.assert_called_once()
        client.delete_object.assert_not_called()

    def test_upload_and_release_exceptions_preserve_both_errors(self) -> None:
        client = MagicMock()
        with TemporaryDirectory() as tmp, patch(
            "music_assembler.extend_from_r2.extend_one_with_retry"
        ), patch(
            "music_assembler.extend_from_r2.upload_file",
            side_effect=RuntimeError("upload timeout"),
        ), patch(
            "music_assembler.extend_from_r2.release_pre_processed_claim",
            side_effect=RuntimeError("release timeout"),
        ) as release:
            result = extend_one_claimed_on_r2(
                client=client,
                cfg=_cfg(),
                execution_id="ext_test",
                filename="photo.jpg",
                work_dir=Path(tmp),
                gemini_client=MagicMock(),
                gemini_settings=_settings(),
            )

        self.assertFalse(result[0])
        self.assertIn("upload timeout", result[1] or "")
        self.assertIn("release timeout", result[1] or "")
        release.assert_called_once()
        client.delete_object.assert_not_called()


class ExtendCloudWorkerFailureTests(unittest.TestCase):
    def test_process_all_stops_after_failed_claim_instead_of_reclaiming_it(self) -> None:
        client = MagicMock()
        google_module = ModuleType("google")
        genai_module = ModuleType("google.genai")
        genai_module.Client = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
        google_module.genai = genai_module  # type: ignore[attr-defined]

        claims = 0

        def claim_again(*args, **kwargs):
            nonlocal claims
            claims += 1
            if claims > 1:
                raise AssertionError("worker reclaimed the failed filename")
            return "photo.jpg"

        with TemporaryDirectory() as tmp, patch.dict(
            "music_assembler.extend_from_r2.os.environ",
            {"GEMINI_API_KEY": "test-key"},
        ), patch.dict(
            sys.modules,
            {"google": google_module, "google.genai": genai_module},
        ), patch(
            "music_assembler.extend_from_r2.r2_config_from_env",
            return_value=_cfg(),
        ), patch(
            "music_assembler.extend_from_r2.r2_client",
            return_value=client,
        ), patch(
            "music_assembler.extend_from_r2._extend_gemini_settings",
            return_value=_settings(),
        ), patch(
            "music_assembler.extend_from_r2.claim_pre_processed_on_r2",
            side_effect=claim_again,
        ) as claim, patch(
            "music_assembler.extend_from_r2.extend_one_claimed_on_r2",
            return_value=(False, "Gemini failed"),
        ):
            result = run_extend_cloud_worker(
                "ext_test",
                work_dir=Path(tmp),
                max_images=None,
            )

        self.assertEqual(result["ok"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["processed"], 1)
        claim.assert_called_once()


if __name__ == "__main__":
    unittest.main()
