"""Regression tests for dashboard health-audit request amplification."""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from music_assembler.api import app as app_module
from music_assembler.api import assembly_health
from music_assembler.api.cache import TTLCache
from music_assembler.api.config import ApiSettings


RUN_COUNT = 50


class _Paginator:
    def __init__(self, client: "_CountingClient") -> None:
        self.client = client

    def paginate(self, **kwargs):
        with self.client.lock:
            self.client.list_calls += 1
        time.sleep(0.002)
        yield {"Contents": []}


class _CountingClient:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.list_calls = 0
        self.head_calls = 0
        self.exceptions = SimpleNamespace(ClientError=RuntimeError)

    def get_paginator(self, name: str) -> _Paginator:
        self.assert_paginator_name(name)
        return _Paginator(self)

    @staticmethod
    def assert_paginator_name(name: str) -> None:
        if name != "list_objects_v2":
            raise AssertionError(f"unexpected paginator: {name}")

    def head_object(self, **_kwargs) -> dict[str, object]:
        with self.lock:
            self.head_calls += 1
        time.sleep(0.002)
        return {}


def _settings() -> ApiSettings:
    return ApiSettings(
        api_key=None,
        dashboard_password=None,
        gcp_project="test",
        gcp_region="test",
        assembly_job_name="test",
        extend_job_name="test",
        extend_use_gcp=False,
        default_category="korean",
        configured_channels=(),
        uploader_api_url=None,
        uploader_api_key=None,
    )


def _runs() -> list[dict[str, object]]:
    return [
        {
            "execution_id": f"asm_{index:03}",
            "category": "korean",
            "images_folder": "korean",
            "channel": "listen-omyo",
            "claimed_background": f"bg_{index:03}.png",
            "progress": {
                "status": "succeeded",
                "video_id": f"mv_{index:03}",
            },
        }
        for index in range(RUN_COUNT)
    ]


class TestDashboardHealthAuditHarness(unittest.TestCase):
    def test_real_dashboard_path_deduplicates_cold_health_audits(self) -> None:
        runs = _runs()
        client = _CountingClient()

        def object_exists(r2_client, bucket: str, key: str) -> bool:
            r2_client.head_object(Bucket=bucket, Key=key)
            return True

        def request_snapshot() -> None:
            app_module.dashboard_snapshot(
                category="korean",
                light=False,
                refresh=False,
                job_limit=RUN_COUNT,
                _auth=None,
                settings=_settings(),
            )

        with (
            patch.object(app_module, "_r2", return_value=(client, "test-bucket")),
            patch.object(
                app_module.job_runs,
                "list_r2_job_runs",
                side_effect=lambda _client, _bucket, *, id_prefix, **_kwargs: (
                    runs if id_prefix == "asm_" else []
                ),
            ),
            patch.object(
                app_module.job_status, "reconcile_assembly_runs", return_value=runs
            ),
            patch.object(app_module.job_status, "reconcile_extend_runs", return_value=[]),
            patch.object(
                app_module.job_status, "runs_need_gcp_reconcile", return_value=False
            ),
            patch.object(app_module.r2_catalog, "dashboard_inventory", return_value={}),
            patch.object(app_module, "count_pending_r2_sources", return_value=0),
            patch.object(app_module, "r2_config_from_env", return_value=object()),
            patch.object(assembly_health.gcp_jobs, "list_executions", return_value=[]),
            patch.object(assembly_health, "object_exists", side_effect=object_exists),
            patch.object(app_module, "dashboard_cache", TTLCache()),
        ):
            request_snapshot()
            one_cold = (client.list_calls, client.head_calls)

            client.list_calls = 0
            client.head_calls = 0
            app_module.dashboard_cache = TTLCache()
            start = threading.Barrier(3)

            def concurrent_request() -> None:
                start.wait()
                request_snapshot()

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(concurrent_request) for _ in range(2)]
                start.wait()
                for future in futures:
                    future.result()
            two_concurrent_cold = (client.list_calls, client.head_calls)

        self.assertEqual(
            [one_cold[0], two_concurrent_cold[0]],
            [1, 1],
        )
        self.assertEqual(
            [one_cold[1], two_concurrent_cold[1]],
            [50, 50],
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
