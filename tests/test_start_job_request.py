"""Tests for manual assembly request normalization."""

from music_assembler.api.app import StartJobRequest


def test_scheduled_upload_at_also_defaults_publish_at():
    request = StartJobRequest(
        channel="nappabeats",
        images_folder="korean",
        upload_schedule_publish=True,
        upload_at="2026-08-01T18:00:00Z",
    )

    assert request.publish_at == "2026-08-01T18:00:00Z"
    assert request.upload_at == "2026-08-01T18:00:00Z"
