"""Focused coverage for manual extend template/aspect propagation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from music_assembler.api import app as api_app
from music_assembler.api import extend_runner, gcp_jobs


def test_start_extend_job_sets_requested_aspect_ratio(monkeypatch):
    class EnvVar:
        def __init__(self, *, name: str, value: str):
            self.name = name
            self.value = value

    monkeypatch.setattr(gcp_jobs, "run_v2", SimpleNamespace(EnvVar=EnvVar))
    captured = {}
    monkeypatch.setattr(
        gcp_jobs,
        "_run_cloud_job",
        lambda *args, **kwargs: captured.update(kwargs) or {"gcp_execution_id": "gcp-1"},
    )

    gcp_jobs.start_extend_job(
        MagicMock(extend_job_resource="extend-resource", extend_job_name="music-extend"),
        execution_id="ext_1",
        category="korean",
        aspect_ratio="9:16",
    )

    env = {item.name: item.value for item in captured["env"]}
    assert env["GEMINI_ASPECT_RATIO"] == "9:16"


def test_queue_extend_cloud_records_template_and_propagates_aspect(monkeypatch):
    meta = {}
    worker = {}
    monkeypatch.setattr(
        api_app,
        "write_meta_json",
        lambda *args, **kwargs: meta.update(kwargs),
    )
    monkeypatch.setattr(api_app, "write_progress_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        api_app.gcp_jobs,
        "start_extend_job",
        lambda *args, **kwargs: worker.update(kwargs) or {},
    )

    result = api_app._queue_extend_job(
        MagicMock(),
        "bucket",
        SimpleNamespace(extend_use_gcp=True),
        execution_id="ext_1",
        category="korean",
        source_folder="vertical-shorts",
        template_id="shorts_vertical",
        aspect_ratio="9:16",
        max_images=1,
        force=False,
        exclude_gcp_ids=set(),
    )

    assert meta["template_id"] == "shorts_vertical"
    assert worker["aspect_ratio"] == "9:16"
    assert result["template_id"] == "shorts_vertical"
    assert result["aspect_ratio"] == "9:16"


def test_queue_extend_local_propagates_aspect_to_runner(monkeypatch):
    meta = {}
    thread_kwargs = {}

    class Thread:
        def __init__(self, *, target, kwargs, daemon):
            thread_kwargs.update(kwargs)
            assert target is api_app.run_extend_job
            assert daemon is True

        def start(self):
            return None

    monkeypatch.setattr(api_app, "write_meta_json", lambda *args, **kwargs: meta.update(kwargs))
    monkeypatch.setattr(api_app, "write_progress_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_app.threading, "Thread", Thread)

    api_app._queue_extend_job(
        MagicMock(),
        "bucket",
        SimpleNamespace(extend_use_gcp=False),
        execution_id="ext_1",
        category="korean",
        source_folder="vertical-shorts",
        template_id="shorts_vertical",
        aspect_ratio="9:16",
        max_images=1,
        force=False,
        exclude_gcp_ids=set(),
    )

    assert meta["template_id"] == "shorts_vertical"
    assert thread_kwargs["aspect_ratio"] == "9:16"


def test_local_runner_passes_aspect_to_extend_worker(monkeypatch):
    worker = {}
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        extend_runner,
        "r2_config_from_env",
        lambda **kwargs: SimpleNamespace(bucket="bucket"),
    )
    monkeypatch.setattr(extend_runner, "r2_client", lambda cfg: MagicMock())
    monkeypatch.setattr(extend_runner, "make_extend_cancel_checker", lambda *args: lambda: False)
    monkeypatch.setattr(extend_runner, "write_progress_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        extend_runner,
        "run_extend_cloud_worker",
        lambda *args, **kwargs: worker.update(kwargs)
        or {
            "ok": 1,
            "failed": 0,
            "cancelled": False,
            "empty": False,
        },
    )

    extend_runner.run_extend_job(
        "ext_1",
        category="korean",
        source_folder="vertical-shorts",
        aspect_ratio="9:16",
    )

    assert worker["aspect_ratio"] == "9:16"
