"""Tests for assembly schedule module."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from music_assembler.api.assembly_schedule import (
    ChannelSchedule,
    DaySlot,
    ScheduleRevisionConflict,
    apply_default_times,
    due_slots,
    ensure_schedule_upload_times,
    get_schedule_with_revision,
    ledger_is_terminal,
    preview_schedule,
    slot_key,
    upsert_schedule_if_revision,
    upload_time_after_assemble,
    upsert_schedule,
)


def test_slot_key_format():
    assert slot_key("nappabeats", datetime(2026, 7, 2).date(), 1, "09:00") == "nappabeats:2026-07-02:1:09:00"


def test_upload_time_after_assemble():
    assert upload_time_after_assemble("09:00") == "10:00"
    assert upload_time_after_assemble("23:45") == "00:45"


def test_apply_default_times_to_enabled_days():
    sched = ChannelSchedule(
        channel="test",
        days=[
            DaySlot(enabled=True, assemble_at="08:00"),
            DaySlot(enabled=False),
            DaySlot(enabled=True, assemble_at="08:00"),
        ]
        + [DaySlot() for _ in range(4)],
    )
    apply_default_times(sched, assemble_at="12:30")
    assert sched.default_assemble_at == "12:30"
    assert sched.default_upload_at == "13:30"
    assert sched.days[0].assemble_at == "12:30"
    assert sched.days[0].upload_at == "13:30"
    assert sched.days[1].assemble_at == "11:00"  # disabled day keeps default
    assert sched.days[2].assemble_at == "12:30"
    assert sched.days[2].upload_at == "13:30"


def test_due_slots_matches_window():
    sched = ChannelSchedule(
        channel="ch",
        timezone="America/New_York",
        days=[DaySlot() for _ in range(4)]
        + [DaySlot(enabled=True, assemble_at="09:00")]
        + [DaySlot() for _ in range(2)],
    )
    now = datetime(2026, 7, 2, 13, 5, tzinfo=timezone.utc)
    slots = due_slots(sched, now_utc=now, window_minutes=15)
    assert len(slots) == 1
    assert slots[0]["assemble_at"] == "09:00"
    assert slots[0]["upload_at"] == "10:00"
    assert slots[0]["day_name"] == "Thursday"


def test_due_slots_skips_disabled_day():
    sched = ChannelSchedule(
        channel="ch",
        timezone="UTC",
        days=[DaySlot(enabled=False, assemble_at="09:00")] + [DaySlot() for _ in range(6)],
    )
    now = datetime(2026, 7, 5, 9, 5, tzinfo=timezone.utc)
    assert due_slots(sched, now_utc=now) == []


def test_ledger_is_terminal():
    assert ledger_is_terminal({"status": "started"})
    assert ledger_is_terminal({"status": "succeeded"})
    assert not ledger_is_terminal({"status": "skipped"})
    assert not ledger_is_terminal(None)


def test_ensure_schedule_upload_times_fills_missing():
    sched = ChannelSchedule(
        channel="ch",
        default_assemble_at="11:00",
        default_upload_at=None,
        days=[DaySlot(enabled=True, assemble_at="11:00", upload_at=None)] + [DaySlot() for _ in range(6)],
    )
    assert ensure_schedule_upload_times(sched) is True
    assert sched.default_upload_at == "12:00"
    assert sched.days[0].upload_at == "12:00"


def test_slot_publish_at_utc_uses_resolved_upload_at():
    from music_assembler.api.assembly_schedule import slot_publish_at_utc

    sched = ChannelSchedule(
        channel="ch",
        timezone="America/New_York",
        queue_youtube=True,
        upload_schedule_publish=True,
        default_assemble_at="11:00",
        default_upload_at="12:00",
        days=[DaySlot(enabled=True, assemble_at="11:00", upload_at=None)] + [DaySlot() for _ in range(6)],
    )
    ensure_schedule_upload_times(sched)
    slot = {
        "local_date": "2026-07-14",
        "assemble_at": "11:00",
        "upload_at": sched.days[0].upload_at,
    }
    assert slot_publish_at_utc(slot, sched) == "2026-07-14T16:00:00Z"


def test_slot_publish_at_utc_none_when_upload_now():
    from music_assembler.api.assembly_schedule import slot_publish_at_utc

    sched = ChannelSchedule(
        channel="ch",
        timezone="America/New_York",
        queue_youtube=True,
        upload_schedule_publish=True,
        upload_now=True,
        default_assemble_at="11:00",
        default_upload_at="12:00",
        days=[DaySlot(enabled=True, assemble_at="11:00", upload_at="12:00")] + [DaySlot() for _ in range(6)],
    )
    slot = {"local_date": "2026-07-14", "assemble_at": "11:00", "upload_at": "12:00"}
    assert slot_publish_at_utc(slot, sched) is None


def test_channel_schedule_from_dict_upload_now_disables_schedule_publish():
    sched = ChannelSchedule.from_dict(
        {
            "channel": "ch",
            "upload_now": True,
            "upload_schedule_publish": True,
            "days": [{"enabled": True, "assemble_at": "11:00"}] + [{} for _ in range(6)],
        }
    )
    assert sched.upload_now is True
    assert sched.upload_schedule_publish is False


def test_channel_schedule_roundtrip_template_id():
    sched = ChannelSchedule.from_dict(
        {
            "channel": "ch",
            "template_id": "shorts_vertical",
            "images_folder": "korean",
            "days": [{"enabled": True, "assemble_at": "11:00"}] + [{} for _ in range(6)],
        }
    )
    assert sched.template_id == "shorts_vertical"
    assert sched.to_dict()["template_id"] == "shorts_vertical"


def test_channel_schedule_defaults_template_id():
    sched = ChannelSchedule.from_dict(
        {
            "channel": "ch",
            "days": [{} for _ in range(7)],
        }
    )
    assert sched.template_id == "playlist_landscape"


def test_effective_schedule_at_keeps_future():
    from music_assembler.api.assembly_schedule import effective_schedule_at

    now = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)
    assert effective_schedule_at("2026-07-14T16:00:00Z", now_utc=now) == "2026-07-14T16:00:00Z"


def test_effective_schedule_at_bumps_past_by_grace():
    from music_assembler.api.assembly_schedule import effective_schedule_at

    now = datetime(2026, 7, 14, 16, 10, tzinfo=timezone.utc)
    assert effective_schedule_at("2026-07-14T16:00:00Z", now_utc=now, grace_minutes=5) == "2026-07-14T16:15:00Z"


def test_preview_schedule_returns_future_slots():
    sched = ChannelSchedule(
        channel="ch",
        timezone="UTC",
        days=[DaySlot(enabled=True, assemble_at="10:00")] + [DaySlot() for _ in range(6)],
    )
    now = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)
    upcoming = preview_schedule(sched, now_utc=now, limit=3)
    assert upcoming
    assert upcoming[0]["assemble_at"] == "10:00"


def test_upsert_schedule_roundtrip():
    client = MagicMock()
    bucket = "b"

    class ClientError(Exception):
        pass

    client.exceptions.ClientError = ClientError
    storage: dict[str, bytes] = {}

    def put_object(**kwargs):
        storage[kwargs["Key"]] = kwargs["Body"]

    def get_object(**kwargs):
        key = kwargs["Key"]
        if key not in storage:
            exc = ClientError()
            exc.response = {"Error": {"Code": "NoSuchKey"}}
            raise exc
        return {"Body": MagicMock(read=lambda k=key: storage[k])}

    client.put_object.side_effect = put_object
    client.get_object.side_effect = get_object

    sched = ChannelSchedule(
        channel="nappabeats",
        variance_min=0,
        days=[DaySlot(enabled=True) for _ in range(7)],
    )
    upsert_schedule(client, bucket, sched)
    from music_assembler.api.assembly_schedule import get_schedule

    loaded = get_schedule(client, bucket, "nappabeats")
    assert loaded is not None
    assert loaded.channel == "nappabeats"
    assert loaded.variance_min == 0


class _ConditionalClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _ConditionalR2:
    exceptions = MagicMock(ClientError=_ConditionalClientError)

    def __init__(self, doc: dict | None):
        self.body = json.dumps(doc).encode("utf-8") if doc is not None else None
        self.revision = 1 if doc is not None else 0
        self.put_calls: list[dict] = []
        self.race_body: bytes | None = None

    @property
    def etag(self) -> str | None:
        return f'"revision-{self.revision}"' if self.body is not None else None

    def get_object(self, **_kwargs):
        if self.body is None:
            raise _ConditionalClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.body), "ETag": self.etag}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        if self.race_body is not None:
            self.body = self.race_body
            self.race_body = None
            self.revision += 1
        if kwargs.get("IfMatch") and kwargs["IfMatch"] != self.etag:
            raise _ConditionalClientError("PreconditionFailed")
        if kwargs.get("IfNoneMatch") == "*" and self.body is not None:
            raise _ConditionalClientError("PreconditionFailed")
        self.body = kwargs["Body"]
        self.revision += 1
        return {"ETag": self.etag}


def _schedule_doc(schedule: ChannelSchedule) -> dict:
    return {"version": 1, "schedules": [schedule.to_dict()]}


def test_conditional_upsert_with_matching_revision_succeeds():
    original = ChannelSchedule(channel="ch", default_assemble_at="10:00")
    client = _ConditionalR2(_schedule_doc(original))
    loaded, revision = get_schedule_with_revision(client, "bucket", "ch")
    assert loaded is not None
    assert revision == '"revision-1"'

    loaded.default_assemble_at = "14:00"
    saved, new_revision = upsert_schedule_if_revision(
        client,
        "bucket",
        loaded,
        expected_revision=revision,
    )

    assert saved.default_assemble_at == "14:00"
    assert new_revision == '"revision-2"'
    assert client.put_calls[-1]["IfMatch"] == '"revision-1"'
    current, _ = get_schedule_with_revision(client, "bucket", "ch")
    assert current is not None
    assert current.default_assemble_at == "14:00"


def test_conditional_upsert_rejects_stale_revision_without_mutation():
    original = ChannelSchedule(channel="ch", default_assemble_at="10:00")
    client = _ConditionalR2(_schedule_doc(original))
    stale, stale_revision = get_schedule_with_revision(client, "bucket", "ch")
    assert stale is not None

    latest = ChannelSchedule(channel="ch", default_assemble_at="14:00")
    upsert_schedule_if_revision(
        client,
        "bucket",
        latest,
        expected_revision=stale_revision,
    )
    body_before_stale_save = client.body
    put_count = len(client.put_calls)

    with pytest.raises(ScheduleRevisionConflict):
        upsert_schedule_if_revision(
            client,
            "bucket",
            stale,
            expected_revision=stale_revision,
        )

    assert client.body == body_before_stale_save
    assert len(client.put_calls) == put_count


def test_conditional_upsert_maps_racing_put_precondition_to_conflict():
    original = ChannelSchedule(channel="ch", default_assemble_at="10:00")
    client = _ConditionalR2(_schedule_doc(original))
    loaded, revision = get_schedule_with_revision(client, "bucket", "ch")
    assert loaded is not None
    external = ChannelSchedule(channel="ch", default_assemble_at="16:00")
    external_body = json.dumps(_schedule_doc(external)).encode("utf-8")
    client.race_body = external_body

    loaded.default_assemble_at = "14:00"
    with pytest.raises(ScheduleRevisionConflict):
        upsert_schedule_if_revision(
            client,
            "bucket",
            loaded,
            expected_revision=revision,
        )

    assert client.body == external_body
    assert client.put_calls[-1]["IfMatch"] == '"revision-1"'


def test_conditional_upsert_creates_missing_document_with_if_none_match():
    client = _ConditionalR2(None)
    schedule = ChannelSchedule(channel="ch")

    _, revision = upsert_schedule_if_revision(
        client,
        "bucket",
        schedule,
        create_if_absent=True,
    )

    assert revision == '"revision-1"'
    assert client.put_calls[-1]["IfNoneMatch"] == "*"
