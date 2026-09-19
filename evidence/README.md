# Evidence

Every run below was produced by `scripts/demo.sh` against the live fixture app,
in one pass. Nothing here is hand-written.

Each directory contains:

| file | what it is |
|---|---|
| `run.jsonl` | every event in order — observations, model decisions, policy verdicts, actions, recoveries, escalations. Redacted on write. |
| `run.json` | the final typed result (`ReplayResult`, or the discovery summary) |
| `screens/` | per-step screenshots |
| `dom/` | full frame dumps, written only on failure |

Discovery runs also carry `capability.json` (the artifact produced) and
`capability-review.txt` (what a reviewer reads before approving).

## What to look at

**`discovery-*`** — the real LLM run. `claude-opus-5` driving the live app
through six moves: fill the member id, run the inquiry, then three reads it
anchors by caption and by row/column heading. Grep `model_move` for its
reasoning, `policy_check` for the guardrail on every action, and `probe_result`
for the `MEMBER_NOT_FOUND` outcome it labelled after being shown what a bad id
produces. The `extracted` events show the account number already redacted.

**`replay-*`** — the same capability run without a model. Among them:

- a success on a *different* member than the one it was recorded with
- `MEMBER_NOT_FOUND` returned as a business outcome, with the app's own message
- an injected core error, classified as `APP_ERROR` with a screenshot and DOM
  snapshot
- a malformed id, `REJECTED` before the browser opened
- a draft capability refused for unattended use
- the same artifact replayed against the second institution

**`handoff-*`** — the human-in-the-loop path. A run gets stuck on a modal it
cannot clear, raises an intervention, an operator takes the live session
through the console's HTTP API, clicks the button, and hands back. The run
re-verifies and completes. Grep `escalation_raised`, `human_action`,
`handoff_resume`.

All data is fabricated — invented institutions, members, and balances.
