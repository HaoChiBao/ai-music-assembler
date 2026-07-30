"""Tests for assembly schedule module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from music_assembler.api import assembly_schedule as schedule_module
from music_assembler.api.assembly_schedule import (
    ChannelSchedule,
    DaySlot,
    apply_default_times,
    due_slots,
    ensure_schedule_upload_times,
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
    assert ledger_is_terminal({"status": "starting"})
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


def _thursday_schedule() -> ChannelSchedule:
    return ChannelSchedule(
        channel="ch",
        timezone="UTC",
        days=[DaySlot() for _ in range(4)]
        + [DaySlot(enabled=True, assemble_at="09:00")]
        + [DaySlot() for _ in range(2)],
    )


def test_claim_deferred_slot_rejects_a_stale_etag():
    client = MagicMock()

    class ClientError(Exception):
        pass

    client.exceptions.ClientError = ClientError
    calls = 0

    def put_object(**kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["IfMatch"] == '"etag-1"'
        if calls == 2:
            exc = ClientError()
            exc.response = {"Error": {"Code": "PreconditionFailed"}}
            raise exc

    client.put_object.side_effect = put_object
    entry = {
        "status": "deferred",
        "extend_execution_id": "ext_1",
        "_etag": '"etag-1"',
    }

    assert schedule_module.claim_deferred_slot(
        client, "bucket", "ch:2026-07-30:4:09:00", entry, channel="ch"
    )
    assert not schedule_module.claim_deferred_slot(
        client, "bucket", "ch:2026-07-30:4:09:00", entry, channel="ch"
    )


def test_read_ledger_preserves_etag_for_conditional_claim():
    client = MagicMock()
    client.get_object.return_value = {
        "Body": MagicMock(read=lambda: b'{"status":"deferred"}'),
        "ETag": '"etag-1"',
    }

    assert schedule_module.read_ledger(
        client, "bucket", "ch:2026-07-30:4:09:00"
    ) == {
        "status": "deferred",
        "_etag": '"etag-1"',
    }


def test_run_due_schedules_starts_deferred_slot_after_extend_outside_window(monkeypatch):
    sched = _thursday_schedule()
    monkeypatch.setattr(schedule_module, "list_schedules", lambda *args, **kwargs: [sched])
    monkeypatch.setattr(
        schedule_module,
        "read_ledger",
        lambda *args, **kwargs: {
            "status": "deferred",
            "extend_execution_id": "ext_1",
        },
    )
    monkeypatch.setattr(
        schedule_module,
        "evaluate_resources",
        lambda *args, **kwargs: {"ready": True},
    )
    monkeypatch.setattr(schedule_module, "claim_deferred_slot", lambda *args, **kwargs: True)
    start_assembly = MagicMock(
        return_value={
            "execution_id": "asm_1",
            "gcp_execution_id": "gcp_1",
            "slot_key": "ch:2026-07-30:4:09:00",
        }
    )
    monkeypatch.setattr(schedule_module, "start_scheduled_assembly", start_assembly)
    start_extend = MagicMock()

    result = schedule_module.run_due_schedules(
        MagicMock(),
        "bucket",
        MagicMock(),
        now_utc=datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc),
        new_execution_id=lambda: "asm_1",
        start_extend_fn=start_extend,
    )

    assert result["results"] == [
        {
            "slot_key": "ch:2026-07-30:4:09:00",
            "action": "started_after_extend",
            "execution_id": "asm_1",
            "gcp_execution_id": "gcp_1",
        }
    ]
    start_assembly.assert_called_once()
    start_extend.assert_not_called()


def test_run_due_schedules_does_not_duplicate_running_deferred_extend(monkeypatch):
    sched = _thursday_schedule()
    monkeypatch.setattr(schedule_module, "list_schedules", lambda *args, **kwargs: [sched])
    monkeypatch.setattr(
        schedule_module,
        "read_ledger",
        lambda *args, **kwargs: {
            "status": "deferred",
            "extend_execution_id": "ext_1",
        },
    )
    resources = {"ready": False, "blockers": ["low_backgrounds"]}
    monkeypatch.setattr(
        schedule_module,
        "evaluate_resources",
        lambda *args, **kwargs: resources,
    )
    start_assembly = MagicMock()
    monkeypatch.setattr(schedule_module, "start_scheduled_assembly", start_assembly)
    start_extend = MagicMock()

    result = schedule_module.run_due_schedules(
        MagicMock(),
        "bucket",
        MagicMock(),
        now_utc=datetime(2026, 7, 30, 9, 5, tzinfo=timezone.utc),
        new_execution_id=lambda: "unused",
        start_extend_fn=start_extend,
    )

    assert result["results"] == [
        {
            "slot_key": "ch:2026-07-30:4:09:00",
            "action": "waiting_for_extend",
            "extend_execution_id": "ext_1",
            "resources": resources,
        }
    ]
    start_assembly.assert_not_called()
    start_extend.assert_not_called()
