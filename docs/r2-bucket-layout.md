# Object storage layout

Cloudflare R2 bucket for the **assembly pipeline** and **background extension** workflow. Music and images are grouped by **category** (genre) subfolders. Upload manually (or sync from local); jobs read/write the paths below.

## Bucket tree

```
s3://{bucket}/
├── music/
│   └── {category}/              # e.g. korean, lofi, jazz
│       └── *.mp3
├── pre-processed/
│   └── {category}/
│       ├── *.jpg / *.png / …   # raw photos (input to extend-backgrounds)
│       ├── in-flight/{execution_id}/  # claimed by parallel extend workers
│       └── used/                # sources retired after successful extend
├── post-processed/
│   └── {category}/
│       ├── *.png                # 16:9 background stills (input to assembly)
│       ├── in-flight/           # backgrounds claimed by running assembly jobs
│       │   └── {execution_id}/
│       │       └── {filename}
│       └── used/                # backgrounds retired after encode
└── music-video/
    └── {youtube-channel}/       # e.g. nappabeats, sapporobeats (not genre category)
        └── mv_{timestamp}/      # assembly job output
            ├── frame_{stem}.png
            ├── mv_{timestamp}_mix.mp3
            ├── mv_{timestamp}_video.mp4
            ├── mv_{timestamp}_tracklist.txt
            └── mv_{timestamp}_thumbnail.png   # when THUMBNAIL_TEXT is set
```

**Pipeline flow:** upload raw photos to `pre-processed/{category}/` → run `extend-from-r2` (Cloud Run Job `music-extend`) → widescreen PNGs land in `post-processed/{category}/` → assembly reads MP3s + post-processed stills from `{category}` → finished runs upload to `music-video/{youtube-channel}/` (channel slug from the uploader, e.g. `nappabeats`).

**First category:** `korean` — set `ASSEMBLY_CATEGORY=korean` in `.env`.

### Initialize empty folders on R2

After filling in `CLOUDFLARE_R2_*` in `.env`:

```bash
set -a && source .env && set +a
./scripts/r2-init-layout.sh
```

Creates placeholder keys so the category tree exists before you upload assets:

```
music/korean/.gitkeep
pre-processed/korean/.gitkeep
pre-processed/korean/used/.gitkeep
post-processed/korean/.gitkeep
post-processed/korean/used/.gitkeep
music-video/korean/.gitkeep
```

The assembly job syncs `music/{category}/` and `post-processed/{category}/` into a flat work dir, runs `make-short-music-video` (or `assemble-from-r2`), then uploads to `music-video/{category}/`.

## Local folders (same layout)

```
music/korean/*.mp3
pre-processed/korean/*.jpg
pre-processed/korean/used/
post-processed/korean/*.png
post-processed/korean/used/
```

## Manual upload (korean example)

```bash
set -a && source .env && set +a
export AWS_ACCESS_KEY_ID="$CLOUDFLARE_R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$CLOUDFLARE_R2_SECRET_ACCESS_KEY"

aws s3 sync ./music/korean/ \
  "s3://${CLOUDFLARE_R2_BUCKET}/music/korean/" \
  --endpoint-url "$CLOUDFLARE_R2_ENDPOINT"

aws s3 sync ./pre-processed/korean/ \
  "s3://${CLOUDFLARE_R2_BUCKET}/pre-processed/korean/" \
  --endpoint-url "$CLOUDFLARE_R2_ENDPOINT" --exclude "used/*"

aws s3 sync ./post-processed/korean/ \
  "s3://${CLOUDFLARE_R2_BUCKET}/post-processed/korean/" \
  --endpoint-url "$CLOUDFLARE_R2_ENDPOINT" --exclude "used/*"
```

## Job env

| Variable | Example | Purpose |
|----------|---------|---------|
| `ASSEMBLY_CATEGORY` | `korean` | Subfolder under `music/`, `pre-processed/`, and `post-processed/` |

## Assembly job I/O

| Step | Path |
|------|------|
| Download music | `music/{category}/` → `/work/music/` |
| Claim background | Copy one `post-processed/{category}/{file}` → `post-processed/{category}/in-flight/{execution_id}/{file}`, delete source (parallel-safe) |
| Download backgrounds | Claimed file only → `/work/post-processed/` (local CLI syncs all except `used/` and `in-flight/`) |
| Upload video | `/work/music-video/` → `music-video/{category}/` |
| Retire used background | After encode: copy from `in-flight/{execution_id}/` (or pool path) → `used/`, delete source |
| No backgrounds left | Job writes `failed` progress and exits (releases nothing — nothing was claimed) |

## Background extension

| Step | Path |
|------|------|
| Raw photos (R2) | `pre-processed/{category}/` |
| Extended PNGs (R2) | `post-processed/{category}/` |
| Retired sources (R2) | `pre-processed/{category}/used/` |

**`extend-from-r2`** (recommended):

```bash
pip install ".[r2]"
extend-from-r2              # one photo per run (default)
extend-from-r2 --all        # every pending photo
extend-from-r2 --limit 3    # batch of three
```

Or run locally: sync `pre-processed/` down, `extend-backgrounds`, sync `post-processed/` and `pre-processed/used/` back.
