"""Tests for partial failures while starting parallel extend jobs."""

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
    monkeypatch.setattr(
        app_module, "_assert_pre_processed_folder_exists", lambda *_args: None
    )
    monkeypatch.setattr(app_module, "_invalidate_category_cache", lambda *_args: None)
    monkeypatch.setattr(app_module, "r2_config_from_env", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(app_module, "count_pending_r2_sources", lambda *_args, **_kwargs: 3)
    ids = iter(("ext_1", "ext_2", "ext_3"))
    monkeypatch.setattr(app_module, "_new_extend_id", lambda: next(ids))


def test_parallel_start_reports_jobs_started_before_failure(monkeypatch: pytest.MonkeyPatch):
    _prepare_start(monkeypatch)
    started = {
        "execution_id": "ext_1",
        "status": "running",
        "gcp_execution_id": "gcp-1",
    }
    queue = MagicMock(
        side_effect=[
            started,
            HTTPException(status_code=502, detail="Cloud Run quota exhausted"),
        ]
    )
    monkeypatch.setattr(app_module, "_queue_extend_job", queue)

    response = app_module.start_extend(
        app_module.StartExtendRequest(source_folder="korean", limit=3),
        settings=_settings(),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 207
    payload = json.loads(response.body)
    assert payload["partial"] is True
    assert payload["jobs"] == [started]
    assert payload["batch_size"] == 1
    assert payload["requested_batch_size"] == 3
    assert payload["failed_job"] == {
        "execution_id": "ext_2",
        "detail": "Cloud Run quota exhausted",
    }
    assert queue.call_count == 2


def test_parallel_start_preserves_total_failure_status(monkeypatch: pytest.MonkeyPatch):
    _prepare_start(monkeypatch)
    failure = HTTPException(status_code=502, detail="Cloud Run unavailable")
    monkeypatch.setattr(
        app_module, "_queue_extend_job", MagicMock(side_effect=failure)
    )

    with pytest.raises(HTTPException) as exc_info:
        app_module.start_extend(
            app_module.StartExtendRequest(source_folder="korean", limit=3),
            settings=_settings(),
        )

    assert exc_info.value is failure
