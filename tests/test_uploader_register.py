import json
from unittest.mock import MagicMock, patch

import pytest

from music_assembler import assemble_from_r2
from music_assembler.api import uploader_client


def test_r2_object_uri():
    assert uploader_client.r2_object_uri("music-assembly-data", "music-video/a/b.mp4") == (
        "s3://music-assembly-data/music-video/a/b.mp4"
    )


def test_resolve_queue_youtube_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("ASSEMBLY_QUEUE_YOUTUBE", "true")
    assert uploader_client.resolve_queue_youtube(False) is False
    assert uploader_client.resolve_queue_youtube(True) is True


def test_resolve_queue_youtube_env_default(monkeypatch):
    monkeypatch.delenv("ASSEMBLY_QUEUE_YOUTUBE", raising=False)
    assert uploader_client.resolve_queue_youtube(None) is True
    monkeypatch.setenv("ASSEMBLY_QUEUE_YOUTUBE", "false")
    assert uploader_client.resolve_queue_youtube(None) is False
    monkeypatch.setenv("ASSEMBLY_QUEUE_YOUTUBE", "yes")
    assert uploader_client.resolve_queue_youtube(None) is True


def test_register_youtube_upload_posts_payload():
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"job_id": "mv_test", "channel_id": "nappabeats", "status": "pending"}
        ).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", fake_urlopen):
        out = uploader_client.register_youtube_upload(
            api_url="https://uploader.example",
            api_key="secret",
            channel="nappabeats",
            title="Late Night Mix",
            video_uri="s3://music-assembly-data/music-video/nappabeats/mv_test/mv_test_video.mp4",
            description="Chapters below",
            thumbnail_uri="s3://music-assembly-data/music-video/nappabeats/mv_test/mv_test_thumbnail.png",
            job_id="mv_test",
            tags=["lofi"],
        )

    assert out["status"] == "pending"
    assert captured["url"].endswith("/v1/channels/nappabeats/jobs/register")
    assert captured["body"]["title"] == "Late Night Mix"
    assert captured["body"]["job_id"] == "mv_test"
    assert captured["body"]["tags"] == ["lofi"]


def test_register_youtube_upload_defaults_upload_at_from_publish_at():
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        resp = MagicMock()
        resp.read.return_value = b'{"job_id": "mv_test", "status": "pending"}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", fake_urlopen):
        uploader_client.register_youtube_upload(
            api_url="https://uploader.example",
            api_key="secret",
            channel="nappabeats",
            title="Scheduled Mix",
            video_uri="s3://music-assembly-data/music-video/nappabeats/mv_test/mv_test_video.mp4",
            publish_at="2026-08-01T12:00:00Z",
        )

    assert captured["body"]["publish_at"] == "2026-08-01T12:00:00Z"
    assert captured["body"]["upload_at"] == "2026-08-01T12:00:00Z"


def test_register_youtube_upload_explicit_upload_at():
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        resp = MagicMock()
        resp.read.return_value = b'{"job_id": "mv_test", "status": "pending"}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", fake_urlopen):
        uploader_client.register_youtube_upload(
            api_url="https://uploader.example",
            api_key="secret",
            channel="nappabeats",
            title="Early upload",
            video_uri="s3://music-assembly-data/music-video/nappabeats/mv_test/mv_test_video.mp4",
            publish_at="2026-08-01T18:00:00Z",
            upload_at="2026-08-01T12:00:00Z",
            privacy="private",
        )

    assert captured["body"]["publish_at"] == "2026-08-01T18:00:00Z"
    assert captured["body"]["upload_at"] == "2026-08-01T12:00:00Z"
    assert captured["body"]["privacy"] == "private"


def test_register_youtube_upload_upload_now():
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        resp = MagicMock()
        resp.read.return_value = b'{"job_id": "mv_test", "status": "pending"}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", fake_urlopen):
        uploader_client.register_youtube_upload(
            api_url="https://uploader.example",
            api_key="secret",
            channel="nappabeats",
            title="Go live now",
            video_uri="s3://music-assembly-data/music-video/nappabeats/mv_test/mv_test_video.mp4",
            privacy="public",
            publish_at="2026-08-01T18:00:00Z",
            upload_at="2026-08-01T12:00:00Z",
            upload_now=True,
            no_schedule=True,
        )

    assert captured["body"]["upload_now"] is True
    assert captured["body"]["no_schedule"] is True
    assert captured["body"]["privacy"] == "public"
    assert "publish_at" not in captured["body"]
    assert "upload_at" not in captured["body"]


def test_register_youtube_upload_requires_title():
    with pytest.raises(ValueError, match="title"):
        uploader_client.register_youtube_upload(
            api_url="https://uploader.example",
            api_key="secret",
            channel="nappabeats",
            title="",
            video_uri="s3://b/k.mp4",
        )


def _worker_queue_kwargs(tmp_path):
    title = tmp_path / "title.txt"
    title.write_text("Late Night Mix", encoding="utf-8")
    return {
        "enabled": True,
        "bucket": "music-assembly-data",
        "output_prefix": "music-video/nappabeats/",
        "channel": "nappabeats",
        "basename": "mv_test",
        "result": {
            "youtube_metadata": None,
            "title_txt": title,
            "description_txt": None,
            "thumbnail_png": None,
        },
        "no_upload": False,
    }


def test_worker_fails_requested_queue_when_credentials_are_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        assemble_from_r2,
        "uploader_credentials_from_env",
        lambda: (None, None),
    )

    with pytest.raises(
        assemble_from_r2.YouTubeQueueError,
        match="UPLOADER_API_URL and UPLOADER_API_KEY",
    ):
        assemble_from_r2._maybe_queue_youtube_upload(**_worker_queue_kwargs(tmp_path))


def test_worker_fails_requested_queue_when_registration_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        assemble_from_r2,
        "uploader_credentials_from_env",
        lambda: ("https://uploader.example", "secret"),
    )

    def fail_registration(**_kwargs):
        raise OSError("uploader unavailable")

    monkeypatch.setattr(assemble_from_r2, "register_youtube_upload", fail_registration)

    with pytest.raises(
        assemble_from_r2.YouTubeQueueError,
        match="uploader unavailable",
    ):
        assemble_from_r2._maybe_queue_youtube_upload(**_worker_queue_kwargs(tmp_path))


def test_worker_records_queue_failure_without_losing_output_location(tmp_path):
    client = MagicMock()
    error = assemble_from_r2.YouTubeQueueError(
        "YouTube queue registration failed: uploader unavailable"
    )

    assemble_from_r2._mark_youtube_queue_failed(
        client,
        "music-assembly-data",
        "asm_test",
        category="korean",
        basename="mv_test",
        channel="nappabeats",
        output_dir=tmp_path / "mv_test",
        error=error,
    )

    payload = json.loads(client.put_object.call_args.kwargs["Body"])
    assert payload["status"] == "failed"
    assert payload["pct"] == 99.0
    assert payload["video_id"] == "mv_test"
    assert payload["channel"] == "nappabeats"
    assert payload["youtube_queue_status"] == "failed"
    assert payload["stage"] == str(error)
