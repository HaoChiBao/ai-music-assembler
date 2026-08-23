from datetime import datetime, timezone
from types import SimpleNamespace

from music_assembler import assemble_from_r2
from music_assembler.api import assembly_schedule


def _queued_schedule(monkeypatch, *, publish_at: str, upload_at: str) -> tuple[dict, type[datetime]]:
    class AdvancingDateTime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            values = (
                datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 23, 12, 1, tzinfo=timezone.utc),
            )
            value = values[min(cls.calls, len(values) - 1)]
            cls.calls += 1
            return value.astimezone(tz) if tz else value

    captured: dict = {}
    monkeypatch.setattr(assemble_from_r2, "datetime", AdvancingDateTime)
    monkeypatch.setattr(assembly_schedule, "datetime", AdvancingDateTime)
    monkeypatch.setattr(
        assemble_from_r2,
        "uploader_credentials_from_env",
        lambda: ("https://uploader.example", "test-key"),
    )
    monkeypatch.setattr(
        assemble_from_r2,
        "register_youtube_upload",
        lambda **kwargs: captured.update(kwargs) or {"job_id": "job", "status": "pending"},
    )
    monkeypatch.setenv("ASSEMBLY_PUBLISH_AT", publish_at)
    monkeypatch.setenv("ASSEMBLY_UPLOAD_AT", upload_at)
    monkeypatch.delenv("ASSEMBLY_UPLOAD_NOW", raising=False)

    assemble_from_r2._maybe_queue_youtube_upload(
        enabled=True,
        bucket="bucket",
        output_prefix="outputs/",
        channel="channel",
        basename="job",
        result={"youtube_metadata": SimpleNamespace(title="Title", description="Description")},
        no_upload=False,
    )
    return captured, AdvancingDateTime


def test_late_publish_and_upload_share_one_now(monkeypatch):
    captured, clock = _queued_schedule(
        monkeypatch,
        publish_at="2026-08-23T11:00:00Z",
        upload_at="2026-08-23T11:30:00Z",
    )

    assert captured["publish_at"] == "2026-08-23T12:05:00Z"
    assert captured["upload_at"] == "2026-08-23T12:05:00Z"
    assert clock.calls == 1


def test_late_upload_clamps_publish_forward(monkeypatch):
    captured, _clock = _queued_schedule(
        monkeypatch,
        publish_at="2026-08-23T12:03:00Z",
        upload_at="2026-08-23T11:30:00Z",
    )

    assert captured["upload_at"] == "2026-08-23T12:05:00Z"
    assert captured["publish_at"] == captured["upload_at"]
