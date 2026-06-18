# Build prompt — YouTube Uploader microservice

Copy everything below the line into a new Cursor chat (or new empty GitHub repo) to build the service.

---

## PROMPT START

Build a new Python project called **youtube-uploader** — a standalone microservice that uploads pre-rendered music videos to YouTube, schedules publish times, and supports multiple YouTube channels. It is split from an existing repo called **ai-music-assembler**, which handles video rendering only and will hand off finished jobs to this service.

### Scope — what this service DOES

- YouTube OAuth (one Google client secret, one refresh token **per channel**)
- Resumable video upload via YouTube Data API v3
- Custom thumbnail upload (best-effort; never fail the video upload if thumbnail fails)
- Schedule publish with `publishAt` (RFC3339 UTC; forces `privacyStatus=private`)
- Upload registry / queue: `pending` → `uploading` → `uploaded` | `failed`
- Batch processing of pending jobs with staggered publish times
- Retry transient failures (timeouts, connection errors, HTTP 408/429/5xx) with linear backoff
- List all videos on a channel; filter to **scheduled only** (private + future `publishAt`)
- Multi-channel via `config/channels.yaml`
- CLI for cron-based daily runs
- Resolve video/thumbnail from `file://`, local paths, or `s3://` URIs

### Scope — what this service does NOT do

- No FFmpeg, no video encoding, no MP3 mixing
- No thumbnail generation / rembg / segmentation
- No OpenAI/Gemini title or description **generation** (receives final strings from upstream)
- No background image processing

### Reference implementation (copy and adapt)

If you have access to **ai-music-assembler**, port these files (rename package from `music_assembler` to `uploader`):

| Source | Target |
|--------|--------|
| `music_assembler/youtube_upload.py` | `uploader/youtube_client.py` |
| `music_assembler/youtube_channel.py` | `uploader/channel_list.py` |
| `music_assembler/video_registry.py` | `uploader/registry.py` |
| `music_assembler/schedule_music_videos.py` | `uploader/scheduler.py` |
| `music_assembler/progress_bars.py` | `uploader/progress.py` |

Preserve behavior from the reference:

- `SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]`
- Resumable upload: `MediaFileUpload(..., chunksize=8*1024*1024, resumable=True)`
- Thumbnail prep: downscale to 1280×720 JPEG if > 2 MB
- `upload_video_with_retry`: default 3 attempts, delay `30 * attempt` seconds
- `is_transient_upload_error`: timeouts, OSError errno ETIMEDOUT/ECONNRESET/etc., HttpError 408/429/5xx
- `list_channel_videos`: channels.list → uploads playlist → videos.list in batches of 50
- Scheduled = `privacyStatus == "private"` and `publishAt` in the future

If you do NOT have the reference repo, implement from the API descriptions below.

---

### Project layout

```
youtube-uploader/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore                    # ignore secrets/, *.json tokens, .env
├── config/
│   └── channels.yaml.example
├── uploader/
│   ├── __init__.py
│   ├── youtube_client.py       # OAuth, upload_video, upload_video_with_retry
│   ├── channel_list.py         # list_channel_videos, YouTubeVideoInfo
│   ├── registry.py             # UploadEntry, UploadRegistry (JSON-lines)
│   ├── scheduler.py            # process pending batch, compute publish_at
│   ├── channels.py             # load channels.yaml
│   ├── storage.py              # resolve URI → local Path (file + s3)
│   └── progress.py             # multi-line progress bars for CLI
├── cli/
│   └── main.py                 # entry point
└── tests/
    ├── test_registry.py
    ├── test_scheduler.py
    ├── test_retry.py
    └── test_storage.py
```

### Dependencies (`pyproject.toml`)

```toml
[project]
name = "youtube-uploader"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "google-api-python-client>=2.100.0",
    "google-auth-oauthlib>=1.2.0",
    "google-auth-httplib2>=0.2.0",
    "Pillow>=10.0.0",
    "python-dotenv>=1.0.0",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
s3 = ["boto3>=1.34"]

[project.scripts]
uploader = "cli.main:main"
```

Use `pip install .` (not editable on Python 3.14+).

---

### Registry schema (JSON-lines, one object per line)

```json
{
  "id": "mv_20260617_180732_01",
  "channel_id": "channel-a",
  "status": "pending",
  "title": "YouTube title (final, from upstream)",
  "description": "Full description text, or s3://bucket/path/description.txt",
  "video_uri": "s3://bucket/videos/channel-a/mv_20260617_180732_01/video.mp4",
  "thumbnail_uri": "s3://bucket/videos/channel-a/mv_20260617_180732_01/thumbnail.png",
  "youtube_id": "",
  "youtube_url": "",
  "publish_at": "",
  "created_at": "2026-06-17T18:07:32Z",
  "uploaded_at": "",
  "error": "",
  "extra": {}
}
```

Status values: `pending`, `uploading`, `uploaded`, `failed`.

Registry API:

- `pending(channel_id=None) -> list[UploadEntry]`
- `append(entry)`, `mark_uploading(id)`, `mark_uploaded(id, youtube_id, publish_at)`, `mark_failed(id, error)`

Default registry path: `state/{channel_id}/upload_registry.txt` (configurable).

**Backward compatibility:** also accept legacy rows where `video` is a local path and `description` is a path to a `.txt` file (read file contents).

---

### channels.yaml

```yaml
channels:
  - id: channel-a
    name: "My Channel"
    token_path: secrets/channel-a/youtube_token.json
    registry_path: state/channel-a/upload_registry.txt
    category_id: "10"
    default_tags: [lofi, chill]
    made_for_kids: false
    publish:
      timezone: America/New_York
      hour: 9
      interval_hours: 24

google:
  client_secret_path: secrets/shared/client_secret.json
  oauth_port: 8080
```

---

### CLI commands (implement all)

```bash
# OAuth — opens browser, saves refresh token for channel
uploader auth --channel channel-a

# Preview schedule for pending jobs (no upload)
uploader plan --channel channel-a [--start "2026-06-21 09:00"] [--interval-hours 24]

# Process all pending for channel
uploader run --channel channel-a [--upload-retries 5] [--retry-delay 30] [--limit N]

# List videos on YouTube
uploader list --channel channel-a [--scheduled-only]

# Append a job manually (for testing)
uploader enqueue --channel channel-a --id test_01 --video ./test.mp4 --title "Test" --description "Desc"
```

**`uploader run` flow:**

1. Load channel config + registry
2. For each `pending` entry (respect `--limit`):
   - Resolve `video_uri` / `thumbnail_uri` / `description` via `storage.py` (download S3 to temp dir)
   - Compute `publish_at`: from `--start` + index × `--interval-hours` (convert local → RFC3339 UTC), unless `--no-schedule`
   - Show per-job progress bar during upload
   - Call `upload_video_with_retry`
   - On success: `mark_uploaded`, persist refreshed OAuth token
   - On failure: `mark_failed`, continue to next job
3. Print summary: uploaded N/M, list YouTube URLs

Print status to stderr immediately on startup (Google client import is slow).

---

### Core functions to implement

```python
# youtube_client.py
def get_credentials(client_secret: Path, token_path: Path, *, oauth_port: int = 8080): ...
def upload_video(video_path: Path, *, title, description, client_secret, token_path,
                 privacy="private", category_id="10", tags=None, made_for_kids=False,
                 thumbnail_path=None, publish_at=None, oauth_port=8080, on_progress=None) -> dict: ...
def upload_video_with_retry(video_path, *, max_attempts=3, retry_delay_sec=30.0, on_retry=None, **kwargs) -> dict: ...
def is_transient_upload_error(exc: BaseException) -> bool: ...

# channel_list.py
@dataclass
class YouTubeVideoInfo: video_id, title, privacy_status, publish_at, url; is_scheduled property
def list_channel_videos(client_secret, token_path, *, scheduled_only=False) -> list[YouTubeVideoInfo]: ...

# storage.py
def resolve_to_local_path(uri: str, *, temp_dir: Path) -> Path: ...
def load_description(description: str) -> str: ...  # inline or fetch from s3:// / file path

# scheduler.py
def compute_publish_schedule(pending: list, start: datetime, interval_hours: float) -> list[tuple[UploadEntry, str]]: ...
def run_channel(channel_id: str, *, dry_run=False, ...) -> RunResult: ...
```

---

### Upstream contract (ai-music-assembler handoff)

The video assembler will produce jobs like:

```json
{
  "id": "mv_20260617_180732_01",
  "channel_id": "channel-a",
  "title": "...",
  "description": "... with YouTube chapter timestamps ...",
  "video_uri": "s3://bucket/videos/channel-a/mv_.../mv_..._video.mp4",
  "thumbnail_uri": "s3://bucket/videos/channel-a/mv_.../mv_..._thumbnail.png"
}
```

Uploader owns everything after that.

---

### Google Cloud setup (document in README)

1. Enable YouTube Data API v3
2. OAuth consent screen
3. OAuth client (Desktop app for `uploader auth`)
4. Register redirect: `http://localhost:8080` (no trailing slash)
5. Verified channel required for custom thumbnails

---

### Testing requirements

- Unit tests: registry read/write, `is_transient_upload_error`, publish_at UTC conversion, description loader
- Mock YouTube API for upload/list where possible
- Manual test doc: auth → enqueue local mp4 → run → list --scheduled-only

---

### Code quality

- Match existing reference style: minimal abstraction, no over-engineering
- Type hints, dataclasses
- Gitignore secrets and tokens
- README with setup, auth, cron example, multi-channel example
- Do NOT commit real credentials

---

### Deliverables (Phase 1 — complete in this session)

1. Full repo scaffold with working `pip install .`
2. All CLI subcommands: `auth`, `plan`, `run`, `list`, `enqueue`
3. File-based registry + channels.yaml
4. Local file paths working end-to-end (S3 optional via `[s3]` extra)
5. Retry logic on upload
6. Progress bars during batch upload
7. README + `.env.example` + `channels.yaml.example`
8. Basic tests

Phase 2 (HTTP API) and Postgres are OUT OF SCOPE unless time permits — stub a `api/` folder with a TODO comment only.

Build the project now. Start by creating `pyproject.toml` and porting/implementing `youtube_client.py`, then registry, scheduler, CLI.

## PROMPT END
