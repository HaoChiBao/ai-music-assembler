"""Regression tests for dashboard health-audit request amplification."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from music_assembler.api import assembly_health
from music_assembler.api.cache import TTLCache


class TestDashboardHealthAuditHarness(unittest.TestCase):
    def test_direct_script_uses_checkout_and_real_dashboard_path(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "scripts/reproduce_dashboard_health_audit.py"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        results = [
            json.loads(line)
            for line in completed.stdout.splitlines()
            if line.strip()
        ]

        self.assertEqual(len(results), 2)
        self.assertEqual(
            [result["list_calls"] for result in results],
            [1, 1],
        )
        self.assertEqual(
            [result["head_calls"] for result in results],
            [50, 50],
        )
        self.assertEqual(
            [result["unique_list_prefixes"] for result in results],
            [1, 1],
        )


class TestAssemblyHealthClaimReads(unittest.TestCase):
    def test_lists_in_flight_claims_once_per_unique_prefix(self) -> None:
        runs = [
            {
                "execution_id": f"asm_{index}",
                "images_folder": "korean",
                "channel": "listen-omyo",
                "claimed_background": f"bg_{index}.png",
                "progress": {"status": "succeeded", "video_id": f"mv_{index}"},
            }
            for index in range(50)
        ]

        with (
            patch.object(
                assembly_health, "list_in_flight_background_claims", return_value={}
            ) as list_claims,
            patch.object(assembly_health, "assembly_output_exists", return_value=True),
            patch.object(assembly_health.gcp_jobs, "list_executions", return_value=[]),
        ):
            report = assembly_health.audit_recent_assemblies(
                MagicMock(), MagicMock(), "bucket", runs
            )

        self.assertEqual(report["checked"], 50)
        self.assertEqual(report["healthy"], 50)
        list_claims.assert_called_once_with(
            unittest.mock.ANY, "bucket", "post-processed/korean/"
        )


class TestTTLCacheSingleFlight(unittest.TestCase):
    def test_concurrent_cold_reads_invoke_factory_once(self) -> None:
        cache = TTLCache()
        worker_count = 8
        start = threading.Barrier(worker_count + 1)
        calls = 0
        calls_lock = threading.Lock()

        def factory() -> str:
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return "health"

        def read() -> tuple[str, bool]:
            start.wait()
            return cache.get_or_set("health:korean", 90.0, factory)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(read) for _ in range(worker_count)]
            start.wait()
            results = [future.result() for future in futures]

        self.assertEqual(calls, 1)
        self.assertEqual([value for value, _hit in results], ["health"] * worker_count)
        self.assertEqual(sum(not hit for _value, hit in results), 1)
        self.assertEqual(cache.stats()["misses"], 1)


if __name__ == "__main__":
    unittest.main()
