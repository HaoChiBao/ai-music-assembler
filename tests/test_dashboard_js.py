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


def test_batched_asset_upload_snapshots_destination_pool(tmp_path):
    js = _dashboard_script()
    assert "const uploadPool = ui.assetPool;" in js
    assert re.search(
        r"uploadAssetBatchWithRetry\(\s*batch,\s*uploadPool,\s*imagesFolder,",
        js,
    )

    start = js.index("function buildAssetUploadFormData")
    end = js.index("\nfunction mergeUploadResults", start)
    build_form_data = js[start:end]
    harness = f"""
class FormData {{
  constructor() {{ this.values = []; }}
  append(key, value) {{ this.values.push([key, value]); }}
}}
function cat() {{ return 'korean'; }}
{build_form_data}
const data = buildAssetUploadFormData(
  [{{name: 'background.jpg'}}],
  'post-processed',
  'backgrounds',
  true
);
const pool = data.values.find(([key]) => key === 'pool');
if (!pool || pool[1] !== 'post-processed') {{
  throw new Error('upload pool was not preserved');
}}
"""
    tmp = tmp_path / "dashboard-upload-pool-test.js"
    tmp.write_text(harness, encoding="utf-8")
    subprocess.run(["node", str(tmp)], check=True)

