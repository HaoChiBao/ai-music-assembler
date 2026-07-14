"""Ensure embedded dashboard JavaScript parses (regression guard)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _dashboard_script() -> str:
    text = Path("music_assembler/api/app.py").read_text(encoding="utf-8")
    start = text.index("_DASHBOARD_HTML = (")
    end = text.index("install_openapi_docs(app)", start)
    chunk = text[start:end]
    pos = list(re.finditer(r"<script>\n", chunk))[-1].end()
    return chunk[pos : chunk.find("\n</script>", pos)]


def test_dashboard_javascript_syntax():
    js = _dashboard_script()
    assert "async function init()" in js
    assert "inv.backgrounds_ready" in js
    assert "inv.music_mp3s" in js
    assert "inv.music_videos" in js
    tmp = Path("/tmp/dashboard-syntax-test.js")
    tmp.write_text(js, encoding="utf-8")
    subprocess.run(["node", "--check", str(tmp)], check=True)
