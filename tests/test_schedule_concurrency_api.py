"""Focused API coverage for schedule optimistic concurrency."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from music_assembler.api import app as api_app
from music_assembler.api import assembly_schedule


def _request(*, if_match: str | None = None, if_none_match: str | None = None) -> Request:
    headers = []
    if if_match:
        headers.append((b"if-match", if_match.encode("ascii")))
    if if_none_match:
        headers.append((b"if-none-match", if_none_match.encode("ascii")))
    return Request({"type": "http", "headers": headers})


def _body() -> api_app.ChannelScheduleRequest:
    return api_app.ChannelScheduleRequest(images_folder="pool-a")


def test_get_schedule_returns_revision_from_same_snapshot(monkeypatch):
    schedule = assembly_schedule.ChannelSchedule(channel="ch", images_folder="pool-a")
    monkeypatch.setattr(api_app, "_r2", lambda: (object(), "bucket"))
    monkeypatch.setattr(
        assembly_schedule,
        "get_schedule_with_revision",
        lambda *_args: (schedule, '"revision-1"'),
    )
    response = Response()

    result = api_app.get_schedule("ch", response)

    assert result["channel"] == "ch"
    assert response.headers["etag"] == '"revision-1"'


def test_get_missing_schedule_returns_document_revision(monkeypatch):
    monkeypatch.setattr(api_app, "_r2", lambda: (object(), "bucket"))
    monkeypatch.setattr(
        assembly_schedule,
        "get_schedule_with_revision",
        lambda *_args: (None, '"revision-1"'),
    )

    with pytest.raises(HTTPException) as exc_info:
        api_app.get_schedule("new-channel", Response())

    assert exc_info.value.status_code == 404
    assert exc_info.value.headers == {"ETag": '"revision-1"'}


def test_put_schedule_requires_loaded_revision():
    with pytest.raises(HTTPException) as exc_info:
        api_app.put_schedule("ch", _body(), _request())

    assert exc_info.value.status_code == 428
    assert "reload" in str(exc_info.value.detail).lower()


def test_put_schedule_maps_storage_precondition_failure_to_412(monkeypatch):
    monkeypatch.setattr(api_app, "_r2", lambda: (object(), "bucket"))
    monkeypatch.setattr(api_app, "_assert_background_folder_exists", lambda *_args: None)

    def conflict(*_args, **_kwargs):
        raise assembly_schedule.ScheduleRevisionConflict("changed")

    monkeypatch.setattr(assembly_schedule, "upsert_schedule_if_revision", conflict)

    with pytest.raises(HTTPException) as exc_info:
        api_app.put_schedule(
            "ch",
            _body(),
            _request(if_match='"revision-1"'),
        )

    assert exc_info.value.status_code == 412
    assert "changed" in str(exc_info.value.detail).lower()
    assert "reload" in str(exc_info.value.detail).lower()


def test_put_schedule_returns_new_revision(monkeypatch):
    schedule = assembly_schedule.ChannelSchedule(channel="ch", images_folder="pool-a")
    monkeypatch.setattr(api_app, "_r2", lambda: (object(), "bucket"))
    monkeypatch.setattr(api_app, "_assert_background_folder_exists", lambda *_args: None)
    monkeypatch.setattr(
        assembly_schedule,
        "upsert_schedule_if_revision",
        lambda *_args, **_kwargs: (schedule, '"revision-2"'),
    )

    response = api_app.put_schedule(
        "ch",
        _body(),
        _request(if_match='"revision-1"'),
    )

    assert response.status_code == 200
    assert response.headers["etag"] == '"revision-2"'
