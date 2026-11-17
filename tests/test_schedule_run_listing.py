import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from music_assembler.api.assembly_schedule import SCHEDULE_RUNS_PREFIX, list_schedule_runs


class _ClientError(Exception):
    pass


class _Paginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, **kwargs):
        self.client.paginate_calls.append(kwargs)
        summaries = list(self.client.summaries)
        for offset in range(0, len(summaries), 1_000):
            yield {"Contents": summaries[offset : offset + 1_000]}


class _FakeR2:
    exceptions = SimpleNamespace(ClientError=_ClientError)

    def __init__(self, entries, *, failing_keys=()):
        self.summaries = [
            {"Key": key, "LastModified": modified}
            for key, modified, _ in entries
        ]
        self.bodies = {key: body for key, _, body in entries}
        self.failing_keys = set(failing_keys)
        self.paginate_calls = []
        self.get_calls = []

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return _Paginator(self)

    def get_object(self, *, Bucket, Key):
        self.get_calls.append((Bucket, Key))
        if Key in self.failing_keys:
            raise _ClientError(Key)
        return {"Body": io.BytesIO(json.dumps(self.bodies[Key]).encode())}


def _ledger_entries(size, *, channels=("channel-a",)):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    entries = []
    for index in range(size):
        modified = base + timedelta(minutes=index)
        channel = channels[index % len(channels)]
        key = (
            f"{SCHEDULE_RUNS_PREFIX}{channel}-"
            f"{modified:%Y-%m-%d}-{modified.weekday()}-{modified:%H-%M}.json"
        )
        entries.append(
            (
                key,
                modified,
                {
                    "channel": channel,
                    "updated_at": modified.isoformat(),
                    "status": "succeeded",
                },
            )
        )
    return entries


def test_list_schedule_runs_gets_only_requested_rows_from_large_ledger():
    client = _FakeR2(_ledger_entries(5_000))

    rows = list_schedule_runs(client, "bucket", limit=40)

    assert len(client.paginate_calls) == 1
    assert len(client.get_calls) == 40
    assert len(rows) == 40
    assert [row["updated_at"] for row in rows] == sorted(
        (row["updated_at"] for row in rows),
        reverse=True,
    )


def test_list_schedule_runs_channel_filter_is_exact_and_bounded():
    client = _FakeR2(_ledger_entries(5_000, channels=("channel-a", "channel-b")))

    rows = list_schedule_runs(client, "bucket", limit=40, channel="channel-a")

    assert len(client.get_calls) == 40
    assert len(rows) == 40
    assert {row["channel"] for row in rows} == {"channel-a"}


def test_list_schedule_runs_preserves_json_timestamp_order():
    entries = _ledger_entries(2)
    entries[0][2]["updated_at"] = "2026-02-01T00:00:00+00:00"
    entries[1][2]["updated_at"] = "2026-01-01T00:00:00+00:00"
    client = _FakeR2(entries)

    rows = list_schedule_runs(client, "bucket", limit=2)

    assert [row["updated_at"] for row in rows] == [
        "2026-02-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    ]


def test_list_schedule_runs_backfills_after_get_failure():
    entries = _ledger_entries(3)
    client = _FakeR2(entries, failing_keys={entries[-1][0]})

    rows = list_schedule_runs(client, "bucket", limit=2)

    assert len(client.get_calls) == 3
    assert len(rows) == 2


def test_list_schedule_runs_zero_limit_avoids_r2_calls():
    client = _FakeR2(_ledger_entries(10))

    assert list_schedule_runs(client, "bucket", limit=0) == []
    assert client.paginate_calls == []
    assert client.get_calls == []
