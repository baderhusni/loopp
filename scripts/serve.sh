#!/usr/bin/env bash
# Single-process mode: build the SPA and serve the whole app from FastAPI.
# Open http://localhost:8000
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

unset ANTHROPIC_API_KEY  # use the Claude subscription, not the metered API

echo "==> Building frontend"
cd "$ROOT/frontend"
npm run build

echo "==> Serving app on http://localhost:8000"
cd "$ROOT/backend"
# shellcheck disable=SC1091
source .venv/bin/activate
uvicorn app.main:app --port 8000
