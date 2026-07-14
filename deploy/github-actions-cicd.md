# GitHub Actions → Cloud Run (dashboard)

CI/CD for the hosted control API / dashboard (`music-assembly-api`).

| Event | Job | Behavior |
|-------|-----|----------|
| Pull request → `main` | **Check** | Install API test surface, run `pytest` |
| Push → `main` | **Check** then **Deploy** | Cloud Build API image → `gcloud run deploy` → `/health` smoke check |

Workflow file: [`.github/workflows/ci-cd.yml`](../.github/workflows/ci-cd.yml).

## Auth (Workload Identity Federation)

Deploy uses the same GCP WIF pool as **youtube-uploader** — no JSON key secret.

| GitHub secret | Value |
|---------------|-------|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/17161979106/locations/global/workloadIdentityPools/github/providers/github` |
| `GCP_SERVICE_ACCOUNT` | `github-deploy@youtube-uploader-499603.iam.gserviceaccount.com` |

The OIDC provider attribute condition allows:

- `HaoChiBao/youtube-uploader`
- `HaoChiBao/ai-music-assembler`

Re-apply secrets (from a machine with `gcloud` + `gh` admin):

```bash
export PROJECT_ID=youtube-uploader-499603
export REPO=HaoChiBao/ai-music-assembler
PROVIDER="$(gcloud iam workload-identity-pools providers describe github \
  --project="$PROJECT_ID" --location=global --workload-identity-pool=github \
  --format='value(name)')"
gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$REPO" --body "$PROVIDER"
gh secret set GCP_SERVICE_ACCOUNT --repo "$REPO" \
  --body "github-deploy@${PROJECT_ID}.iam.gserviceaccount.com"
```

## One-time GCP notes

Deploy SA: `github-deploy@youtube-uploader-499603.iam.gserviceaccount.com`

Needed roles (already granted for both repos when WIF was wired):

- `roles/artifactregistry.writer`
- `roles/run.admin`
- `roles/cloudbuild.builds.editor`
- `roles/storage.admin` (Cloud Build source upload)
- `roles/iam.serviceAccountUser` on `music-assembly-api@…` (and the uploader runtime SA)

## Behavior notes

- Existing Cloud Run env vars / secrets / identity are preserved; only the container image, `ASSEMBLY_BUILD_ID` (short commit SHA), and `ASSEMBLY_DEPLOYED_AT` are updated.
- Before Cloud Build, CI runs `scripts/write_deploy_manifest.py` so the image includes recent `main` commits. The live dashboard **Updates** tab (and `GET /v1/updates`) shows that log.
- The Artifact Registry image path is built inside the deploy step (job-level `env` cannot reference `${{ env.* }}`).
- Images are tagged `…/music-assembly-api:<sha>` and retagged `:latest`.
- Worker jobs (`music-assemble`, `music-extend`) are **not** auto-redeployed — update those manually when the encode/extend images change (see [cloud-run-job.md](cloud-run-job.md)).

## Manual fallback

See [music-assembly-api.md](music-assembly-api.md).
