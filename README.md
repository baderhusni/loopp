# Scribe

**A model works out how to do a job in a legacy bank application, once. After
that, a machine does it — deterministically, cheaply, and with a way to call a
human when it gets stuck.**

Built for the interface.ai computer-use take-home. The design write-up is in
[`REPORT.md`](REPORT.md); recorded runs are in [`evidence/`](evidence/).

```
     discovery (once)                      replay (every time after)
  ┌──────────────────────┐              ┌────────────────────────────┐
  │ goal + live app      │              │ capability + typed inputs  │
  │   ↓                  │              │   ↓                        │
  │ observe → LLM → act  │   ────────▶  │ observe → step → verify    │
  │   ↓                  │  capability  │   ↓                        │
  │ typed capability     │   artifact   │ outputs | outcome | error  │
  └──────────────────────┘              └────────────────────────────┘
        model in the loop                     no model in the loop
                                              ↓ when stuck
                                        human takes the live session
```

---

## What's here

A complete vertical slice, running against a deliberately hostile stand-in for
a credit-union core banking console:

| | |
|---|---|
| **Target app** | `target_app/` — "MERIDIAN CORE 4.2", server-rendered, framesets, table layout, no test IDs, element ids regenerated every render, real sessions, injectable runtime faults. Two institutions run the same build with different branding and wording. |
| **Perception** | A normalized control graph per frame, with an accessible-name algorithm that recovers names legacy markup does not declare. |
| **Discovery** | An `observe → decide → act` loop on `claude-opus-5`, policy-gated, which records what worked. |
| **Artifact** | A typed, versioned capability: steps, ranked locators, typed inputs and outputs, declared business outcomes, recovery rules, a checkpoint, risk, provenance, approval state. |
| **Replay** | An LLM-free executor with an explicit error taxonomy: business outcome vs. recoverable condition vs. hard failure. |
| **Escalation** | Stuck detection, an intervention request carrying context, and a real operator console that drives the *same live session*, then hands it back. |
| **Safety** | An allowlist and risk gate every action passes through, secrets resolved at act time and never persisted, redaction on everything written to disk. |
| **Catalog** | Capabilities exposed as tool schemas an agent calls by name with typed args. |

---

## Setup

Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium          # skip if a Chromium is already provisioned
```

Two environment variables, both for the fixture's sign-on. Real deployments
point these at a secret manager; the policy file declares which names a
capability may resolve, and nothing else is reachable.

```bash
export MERIDIAN_OPERATOR_ID=svc_automation
export MERIDIAN_PASSCODE='Tr0ubadour!'
```

**Model access** — the discovery run needs a model. Either works:

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # backend: api  (the default when set)
# or nothing at all, if the `claude` CLI is signed in  → backend: cli
```

`--backend auto` prefers the API key and falls back to the CLI. The committed
evidence was produced through the CLI backend, on `claude-opus-5`.

| Variable | Purpose |
|---|---|
| `SCRIBE_CHROMIUM_PATH` | Use a preinstalled Chromium instead of Playwright's. Auto-detects `/opt/pw-browsers/chromium`. |
| `SCRIBE_HEADLESS` | `false` to watch the browser work. |
| `SCRIBE_MODEL` | Defaults to `claude-opus-5`. |

**Running without model access:** everything except `scribe discover` is
LLM-free. The recorded capability is committed, so replay, the error paths, the
human handoff, and cross-tenant reuse all run with no key:
`scripts/demo.sh replay`.

---

## Demo path

Start the fixture — two institutions on the same vendor build:

```bash
python -m target_app --port 8799 --tenant northstar-cu   &
python -m target_app --port 8800 --tenant riverbend-fcu  &
```

**1. Discovery.** A model drives the live app and records what worked:

```bash
scribe discover \
  --goal "Look up member 100412 in the member inquiry screen and return their name, their savings share balance, and the savings account number." \
  --base-url http://127.0.0.1:8799 \
  --id meridian_core.member_savings_balance \
  --name "Read a member's savings balance" \
  --param member_id=100412 \
  --probe member_id=999999 \
  --tenant northstar-cu
```

`--probe` is the interesting flag. After the happy path is recorded, the flow is
re-run deterministically with an id that does not exist, and the model is shown
the resulting screen and asked to label it. That is how `MEMBER_NOT_FOUND`
becomes a declared outcome grounded in a real observation rather than a guess.

**2. Review and approve.** Discovery produces a *draft*. Drafts do not run
unattended:

```bash
scribe show capabilities/meridian_core/member_savings_balance.json
scribe approve capabilities/meridian_core/member_savings_balance.json --by "your.name"
```

**3. Replay** — no model in the decision loop, a different member than the one
it was recorded with:

```bash
scribe replay capabilities/meridian_core/member_savings_balance.json \
  --base-url http://127.0.0.1:8799 --param member_id=100413
```

**4. The paths that matter.** A business outcome, a runtime failure, and a bad
argument are three different answers:

```bash
# "no such member" -- an answer, not a crash
scribe replay <cap> --base-url http://127.0.0.1:8799 --param member_id=999999

# the core throws an error mid-flow
curl -X POST http://127.0.0.1:8799/__fault -H 'Content-Type: application/json' \
     -d '{"kind":"app_error","count":1}'
scribe replay <cap> --base-url http://127.0.0.1:8799 --param member_id=100412

# a malformed id -- rejected against the contract, the browser never opens
scribe replay <cap> --base-url http://127.0.0.1:8799 --param member_id=oops
```

Other faults: `session_expired`, `interstitial`, `permission_denied`, `slow`.
The first two are *recovered from* — the run still succeeds and records that it
had to.

**5. Human handoff** — a run that cannot continue, an operator who takes the
live session, and a resume:

```bash
python examples/handoff_demo.py --base-url http://127.0.0.1:8799
```

Or drive it yourself: add `--console` to any `scribe replay`, and open
<http://127.0.0.1:8900/> when it escalates.

**6. Cross-tenant reuse** — the *same artifact*, a second institution, via a
three-line overlay:

```bash
scribe replay <cap> --tenant riverbend-fcu --param member_id=100416
```

**7. Agent invocation** — the catalog as tool schemas, and a model calling one:

```bash
scribe catalog --as-tools
python examples/agent_invokes_capability.py --base-url http://127.0.0.1:8799
```

**Everything at once**, writing to `evidence/`:

```bash
scripts/demo.sh            # includes discovery (needs model access)
scripts/demo.sh replay     # skips discovery, uses the committed capability
```

---

## Commands

| | |
|---|---|
| `scribe discover` | Record a capability with a model in the loop. |
| `scribe replay` | Run one deterministically. `--tenant`, `--console`, `--allow-draft`, `--json`. |
| `scribe show` | The reviewable summary and the call contract. |
| `scribe approve` | Promote a draft for unattended use. |
| `scribe catalog` | List capabilities, or `--as-tools` for agent tool schemas. |
| `scribe invoke <id>` | Call by id, the way an agent would. |
| `scribe console` | The operator console, standalone. |

---

## Layout

```
src/scribe/
  types/        artifact, conditions, actions, observations, results  ← the contracts
  surface/      Surface protocol + Playwright web surface             ← the seam
  agent/        discovery loop, LLM backends, artifact recorder
  replay/       deterministic executor, condition evaluator, extraction
  policy/       allowlist + risk gate, redaction
  escalation/   control token, intervention broker, session host, console
  apps/         per-vendor-product knowledge (sign-on, recovery, error signals)
  catalog/      capability store and agent-facing tool schemas
target_app/     the legacy fixture, two tenants, injectable faults
capabilities/   recorded artifacts + per-tenant overlays
evidence/       committed runs: discovery, replays, failures, handoff
```

---

## Tests

```bash
pytest                      # everything
pytest -m "not browser"     # fast: contracts, policy, redaction, recorder, escalation
```

The browser-marked tests drive a real Chromium against the fixture. They cover
the claims the design rests on: that names are recovered from legacy markup,
that replay never calls a model, that a not-found is an outcome and a dead
locator is a failure, that a dying session is re-authenticated mid-flow, and
that an unclaimed escalation ends the run instead of hanging.

---

## Notes

The fixture is fabricated: invented institutions, invented members, invented
balances. No real financial data, no real credentials, and nothing here talks
to a real banking system.

`legacy/refund-agent/` is an unrelated earlier project that shares this
repository. It is not part of this submission.
