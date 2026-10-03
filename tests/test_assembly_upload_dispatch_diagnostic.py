"""Temporary diagnostics for the assembly API's default YouTube dispatch mode."""

from types import SimpleNamespace

import music_assembler.api.app as app_module
from music_assembler import assemble_from_r2
from music_assembler.api import gcp_jobs


def test_omitted_upload_mode_becomes_immediate(monkeypatch):
    captured: dict = {}
    body = app_module.StartJobRequest(channel="nappabeats", images_folder="korean")
    assert "queue_youtube" not in body.model_fields_set
    assert "upload_schedule_publish" not in body.model_fields_set

    monkeypatch.setattr(app_module, "_r2", lambda: (object(), "bucket"))
    monkeypatch.setattr(app_module, "_assert_background_folder_exists", lambda *args: None)
    monkeypatch.setattr(app_module, "_invalidate_category_cache", lambda *args: None)
    monkeypatch.setattr(app_module, "write_meta_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "write_progress_json", lambda *args, **kwargs: None)

    def fake_start(_settings, **kwargs):
        captured.update(kwargs)
        return {"status": "running"}

    monkeypatch.setattr(app_module.gcp_jobs, "start_assembly_job", fake_start)
    app_module.start_job(
        body,
        None,
        SimpleNamespace(default_category="korean"),
    )

    assert captured["queue_youtube"] is True
    assert captured["upload_now"] is True
    assert captured["publish_at"] is None
    assert captured["upload_at"] is None


def test_immediate_mode_reaches_cloud_run_and_worker(monkeypatch):
    cloud_run: dict = {}
    monkeypatch.setattr(
        gcp_jobs,
        "run_v2",
        SimpleNamespace(EnvVar=lambda *, name, value: SimpleNamespace(name=name, value=value)),
    )

    def fake_run(_settings, **kwargs):
        cloud_run.update(kwargs)
        return {"status": "running"}

    monkeypatch.setattr(gcp_jobs, "_run_cloud_job", fake_run)
    gcp_jobs.start_assembly_job(
        SimpleNamespace(job_resource="assembly-job", assembly_job_name="music-assemble"),
        execution_id="asm_test",
        category="korean",
        upload_now=True,
    )
    overrides = {item.name: item.value for item in cloud_run["env"]}
    assert overrides["ASSEMBLY_UPLOAD_NOW"] == "true"

    register: dict = {}
    monkeypatch.setenv("ASSEMBLY_UPLOAD_NOW", "true")
    monkeypatch.delenv("ASSEMBLY_PUBLISH_AT", raising=False)
    monkeypatch.delenv("ASSEMBLY_UPLOAD_AT", raising=False)
    monkeypatch.setattr(
        assemble_from_r2,
        "uploader_credentials_from_env",
        lambda: ("https://uploader.invalid", "diagnostic-key"),
    )

    def fake_register(**kwargs):
        register.update(kwargs)
        return {"job_id": "mv_test", "status": "pending"}

    monkeypatch.setattr(assemble_from_r2, "register_youtube_upload", fake_register)
    assemble_from_r2._maybe_queue_youtube_upload(
        enabled=True,
        bucket="bucket",
        output_prefix="music-video/nappabeats/",
        channel="nappabeats",
        basename="mv_test",
        result={
            "youtube_metadata": SimpleNamespace(title="Diagnostic title", description=""),
            "thumbnail_png": None,
        },
        no_upload=False,
    )

    assert register["upload_now"] is True
    assert register["no_schedule"] is True
    assert register["publish_at"] is None
    assert register["upload_at"] is None
