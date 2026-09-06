"""Plan bounded Cloud Run fan-out for background extension batches."""

from __future__ import annotations

MAX_PARALLEL_EXTEND_JOBS = 20


def parallel_extend_workloads(
    batch_size: int,
    *,
    max_jobs: int = MAX_PARALLEL_EXTEND_JOBS,
) -> list[int]:
    """Split an image batch across at most ``max_jobs`` workers."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if max_jobs < 1:
        raise ValueError("max_jobs must be at least 1")

    worker_count = min(batch_size, max_jobs)
    per_worker, extra = divmod(batch_size, worker_count)
    return [
        per_worker + (1 if index < extra else 0)
        for index in range(worker_count)
    ]
