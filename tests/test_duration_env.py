"""Tests for ASSEMBLY_DURATION_MIN / ASSEMBLY_VARIANCE_MIN worker env fallback."""

from __future__ import annotations

import os
import unittest

from music_assembler.assemble_options import duration_bounds_from_env, resolve_duration_bounds


class TestDurationEnv(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("ASSEMBLY_DURATION_MIN", None)
        os.environ.pop("ASSEMBLY_VARIANCE_MIN", None)

    def test_missing_env(self) -> None:
        os.environ.pop("ASSEMBLY_DURATION_MIN", None)
        os.environ.pop("ASSEMBLY_VARIANCE_MIN", None)
        self.assertEqual(duration_bounds_from_env(), (None, None))

    def test_duration_and_variance_minutes_to_seconds(self) -> None:
        os.environ["ASSEMBLY_DURATION_MIN"] = "120"
        os.environ["ASSEMBLY_VARIANCE_MIN"] = "15"
        duration_sec, variance_sec = duration_bounds_from_env()
        self.assertEqual(duration_sec, 120 * 60)
        self.assertEqual(variance_sec, 15 * 60)
        bounds = resolve_duration_bounds(
            duration_sec=duration_sec,
            variance_sec=variance_sec,
            min_sec=None,
            max_sec=None,
        )
        self.assertEqual(bounds.min_sec, 105 * 60)
        self.assertEqual(bounds.max_sec, 135 * 60)

    def test_invalid_env_ignored(self) -> None:
        os.environ["ASSEMBLY_DURATION_MIN"] = "nope"
        os.environ["ASSEMBLY_VARIANCE_MIN"] = "-1"
        self.assertEqual(duration_bounds_from_env(), (None, None))

    def test_short_duration_variance_keeps_positive_min(self) -> None:
        """5±15 must not produce min_sec <= 0 (DurationBounds rejects that)."""
        bounds = resolve_duration_bounds(
            duration_sec=5 * 60,
            variance_sec=15 * 60,
            min_sec=None,
            max_sec=None,
        )
        self.assertGreater(bounds.min_sec, 0)
        self.assertEqual(bounds.max_sec, 20 * 60)


if __name__ == "__main__":
    unittest.main()
