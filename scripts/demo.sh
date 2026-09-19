#!/usr/bin/env bash
# End-to-end demo: discover once, then replay many ways.
#
# Produces everything under /evidence: a real LLM discovery run, a deterministic
# replay, a business outcome, an injected runtime failure, a human handoff on a
# live session, and the same artifact replayed against a second institution.
#
#   scripts/demo.sh            # everything (needs model access for step 1)
#   scripts/demo.sh replay     # skip discovery, use the committed capability
#
set -uo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-.venv/bin/python}
export PYTHONPATH=src
export MERIDIAN_OPERATOR_ID=${MERIDIAN_OPERATOR_ID:-svc_automation}
export MERIDIAN_PASSCODE=${MERIDIAN_PASSCODE:-Tr0ubadour!}

NORTHSTAR=http://127.0.0.1:8799
RIVERBEND=http://127.0.0.1:8800
CAP=capabilities/meridian_core/member_savings_balance.json
MODE=${1:-all}

step() { printf '\n\033[1m=== %s\033[0m\n' "$*"; }
fault() { curl -sS -X POST "$1/__fault" -H 'Content-Type: application/json' \
                 -d "$2" >/dev/null; }

# --- fixture apps: the same vendor product, two institutions ----------------
step "starting MERIDIAN CORE fixtures (northstar :8799, riverbend :8800)"
PIDS=()
for spec in "8799 northstar-cu" "8800 riverbend-fcu"; do
  set -- $spec
  if curl -sf "http://127.0.0.1:$1/__health" >/dev/null 2>&1; then
    echo "  :$1 already running"
  else
    PYTHONPATH=. $PY -m target_app --port "$1" --tenant "$2" >/dev/null 2>&1 &
    PIDS+=($!)
    echo "  :$1 $2 (pid ${PIDS[-1]})"
  fi
done
trap '[[ ${#PIDS[@]} -gt 0 ]] && kill "${PIDS[@]}" 2>/dev/null' EXIT
for _ in $(seq 1 25); do
  curl -sf "$NORTHSTAR/__health" >/dev/null 2>&1 && break || sleep 0.4
done

# --- 1. discovery: a model works the flow out for the first time ------------
if [[ "$MODE" != "replay" ]]; then
  step "1/8  DISCOVERY -- an LLM drives the live app and records a capability"
  $PY -m scribe discover \
    --goal "Look up member 100412 in the member inquiry screen and return their name, their savings share balance, and the savings account number." \
    --base-url "$NORTHSTAR" \
    --id meridian_core.member_savings_balance \
    --name "Read a member's savings balance" \
    --param member_id=100412 \
    --probe member_id=999999 \
    --tenant northstar-cu \
    --max-steps 14 --claim-timeout 20 || echo "  (discovery failed; keeping the committed capability)"
fi

step "2/8  REVIEW -- what a human approves, in plain text"
$PY -m scribe show "$CAP"

step "3/8  GUARDRAIL -- a draft capability will not run unattended"
$PY -m scribe replay "$CAP" --base-url "$NORTHSTAR" --param member_id=100413 \
  --no-escalation || true

step "4/8  APPROVE -- a reviewer promotes it"
$PY -m scribe approve "$CAP" --by "j.rivera (ops review)" \
  --note "read-only; output sensitivity confirmed; MEMBER_NOT_FOUND detector verified"

step "5/8  REPLAY -- deterministic, different member, no model in the loop"
$PY -m scribe replay "$CAP" --base-url "$NORTHSTAR" --param member_id=100413 \
  --record-stats

step "6/8  BUSINESS OUTCOME -- 'no such member' is an answer, not a crash"
$PY -m scribe replay "$CAP" --base-url "$NORTHSTAR" --param member_id=999999 \
  --record-stats

step "6b/8 RUNTIME FAILURE -- the core throws an error mid-flow"
fault "$NORTHSTAR" '{"kind":"app_error","count":1}'
$PY -m scribe replay "$CAP" --base-url "$NORTHSTAR" --param member_id=100412 \
  --no-escalation --record-stats || true
fault "$NORTHSTAR" '{"kind":"clear"}'

step "6c/8 BAD INPUT -- rejected against the declared contract, browser never opens"
$PY -m scribe replay "$CAP" --base-url "$NORTHSTAR" --param member_id=oops \
  --no-escalation || true

step "7/8  HUMAN HANDOFF -- stuck run, operator takes the live session, resumes"
$PY examples/handoff_demo.py --base-url "$NORTHSTAR"

step "8/8  CROSS-TENANT -- same artifact, second institution, 3-line overlay"
$PY -m scribe replay "$CAP" --tenant riverbend-fcu --param member_id=100416 \
  --no-escalation

step "catalog -- capabilities as tool schemas an agent can call"
$PY -m scribe catalog
$PY -m scribe catalog --as-tools

printf '\n\033[1mdone. evidence is under ./evidence\033[0m\n'
