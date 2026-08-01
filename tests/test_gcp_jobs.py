"""Tests for Cloud Run job environment overrides."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from music_assembler.api import gcp_jobs


def test_start_extend_job_sets_requested_aspect_ratio(monkeypatch):
    class EnvVar:
        def __init__(self, *, name: str, value: str):
            self.name = name
            self.value = value

    monkeypatch.setattr(gcp_jobs, "run_v2", SimpleNamespace(EnvVar=EnvVar))
    captured = {}

    def run_cloud_job(*args, **kwargs):
        captured.update(kwargs)
        return {"gcp_execution_id": "extend-1"}

    monkeypatch.setattr(gcp_jobs, "_run_cloud_job", run_cloud_job)
    settings = MagicMock(
        extend_job_resource="projects/test/locations/test/jobs/music-extend",
        extend_job_name="music-extend",
    )

    gcp_jobs.start_extend_job(
        settings,
        execution_id="ext_1",
        category="korean",
        aspect_ratio="9:16",
    )

    env = {item.name: item.value for item in captured["env"]}
    assert env["GEMINI_ASPECT_RATIO"] == "9:16"
