"""Tests for bounded parallel extend batch planning."""

from __future__ import annotations

import unittest

from music_assembler.api.extend_batches import (
    MAX_PARALLEL_EXTEND_JOBS,
    parallel_extend_workloads,
)


class ParallelExtendWorkloadsTests(unittest.TestCase):
    def test_small_batch_keeps_one_image_per_worker(self) -> None:
        self.assertEqual(parallel_extend_workloads(3), [1, 1, 1])

    def test_large_batch_caps_workers_and_preserves_total(self) -> None:
        workloads = parallel_extend_workloads(1000)

        self.assertEqual(len(workloads), MAX_PARALLEL_EXTEND_JOBS)
        self.assertEqual(sum(workloads), 1000)
        self.assertEqual(workloads, [50] * MAX_PARALLEL_EXTEND_JOBS)

    def test_uneven_batch_distributes_exact_requested_count(self) -> None:
        workloads = parallel_extend_workloads(21)

        self.assertEqual(len(workloads), MAX_PARALLEL_EXTEND_JOBS)
        self.assertEqual(sum(workloads), 21)
        self.assertEqual(workloads.count(2), 1)
        self.assertEqual(workloads.count(1), 19)

    def test_invalid_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parallel_extend_workloads(0)
        with self.assertRaises(ValueError):
            parallel_extend_workloads(1, max_jobs=0)


if __name__ == "__main__":
    unittest.main()
