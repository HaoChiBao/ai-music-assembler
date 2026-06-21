# Object storage layout (assembly only)

Minimal bucket for the **Cloud Run assembly job**. Music and backgrounds are grouped by **category** (genre) subfolders. Upload manually; the job syncs one category, assembles a video, and uploads the result.

## Bucket tree

```
s3://{bucket}/
├── music/
│   └── {category}/              # e.g. korean, lofi, jazz
│       └── *.mp3
├── post-processed/
│   └── {category}/
│       ├── *.png                # 16:9 background stills
│       └── used/                # backgrounds retired after encode
└── music-video/
    └── {category}/
        └── mv_{timestamp}/      # job output
            ├── frame_{stem}.png
            ├── mv_{timestamp}_mix.mp3
            ├── mv_{timestamp}_video.mp4
            ├── mv_{timestamp}_tracklist.txt
            └── mv_{timestamp}_thumbnail.png   # when THUMBNAIL_TEXT is set
```

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
post-processed/korean/.gitkeep
post-processed/korean/used/.gitkeep
music-video/korean/.gitkeep
```

The job syncs `music/{category}/` and `post-processed/{category}/` into a flat work dir, runs `make-short-music-video`, then uploads to `music-video/{category}/`.

## Local folders (same layout)

```
music/korean/*.mp3
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

aws s3 sync ./post-processed/korean/ \
  "s3://${CLOUDFLARE_R2_BUCKET}/post-processed/korean/" \
  --endpoint-url "$CLOUDFLARE_R2_ENDPOINT" --exclude "used/*"
```

## Job env

| Variable | Example | Purpose |
|----------|---------|---------|
| `ASSEMBLY_CATEGORY` | `korean` | Which subfolder under `music/` and `post-processed/` |

## Job I/O

| Step | Path |
|------|------|
| Download music | `music/{category}/` → `/work/music/` |
| Download backgrounds | `post-processed/{category}/` → `/work/post-processed/` |
| Upload video | `/work/music-video/` → `music-video/{category}/` |
| Upload used backgrounds | `/work/post-processed/used/` → `post-processed/{category}/used/` |
