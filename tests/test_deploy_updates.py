"""Tests for deploy manifest loader and /v1/updates."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from music_assembler.api.deploy_manifest import load_deploy_manifest


class TestLoadDeployManifest(unittest.TestCase):
    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = load_deploy_manifest(Path(tmp) / "nope.json")
        self.assertEqual(data["source"], "missing")
        self.assertEqual(data["commits"], [])

    def test_reads_commits(self) -> None:
        payload = {
            "version": "0.1.11",
            "ref": "main",
            "git_sha": "abcdef0123456789",
            "git_sha_short": "abcdef0",
            "generated_at": "2026-07-14T01:00:00Z",
            "repo_url": "https://github.com/HaoChiBao/ai-music-assembler",
            "commits": [
                {
                    "sha": "abcdef0123456789",
                    "short": "abcdef0",
                    "subject": "Fix blank dashboard inventory metrics. (#1)",
                    "date": "2026-07-13",
                    "pr": 1,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deploy_manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            data = load_deploy_manifest(path)
        self.assertEqual(data["source"], "file")
        self.assertEqual(data["git_sha_short"], "abcdef0")
        self.assertEqual(len(data["commits"]), 1)
        self.assertEqual(data["commits"][0]["pr"], 1)


class TestUpdatesEndpoint(unittest.TestCase):
    def test_updates_shape(self) -> None:
        from music_assembler.api import app as app_module

        with mock.patch.dict(
            "os.environ",
            {"ASSEMBLY_BUILD_ID": "abc1234", "ASSEMBLY_DEPLOYED_AT": "2026-07-14T02:00:00Z"},
            clear=False,
        ):
            client = TestClient(app_module.app)
            r = client.get("/v1/updates")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("version", body)
        self.assertIn("commits", body)
        self.assertEqual(body["build"], "abc1234")
        self.assertIsInstance(body["commits"], list)


if __name__ == "__main__":
    unittest.main()
