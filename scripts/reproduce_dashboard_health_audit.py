"""Deterministic reproduction harness for dashboard health-audit R2 amplification."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from music_assembler.api import app as app_module
from music_assembler.api import assembly_health
from music_assembler.api.cache import TTLCache
from music_assembler.api.config import ApiSettings


RUN_COUNT = 50


def _runs() -> list[dict[str, object]]:
    return [
        {
            "execution_id": f"asm_debug_{index:03}",
            "category": "korean",
            "images_folder": "korean",
            "channel": "listen-omyo",
            "claimed_background": f"bg_{index:03}.png",
            "progress": {
                "status": "succeeded",
                "video_id": f"mv_debug_{index:03}",
            },
        }
        for index in range(RUN_COUNT)
    ]


class _Paginator:
    def __init__(self, client: "_CountingClient") -> None:
        self.client = client

    def paginate(self, **kwargs):
        with self.client.lock:
            self.client.list_calls += 1
            self.client.list_prefixes.append(kwargs["Prefix"])
        time.sleep(0.002)
        yield {"Contents": []}


class _CountingClient:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.list_calls = 0
        self.list_prefixes: list[str] = []
        self.head_calls = 0
        self.exceptions = SimpleNamespace(ClientError=RuntimeError)

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return _Paginator(self)

    def head_object(self, **_kwargs) -> dict[str, object]:
        with self.lock:
            self.head_calls += 1
        time.sleep(0.002)
        return {}

    def reset(self) -> None:
        with self.lock:
            self.list_calls = 0
            self.list_prefixes.clear()
            self.head_calls = 0


def _settings() -> ApiSettings:
    return ApiSettings(
        api_key=None,
        dashboard_password=None,
        gcp_project="debug",
        gcp_region="debug",
        assembly_job_name="debug",
        extend_job_name="debug",
        extend_use_gcp=False,
        default_category="korean",
        configured_channels=(),
        uploader_api_url=None,
        uploader_api_key=None,
    )


def main() -> None:
    runs = _runs()
    client = _CountingClient()
    app_module._r2 = lambda: (client, "debug-bucket")
    app_module.job_runs.list_r2_job_runs = (
        lambda _client, _bucket, *, id_prefix, **_kwargs: runs if id_prefix == "asm_" else []
    )
    app_module.job_status.reconcile_assembly_runs = lambda *_args, **_kwargs: runs
    app_module.job_status.reconcile_extend_runs = lambda *_args, **_kwargs: []
    app_module.job_status.runs_need_gcp_reconcile = lambda _runs: False
    app_module.r2_catalog.dashboard_inventory = lambda *_args: {}
    app_module.count_pending_r2_sources = lambda *_args, **_kwargs: 0
    app_module.r2_config_from_env = lambda **_kwargs: object()
    assembly_health.gcp_jobs.list_executions = lambda *_args, **_kwargs: []

    request_barrier: threading.Barrier | None = None

    def request_snapshot() -> None:
        if request_barrier is not None:
            request_barrier.wait(timeout=5)
        app_module.dashboard_snapshot(
            category="korean",
            light=False,
            refresh=False,
            job_limit=RUN_COUNT,
            _auth=None,
            settings=_settings(),
        )

    app_module.dashboard_cache = TTLCache()
    request_snapshot()
    print(
        json.dumps(
            {
                "scenario": "one cold dashboard request",
                "runs": RUN_COUNT,
                "list_calls": client.list_calls,
                "unique_list_prefixes": len(set(client.list_prefixes)),
                "head_calls": client.head_calls,
            },
            sort_keys=True,
        )
    )

    client.reset()
    app_module.dashboard_cache = TTLCache()
    request_barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request_snapshot) for _ in range(2)]
        for future in futures:
            future.result()
    print(
        json.dumps(
            {
                "scenario": "two concurrent cold dashboard requests",
                "runs_per_request": RUN_COUNT,
                "list_calls": client.list_calls,
                "unique_list_prefixes": len(set(client.list_prefixes)),
                "head_calls": client.head_calls,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
