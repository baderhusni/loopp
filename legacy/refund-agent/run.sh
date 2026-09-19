#!/usr/bin/env bash
# One command to run the whole app locally.
#
#   ./run.sh
#
# Sets up the backend (venv + deps) and frontend (deps + build) on first run,
# then serves the entire app — API + UI — from a single process at
# http://localhost:8000. Re-running is fast (it skips setup once done).
#
# Uses your Claude subscription (no API key). Pick "mock" in the engine dropdown
# if you want to run it without a subscription.
set -euo pipefail
cd "$(dirname "$0")"
unset ANTHROPIC_API_KEY   # use the Claude subscription, not the metered API

echo "==> Backend: virtualenv + dependencies"
[ -d backend/.venv ] || python3 -m venv backend/.venv
# shellcheck disable=SC1091
source backend/.venv/bin/activate
if ! python -c "import fastapi, uvicorn, claude_agent_sdk" 2>/dev/null; then
  pip install --quiet --upgrade pip
  pip install --quiet -r backend/requirements.txt
fi

echo "==> Frontend: dependencies + build"
( cd frontend && { [ -d node_modules ] || npm install; } && npm run build )

echo
echo "==> Ready. Open  http://localhost:8000   (Ctrl+C to stop)"
echo
cd backend
exec uvicorn app.main:app --host 127.0.0.1 --port 8000
