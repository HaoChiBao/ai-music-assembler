# YouTube Uploader Microservice — explanation

This document explains how to split **YouTube uploading** out of **ai-music-assembler** into a separate GitHub repository. For the full build spec (API contracts, repo layout, checklist), see **[YOUTUBE_UPLOADER_MICROSERVICE.md](./YOUTUBE_UPLOADER_MICROSERVICE.md)**.

---

## Why split?

**ai-music-assembler** is heavy: FFmpeg encoding, BiRefNet thumbnails, MP3 mixing, OpenAI/Gemini metadata. **YouTube uploading** is a different problem: OAuth, resumable uploads, scheduling, multi-channel tokens, retries, and queue management.

Separating them lets you:

- Run uploads on a **small cloud worker** (I/O bound, not 32 GB RAM)
- Manage **multiple YouTube channels** from one uploader service
- **Cron daily** upload jobs without keeping your Mac awake
- **Retry failures** independently of video rendering

---

## Service boundary

```mermaid
flowchart LR
  subgraph assembler [ai-music-assembler]
    BUILD[generate-music-videos]
    OUT[mp4 + thumbnail + title + description]
  end

  subgraph storage [Object storage — S3 / R2]
    S3[(videos + registry)]
  end

  subgraph uploader [youtube-uploader — new repo]
    RUN[uploader run]
    YT[YouTube API]
  end

  BUILD --> OUT --> S3
  S3 --> RUN --> YT
```

| Responsibility | ai-music-assembler | youtube-uploader |
|----------------|:------------------:|:----------------:|
| MP3 mix + video encode | ✅ | ❌ |
| Thumbnail render (rembg) | ✅ | ❌ |
| Title/description **generation** | ✅ | ❌ |
| Receive final title + description | ❌ | ✅ |
| YouTube OAuth (per channel) | ❌ | ✅ |
| Video upload + custom thumbnail | ❌ | ✅ |
| Schedule `publishAt` | ❌ | ✅ |
| Upload queue / registry | writes `pending` | owns lifecycle |
| List scheduled videos | ❌ | ✅ |
| Retry on timeout / 429 / 5xx | ❌ | ✅ |
| Multi-channel | ❌ | ✅ |

---

## What stays in ai-music-assembler

- `generate-music-videos` — full render pipeline
- `youtube_metadata.py` + `prompts/youtube_metadata.txt`
- Backgrounds, music library, FFmpeg, rembg
- Output folder: `music-video/mv_*/`

**After a video is built**, the assembler only needs to:

1. Upload files to object storage (or keep local paths for now)
2. Append a **pending** row to the upload registry **or** `POST` a job to the uploader API

It should **stop** calling `schedule-music-videos` once the uploader service is live.

---

## What moves to youtube-uploader (new repo)

### Source files to copy from this repo

| This repo | New repo module |
|-----------|-----------------|
| `music_assembler/youtube_upload.py` | `uploader/youtube_client.py` |
| `music_assembler/youtube_channel.py` | `uploader/channel_list.py` |
| `music_assembler/video_registry.py` | `uploader/registry.py` (generalized) |
| `music_assembler/schedule_music_videos.py` | `uploader/scheduler.py` |
| `music_assembler/progress_bars.py` | `uploader/progress.py` (optional) |

**Do not copy:** pipeline, ffmpeg, audio, metadata generation, segmentation.

### Core capabilities

1. **OAuth** — one Google client secret; one refresh token **per channel**
2. **Upload** — resumable insert + optional thumbnail (with retries)
3. **Schedule** — set `publishAt` (RFC3339 UTC); video starts private
4. **Registry** — track `pending` → `uploaded` / `failed`
5. **List** — all channel videos or `--scheduled-only`
6. **Multi-channel** — `channels.yaml` routes token + publish settings

---

## Job handoff (assembler → uploader)

Each finished video becomes one registry entry or API job:

```json
{
  "id": "mv_20260617_180732_01",
  "channel_id": "channel-a",
  "status": "pending",
  "title": "Final YouTube title (from assembler metadata step)",
  "description": "Full description text with chapters",
  "video_uri": "s3://bucket/videos/channel-a/mv_…/video.mp4",
  "thumbnail_uri": "s3://bucket/videos/channel-a/mv_…/thumbnail.png"
}
```

The uploader downloads from `video_uri`, uploads to YouTube, sets schedule, marks `uploaded`.

---

## Multi-channel setup

```yaml
# config/channels.yaml (in uploader repo)
channels:
  - id: channel-a
    token_secret: secrets/channel-a/youtube_token.json
    publish:
      timezone: America/New_York
      hour: 9
      interval_hours: 24

  - id: channel-b
    token_secret: secrets/channel-b/youtube_token.json
    publish:
      hour: 12
      interval_hours: 24

google:
  client_secret: secrets/shared/google_oauth_client.json
```

Authorize each channel once (`uploader auth --channel X`), store token in secrets manager in production.

---

## How it runs in production

```mermaid
flowchart TB
  CRON[Daily cron] --> RUN[uploader run --channel X]
  RUN --> S3[Read registry + mp4 from S3]
  RUN --> YT[YouTube Data API v3]
```

| Component | Uploader needs | Assembler needs |
|-----------|----------------|-----------------|
| Worker VM | 2–4 vCPU, 4–8 GB RAM | 32 GB RAM for encode |
| Storage | Read videos + registry | Write videos + pending jobs |
| Scheduler | Cron per channel | Cron for daily **build** |
| Secrets | OAuth tokens per channel | OpenAI/Gemini keys only |

---

## CLI commands (new repo)

```bash
# One-time per channel (browser OAuth)
uploader auth --channel channel-a

# Preview publish schedule
uploader plan --channel channel-a --start "2026-06-21 09:00" --interval-hours 24

# Upload all pending
uploader run --channel channel-a --upload-retries 5

# Verify on YouTube
uploader list --channel channel-a --scheduled-only
```

**Daily cron example:**

```cron
0 3 * * * uploader run --channel channel-a --upload-retries 5
0 4 * * * uploader run --channel channel-b --upload-retries 5
```

---

## Implementation phases

| Phase | Deliverable |
|-------|-------------|
| **1** | Copy modules, CLI (`auth`, `run`, `plan`, `list`), file-based registry |
| **2** | Assembler writes S3 URIs + pending jobs; cron on one VM |
| **3** | HTTP API (`POST /v1/jobs`), Postgres registry, secrets manager |
| **4** | Idempotency, quota tracking, alerts on failure |

---

## Google Cloud / YouTube requirements

- Google Cloud project with **YouTube Data API v3** enabled
- OAuth consent screen + OAuth client (Desktop for dev, Web for prod)
- One **verified YouTube channel** per `channel_id` (for custom thumbnails)
- Upload quota: ~6 videos/day default per project unless extended

---

## Commands today vs after split

**Today (everything in this repo):**

```bash
generate-music-videos -n 1 --thumbnail-text "OMYO" --workers 2
schedule-music-videos --start "2026-06-21 09:00" --interval-hours 24
list-youtube-videos --scheduled-only
```

**After split:**

```bash
# ai-music-assembler
generate-music-videos -n 1 --thumbnail-text "OMYO" --workers 2
# → sync to S3 + write pending registry row

# youtube-uploader (new repo)
uploader run --channel channel-a --upload-retries 5
uploader list --channel channel-a --scheduled-only
```

---

## Related docs in this repo

| File | Contents |
|------|----------|
| **[YOUTUBE_UPLOADER_MICROSERVICE.md](./YOUTUBE_UPLOADER_MICROSERVICE.md)** | Full build spec: API shapes, repo layout, checklist, YouTube API details |
| **[FUTURE_PLAN.md](./FUTURE_PLAN.md)** | End-to-end hosting (storage, cron, multi-channel platform) |
| `music-video/video_registry.txt` | Example pending/uploaded registry format today |
