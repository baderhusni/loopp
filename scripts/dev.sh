#!/usr/bin/env bash
# Dev mode: run the FastAPI backend (:8000) and the Vite frontend (:5173) together.
# Open http://localhost:5173
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

unset ANTHROPIC_API_KEY  # use the Claude subscription, not the metered API

echo "==> Starting backend on :8000"
cd "$ROOT/backend"
# shellcheck disable=SC1091
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000 &
BACKEND_PID=$!

cleanup() { echo; echo "Stopping backend ($BACKEND_PID)"; kill "$BACKEND_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "==> Starting frontend on :5173  (open http://localhost:5173)"
cd "$ROOT/frontend"
npm run dev
