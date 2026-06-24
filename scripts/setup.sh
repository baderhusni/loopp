#!/usr/bin/env bash
# One-time setup: backend venv + deps, frontend deps.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Backend: creating venv and installing deps"
cd "$ROOT/backend"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "==> Frontend: installing deps"
cd "$ROOT/frontend"
npm install

echo
echo "Setup complete."
echo "  • To use the real (subscription) agent, sign in once:  claude   (then /login)"
echo "  • Then run:  scripts/dev.sh"
