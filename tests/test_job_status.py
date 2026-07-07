"""Job status reconciliation helpers."""

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
