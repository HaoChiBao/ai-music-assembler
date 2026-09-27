import pytest

from music_assembler import schedule_music_videos, youtube_upload
from music_assembler.video_registry import VideoEntry, VideoRegistry


class _FakeMediaFileUpload:
    def __init__(self, *_args, **_kwargs):
        pass


class _FakeInsertRequest:
    def __init__(self, service, video_id):
        self.service = service
        self.video_id = video_id
        self.committed = False

    def next_chunk(self):
        self.service.next_chunk_calls += 1
        if not self.committed:
            self.committed = True
            self.service.committed_ids.append(self.video_id)
        if self.service.timeouts_remaining:
            self.service.timeouts_remaining -= 1
            raise TimeoutError("final response timed out after server commit")
        return None, {"id": self.video_id}


class _FakeYouTube:
    def __init__(self, *, timeouts_before_success=1):
        self.insert_calls = 0
        self.next_chunk_calls = 0
        self.committed_ids = []
        self.timeouts_remaining = timeouts_before_success

    def videos(self):
        return self

    def insert(self, **_kwargs):
        self.insert_calls += 1
        video_id = (
            "first-committed-id"
            if self.insert_calls == 1
            else "second-committed-id"
        )
        return _FakeInsertRequest(self, video_id)


def _install_fake_youtube(monkeypatch, *, timeouts_before_success=1):
    youtube = _FakeYouTube(timeouts_before_success=timeouts_before_success)
    monkeypatch.setattr(
        youtube_upload,
        "_require_google_libs",
        lambda: (
            object,
            object,
            object,
            lambda *_a, **_k: youtube,
            Exception,
            _FakeMediaFileUpload,
        ),
    )
    monkeypatch.setattr(youtube_upload, "get_credentials", lambda *_a, **_k: object())
    monkeypatch.setattr(youtube_upload.time, "sleep", lambda _seconds: None)
    return youtube


def test_final_response_timeout_reuses_insert_and_records_committed_id(
    tmp_path, monkeypatch
):
    video = tmp_path / "mix.mp4"
    video.write_bytes(b"fake video")
    client_secret = tmp_path / "client_secret.json"
    client_secret.write_text("{}", encoding="utf-8")
    registry_path = tmp_path / "registry.txt"
    registry = VideoRegistry(registry_path)
    registry.append(
        VideoEntry(
            id="mix-1",
            video=str(video),
            title="Retry reproduction",
        )
    )

    youtube = _install_fake_youtube(monkeypatch)

    result = schedule_music_videos.main(
        [
            "--registry",
            str(registry_path),
            "--client-secret",
            str(client_secret),
            "--used-titles-file",
            str(tmp_path / "used_titles.txt"),
            "--upload-retries",
            "2",
            "--retry-delay",
            "0",
            "--start",
            "2030-01-01 09:00",
        ]
    )

    stored = VideoRegistry(registry_path).load()[0]
    assert result == 0
    assert youtube.insert_calls == 1
    assert youtube.next_chunk_calls == 2
    assert youtube.committed_ids == ["first-committed-id"]
    assert stored.youtube_id == "first-committed-id"


def test_retry_exhaustion_does_not_create_another_insert(tmp_path, monkeypatch):
    video = tmp_path / "mix.mp4"
    video.write_bytes(b"fake video")
    youtube = _install_fake_youtube(monkeypatch, timeouts_before_success=2)
    retries = []

    with pytest.raises(TimeoutError, match="after server commit"):
        youtube_upload.upload_video_with_retry(
            video,
            max_attempts=2,
            retry_delay_sec=0,
            on_retry=lambda attempt, attempts, error: retries.append(
                (attempt, attempts, type(error).__name__)
            ),
            title="Retry exhaustion",
            description="",
            client_secret=tmp_path / "client_secret.json",
            token_path=tmp_path / "token.json",
        )

    assert retries == [(1, 2, "TimeoutError")]
    assert youtube.insert_calls == 1
    assert youtube.next_chunk_calls == 2
    assert youtube.committed_ids == ["first-committed-id"]
