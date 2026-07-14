# Production status — July 2026

Mirror for [Linear production doc](https://linear.app/yangspace/document/production-status-june-2026-c5db29e1b5b9) and [Notion Project Wiki](https://app.notion.com/p/387850bd5b0881c1a746d84960d54fbf). Last updated: **2026-07-06**.

---

## Hosted services

| Service | Type | URL / revision |
|---------|------|----------------|
| **music-assembly-api** | Cloud Run Service | https://music-assembly-api-q3uklh4a6a-pd.a.run.app (`music-assembly-api-00036-gmp`) |
| **music-assemble** | Cloud Run Job | `northamerica-northeast2` |
| **youtuber-uploader-app** | Cloud Run Service | https://youtuber-uploader-app-q3uklh4a6a-pd.a.run.app |

- **R2 bucket:** `music-assembly-data` · **Category:** `korean`
- **Auth:** `ASSEMBLY_DASHBOARD_PASSWORD` + `ASSEMBLY_API_KEY`
- **Package version:** `0.1.11`

---

## Shipped this week (2026-07-05 → 2026-07-06)

### Mass upload to R2 (dashboard + API) — **Done**

- `POST /v1/assets/upload` — multipart, up to 50 files × 20 MB (jpg/png/webp)
- Dashboard: **Library → Backgrounds → Pre-processed / Post-processed** upload UI
- Pools: flat `pre-processed/{category}/` and `post-processed/{folder}/`
- Dependency: `python-multipart` in `[api]` extras
- Tests: `tests/test_asset_upload.py`

**Linear:** create **YAN-XX** → mark **Done** (or reuse backlog item if one exists).

**Notion:** mark task **Mass upload pre-processed images** → **Done**.

### Cloud Build deploy size fix — **Done**

- Added `.gcloudignore` — excludes `music/`, `music-video/`, `pre-processed/`, etc.
- Upload tarball: **~7.4 GiB → ~1.2 MiB**

**Linear:** create **YAN-XX** → mark **Done** (infra / deploy).

### Schedule tab UI — **Done**

- Seven day tiles (Sun–Sat): On toggle, assemble time, upload time
- Video settings from New run: **thumbnail text**, **duration (min)**, **variance (min)**
- Live summary panel (`#scheduleSummary`) for active channel
- Bug fix: `saveSchedule()` now persists `duration_min`, `variance_min`, `thumbnail_text` (backend already supported them)

**Linear:** update **YAN-46** — dashboard schedule UX **Done**; Cloud Scheduler cron wiring still **Backlog**.

### Gitignore / local media — **Done**

- Recursive ignore for `music/**`, `music-video/**`, asset folders
- Untracked local media from git index (no binaries were ever committed)

---

## Still open / backlog

| Item | Linear | Status |
|------|--------|--------|
| Cloud Scheduler job for `/v1/cron/run-schedules` | **YAN-46** | Backlog — UI ready, cron not wired |
| Pre-processed **subfolder** batches on R2 (extend) | **YAN-XX** (new) | Backlog — Phase 2 |
| Channel list from uploader API | **YAN-54**, **YAN-55** | Backlog |
| Confirm full dashboard encode | **YAN-45** | In progress |
| Uploader register on finish | **YAN-48** | In progress |

---

## Phase 2 — pre-processed subfolders (not started)

Extend pipeline is **flat-only** on R2 today (`list_claimable_pre_processed_keys` skips nested keys). Phase 2 would add:

- `source_folder` on extend jobs (mirror post-processed `images_folder`)
- Upload to `pre-processed/{category}/{batch}/`
- Dashboard folder picker for pre-processed batches

---

## Quick links

- Dashboard: https://music-assembly-api-q3uklh4a6a-pd.a.run.app
- Repo: `FUTURE_PLAN.md`, `docs/r2-bucket-layout.md`, `deploy/cloud-run-job.md` (YAN-46 cron setup)
- CI/CD: [deploy/github-actions-cicd.md](../deploy/github-actions-cicd.md) — tests on PRs; deploy dashboard on push to `main`
