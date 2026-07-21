"""Ensure embedded dashboard JavaScript parses (regression guard)."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


def _dashboard_html_runtime() -> str:
    """Evaluate ``_DASHBOARD_HTML`` as Python does (catches ``\\n`` escape bugs)."""
    text = Path("music_assembler/api/app.py").read_text(encoding="utf-8")
    fonts_start = text.index("_DASHBOARD_DESIGN_FONTS = ")
    login_start = text.index("\n_LOGIN_HTML")
    html_start = text.index("_DASHBOARD_HTML = (")
    html_end = text.index("\ninstall_openapi_docs(app)", html_start)
    code = (
        text[fonts_start:login_start]
        + "\n"
        + text[html_start:html_end]
        + "\nhtml = _DASHBOARD_HTML\n"
    )
    ns: dict = {}
    exec(code, ns)
    return ns["html"]


def _dashboard_script() -> str:
    html = _dashboard_html_runtime()
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert scripts, "dashboard HTML missing <script> block"
    return scripts[-1]


def test_dashboard_javascript_syntax():
    js = _dashboard_script()
    assert "async function init()" in js
    assert "inv.backgrounds_ready" in js
    assert "inv.backgrounds_in_flight" in js
    assert "inv.backgrounds_used" in js
    assert "inv.music_mp3s" in js
    assert "inv.music_videos" in js
    assert "function fmtDuration(sec)" in js
    assert "function jobTimingHtml(row)" in js
    assert "function jobDetailsHtml(row)" in js
    assert "function applyRunMetrics(metrics)" in js
    assert "function applyAssemblyHealth(health)" in js
    assert "jobTimingHtml(row)" in js
    assert "jobDetailsHtml(row)" in js
    assert "function initTemplatePickers()" in js
    assert "function renderTemplatePicker(" in js
    assert "template_id: templateId" in js or "template_id: video.template_id" in js
    # Placeholders must remain valid JS syntax before server-side substitution.
    assert "JSON.parse('__VIDEO_TEMPLATES_JSON__')" in js
    # Runtime string must keep JS newline escapes (not expand them to real newlines).
    assert ").join('\\n');" in js or ').join("\\n");' in js
    assert ").join('\n');" not in js
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dashboard-syntax-test.js"
        path.write_text(js, encoding="utf-8")
        subprocess.run(["node", "--check", str(path)], check=True)


def test_dashboard_timing_helpers():
    js = _dashboard_script()
    start = js.index("function fmtDuration(sec)")
    end = js.index("\nfunction fmtBytes", start)
    helpers = js[start:end]
    harness = f"""
function esc(s) {{ return String(s); }}
function fmtTime(iso) {{ return iso || ''; }}
function isCancellableStatus(status) {{
  return status === 'running' || status === 'cancelling';
}}
{helpers}
const finished = jobTimingHtml({{
  started_at: '2026-07-18T12:00:00Z',
  finished_at: '2026-07-18T13:05:00Z',
  elapsed_sec: 3900,
  status: 'succeeded',
}});
if (!finished.includes('Took') || !finished.includes('1h 5m')) {{
  throw new Error('finished timing missing Took duration: ' + finished);
}}
if (!finished.includes('Start') || !finished.includes('Finish')) {{
  throw new Error('finished timing missing start/finish labels');
}}
const running = jobTimingHtml({{
  created_at: '2026-07-18T12:00:00Z',
  elapsed_sec: 125,
  status: 'running',
}});
if (!running.includes('Elapsed') || !running.includes('2m 5s')) {{
  throw new Error('running timing missing Elapsed: ' + running);
}}
if (fmtDuration(45) !== '45s') throw new Error('fmtDuration seconds');
if (fmtDuration(125) !== '2m 5s') throw new Error('fmtDuration minutes');
if (fmtDuration(3900) !== '1h 5m') throw new Error('fmtDuration hours');
const details = jobDetailsHtml({{
  channel: 'nappabeats',
  video_id: 'mv_test',
  claimed_background: 'post-processed/korean/bg01.png',
  duration_min: 90,
}});
if (!details.includes('Channel') || !details.includes('nappabeats')) {{
  throw new Error('details missing channel: ' + details);
}}
if (!details.includes('Video') || !details.includes('mv_test')) {{
  throw new Error('details missing video: ' + details);
}}
if (!details.includes('BG') || !details.includes('bg01.png')) {{
  throw new Error('details missing claimed background: ' + details);
}}
if (!details.includes('Target') || !details.includes('90 min')) {{
  throw new Error('details missing target duration: ' + details);
}}
if (fmtPct(0.75) !== '75%') throw new Error('fmtPct');
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dashboard-timing-helpers-test.js"
        path.write_text(harness, encoding="utf-8")
        subprocess.run(["node", str(path)], check=True)


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


def test_schedule_subtab_switch_preserves_unsaved_editor_state():
    js = _dashboard_script()
    start = js.index("function showScheduleSubtab(tab)")
    end = js.index("\nfunction openScheduleEditorForChannel", start)

    assert "loadScheduleOverview()" in js[start:end]
    assert "loadScheduleEditor(" not in js[start:end]
