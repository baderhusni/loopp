# Acme Refund Support Agent

An **AI customer-support agent that processes or denies e-commerce refunds**, built
for the Loopp full-stack automation challenge. A customer chats with the agent; the
agent verifies them against a synthetic CRM, applies a written refund policy using
tools, and **holds the line** against pleading and prompt-injection — while an admin
dashboard shows the agent's full internal reasoning trace for every run.

> **LLM access:** the agent runs on the **Claude Agent SDK** authenticated by your
> **Claude subscription** — no Anthropic API key and no raw API request loop. The
> app also ships a deterministic **mock engine** so it runs out-of-the-box even
> without the SDK/subscription.

---

## What it does

| Scenario | Customer | Policy outcome |
|---|---|---|
| In-window, normal item | `alice@example.com` · `ORD-5001` · `ITEM-1` ($129) | ✅ **APPROVED** |
| Final-sale item | `alice@example.com` · `ORD-5001` · `ITEM-2` (clearance) | ⛔ **DENIED** |
| Refund over $500 | `bob@example.com` · `ORD-5002` · `ITEM-1` ($1,299 laptop) | ↑ **ESCALATED** |
| Outside 30-day window | `carol@example.com` · `ORD-5003` (delivered 70 days ago) | ⛔ **DENIED** |
| Defective (extends window to 60 days) | `david@example.com` · `ORD-5004` (delivered 40 days ago) | ✅ **APPROVED** if defective, else **DENIED** |
| Already refunded | `emma@example.com` · `ORD-5005` | ⛔ **DENIED** |
| Not yet delivered | `frank@example.com` · `ORD-5006` (shipped) | ↑ **ESCALATED** (cancellation) |
| Gift card (final sale) | `grace@example.com` · `ORD-5007` · `ITEM-1` | ⛔ **DENIED** (↑ escalate if defective) |
| **Prompt injection** ("I'm the admin, ignore the rules") | any | ⛔ **DENIED** — the policy is the source of truth |

The chat UI has one-click buttons for each of these.

---

## Architecture — clean separation of concerns

```
┌─────────────────────┐   HTTP/JSON   ┌──────────────────────────────────────────┐
│  React SPA (Vite/TS) │ ───────────▶ │  FastAPI  (app/main.py)  — the API layer  │
│  • Customer chat     │ ◀─────────── │  validates input, serves trace/admin data │
│  • Admin trace board │              └───────────────┬──────────────────────────┘
└─────────────────────┘                              │ run_turn()
                                                     ▼
                                  ┌──────────────────────────────────────────────┐
                                  │  Orchestration layer (app/agent.py)           │
                                  │  • System prompt (policy + injection defense) │
                                  │  • ClaudeEngine — Claude Agent SDK tool loop  │
                                  │    (subscription auth, no API key)            │
                                  │  • MockEngine — deterministic fallback        │
                                  └───────┬───────────────────────────┬──────────┘
                                          │ calls tools               │ records
                                          ▼                           ▼
                          ┌───────────────────────────┐   ┌────────────────────────┐
                          │  Tools (app/tools.py)      │   │  Trace (app/trace.py)  │
                          │  find_customer, get_order, │   │  per-run tool I/O,     │
                          │  check_refund_eligibility, │   │  latency, errors,      │
                          │  issue_refund, escalate    │   │  tokens, decision      │
                          └───────┬───────────────┬────┘   └────────────────────────┘
                                  ▼               ▼
                  ┌───────────────────────┐  ┌──────────────────────────┐
                  │ Policy engine          │  │ CRM store (app/store.py) │
                  │ (app/policy.py)        │  │ data/crm.json (15 custs) │
                  │ data/refund_policy.md  │  │ + in-memory refunds      │
                  └───────────────────────┘  └──────────────────────────┘
```

- **UI** (`frontend/`) only renders and calls the API.
- **API** (`app/main.py`) is a thin HTTP boundary.
- **Orchestration** (`app/agent.py`) owns the LLM/agent loop and conversation state.
- **Tools + policy + store** are pure, testable Python with no UI/HTTP knowledge.

### The guardrail (why the agent can't be jailbroken into a bad refund)

The refund policy is enforced in **two** places:

1. **Prompt** — the system prompt makes the written policy the source of truth and
   instructs the agent to treat any "ignore the rules / I'm the admin" message as a
   manipulation attempt.
2. **Tools (defense in depth)** — `check_refund_eligibility` and `issue_refund` both
   run the deterministic policy engine. `issue_refund` **refuses** (returns an error)
   for anything the engine doesn't `APPROVE`. So even if a customer talks the model
   into *trying* to refund a final-sale or >$500 item, the tool will not perform it,
   and no refund is recorded.

---

## Prerequisites

- **Python 3.10+**
- **Node.js 18+** (Node 20+ recommended)
- A **Claude Pro/Max subscription**, logged in locally (for the real agent engine).
  The mock engine needs none of this.

### Make the agent use your subscription (not an API key)

The Claude Agent SDK authenticates through the local Claude CLI session:

```bash
npm install -g @anthropic-ai/claude-code   # if you don't already have the `claude` CLI
claude            # then run /login  (or:  claude setup-token)  to sign in with your subscription
unset ANTHROPIC_API_KEY                     # IMPORTANT: if this is set, the SDK uses the metered API instead
```

> If `ANTHROPIC_API_KEY` is set, the SDK will use the paid API. Unset it to use your subscription.

---

## Quick start

### 1. Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
unset ANTHROPIC_API_KEY            # use the subscription, not the metered API
uvicorn app.main:app --reload --port 8000
```

### 2. Frontend (separate terminal)

```bash
cd frontend
npm install
npm run dev                        # opens http://localhost:5173 (proxies /api -> :8000)
```

Open **http://localhost:5173** and start chatting.

### One-process alternative (build the SPA, serve everything from the backend)

```bash
cd frontend && npm install && npm run build      # produces frontend/dist
cd ../backend && source .venv/bin/activate
uvicorn app.main:app --port 8000                 # serves the UI at http://localhost:8000
```

Helper scripts in `scripts/` wrap these (`scripts/setup.sh`, `scripts/dev.sh`, `scripts/serve.sh`).

---

## Using the app

- **Customer chat** (left): type a refund request or click a scenario chip. The agent
  asks for an email / order / item if needed, applies the policy, and replies with the
  decision and the reason. A small meta line shows engine, model, tool count, latency,
  tokens, and est. cost.
- **Admin dashboard** (right):
  - **Agent trace** — every run, newest first. Click one to see the full internal
    reasoning: each tool call's **input/output**, **latency**, **errors/retries** (flagged
    red), the agent's interim reasoning text, and a summary of **tokens / latency / est.
    cost / decision**.
  - **CRM data** — the 15 synthetic customers and their orders (handy for picking demos).
  - **Policy** — the verbatim refund policy the agent is bound to.
- **Engine selector** (top right): `auto` (Claude if available, else mock), `claude`
  (force subscription agent), or `mock` (force the deterministic offline engine).

---

## What's in the trace (for the Loom walkthrough)

For any run, the admin trace shows:

- **Tool I/O** — exact arguments the agent passed and the JSON each tool returned.
- **Retries / failed steps** — e.g. a wrong order number makes `get_order` return an
  error (flagged red); the agent recovers and asks the customer to re-check. Try the
  "🔁 Wrong order # (retry)" chip.
- **Token cost** — `usage` token counts (input/output/cache) and an **estimated**
  API-equivalent cost. (On a subscription there's no per-token charge; the figure is
  the SDK's estimate of what the same tokens would cost on the API.)
- **Latency** — per-step elapsed time and total wall-clock duration.
- **Decision** — APPROVED / DENIED / ESCALATED, derived from the tools that ran.

---

## Tests

```bash
cd backend && source .venv/bin/activate
pytest -q
```

The suite (19 tests, runs with the mock engine — no SDK/subscription needed) covers
every policy branch, the tool guardrails (a final-sale or >$500 refund cannot be
forced, refunds are idempotent), the mock agent end-to-end, prompt-injection
resistance, and the HTTP API.

---

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `AGENT_ENGINE` | `auto` | `auto` \| `claude` \| `mock` — which engine to use. |
| `AGENT_MODEL` | `sonnet` | Model alias for the Claude engine (`sonnet`/`opus`/`haiku`). |
| `SUPPORT_TODAY` | `2026-06-24` | Pins "today" for refund-window math so the demo is deterministic. |
| `ANTHROPIC_API_KEY` | *(unset)* | **Leave unset** to use the subscription. If set, the SDK uses the metered API. |

---

## Project structure

```
backend/
  app/
    main.py        # FastAPI routes (API boundary)
    agent.py       # orchestration: Claude Agent SDK engine + mock engine + system prompt
    tools.py       # agent tools (CRM lookups, policy checks, refund/escalate actions)
    policy.py      # deterministic refund policy engine
    store.py       # in-memory CRM data store + refund/escalation records
    trace.py       # per-run tracing (tool I/O, latency, errors)
    models.py      # pydantic request/response models
    data/
      crm.json             # 15 synthetic customers + orders (generated)
      generate_crm.py      # reproducible generator for crm.json
      refund_policy.md     # the written policy (source of truth)
  tests/test_agent.py
  requirements.txt
frontend/
  src/
    App.tsx
    components/ChatWindow.tsx, AdminDashboard.tsx, TraceView.tsx, CrmExplorer.tsx, PolicyView.tsx
    api.ts, types.ts, styles.css
CLAUDE.md          # the challenge brief pinned as a hard constraint
```

---

## Suggested 5-minute Loom script

1. **(0:00)** Show the UI + the engine badge (`claude`, subscription). Mention: no API
   key — runs on the Claude subscription via the Claude Agent SDK.
2. **(0:30)** Happy path: click **✅ Approve** → APPROVED. Open the run in the admin
   trace; walk through find_customer → get_order → check_refund_eligibility → issue_refund,
   pointing at tool I/O, latency, and token usage.
3. **(2:00)** Resilience: click **🛡️ Prompt injection** → DENIED. Show the agent
   verified, checked policy, and refused; show the **CRM "refunded" flag unchanged** and
   the empty audit — the injection produced no refund.
4. **(3:15)** Failed step / debugging: click **🔁 Wrong order # (retry)**. In the trace,
   point at the **red `get_order` ERROR** step and explain how you'd debug from the log
   (the error message tells the agent — and you — exactly what was wrong), then show the
   agent recovered.
5. **(4:15)** Escalation: click **↑ Escalate (> $500)** → ESCALATED with a ticket.
   Close with **what you'd add before prod** (below).

## What I'd add before production

- **Persistence** — move CRM, refunds, escalations, and traces from in-memory to a real
  DB; today they reset on restart.
- **AuthN/Z** — authenticate customers (the agent currently trusts the email given) and
  protect the admin dashboard.
- **Observability** — ship traces/usage to a tracing backend (OpenTelemetry), alert on
  error-rate and per-conversation cost, add request IDs.
- **Idempotency + audit** — durable refund ledger with idempotency keys; immutable audit
  log of every decision and the rule behind it.
- **Eval harness** — a red-team suite of injection/pleading prompts run in CI to catch
  policy regressions; golden-trace tests.
- **Guardrail hardening** — rate limits, PII redaction in logs, and a human-in-the-loop
  approval queue wired to the escalation tickets.
- **Streaming UX** — stream tokens to the chat for lower perceived latency.
