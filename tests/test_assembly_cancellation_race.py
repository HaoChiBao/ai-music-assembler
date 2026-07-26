"""Runtime reproduction for cancellation racing an active assembly worker."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_assembler import assemble_from_r2
from music_assembler.api import job_cancel
from music_assembler.api.config import ApiSettings
from music_assembler.job_progress import (
    cancellation_key,
    read_progress_json,
    write_meta_json,
    write_progress_json,
)


class FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeR2:
    exceptions = SimpleNamespace(ClientError=FakeClientError)

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.get_counts: dict[str, int] = {}

    def put_object(self, *, Key: str, Body, **_kwargs) -> None:
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()

    def get_object(self, *, Key: str, **_kwargs):
        self.get_counts[Key] = self.get_counts.get(Key, 0) + 1
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def download_file(self, _bucket: str, _key: str, filename: str) -> None:
        Path(filename).write_bytes(b"fake-image")


def _settings() -> ApiSettings:
    return ApiSettings(
        api_key=None,
        dashboard_password=None,
        gcp_project="test-project",
        gcp_region="test-region",
        assembly_job_name="music-assemble",
        extend_job_name="music-extend",
        extend_use_gcp=True,
        default_category="korean",
        configured_channels=(),
        uploader_api_url=None,
        uploader_api_key=None,
    )


@pytest.mark.parametrize("gcp_cancel_fails", [False, True])
def test_cancellation_prevents_publication_and_success_overwrite(
    monkeypatch, tmp_path, gcp_cancel_fails
):
    client = FakeR2()
    bucket = "test-bucket"
    execution_id = "asm_cancel_race"
    write_meta_json(
        client,
        bucket,
        execution_id,
        category="korean",
        channel="demo",
        gcp_execution_id="gcp-execution-123",
        job_type="assembly",
    )
    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=1,
        stage="Cloud Run execution started…",
        category="korean",
        status="running",
    )

    cancellation: dict = {}
    uploader_calls: list[dict] = []
    sync_calls = 0

    def fake_gcp_cancel(*_args, **_kwargs):
        if gcp_cancel_fails:
            raise RuntimeError("simulated GCP cancellation outage")
        return {"status": "cancelled"}

    def fake_sync_prefix(_client, _bucket, _prefix, local_dir, **_kwargs):
        nonlocal sync_calls
        sync_calls += 1
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "song.mp3").write_bytes(b"fake-mp3")
        if sync_calls == 1:
            cancellation.update(job_cancel.cancel_job(client, bucket, execution_id, _settings()))
        return 1

    def fake_assemble(cfg, *, output_basename: str, **_kwargs):
        run_dir = cfg.paths.output_dir / output_basename
        run_dir.mkdir(parents=True, exist_ok=True)
        files = {
            "frame_png": run_dir / f"{output_basename}_frame.png",
            "audio_mp3": run_dir / f"{output_basename}_audio.mp3",
            "video_mp4": run_dir / f"{output_basename}_video.mp4",
            "tracklist_txt": run_dir / f"{output_basename}_tracklist.txt",
            "title_txt": run_dir / f"{output_basename}_title.txt",
            "description_txt": run_dir / f"{output_basename}_description.txt",
            "used_image": run_dir / "background.png",
        }
        for name, path in files.items():
            path.write_text("Runtime cancellation race" if name == "title_txt" else "fake")
        return {
            "output_dir": run_dir,
            **files,
            "thumbnail_png": None,
            "youtube_metadata": None,
            "final_audio_duration_sec": 60.0,
        }

    def fake_register(**kwargs):
        uploader_calls.append(kwargs)
        return {"job_id": kwargs["job_id"], "status": "pending"}

    monkeypatch.setattr(job_cancel.gcp_jobs, "cancel_execution", fake_gcp_cancel)
    monkeypatch.setattr(assemble_from_r2, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(assemble_from_r2, "find_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(
        assemble_from_r2,
        "r2_config_from_env",
        lambda **_kwargs: SimpleNamespace(bucket=bucket),
    )
    monkeypatch.setattr(assemble_from_r2, "r2_client", lambda _cfg: client)
    monkeypatch.setattr(assemble_from_r2, "sync_prefix_to_dir", fake_sync_prefix)
    monkeypatch.setattr(
        assemble_from_r2,
        "claim_background_on_r2",
        lambda *_args, **_kwargs: "background.png",
    )
    monkeypatch.setattr(assemble_from_r2, "resolve_font_key", lambda *_args, **_kwargs: "arial")
    monkeypatch.setattr(assemble_from_r2, "assemble", fake_assemble)
    monkeypatch.setattr(
        assemble_from_r2,
        "retire_claimed_background_on_r2",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        assemble_from_r2,
        "verify_background_retired_on_r2",
        lambda *_args, **_kwargs: {"in_used": True, "in_pool": False, "in_flight": False},
    )
    monkeypatch.setattr(assemble_from_r2, "sync_dir_to_prefix", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(assemble_from_r2, "register_youtube_upload", fake_register)

    monkeypatch.setenv("ASSEMBLY_EXECUTION_ID", execution_id)
    monkeypatch.setenv("ASSEMBLY_QUEUE_YOUTUBE", "true")
    monkeypatch.setenv("ASSEMBLY_UPLOAD_NOW", "true")
    monkeypatch.setenv("UPLOADER_API_URL", "https://uploader.invalid")
    monkeypatch.setenv("UPLOADER_API_KEY", "fake-key")

    exit_code = assemble_from_r2.main(
        [
            "--category",
            "korean",
            "--channel",
            "demo",
            "--duration",
            "1m",
            "--work-dir",
            str(tmp_path),
            "--no-thumbnail",
            "--no-metadata",
            "--queue-youtube",
        ]
    )

    final_progress = read_progress_json(client, bucket, execution_id)
    cancel_key = cancellation_key(execution_id)
    assert exit_code == 0
    assert cancellation["status"] == "cancelled"
    assert cancellation["gcp_cancelled"] is (not gcp_cancel_fails)
    if gcp_cancel_fails:
        assert cancellation["gcp_cancel_error"] == "simulated GCP cancellation outage"
    else:
        assert "gcp_cancel_error" not in cancellation
    assert uploader_calls == []
    assert final_progress and final_progress["status"] == "cancelled"
    assert json.loads(client.objects[cancel_key])["cancel_requested"] is True
    assert client.get_counts[cancel_key] == 1
