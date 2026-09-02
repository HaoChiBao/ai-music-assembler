"""Regression tests for assembly job schedule timestamp validation."""

from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from music_assembler.api import app as app_module
from music_assembler.api.auth import require_api_auth


@pytest.mark.parametrize("field", ["publish_at", "upload_at"])
@pytest.mark.parametrize("value", ["2026-07-14T16:00:0OZ", "2026-13-40T16:00:00Z"])
def test_start_job_api_rejects_malformed_schedule_without_dispatch(field, value):
    app_module.app.dependency_overrides[require_api_auth] = lambda: None
    payload = {
        "channel": "nappabeats",
        "images_folder": "korean",
        "upload_schedule_publish": True,
        field: value,
    }
    try:
        with mock.patch.object(app_module.gcp_jobs, "start_assembly_job") as start_assembly_job:
            response = TestClient(app_module.app).post("/v1/assembly/jobs", json=payload)
    finally:
        app_module.app.dependency_overrides.pop(require_api_auth, None)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == field
    start_assembly_job.assert_not_called()
