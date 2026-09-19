# Scribe — design write-up

A model works out how to do a job in a legacy bank application once; a machine
repeats it forever. What follows is why the pieces are shaped the way they are,
including the places the design bit back.

---

## 1. Architecture

Python 3.11, Pydantic v2, Playwright, FastAPI. Pydantic because the artifact
*is* the deliverable: a schema that emits JSON Schema for free is what makes a
recording directly callable by an agent, rather than something needing a
hand-written adapter. One process, no queue — the interesting problems here are
the contracts, and scaling infrastructure would have bought nothing.

Five layers, and the boundaries carry weight:

```
 catalog        capabilities as tool schemas; approval gate
 agent          discovery loop  ─┐
 replay         executor        ─┤── both act only through policy + surface
 policy         allowlist, risk, secrets, redaction
 surface        observe / resolve / act over a normalized control graph
```

Two choices did most of the work.

**Everything above `surface/` speaks `Observation`, `UIElement` and
`ControlRef`, and has never heard of a CSS selector.** The perception layer
builds a control graph per frame: role, the accessible name a person reads,
value, enabled, frame path, bounding box, and a ranked list of ways to find the
control again. A desktop surface would fill the same shapes from UI Automation;
a pixel-only surface from OCR. Nothing above changes.

That layer earns its keep immediately. The fixture's member-id field has no
`<label>`, no `aria-label`, and an id regenerated on every render — its name is
the `<td>` to its left. **Chromium's own accessibility tree reports no name for
it at all.** We still read the AX tree, as corroboration, and `name_source`
records which evidence produced each name (`table_adjacent` here, `ax` where
the browser agreed). A system that trusted the AX tree alone could not name the
most important control on the screen.

**The model points; the recorder writes down.** The model refers to controls by
the ephemeral ref it was just shown (`e6`), never by a selector. The recorder —
which holds the same observation, including every locator candidate computed
for that element — decides how to find it again. A model inventing CSS would be
authoring locators it has no way to evaluate.

Sign-on runs deterministically from an app profile before the model is handed
the session, so it never sees a credential.

---

## 2. Artifact schema

A macro recording says "click here, then here". That is not enough to invoke
unattended. A capability additionally declares what the caller must supply,
what comes back, which failures are legitimate answers, how far it may go
without a person, and how to tell whether it actually worked.

```
Capability
  id, version, description             ← agent-facing
  app: {vendor_product, version, surface, scope, recorded_on_tenant}
  inputs:  [Param]                     ← typed, constrained, sensitivity-tagged
  outputs: [OutputField]               ← typed, sensitivity-tagged
  preconditions: [Step]                ← sign-on prologue, shared per product
  steps:   [Step]
  recovery:    [RecoveryRule]          ← known interruptions, as data
  environment: [EnvironmentSignal]     ← how this product reports its own faults
  outcomes:    [BusinessOutcome]       ← legitimate non-success, with stable codes
  checkpoint:  Condition               ← proof we arrived
  risk, approval, provenance, stats
```

Four decisions worth defending:

**Conditions are data, not code.** A small serializable predicate language
(`url_matches`, `text_present`, `control_present`, `all_of`/`any_of`/`not`)
covers checkpoints, outcome detectors, preconditions and recovery triggers.
Less expressive than a lambda, deliberately: an artifact is something a
compliance reviewer reads and a machine replays, and neither survives `eval` in
a JSON file. It also means a capability cannot smuggle behaviour past the
guardrails. Every condition renders itself in English, and
`scribe show` prints the whole artifact as a page of plain text — that is the
review artifact, not a diff of JSON.

**Business outcomes are first-class, with codes.** `MEMBER_NOT_FOUND` is what
the caller asked for. It gets a stable code so an agent branches on it without
string-matching wording that differs per institution. Conflating this with
failure is the mistake that makes UI automation untrustworthy: callers start
treating every non-success as retryable noise, and real breakage hides inside
it.

**Outcomes are discovered by probing, not guessed.** The happy path cannot tell
you what "no such member" looks like. So after recording, `--probe
member_id=999999` re-runs the fresh recording deterministically with an input
expected to fail, shows the model the screen that came back, and asks it to
name the outcome and give the shortest stable text identifying it. The detector
is grounded in a real observation.

**App-level knowledge lives with the product, not the capability.** How you
sign on, which interruptions are routine, and how this core words its own
errors are properties of MERIDIAN CORE — authored once in `apps/` and stamped
onto every capability recorded against it. A thousand artifacts each carrying
their own slightly different opinion of "session expired" is a thousand places
to fix it.

Durability is mostly decided by the recorder *not trusting the model*: values
matching a supplied input become parameters even when the model forgot the
placeholder (otherwise you get a capability welded to one member that passes
its own replay test); URLs are canonicalized to `{base_url}/…?f_mid={member_id}`;
postconditions are derived from the transition actually observed, with anything
carrying this run's input discarded — an assertion containing the member id
would pass exactly once.

---

## 3. Determinism & error handling

Replay walks recorded steps with no model in the decision loop.
`ReplayResult.used_llm` is asserted false, and a test enforces it.

**Locators** are tried in order of durability: `role_name` → `label_adjacent` →
`link_text` → `field_name` → `css`. Falling through to a structural locator
still works but is reported as a **drift signal** — the run succeeds and the
capability gets flagged for review. Ambiguity is a failure, not a coin toss:
two matching controls raise rather than picking one.

One deletion matters. The first version recorded a coordinate for every
element. A point *always* resolves — there is something at every pixel — so
having one in the ladder guaranteed the resolver could never report
`TARGET_NOT_FOUND`, and would instead click whatever had since moved under it.
It masked a real bug during development. Coordinates remain in the schema for
surfaces with genuinely nothing better, where a caller opts in explicitly.

**After every action, four checks in this order:**

1. **Business outcomes** — a declared answer, returned to the caller. First,
   because "no member found" reaching a postcondition check gets reported as a
   defect instead of an answer.
2. **Recovery rules** — a known interruption, cleared. Handled before it can be
   mistaken for breakage.
3. **Environment signals** — the app said something went wrong; classify it
   (`APP_ERROR`, `PERMISSION_DENIED`, `SESSION_LOST`) rather than reporting a
   generic timeout.
4. **Postcondition** — only now: are we where the recording said we would be?

Get that order wrong and the system either hides breakage inside "expected
outcomes" or pages someone at 2am because a member id had no match.

Three ordering bugs this shook out, all fixed:

- **A cleared interruption is not a failed attempt.** Recovery initially
  consumed the step's retry budget, so any capability with a recovery rule and
  the default `retries: 0` failed the instant one fired.
- **Clearing an obstruction is not undoing the step.** A notice often appears
  *after* the action landed — rendered by the very navigation just made.
  Re-running then repeats it, and in this domain repeating a click can mean
  submitting a transaction twice. Replay now re-runs only when it can positively
  show the step did not take: a postcondition defined and still false. No
  postcondition means no evidence, and the safe reading of no evidence is *do
  not do it again*.
- **Generic signals shadow specific ones.** This core renders every fault
  through the same "Application Error" chrome, including the privacy-hold
  refusal, so the generic signal swallowed the specific one. Fixed by
  discriminating on the error code the vendor documents (`SEC-0041`), which is
  order-independent, rather than by ordering the list carefully and hoping.

The result contract is a closed set: `success` (with typed outputs),
`business_outcome` (code, title, the app's own message), `failed` (a
`FailureClass`, the step, expected vs. observed, remediation, screenshot and
DOM snapshot), `escalated`, `rejected` (refused pre-flight — a malformed
argument costs one millisecond, not three screens of navigation).

Observed end to end: a session dying mid-flow is re-authenticated and the run
still succeeds; an unexpected modal is cleared twice and the run still
succeeds; both show up as drift signals rather than silence.

---

## 4. Heterogeneity & multi-tenant

**Other surfaces.** The seam is `Surface`: `observe` → control graph, `resolve`
→ a handle, then act. Web fills it from the DOM plus the AX tree; a desktop
surface would fill the identical shapes from UI Automation or AX API, which
expose the same role/name/value/bounds vocabulary — that is why the schema
speaks accessibility terms rather than web ones. Artifacts need no new fields:
`frame_path` generalizes to a window/pane path, and `TABLE_CELL` extraction
addresses a grid the same way in either. What a desktop surface would need is
launch/attach semantics in `AppBinding` and a coordinate fallback that actually
gets used. Not built — stubbed at the protocol, and the protocol is the claim.

**Many tenants, one product.** Re-recording per institution is the failure mode
to avoid: it multiplies maintenance by the tenant count and guarantees the
variants drift apart. So a capability is written against the *vendor product*
(`scope: "product"`) and a tenant supplies a thin overlay — control aliases,
text aliases, extra recovery rules, and a narrow per-step escape hatch. An
overlay cannot add steps or change what the capability does; anything needing
more is a signal that tenant is running a genuinely different flow and deserves
its own capability rather than an ever-growing patch file.

This is demonstrated, not asserted. The committed capability was recorded on
`northstar-cu` and replays successfully on `riverbend-fcu` — different
branding, both controls renamed (`Member ID` → `Member Number`, `Search` →
`Find Member`), the accounts grid re-columned, and an extra post-sign-on
compliance notice — through a **three-line overlay**. Two of those lines are
the real renames. The third is avoidable: the discovery run recorded the app's
whole not-found sentence as its outcome detector, and that sentence embeds each
deployment's own name for the id field. Only the prefix was ever needed.

Both kinds of mismatch showed up the same way, and usefully. An earlier
recording pinned a step postcondition to a tenant-configurable heading;
cross-tenant replay surfaced it immediately as a clean `TARGET_NOT_FOUND` with
a screenshot and the step it died on — not a silent wrong answer. That is the
drift-detection story working, and catching these before approval is precisely
what capability review is for, which is why recordings land as drafts.

Nothing was needed for the reordered columns, because reads address cells by
row text and column heading rather than by index — an index-based read would
quietly return the wrong number after a tenant upgrade, which is worse than
failing.

Drift management, given stable UIs: `drift` signals on every replay
(locator fallback, structural locator, recovery fired) plus per-capability
counters give a health signal without extra infrastructure. Those counters
separate failures the capability is answerable for — a dead locator, a failed
checkpoint — from failures of the environment, because a score that drops
when the core has a bad afternoon is not a signal about the recording. The
intended operational loop is: drift rises on one tenant → review → either
tighten the base capability or add an overlay line. Re-recording is the last
resort, not the first.

---

## 5. Escalation & handoff

**Detecting stuck** differs by phase. Discovery: the model calls `escalate`, or
repeats an action three times, or the screen stops changing after a move that
should have changed it, or policy refuses its approach twice. (`read` moves are
excluded from the stall detector — they are *supposed* to leave the screen
alone. An early version escalated on three consecutive reads, one move before
the model would have finished.) Replay: any failure in `ESCALATABLE` — a dead
locator, a failed checkpoint, an unexpected state, a lost session, a permission
refusal. Deliberately *not* escalatable: a malformed argument or a policy
denial, which are the caller's problem and should never wake anyone.

**Control transfer** is the part that needed care. The hard problem is not the
UI, it is the invariant: exactly one party may act on a live session at a time,
and both must agree on which. A `ControlToken` state machine —
`AUTOMATION → HANDOFF_PENDING → HUMAN → RESUMING → AUTOMATION` — is checked by
*both* sides before every interaction: the executor before each action, the
console before forwarding each click. Violations raise rather than race.
`HUMAN` holds a lease, so an operator who closes the tab returns the session to
`HANDOFF_PENDING` instead of wedging it.

Playwright's sync API is thread-bound, which forced a clarifying decision: the
console's HTTP handlers cannot touch the page. So ownership and authority are
separate. One thread owns the browser for the life of the run; everyone else
submits commands to a queue. Control transfer moves *who may enqueue*, not
which thread executes. While a human holds control the automation is sitting in
`wait_for_resolution` draining that queue, so operator clicks execute promptly
and there is never a moment when two parties are driving. That queue is also
the RPC seam: in a deployment the session host sits behind it as its own
service and only the transport changes.

The console is real, not a mock: it streams frames of the live page, forwards
clicks and keystrokes into it, exposes the same control graph the automation
perceives (so an operator can see what the automation can see rather than
guessing at pixels), and records every human action against the intervention.
The intervention request carries what a person needs to act without calling the
engineer who wrote the capability — capability, step, expected vs. observed,
screenshot, and a suggested next move. Routing is a seam: `InterventionSink`
has console, log, and file implementations; a ticket queue or pager is one more.

**Resume verifies rather than trusts.** On handback, the step's postcondition
is re-evaluated. Satisfied → the operator did it, continue without re-running.
Not satisfied → retry under automation. No postcondition *and* the step is
irreversible → stop and say why, because there is no way to tell whether a
posted transaction already went through and guessing either way is worse than
stopping. Escalation is bounded twice: verifying the checkpoint after an
operator says they finished cannot itself escalate (that loops straight back),
and one step may be handed back and forth at most twice before the run gives
up.

`examples/handoff_demo.py` runs the whole path: a replay gets stuck on a modal
it cannot clear, raises an intervention, an operator claims control through the
console's own HTTP API, inspects the live screen, clicks the button, hands
back — and the run re-verifies and completes with correct outputs. The only
thing stood in for is the pair of hands.

---

## 6. Safety

**One chokepoint.** Discovery and replay both call `PolicyEngine.check` before
anything reaches the surface, and neither has a path that skips it — including
actions inside recovery rules. Prompt-level instructions are not a guardrail;
they are a suggestion to a system whose job is to improvise.

**Risk is decided at act time, from what the control says on screen.** The tier
recorded in an artifact is advisory — it exists for reviewers — and is never
what the gate consults. So an artifact edited to downgrade its own risk buys
nothing. Three verdicts: allow, deny, and `require_approval`, which routes to a
human through the same escalation machinery. That is what makes the handoff
part of the safety model rather than a separate feature: a risky step does not
fail, it goes to a person.

**Secrets never become data.** A capability stores `{"kind":"secret","secret":
"core_passcode"}` — a name. The value is resolved at act time from a source the
policy declares, registered with the redactor on the way through, and never
enters an artifact, a log, or a prompt. Sign-on runs before the model gets the
session, so the transcript has no credential to leak.

**Redaction has two layers, and the second is the one that holds.** Declared
sensitivity masks PII and secrets on the way to disk. Underneath, every secret
resolved during a run is scrubbed by value from every string written anywhere.
The first layer depends on someone having labelled the field correctly; the
second does not — a passcode echoed back inside an app's own error message was
never labelled by anyone.

**Limits, stated plainly.** Screenshots cannot be scrubbed by a regex, so a
screenshot inherits the sensitivity of whatever was on screen; that is handled
by capture policy (`on_failure` by default for replay) and by treating
`evidence/` as regulated, not by pretending otherwise. Redaction patterns are
US-centric and will miss formats they were not written for. Allowlists are
per-app and coarse: they constrain where and what kind, not the *semantics* of
a value typed into a permitted field — a capability approved to open
sub-accounts is trusted with the amount its caller supplies, so parameter
constraints and approval state are doing real work there. And the approval gate
is only as good as the review; `scribe show` exists to make that review cheap
enough to actually happen.

---

## 7. Cuts

**Deliberately not built.**

- *Desktop and terminal surfaces.* Stubbed at the `Surface` protocol. Building
  a second surface was the largest thing I could have done, and the least
  informative: it would have exercised the same seam twice rather than pushing
  on the artifact schema or the error taxonomy.
- *A polished co-browsing console.* The console is deliberately plain — frames
  and forwarded input. The control-transfer model underneath it is the real
  work, and it is complete.
- *Multi-tenant plumbing.* One overlay, two tenants, a real demonstration. No
  tenant registry, no per-tenant config service.
- *Assisted LLM recovery on replay failure.* Tempting and deliberately omitted:
  a model improvising on a production path is the thing this design exists to
  remove. Escalating to a human is the honest answer.
- *Queues, workers, concurrency.* The brief says not to, and it was right to.

**Known weaknesses.**

- Outcome discovery needs one probe input per outcome. A capability with five
  legitimate non-success answers needs five, supplied by a human. Authoring
  from a negative-case suite is the obvious next step.
- Derived postconditions are heuristic. One of them picked a tenant-configurable
  heading, which cross-tenant replay caught — but the heuristic caught it after
  the fact, not before.
- Frame paths are matched by name with a loose fallback; deeply nested
  framesets that rename frames between deployments would need a more careful
  identity story.

**With more time, in order.** (1) Replay N times and score stability, gating
approval on the result — the per-capability counters and the fault
attribution behind them exist; the multi-run harness and the gate do not. (2) Treat drift
signals as a per-tenant fleet health metric, since at thousands of app
instances that is how you find the one capability quietly falling back to a
structural locator. (3) Let an operator's manual fix during a handoff be
promoted into a proposed patch to the capability, reviewed like any other
change — the human actions are already recorded, so the data is there. (4) A
second surface, desktop, to make the seam prove itself.
