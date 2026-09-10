"""Cancellation behavior for the production Cloud Run extend entry point."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from music_assembler import extend_from_r2


class ExtendCloudCancelTests(unittest.TestCase):
    def test_cloud_worker_observes_and_preserves_cancellation(self) -> None:
        cancelled = False
        statuses: list[str] = []

        def should_cancel() -> bool:
            return cancelled

        def run_worker(execution_id: str, **kwargs):
            nonlocal cancelled
            self.assertEqual(execution_id, "ext_cancel")
            self.assertIs(kwargs["should_cancel"], should_cancel)
            cancelled = True
            kwargs["on_progress"](50, "Extended image.jpg")
            return {"cancelled": True}

        def write_progress(_client, _bucket, _execution_id, **kwargs) -> None:
            statuses.append(kwargs["status"])

        cfg = SimpleNamespace(bucket="test-bucket", category="korean")
        prefixes = SimpleNamespace(
            pre_processed_prefix="pre-processed/korean/",
            source_folder="korean",
        )

        with (
            patch.dict(
                os.environ,
                {
                    "EXTEND_EXECUTION_ID": "ext_cancel",
                    "ASSEMBLY_CATEGORY": "korean",
                },
                clear=True,
            ),
            patch.object(extend_from_r2, "load_dotenv"),
            patch.object(extend_from_r2, "r2_config_from_env", return_value=cfg),
            patch.object(extend_from_r2, "r2_client", return_value=MagicMock()),
            patch.object(
                extend_from_r2,
                "extend_prefixes_for_config",
                return_value=prefixes,
            ),
            patch.object(
                extend_from_r2,
                "run_extend_cloud_worker",
                side_effect=run_worker,
            ),
            patch(
                "music_assembler.api.job_cancel.make_extend_cancel_checker",
                return_value=should_cancel,
            ),
            patch(
                "music_assembler.job_progress.write_progress_json",
                side_effect=write_progress,
            ),
        ):
            return_code = extend_from_r2.main([])

        self.assertEqual(return_code, 0)
        self.assertEqual(statuses[0], "running")
        self.assertTrue(all(status != "succeeded" for status in statuses))
        self.assertEqual(statuses[-1], "cancelled")


if __name__ == "__main__":
    unittest.main()
