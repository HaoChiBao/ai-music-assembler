# Deploy Music Assembly API (control plane)

Lightweight Cloud Run **Service** that triggers the **Job** `music-assemble` and serves the dashboard.

## Prerequisites

- Service account `music-assembly-api@PROJECT.iam.gserviceaccount.com` with `roles/run.developer` on the job
- R2 credentials in env (same as assembly worker)
- Optional: `ASSEMBLY_API_KEY` for dashboard/API auth

See [music-assembly-iam.md](music-assembly-iam.md).

## Build and deploy

```bash
export PROJECT_ID=youtube-uploader-499603
export REGION=northamerica-northeast2
export IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/music-assembler/music-assembly-api:latest

gcloud builds submit --config=cloudbuild.api.yaml --substitutions=_IMAGE="$IMAGE" .

gcloud run deploy music-assembly-api \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --service-account music-assembly-api@${PROJECT_ID}.iam.gserviceaccount.com \
  --set-env-vars "\
ASSEMBLY_GCP_PROJECT=${PROJECT_ID},\
ASSEMBLY_GCP_REGION=${REGION},\
ASSEMBLY_JOB_NAME=music-assemble,\
EXTEND_JOB_NAME=music-extend,\
ASSEMBLY_CATEGORY=korean,\
ASSEMBLY_API_KEY=your-secret-key"
```

Add R2 vars (`CLOUDFLARE_R2_*`) via `--set-env-vars` or Secret Manager. Gemini keys live on the **`music-extend`** job (not required on the API service).

Extend runs on the **`music-extend`** Cloud Run Job (see [cloud-run-job.md](cloud-run-job.md)). The API only queues executions and tracks R2 progress. Set `EXTEND_USE_GCP=false` only for in-process extend on the API machine.

## Local dev

```bash
pip install ".[api]"
# In .env: set ASSEMBLY_GCP_SA_* fields (see .env.example) instead of a JSON key file
assembly-api
```

Open http://127.0.0.1:8080/

## API overview

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web dashboard |
| GET | `/health` | Liveness |
| GET | `/v1/capabilities` | Config summary |
| GET | `/v1/dashboard` | Jobs + videos + inventory |
| POST | `/v1/assembly/jobs` | Start `music-assemble` |
| GET | `/v1/assembly/jobs` | List GCP executions |
| GET | `/v1/dashboard/snapshot` | Job poll (`?light=1` = jobs only) |
| GET | `/v1/dashboard/stats` | Cached inventory + extend pending |
| GET | `/v1/videos?summary=1` | Video list metadata only (no title reads) |
| GET | `/v1/videos/{id}` | Full title, description, tracklist, file list |
| GET | `/v1/assets?pool=pre-processed` | Image filenames only (lazy load bytes) |
| GET | `/v1/media/video` | MP4 stream with Range (in-browser preview) |
| GET | `/v1/media/asset` | Single pre/post-processed image |
| GET | `/v1/media/thumbnail` | Stable thumbnail proxy (no presigned URL churn) |
| GET | `/v1/observability` | Server cache hit/miss stats |
| GET | `/v1/extend/pending` | Count pending pre-processed images |
| POST | `/v1/extend/jobs` | Start background extend (Gemini) |
| GET | `/v1/jobs/{id}/cancel` | Preview cancel (two-step) |
| POST | `/v1/jobs/{id}/cancel` | Cancel job (`{"confirm": true}`) |
| GET | `/v1/extend/runs` | List extend runs on R2 |
| GET | `/v1/extend/jobs/{id}/progress` | Extend progress |
| GET | `/v1/videos` | List outputs on R2 |
| GET | `/v1/categories/{cat}/inventory` | Asset counts |

Auth: `X-API-Key` for programmatic `/v1/*` calls. Browser dashboard uses `ASSEMBLY_DASHBOARD_PASSWORD` → httpOnly session cookie (no API key in the UI).

Progress: worker writes `jobs/{ASSEMBLY_EXECUTION_ID}/progress.json` on R2 during encode.
