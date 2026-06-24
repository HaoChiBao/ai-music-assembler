import json
from unittest.mock import MagicMock, patch

import pytest

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


def test_register_youtube_upload_requires_title():
    with pytest.raises(ValueError, match="title"):
        uploader_client.register_youtube_upload(
            api_url="https://uploader.example",
            api_key="secret",
            channel="nappabeats",
            title="",
            video_uri="s3://b/k.mp4",
        )
