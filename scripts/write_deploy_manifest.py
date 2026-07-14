#!/usr/bin/env python3
"""Write music_assembler/api/deploy_manifest.json for the dashboard Updates tab.

Run in CI before Cloud Build so the API image includes the commits baked into that deploy.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_PR_IN_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUT = _REPO_ROOT / "music_assembler" / "api" / "deploy_manifest.json"
_VERSION_FILE = _REPO_ROOT / "music_assembler" / "__init__.py"
_REPO_URL = "https://github.com/HaoChiBao/ai-music-assembler"


def _read_version() -> str:
    text = _VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise SystemExit(f"error: could not parse __version__ from {_VERSION_FILE}")
    return match.group(1)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=_REPO_ROOT, text=True).strip()


def _commits(limit: int) -> list[dict[str, object]]:
    fmt = "%H%x09%ad%x09%s"
    raw = _git("log", f"-{limit}", f"--pretty=format:{fmt}", "--date=short")
    if not raw:
        return []
    out: list[dict[str, object]] = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        entry: dict[str, object] = {
            "sha": sha,
            "short": sha[:7],
            "subject": subject,
            "date": date,
        }
        pr = _PR_IN_SUBJECT.search(subject)
        if pr:
            entry["pr"] = int(pr.group(1))
        out.append(entry)
    return out


def build_manifest(*, limit: int, ref: str | None) -> dict[str, object]:
    sha = _git("rev-parse", "HEAD")
    return {
        "version": _read_version(),
        "ref": ref or _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_sha": sha,
        "git_sha_short": sha[:7],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo_url": _REPO_URL,
        "commits": _commits(limit),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=30, help="Number of commits to include")
    p.add_argument("--ref", default=None, help="Branch / ref label (default: current branch)")
    p.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"Output path (default: {_DEFAULT_OUT})",
    )
    args = p.parse_args(argv)

    try:
        manifest = build_manifest(limit=max(1, args.limit), ref=args.ref)
    except subprocess.CalledProcessError as e:
        print(f"error: git failed: {e}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out} ({len(manifest['commits'])} commits, {manifest['git_sha_short']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
