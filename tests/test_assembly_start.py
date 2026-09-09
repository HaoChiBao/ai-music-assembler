"""Tests for partial failures while starting multiple assembly jobs."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

import music_assembler.api.app as app_module
from music_assembler.api.config import ApiSettings


def _settings() -> ApiSettings:
    return ApiSettings(
        api_key=None,
        dashboard_password=None,
        gcp_project="project",
        gcp_region="region",
        assembly_job_name="assemble",
        extend_job_name="extend",
        extend_use_gcp=True,
        default_category="korean",
        configured_channels=(),
        uploader_api_url=None,
        uploader_api_key=None,
    )


def _prepare_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "_r2", lambda: (MagicMock(), "bucket"))
    monkeypatch.setattr(app_module, "_assert_background_folder_exists", lambda *_args: None)
    monkeypatch.setattr(app_module, "_invalidate_category_cache", lambda *_args: None)
    monkeypatch.setattr(app_module, "write_meta_json", MagicMock())
    monkeypatch.setattr(app_module, "write_progress_json", MagicMock())
    monkeypatch.setattr(app_module, "patch_meta_gcp_execution_id", MagicMock())
    ids = iter(("asm_1", "asm_2", "asm_3"))
    monkeypatch.setattr(app_module, "_new_execution_id", lambda: next(ids))


def _request() -> app_module.StartJobRequest:
    return app_module.StartJobRequest(
        channel="nappabeats",
        images_folder="korean",
        count=3,
        queue_youtube=False,
    )


def test_multi_start_reports_jobs_started_before_failure(monkeypatch: pytest.MonkeyPatch):
    _prepare_start(monkeypatch)
    start = MagicMock(
        side_effect=[
            {"status": "running", "gcp_execution_id": "gcp-1"},
            RuntimeError("Cloud Run quota exhausted"),
        ]
    )
    monkeypatch.setattr(app_module.gcp_jobs, "start_assembly_job", start)

    response = app_module.start_job(_request(), settings=_settings())

    assert isinstance(response, JSONResponse)
    assert response.status_code == 207
    payload = json.loads(response.body)
    assert payload["partial"] is True
    assert payload["jobs"] == [
        {
            "status": "running",
            "gcp_execution_id": "gcp-1",
            "api_execution_id": "asm_1",
        }
    ]
    assert payload["count"] == 1
    assert payload["requested_count"] == 3
    assert payload["failed_job"] == {
        "execution_id": "asm_2",
        "detail": "Failed to start Cloud Run Job: Cloud Run quota exhausted",
    }
    assert start.call_count == 2


def test_multi_start_preserves_total_failure_status(monkeypatch: pytest.MonkeyPatch):
    _prepare_start(monkeypatch)
    monkeypatch.setattr(
        app_module.gcp_jobs,
        "start_assembly_job",
        MagicMock(side_effect=RuntimeError("Cloud Run unavailable")),
    )

    with pytest.raises(HTTPException) as exc_info:
        app_module.start_job(_request(), settings=_settings())

    assert exc_info.value.status_code == 502
    assert "asm_1" in str(exc_info.value.detail)
