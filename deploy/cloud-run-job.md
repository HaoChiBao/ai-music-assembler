# Cloud Run Job — assemble one video

Runs ``assemble-from-r2``: sync MP3s + backgrounds from R2, encode (with YouTube metadata by default), sync the ``mv_*`` folder back.

## 1. Bucket layout

See [docs/r2-bucket-layout.md](../docs/r2-bucket-layout.md). Upload `music/`, `pre-processed/`, and `post-processed/` manually before the first job.

## 2. Build and push the image

```bash
export PROJECT_ID=your-gcp-project
export REGION=northamerica-northeast2
export IMAGE=gcr.io/$PROJECT_ID/music-assembler:latest

gcloud builds submit --tag "$IMAGE" .
```

## 3. Create the Cloud Run Job

```bash
gcloud run jobs create music-assemble \
  --image "$IMAGE" \
  --region "$REGION" \
  --task-timeout 10800 \
  --memory 16Gi \
  --cpu 4 \
  --max-retries 0 \
  --set-env-vars "CLOUDFLARE_R2_BUCKET=music-assembly-data,CLOUDFLARE_R2_ENDPOINT=https://ACCOUNT.r2.cloudflarestorage.com,ASSEMBLY_CATEGORY=korean,THUMBNAIL_TEXT=OMYO,YOUTUBE_METADATA_PROVIDER=auto,ASSEMBLY_QUEUE_YOUTUBE=true,UPLOADER_API_URL=https://youtuber-uploader-app-17161979106.northamerica-northeast2.run.app" \
  --set-secrets "CLOUDFLARE_R2_ACCESS_KEY_ID=r2-access-key:latest,CLOUDFLARE_R2_SECRET_ACCESS_KEY=r2-secret-key:latest,UPLOADER_API_KEY=uploader-api-key:latest"
```

Store R2 keys in [Secret Manager](https://cloud.google.com/secret-manager) and reference them as above, or pass env vars directly for a first test.

`task-timeout` is in seconds (10800 = 3 hours) for long ffmpeg encodes.

## 4. Run once

```bash
gcloud run jobs execute music-assemble --region "$REGION" --wait
```

Output appears under `s3://your-bucket/music-video/{channel}/mv_*/`.

The control API passes per-run env overrides: ``ASSEMBLY_EXECUTION_ID``, ``ASSEMBLY_CATEGORY`` (music MP3s), optional ``ASSEMBLY_IMAGES_FOLDER`` (``post-processed/{folder}/`` backgrounds), ``ASSEMBLY_CHANNEL``, duration, and queue flags.

Service accounts for a hosted control API and optional worker identity:
[deploy/music-assembly-iam.md](music-assembly-iam.md) (`music-assembly-api`, `music-assembly-worker`).

## 5. Schedule (optional)

```bash
gcloud scheduler jobs create http music-assemble-daily \
  --location "$REGION" \
  --schedule "0 2 * * *" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/music-assemble:run" \
  --http-method POST \
  --oauth-service-account-email music-assembly-api@${PROJECT_ID}.iam.gserviceaccount.com
```

## Local smoke test (no GCP)

With R2 credentials in the environment:

```bash
# Fill in CLOUDFLARE_R2_* in .env first (see .env.example)

docker build -t music-assembler .
docker run --rm --env-file .env music-assembler
```

---

# Cloud Run Job — extend backgrounds (Gemini)

Runs ``extend-from-r2``: atomically claims pre-processed photos from R2, extends them with Gemini, uploads PNGs to ``post-processed/``, and retires sources to ``pre-processed/{category}/used/``. Parallel workers use ``pre-processed/{category}/in-flight/{execution_id}/`` so the same image is never extended twice. When nothing is claimable, the execution exits successfully.

## Build and push

```bash
export PROJECT_ID=your-gcp-project
export REGION=northamerica-northeast2
export IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/music-assembler/music-extend:latest

gcloud builds submit --config=cloudbuild.extend.yaml --substitutions="_IMAGE=$IMAGE" .
```

## Create the job

```bash
gcloud run jobs create music-extend \
  --image "$IMAGE" \
  --region "$REGION" \
  --task-timeout 3600 \
  --memory 8Gi \
  --cpu 2 \
  --max-retries 0 \
  --service-account music-assembly-worker@${PROJECT_ID}.iam.gserviceaccount.com \
  --set-env-vars "CLOUDFLARE_R2_BUCKET=your-bucket,CLOUDFLARE_R2_ENDPOINT=https://ACCOUNT.r2.cloudflarestorage.com,ASSEMBLY_CATEGORY=korean" \
  --set-secrets "CLOUDFLARE_R2_ACCESS_KEY_ID=r2-access-key:latest,CLOUDFLARE_R2_SECRET_ACCESS_KEY=r2-secret-key:latest,GEMINI_API_KEY=gemini-api-key:latest"
```

The control API passes ``EXTEND_EXECUTION_ID``, ``ASSEMBLY_CATEGORY``, and optional ``EXTEND_MAX_IMAGES`` per run. Grant ``music-assembly-api`` permission to run this job (same as ``music-assemble``).

## Run once (manual)

```bash
gcloud run jobs execute music-extend \
  --region "$REGION" \
  --update-env-vars "EXTEND_EXECUTION_ID=ext_manual_test,ASSEMBLY_CATEGORY=korean,EXTEND_MAX_IMAGES=1" \
  --wait
```
