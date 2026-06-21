#!/usr/bin/env bash
# Cloud Run Job entrypoint — mirrors local `make-short-music-video`.
#
# Required env (Cloudflare R2):
#   CLOUDFLARE_R2_BUCKET
#   CLOUDFLARE_R2_ENDPOINT          e.g. https://ACCOUNT_ID.r2.cloudflarestorage.com
#   CLOUDFLARE_R2_ACCESS_KEY_ID
#   CLOUDFLARE_R2_SECRET_ACCESS_KEY
#   ASSEMBLY_CATEGORY               genre subfolder, e.g. korean
#
# Optional env:
#   THUMBNAIL_TEXT                  passed to --thumbnail-text (e.g. OMYO)
#   WORK_DIR                        scratch dir (default /work)

set -euo pipefail

: "${CLOUDFLARE_R2_BUCKET:?Set CLOUDFLARE_R2_BUCKET}"
: "${CLOUDFLARE_R2_ENDPOINT:?Set CLOUDFLARE_R2_ENDPOINT}"
: "${CLOUDFLARE_R2_ACCESS_KEY_ID:?Set CLOUDFLARE_R2_ACCESS_KEY_ID}"
: "${CLOUDFLARE_R2_SECRET_ACCESS_KEY:?Set CLOUDFLARE_R2_SECRET_ACCESS_KEY}"
: "${ASSEMBLY_CATEGORY:?Set ASSEMBLY_CATEGORY (e.g. korean)}"

export AWS_ACCESS_KEY_ID="$CLOUDFLARE_R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$CLOUDFLARE_R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION=auto

WORK="${WORK_DIR:-/work}"
CATEGORY="$ASSEMBLY_CATEGORY"
S3=(aws s3 --endpoint-url "$CLOUDFLARE_R2_ENDPOINT")

mkdir -p "$WORK/music" "$WORK/post-processed" "$WORK/music-video"

echo "==> Sync inputs for category: ${CATEGORY}"
"${S3[@]}" sync "s3://${CLOUDFLARE_R2_BUCKET}/music/${CATEGORY}/" "$WORK/music/"
"${S3[@]}" sync "s3://${CLOUDFLARE_R2_BUCKET}/post-processed/${CATEGORY}/" "$WORK/post-processed/" \
  --exclude "used/*"

if ! compgen -G "$WORK/music/"*.mp3 >/dev/null 2>&1 \
  && ! compgen -G "$WORK/music/"*.MP3 >/dev/null 2>&1; then
  echo "error: no MP3s in s3://${CLOUDFLARE_R2_BUCKET}/music/${CATEGORY}/" >&2
  exit 1
fi

shopt -s nullglob
images=("$WORK/post-processed/"*.png "$WORK/post-processed/"*.jpg \
  "$WORK/post-processed/"*.jpeg "$WORK/post-processed/"*.webp)
if ((${#images[@]} == 0)); then
  echo "error: no backgrounds in s3://${CLOUDFLARE_R2_BUCKET}/post-processed/${CATEGORY}/" >&2
  exit 1
fi

echo "==> Assemble one video in $WORK"
cd "$WORK"

args=()
if [[ -n "${THUMBNAIL_TEXT:-}" ]]; then
  args+=(--thumbnail-text "$THUMBNAIL_TEXT")
fi

make-short-music-video "${args[@]}"

echo "==> Sync outputs to s3://${CLOUDFLARE_R2_BUCKET}/music-video/${CATEGORY}/"
"${S3[@]}" sync "$WORK/music-video/" "s3://${CLOUDFLARE_R2_BUCKET}/music-video/${CATEGORY}/"

if [[ -d "$WORK/post-processed/used" ]]; then
  echo "==> Sync used backgrounds"
  "${S3[@]}" sync "$WORK/post-processed/used/" \
    "s3://${CLOUDFLARE_R2_BUCKET}/post-processed/${CATEGORY}/used/"
fi

echo "==> Done"
