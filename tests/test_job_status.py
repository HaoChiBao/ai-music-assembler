"""Job status reconciliation helpers."""

from datetime import datetime, timedelta, timezone

import pytest

from music_assembler.api import job_status


def test_runs_need_gcp_reconcile_terminal_only():
    runs = [
        {"progress": {"status": "succeeded"}},
        {"progress": {"status": "failed"}},
        {"progress": {"status": "cancelled"}},
    ]
    assert job_status.runs_need_gcp_reconcile(runs) is False


def test_runs_need_gcp_reconcile_running():
    runs = [{"progress": {"status": "running"}}]
    assert job_status.runs_need_gcp_reconcile(runs) is True


def test_reconcile_assembly_runs_skips_gcp_when_disabled():
    runs = [{"execution_id": "asm_1", "progress": {"status": "succeeded", "pct": 100}}]
    out = job_status.reconcile_assembly_runs(None, None, None, runs, reconcile_gcp=False)
    assert len(out) == 1
    assert out[0]["status"] == "succeeded"


def test_compute_run_timing_finished_from_r2():
    start = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=47, seconds=30)
    out = job_status.compute_run_timing(
        created_at=start.isoformat(),
        updated_at=end.isoformat(),
        status="succeeded",
    )
    assert out["started_at"] == start.isoformat()
    assert out["finished_at"] == end.isoformat()
    assert out["elapsed_sec"] == 47 * 60 + 30


def test_compute_run_timing_prefers_gcp_timestamps():
    created = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    gcp_start = created + timedelta(seconds=20)
    gcp_end = gcp_start + timedelta(hours=1, minutes=5)
    out = job_status.compute_run_timing(
        created_at=created.isoformat(),
        updated_at=(created + timedelta(hours=2)).isoformat(),
        status="succeeded",
        gcp_row={
            "start_time": gcp_start.isoformat(),
            "completion_time": gcp_end.isoformat(),
        },
    )
    assert out["started_at"] == gcp_start.isoformat()
    assert out["finished_at"] == gcp_end.isoformat()
    assert out["elapsed_sec"] == 65 * 60


def test_compute_run_timing_running_has_elapsed_no_finish():
    start = datetime.now(timezone.utc) - timedelta(minutes=12)
    out = job_status.compute_run_timing(
        created_at=start.isoformat(),
        updated_at=None,
        status="running",
    )
    assert out["started_at"] == start.isoformat()
    assert out["finished_at"] is None
    assert out["elapsed_sec"] is not None
    assert out["elapsed_sec"] >= 11 * 60


def test_reconcile_includes_timing_fields():
    start = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=33)
    runs = [
        {
            "execution_id": "asm_timing",
            "created_at": start.isoformat(),
            "progress": {
                "status": "succeeded",
                "pct": 100,
                "updated_at": end.isoformat(),
            },
        }
    ]
    out = job_status.reconcile_assembly_runs(None, None, None, runs, reconcile_gcp=False)
    assert out[0]["started_at"] == start.isoformat()
    assert out[0]["finished_at"] == end.isoformat()
    assert out[0]["elapsed_sec"] == 33 * 60


def test_reconcile_includes_ops_detail_fields():
    runs = [
        {
            "execution_id": "asm_ops",
            "channel": "NappaBeats",
            "claimed_background": "post-processed/korean/bg01.png",
            "duration_min": 90,
            "images_folder": "korean",
            "category": "korean",
            "progress": {
                "status": "succeeded",
                "pct": 100,
                "video_id": "mv_abc",
                "channel": "NappaBeats",
            },
        }
    ]
    out = job_status.reconcile_assembly_runs(None, None, None, runs, reconcile_gcp=False)
    row = out[0]
    assert row["channel"] == "nappabeats"
    assert row["video_id"] == "mv_abc"
    assert row["claimed_background"] == "post-processed/korean/bg01.png"
    assert row["duration_min"] == 90
    assert row["images_folder"] == "korean"


def test_summarize_run_metrics_success_and_percentiles():
    runs = [
        {"status": "succeeded", "elapsed_sec": 60},
        {"status": "succeeded", "elapsed_sec": 120},
        {"status": "succeeded", "elapsed_sec": 180},
        {"status": "failed", "elapsed_sec": 40},
        {"status": "running", "elapsed_sec": 10},
    ]
    summary = job_status.summarize_run_metrics(runs)
    assert summary["succeeded"] == 3
    assert summary["failed"] == 1
    assert summary["running"] == 1
    assert summary["terminal"] == 4
    assert summary["success_rate"] == 0.75
    assert summary["elapsed_p50_sec"] == 120
    assert summary["elapsed_p95_sec"] == pytest.approx(174)
