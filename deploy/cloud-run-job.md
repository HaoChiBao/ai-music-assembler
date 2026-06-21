# Cloud Run Job — assemble one video

Runs the same pipeline as local `make-short-music-video`: sync MP3s + backgrounds from R2, encode, sync the `mv_*` folder back.

## 1. Bucket layout

See [docs/r2-bucket-layout.md](../docs/r2-bucket-layout.md). Upload `music/` and `post-processed/` manually before the first job.

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
  --set-env-vars "CLOUDFLARE_R2_BUCKET=your-bucket,CLOUDFLARE_R2_ENDPOINT=https://ACCOUNT.r2.cloudflarestorage.com,ASSEMBLY_CATEGORY=korean,THUMBNAIL_TEXT=OMYO" \
  --set-secrets "CLOUDFLARE_R2_ACCESS_KEY_ID=r2-access-key:latest,CLOUDFLARE_R2_SECRET_ACCESS_KEY=r2-secret-key:latest"
```

Store R2 keys in [Secret Manager](https://cloud.google.com/secret-manager) and reference them as above, or pass env vars directly for a first test.

`task-timeout` is in seconds (10800 = 3 hours) for long ffmpeg encodes.

## 4. Run once

```bash
gcloud run jobs execute music-assemble --region "$REGION" --wait
```

Output appears under `s3://your-bucket/music-video/mv_*/`.

## 5. Schedule (optional)

```bash
gcloud scheduler jobs create http music-assemble-daily \
  --location "$REGION" \
  --schedule "0 2 * * *" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/music-assemble:run" \
  --http-method POST \
  --oauth-service-account-email YOUR_SA@${PROJECT_ID}.iam.gserviceaccount.com
```

## Local smoke test (no GCP)

With R2 credentials in the environment:

```bash
# Fill in CLOUDFLARE_R2_* in .env first (see .env.example)

docker build -t music-assembler .
docker run --rm --env-file .env music-assembler
```
