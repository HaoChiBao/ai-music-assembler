# GitHub Actions → Cloud Run (dashboard)

CI/CD for the hosted control API / dashboard (`music-assembly-api`).

| Event | Job | Behavior |
|-------|-----|----------|
| Pull request → `main` | **Check** | Install `.[api]`, run `pytest` |
| Push → `main` | **Check** then **Deploy** | Cloud Build API image → `gcloud run deploy` → `/health` smoke check |

Workflow file: [`.github/workflows/ci-cd.yml`](../.github/workflows/ci-cd.yml).

## One-time GCP setup

Create a deploy identity (do **not** reuse `music-assembly-api` for GitHub — keep runtime and deploy separate):

```bash
export PROJECT_ID=youtube-uploader-499603
export REGION=northamerica-northeast2

gcloud iam service-accounts create github-actions-deploy \
  --project="$PROJECT_ID" \
  --display-name="GitHub Actions deploy" \
  --description="Build and deploy music-assembly-api from GitHub Actions"

# Push images via Cloud Build + Artifact Registry
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:github-actions-deploy@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.editor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:github-actions-deploy@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:github-actions-deploy@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.developer"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:github-actions-deploy@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.admin"

# Allow deploy to attach / update the runtime service account
gcloud iam service-accounts add-iam-policy-binding \
  "music-assembly-api@${PROJECT_ID}.iam.gserviceaccount.com" \
  --member="serviceAccount:github-actions-deploy@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# Cloud Build's default SA must be able to push to Artifact Registry (usually already true)
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

Create a JSON key and store it in GitHub (prefer Workload Identity Federation later):

```bash
gcloud iam service-accounts keys create github-actions-deploy-key.json \
  --iam-account="github-actions-deploy@${PROJECT_ID}.iam.gserviceaccount.com"

gh secret set GCP_SA_KEY < github-actions-deploy-key.json
rm github-actions-deploy-key.json
```

Repo → **Settings → Secrets and variables → Actions →** secret name **`GCP_SA_KEY`**.

## Behavior notes

- Existing Cloud Run env vars / secrets / identity are preserved; only the container image, `ASSEMBLY_BUILD_ID` (short commit SHA), and `ASSEMBLY_DEPLOYED_AT` are updated.
- Before Cloud Build, CI runs `scripts/write_deploy_manifest.py` so the image includes recent `main` commits. The live dashboard **Updates** tab (and `GET /v1/updates`) shows that log.
- Images are tagged `…/music-assembly-api:<sha>` and retagged `:latest`.
- Worker jobs (`music-assemble`, `music-extend`) are **not** auto-redeployed — update those manually when the encode/extend images change (see [cloud-run-job.md](cloud-run-job.md)).

## Manual fallback

See [music-assembly-api.md](music-assembly-api.md).
