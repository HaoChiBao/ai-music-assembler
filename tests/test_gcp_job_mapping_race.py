"""Deterministic reproduction for parallel Cloud Run execution mapping."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from google.api_core import operation as operation_module
from google.cloud import run_v2
from google.longrunning import operations_pb2
from google.protobuf import any_pb2

from music_assembler.api import gcp_jobs


JOB_RESOURCE = "projects/test/locations/us-central1/jobs/music-assemble"


class _FakeJobsClient:
    def __init__(self, executions):
        self.executions = executions
        self.operations = {}

    def run_job(self, *, request):
        env = request.overrides.container_overrides[0].env
        api_execution_id = next(
            item.value for item in env if item.name == "ASSEMBLY_EXECUTION_ID"
        )
        operation = _operation(self.executions[api_execution_id])
        self.operations[api_execution_id] = operation
        return operation


class _FakeExecutionsClient:
    def __init__(self, executions):
        self.executions = executions
        self.list_calls = 0

    def list_executions(self, *, parent):
        assert parent == JOB_RESOURCE
        self.list_calls += 1
        return list(self.executions)


def _execution(short_id: str):
    return run_v2.Execution(
        name=f"{JOB_RESOURCE}/executions/{short_id}",
        create_time=datetime.now(timezone.utc),
    )


def _operation(execution):
    metadata = any_pb2.Any()
    metadata.Pack(run_v2.Execution.pb(execution))
    raw_operation = operations_pb2.Operation(
        name=f"operations/{execution.name.rsplit('/', 1)[-1]}",
        metadata=metadata,
    )
    operation = operation_module.Operation(
        raw_operation,
        lambda: raw_operation,
        lambda: None,
        run_v2.Execution,
        metadata_type=run_v2.Execution,
    )

    def result_must_not_be_called(*args, **kwargs):
        raise AssertionError("operation.result() would wait for job completion")

    operation.result = result_must_not_be_called
    return operation


def test_parallel_runs_use_their_returned_operation_execution(monkeypatch):
    gcp_one = _execution("gcp-one")
    gcp_two = _execution("gcp-two")
    by_api_id = {"api-one": gcp_one, "api-two": gcp_two}
    jobs_client = _FakeJobsClient(by_api_id)
    executions_client = _FakeExecutionsClient([gcp_one, gcp_two])

    monkeypatch.setattr(
        gcp_jobs,
        "_require_client",
        lambda: (jobs_client, executions_client),
    )

    def start(api_execution_id: str):
        env = [
            run_v2.EnvVar(
                name="ASSEMBLY_EXECUTION_ID",
                value=api_execution_id,
            )
        ]
        return gcp_jobs._run_cloud_job(
            SimpleNamespace(),
            job_resource=JOB_RESOURCE,
            job_name="music-assemble",
            env=env,
            execution_id=api_execution_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(start, ("api-one", "api-two")))

    mapped = {row["execution_id"]: row["gcp_execution_id"] for row in rows}
    returned = {
        api_id: operation.metadata.name.rsplit("/", 1)[-1]
        for api_id, operation in jobs_client.operations.items()
    }

    assert returned == {"api-one": "gcp-one", "api-two": "gcp-two"}
    assert mapped == returned
    assert executions_client.list_calls == 0


def test_incomplete_operation_metadata_refreshes_before_list_fallback(monkeypatch):
    exact_execution = _execution("gcp-exact")
    wrong_execution = _execution("gcp-wrong")
    executions_client = _FakeExecutionsClient([wrong_execution])

    class RefreshingOperation:
        metadata = run_v2.Execution()

        def done(self):
            self.metadata = exact_execution
            return False

        def result(self, *args, **kwargs):
            raise AssertionError("operation.result() would wait for job completion")

    class RefreshingJobsClient:
        def run_job(self, *, request):
            return RefreshingOperation()

    monkeypatch.setattr(
        gcp_jobs,
        "_require_client",
        lambda: (RefreshingJobsClient(), executions_client),
    )

    row = gcp_jobs._run_cloud_job(
        SimpleNamespace(),
        job_resource=JOB_RESOURCE,
        job_name="music-assemble",
        env=[],
        execution_id="api-one",
    )

    assert row["gcp_execution_id"] == "gcp-exact"
    assert executions_client.list_calls == 0


def test_missing_operation_metadata_uses_list_fallback(monkeypatch):
    listed_execution = _execution("gcp-listed")
    executions_client = _FakeExecutionsClient([listed_execution])

    class JobsClientWithoutMetadata:
        def run_job(self, *, request):
            operation = SimpleNamespace(metadata=None, done=lambda: False)

            def result_must_not_be_called(*args, **kwargs):
                raise AssertionError("operation.result() would wait for job completion")

            operation.result = result_must_not_be_called
            return operation

    monkeypatch.setattr(
        gcp_jobs,
        "_require_client",
        lambda: (JobsClientWithoutMetadata(), executions_client),
    )

    row = gcp_jobs._run_cloud_job(
        SimpleNamespace(),
        job_resource=JOB_RESOURCE,
        job_name="music-assemble",
        env=[],
        execution_id="api-one",
    )

    assert row["gcp_execution_id"] == "gcp-listed"
    assert executions_client.list_calls == 1
