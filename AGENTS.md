# AGENTS.md

## Cursor Cloud specific instructions

Python project (`music-video-assembler`, requires Python 3.10+; the VM has 3.12). There is no
Node/JS package — Node is only used by one test that syntax-checks the dashboard's embedded JS.
See `README.md` for the full product/CLI reference and `.env.example` for all config variables.

### Environment
- Dependencies live in a local virtualenv at `.venv` (the startup update script creates it and
  installs `.[api,youtube]` + `pytest`). Use `.venv/bin/<tool>` or activate with
  `source .venv/bin/activate`.
- `ffmpeg`/`ffprobe` must be on `PATH` (already present in the base image) — required for every
  audio/video build.
- No database. State is file/object-based (local folders, or Cloudflare R2 in production).

### Test
- `.venv/bin/pytest -q` from the repo root (CI runs `pytest -q`). Tests mock R2/GCP/YouTube, so
  they need no credentials or network.

### Run — CLI video assembler (core product, no API keys needed)
- `music/` and `post-processed/` are gitignored and empty by default. Populate `music/` with MP3s
  and `post-processed/` with a 16:9 background image before building.
- Build: `.venv/bin/make-short-music-video`. The default target length is 75–90 min (huge); for a
  quick smoke build pass small values, e.g. `--min-duration 0.3 --max-duration 0.6` (minutes;
  fractions allowed). Output lands in `music-video/mv_*/` (frame PNG, mix MP3, MP4, tracklist).

### Run — control API + dashboard (service, port 8080)
- Start: `ASSEMBLY_API_HOST=127.0.0.1 .venv/bin/assembly-api` (binds 0.0.0.0:8080 by default).
- Backend works without credentials: `/health`, `/v1/version`, and Swagger UI at `/docs` all
  respond. It is a control plane only — actually dispatching assemble/extend jobs needs GCP + R2
  credentials (optional; see `.env.example`).
- Gotcha: as of this setup the dashboard homepage `/` does not fully render in a browser (stuck on
  "Loading dashboard…") due to a syntax error in the served JavaScript — `_DASHBOARD_HTML` in
  `music_assembler/api/app.py` is a non-raw string, so a `.join('\n')` in the embedded JS is
  emitted with a literal newline and breaks parsing. `tests/test_dashboard_js.py` passes because it
  `node --check`s the raw source (where `\n` is still an escape), not the served output. This is an
  application bug, not an environment problem; the backend API is unaffected.
