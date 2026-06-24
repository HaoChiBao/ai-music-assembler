# Music Assembly — GCP service accounts

Naming is **music assembly**, not youtube-uploader. Your GCP **project id** may still be
`youtube-uploader-499603`; only the service account ids below are assembly-specific.

## Accounts

| Account id | Email | Used by |
|------------|-------|---------|
| `music-assembly-api` | `music-assembly-api@PROJECT_ID.iam.gserviceaccount.com` | Hosted **control API** (trigger jobs, list videos, dashboard) |
| `music-assembly-worker` | `music-assembly-worker@PROJECT_ID.iam.gserviceaccount.com` | Optional: Cloud Run **Job** `music-assemble` runtime identity |

The default compute service account works for the job today; use `music-assembly-worker` when
you want a dedicated worker identity.

---

## 1. Create the control API service account

```bash
export PROJECT_ID=youtube-uploader-499603
export REGION=northamerica-northeast2

gcloud iam service-accounts create music-assembly-api \
  --project="$PROJECT_ID" \
  --display-name="Music Assembly Control API" \
  --description="Triggers and monitors music-assemble Cloud Run Jobs; reads R2 catalog."
```

---

## 2. Let the API start and observe assembly jobs

```bash
# Run (start) the assembly job
gcloud run jobs add-iam-policy-binding music-assemble \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="serviceAccount:music-assembly-api@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.developer"

# Same for the extend job
gcloud run jobs add-iam-policy-binding music-extend \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="serviceAccount:music-assembly-api@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.developer"

# Optional: read execution status without run.developer (narrower)
# roles/run.viewer on the job + roles/run.developer only on :run
```

`roles/run.developer` includes `run.jobs.run`, `run.executions.get`, and list operations
needed for a dashboard.

---

## 3. Optional: dedicated worker account for the Job container

```bash
gcloud iam service-accounts create music-assembly-worker \
  --project="$PROJECT_ID" \
  --display-name="Music Assembly Worker" \
  --description="Runtime identity for music-assemble Cloud Run Job (encode + R2 sync)."

gcloud run jobs update music-assemble \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --service-account="music-assembly-worker@${PROJECT_ID}.iam.gserviceaccount.com"
```

The worker does **not** need `run.jobs.run`; it only needs R2 credentials in env vars /
Secret Manager.

---

## 4. Cloud Scheduler (daily assemble)

Point the scheduler at the **worker trigger**, authenticated as the API account (or a
scheduler-only account):

```bash
gcloud scheduler jobs create http music-assemble-daily \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --schedule "0 2 * * *" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/music-assemble:run" \
  --http-method POST \
  --oauth-service-account-email="music-assembly-api@${PROJECT_ID}.iam.gserviceaccount.com"
```

---

## 5. Deploy the control API (future)

When `assembly-api` exists as a Cloud Run **Service**, attach the same identity:

```bash
gcloud run deploy music-assembly-api \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --service-account="music-assembly-api@${PROJECT_ID}.iam.gserviceaccount.com" \
  ...
```

Env vars for that service:

- `ASSEMBLY_GCP_PROJECT` = project id  
- `ASSEMBLY_GCP_REGION` = `northamerica-northeast2`  
- `ASSEMBLY_JOB_NAME` = `music-assemble`  
- `CLOUDFLARE_R2_*` — list `music-video/` and inventory  
- `ASSEMBLY_API_KEY` — your dashboard/API auth (not a Google key)

---

## Quick reference

```text
music-assembly-api@youtube-uploader-499603.iam.gserviceaccount.com
  → hosted server that calls Run Jobs API

music-assembly-worker@youtube-uploader-499603.iam.gserviceaccount.com
  → optional identity inside the encode container
```
