"""Regression tests for mixed-result image-extension batches."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from music_assembler import extend_from_r2
from music_assembler.api import extend_runner


_PARTIAL_RESULT = {
    "ok": 2,
    "failed": 1,
    "processed": 3,
    "failures": [{"key": "pre-processed/korean/bad.jpg", "error": "blocked"}],
    "cancelled": False,
    "empty": False,
}


class PartialExtendFailureTests(unittest.TestCase):
    def test_cloud_worker_records_partial_batch_as_failed(self) -> None:
        cfg = SimpleNamespace(
            bucket="bucket",
            category="korean",
            pre_processed_prefix="pre-processed/korean/",
        )
        prefixes = SimpleNamespace(
            source_folder="korean",
            pre_processed_prefix="pre-processed/korean/",
        )
        write_progress = MagicMock()
        env = {
            "EXTEND_EXECUTION_ID": "ext_test",
            "ASSEMBLY_CATEGORY": "korean",
            "EXTEND_SOURCE_FOLDER": "korean",
        }

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(extend_from_r2, "load_dotenv"),
            patch.object(extend_from_r2, "r2_config_from_env", return_value=cfg),
            patch.object(extend_from_r2, "r2_client", return_value=MagicMock()),
            patch.object(extend_from_r2, "extend_prefixes_for_config", return_value=prefixes),
            patch.object(
                extend_from_r2,
                "run_extend_cloud_worker",
                return_value=dict(_PARTIAL_RESULT),
            ),
            patch(
                "music_assembler.job_progress.write_progress_json",
                write_progress,
            ),
        ):
            exit_code = extend_from_r2.main([])

        self.assertEqual(exit_code, 1)
        self.assertEqual(write_progress.call_args.kwargs["status"], "failed")
        self.assertIn("failed 1", write_progress.call_args.kwargs["stage"])

    def test_local_runner_records_partial_batch_as_failed(self) -> None:
        write_progress = MagicMock()
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test"}, clear=True),
            patch.object(extend_runner, "r2_config_from_env") as config,
            patch.object(extend_runner, "r2_client", return_value=MagicMock()),
            patch.object(extend_runner, "make_extend_cancel_checker", return_value=lambda: False),
            patch.object(
                extend_runner,
                "run_extend_cloud_worker",
                return_value=dict(_PARTIAL_RESULT),
            ),
            patch.object(extend_runner, "write_progress_json", write_progress),
        ):
            config.return_value = SimpleNamespace(bucket="bucket")
            extend_runner.run_extend_job(
                "ext_test",
                category="korean",
                source_folder="korean",
                max_images=3,
            )

        self.assertEqual(write_progress.call_args.kwargs["status"], "failed")
        self.assertIn("failed 1", write_progress.call_args.kwargs["stage"])

    def test_cli_returns_failure_when_only_part_of_batch_failed(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(extend_from_r2, "load_dotenv"),
            patch.object(
                extend_from_r2,
                "run_extend_from_r2",
                return_value={
                    "pending": 3,
                    "ok": 2,
                    "failures": list(_PARTIAL_RESULT["failures"]),
                },
            ),
        ):
            exit_code = extend_from_r2.main(["--category", "korean"])

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
