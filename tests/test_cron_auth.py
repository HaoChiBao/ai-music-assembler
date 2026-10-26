"""Regression tests for cron endpoint method and authentication boundaries."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from music_assembler.api import app as app_module


class TestCronAuthentication(unittest.TestCase):
    def test_cookie_authenticated_gets_are_method_not_allowed(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ASSEMBLY_API_KEY": "scheduler-key",
                "ASSEMBLY_DASHBOARD_PASSWORD": "dashboard-password",
            },
            clear=True,
        ):
            client = TestClient(app_module.app)
            login = client.post(
                "/v1/dashboard/login",
                json={"password": "dashboard-password"},
            )
            self.assertEqual(login.status_code, 200)

            with mock.patch.object(app_module, "_r2") as r2:
                schedule = client.get("/v1/cron/run-schedules")
                health = client.get("/v1/cron/assembly-health")

        self.assertEqual(schedule.status_code, 405)
        self.assertEqual(health.status_code, 405)
        r2.assert_not_called()

    def test_dashboard_cookie_cannot_authorize_cron_posts(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ASSEMBLY_API_KEY": "scheduler-key",
                "ASSEMBLY_DASHBOARD_PASSWORD": "dashboard-password",
            },
            clear=True,
        ):
            client = TestClient(app_module.app)
            login = client.post(
                "/v1/dashboard/login",
                json={"password": "dashboard-password"},
            )
            self.assertEqual(login.status_code, 200)

            with mock.patch.object(app_module, "_r2") as r2:
                schedule = client.post("/v1/cron/run-schedules")
                health = client.post("/v1/cron/assembly-health")

        self.assertEqual(schedule.status_code, 401)
        self.assertEqual(health.status_code, 401)
        r2.assert_not_called()

    def test_cron_posts_fail_closed_when_only_dashboard_auth_is_configured(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ASSEMBLY_DASHBOARD_PASSWORD": "dashboard-password"},
            clear=True,
        ):
            client = TestClient(app_module.app)
            login = client.post(
                "/v1/dashboard/login",
                json={"password": "dashboard-password"},
            )
            self.assertEqual(login.status_code, 200)

            with mock.patch.object(app_module, "_r2") as r2:
                schedule = client.post("/v1/cron/run-schedules")
                health = client.post("/v1/cron/assembly-health")

        self.assertEqual(schedule.status_code, 503)
        self.assertEqual(health.status_code, 503)
        self.assertEqual(
            schedule.json()["detail"],
            "API key authentication is not configured",
        )
        r2.assert_not_called()

    def test_api_key_posts_preserve_cloud_scheduler_behavior(self) -> None:
        def run_schedules(_client, _bucket, _settings, **kwargs):
            return {"dry_run": kwargs["dry_run"], "results": []}

        with mock.patch.dict(
            os.environ,
            {
                "ASSEMBLY_API_KEY": "scheduler-key",
                "ASSEMBLY_DASHBOARD_PASSWORD": "dashboard-password",
            },
            clear=True,
        ):
            client = TestClient(app_module.app)
            headers = {"X-API-Key": "scheduler-key"}
            with (
                mock.patch.object(
                    app_module,
                    "_r2",
                    return_value=(object(), "test-bucket"),
                ),
                mock.patch.object(
                    app_module.job_runs,
                    "list_r2_job_runs",
                    return_value=[],
                ),
                mock.patch.object(
                    app_module.assembly_schedule,
                    "run_due_schedules",
                    side_effect=run_schedules,
                ) as schedules,
                mock.patch.object(
                    app_module.assembly_health,
                    "audit_recent_assemblies",
                    return_value={"issues": [], "runs": []},
                ) as health_audit,
            ):
                schedule = client.post(
                    "/v1/cron/run-schedules",
                    headers=headers,
                )
                health = client.post(
                    "/v1/cron/assembly-health",
                    headers=headers,
                )

        self.assertEqual(schedule.status_code, 200)
        self.assertEqual(health.status_code, 200)
        self.assertFalse(schedules.call_args.kwargs["dry_run"])
        self.assertTrue(health_audit.call_args.kwargs["repair"])


if __name__ == "__main__":
    unittest.main()
