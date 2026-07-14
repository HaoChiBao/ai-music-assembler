"""Load the deploy manifest baked into the API image (dashboard Updates tab)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_MANIFEST_PATH = Path(__file__).resolve().parent / "deploy_manifest.json"


def load_deploy_manifest(path: Path | None = None) -> dict[str, Any]:
    """Return the deploy manifest, or a minimal stub when the file is missing."""
    target = path or _MANIFEST_PATH
    if not target.is_file():
        return {
            "version": None,
            "ref": None,
            "git_sha": None,
            "git_sha_short": None,
            "generated_at": None,
            "repo_url": "https://github.com/HaoChiBao/ai-music-assembler",
            "commits": [],
            "source": "missing",
        }
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "version": None,
            "ref": None,
            "git_sha": None,
            "git_sha_short": None,
            "generated_at": None,
            "repo_url": "https://github.com/HaoChiBao/ai-music-assembler",
            "commits": [],
            "source": "invalid",
        }
    if not isinstance(data, dict):
        data = {}
    commits = data.get("commits")
    if not isinstance(commits, list):
        commits = []
    return {
        "version": data.get("version"),
        "ref": data.get("ref"),
        "git_sha": data.get("git_sha"),
        "git_sha_short": data.get("git_sha_short"),
        "generated_at": data.get("generated_at") or os.environ.get("ASSEMBLY_DEPLOYED_AT"),
        "repo_url": data.get("repo_url") or "https://github.com/HaoChiBao/ai-music-assembler",
        "commits": commits,
        "source": "file",
    }
