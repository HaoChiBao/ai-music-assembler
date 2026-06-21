#!/usr/bin/env bash
# Create category folder placeholders on Cloudflare R2.
#
# Usage (from repo root):
#   set -a && source .env && set +a && ./scripts/r2-init-layout.sh
#   ./scripts/r2-init-layout.sh korean          # optional category arg
#
# Requires: CLOUDFLARE_R2_* in environment (uses scripts/r2_init_layout.py + boto3)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${ROOT}/.venv/bin/python" "${ROOT}/scripts/r2_init_layout.py" "${1:-}"
