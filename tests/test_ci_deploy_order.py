"""Regression tests for safe API/worker deployment ordering."""

import re
import unittest
from pathlib import Path


class TestCiDeployOrder(unittest.TestCase):
    def test_workers_are_built_and_updated_before_api_deploy(self) -> None:
        workflow = Path(".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
        steps = [
            "      - name: Build and push API image",
            "      - name: Build and push assemble worker image",
            "      - name: Build and push extend worker image",
            "      - name: Update music-assemble Cloud Run Job",
            "      - name: Update music-extend Cloud Run Job",
            "      - name: Deploy Cloud Run service",
            "      - name: Smoke check",
        ]

        positions = [workflow.index(step) for step in steps]
        self.assertEqual(
            positions,
            sorted(positions),
            "Build every image, update both workers, then expose the matching API",
        )

        build_positions = positions[:3]
        worker_updates = [
            match.start()
            for match in re.finditer(r"\bgcloud run jobs update\b", workflow)
        ]
        api_deploys = [
            match.start()
            for match in re.finditer(r"\bgcloud run deploy\b", workflow)
        ]
        self.assertEqual(len(worker_updates), 2)
        self.assertEqual(len(api_deploys), 1)
        self.assertLess(max(build_positions), min(worker_updates + api_deploys))
        self.assertLess(max(worker_updates), api_deploys[0])
        self.assertLess(api_deploys[0], positions[-1])


if __name__ == "__main__":
    unittest.main()
