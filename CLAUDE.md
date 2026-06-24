# CLAUDE.md — Project Constitution (HARD RULES)

> This file is the **binding specification** for this repository. The Loopp
> "AI Agent" Full-Stack Automation Challenge brief below is a **HARD RULE AND
> CONSTRAINT**. Do **NOT** build anything outside it. Do **NOT** miss anything
> inside it. When in doubt, re-read this file and conform to it. Scope creep is
> a failure; an omitted requirement is a failure.

---

## 0. The Challenge (verbatim requirements — must all be satisfied)

Build a fully functional web application: **An AI Customer Support Agent that
processes or denies e-commerce refunds.** Three key components:

1. **Synthetic Data Storage**
   - A mock CRM database: **15 customer profiles and order histories**.
   - A corporate **"Refund Policy" text document** with strict rules, e.g.:
     - final sale items cannot be refunded,
     - refunds over **$500 require human escalation**.

2. **The Backend & Agent Layer**
   - A **local API server** (FastAPI, Express, etc.) hosting an **agent loop**
     (LangGraph, CrewAI, or raw function calling).
   - The agent must **dynamically call tools** to query the synthetic database
     and **validate user requests against the policy**.
   - Customers will plead, argue, or try to talk the agent into breaking the
     rules — **the written policy is the source of truth and the agent must
     hold the line.**

3. **The Frontend UI**
   - A clean interface (React / SPA Framework, or Streamlit) with:
     - a **customer chat window** to test the agent, AND
     - an **admin dashboard** displaying the agent's **internal reasoning logs**.

### Deliverable requirements ("Finished")
- **Loom video walkthrough (≤ 5 min)** that shows: the live UI, a successful run
  of the agent loop, and a walkthrough of the trace for one run — **including a
  step that failed or retried and how you'd debug it from the logs.** Call out
  what's in the trace: **tool I/O, retries, token cost, latency**, and **what
  you'd add before prod.**
- **Live URL: OPTIONAL** (nice bonus, not required). We are NOT deploying.

### What is being evaluated
- **Product Completeness:** works out of the box with **zero configuration
  errors**.
- **Agent Resilience:** handles edge cases, policy violations, and aggressive
  **prompt injection** trying to force an unauthorized refund.
- **System Architecture:** clean **separation of concerns** between UI, API, and
  LLM orchestration layer.

---

## 1. Locked decisions (agreed with the user)

| Area | Decision | Rationale |
|---|---|---|
| LLM access | **Claude subscription via the Claude Agent SDK** — NO Anthropic API key, NO raw API request loop. | User constraint: "no API loop, we use a subscription." |
| Orchestration | Claude Agent SDK tool-calling agent loop. | Satisfies the brief's "agent loop … (raw) function calling" while honoring the subscription constraint. Documented deviation from the literal LangGraph/CrewAI list. |
| Backend | **FastAPI (Python)** | From the brief's "FastAPI, Express, etc." |
| Frontend | **React (Vite + TypeScript) SPA** | From the brief's "React / SPA Framework, or Streamlit." |
| Run mode | **Local only** for the Loom demo. | Live URL is optional; subscription auth can't be safely hosted. |
| Guardrails | Policy enforced **in the prompt AND deterministically inside the tools** (defense in depth). | "Hold the line" / resilience to prompt injection — the tool refuses unauthorized refunds even if the model is jailbroken. |

---

## 2. Scope fence (do NOT do these)

- ❌ Do **not** add features not in §0 (no auth/login system, no real payment
  processing, no email sending, no analytics product, no multi-tenant, etc.).
- ❌ Do **not** integrate Supabase, Notion, Apollo, or any external MCP server.
  They exist in the environment but are **irrelevant** to this challenge.
- ❌ Do **not** require an Anthropic API key or a raw `/v1/messages` loop.
- ❌ Do **not** deploy to a cloud host or wire up a live URL.
- ❌ Do **not** invent extra data schemas/rules that aren't needed to exercise
  the policy.
- ✅ Keep it **out-of-the-box runnable** with minimal, clearly-documented setup.

---

## 3. Mapping requirements → implementation (the checklist)

- [x] CRM: 15 customers + order histories — `backend/app/data/crm.json` (generated, reproducible).
- [x] Refund policy doc — `backend/app/data/refund_policy.md` (source of truth, strict rules incl. final-sale + >$500 escalation).
- [x] FastAPI server — `backend/app/main.py`.
- [x] Agent loop (Claude Agent SDK) dynamically calling tools — `backend/app/agent.py`.
- [x] Tools query the CRM + validate against policy — `backend/app/tools.py` (with deterministic policy engine).
- [x] Agent holds the line vs pleading / prompt injection — system prompt + tool-level enforcement (verified live).
- [x] React chat window — `frontend/src/components/ChatWindow.tsx`.
- [x] React admin dashboard showing internal reasoning logs / trace — `frontend/src/components/AdminDashboard.tsx`.
- [x] Trace captures: tool I/O, retries, token usage, latency, decision — per-run trace surfaced via API + UI.
- [x] README: zero-config-error run instructions + how to record the Loom.

---

## 4. Loom support requirements (the trace MUST make these demonstrable)

The trace shown in the admin dashboard must let the presenter point at, for one
run: each **tool call input/output**, any **failed or retried step** and how to
debug it, **token counts**, **latency per step and total**, and the final
**decision + the policy rule behind it**. Build the trace so a "denied / final
sale" or "escalate / >$500" run naturally shows a tool refusing an unauthorized
action (the failed-step story).

> Token COST note: on a subscription there is no per-token dollar charge. Surface
> token **counts** + latency + the SDK's reported cost field, and state in the
> Loom that billing is subscription-based.

---

## 5. Definition of done

1. `README.md` steps run the app locally with **zero configuration errors**.
2. Chat window processes/denies/escalates refunds correctly across the edge cases.
3. Aggressive pleading and prompt-injection attempts do **not** yield an
   unauthorized refund.
4. Admin dashboard shows the full internal trace for every run.
5. Trace contains a demonstrable failed/retried step.
6. Clean separation: `frontend/` (UI) ⟂ FastAPI routes (API) ⟂ `agent.py`/`tools.py` (LLM orchestration).
7. Nothing outside §0 was built; nothing inside §0 was missed.
