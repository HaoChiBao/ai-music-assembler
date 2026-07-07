"""Tests for assembly schedule module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from music_assembler.api.assembly_schedule import (
    ChannelSchedule,
    DaySlot,
    apply_default_times,
    due_slots,
    ledger_is_terminal,
    preview_schedule,
    slot_key,
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

    sched = ChannelSchedule(channel="nappabeats", days=[DaySlot(enabled=True) for _ in range(7)])
    upsert_schedule(client, bucket, sched)
    from music_assembler.api.assembly_schedule import get_schedule

    loaded = get_schedule(client, bucket, "nappabeats")
    assert loaded is not None
    assert loaded.channel == "nappabeats"
