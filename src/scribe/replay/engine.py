"""Deterministic replay: the production execution path.

No model is consulted here. Given an artifact and a set of inputs, this walks
the recorded steps and returns a typed result. `ReplayResult.used_llm` is
asserted False at the end, and a test enforces it -- the whole value of the
record-once model evaporates if a "small" model call creeps back into the hot
path.

The ordering inside `_after_action` is the load-bearing part of the design, and
it is deliberate:

  1. business outcomes  -- a declared, legitimate answer. Checked first, because
                           "no such member" reaching a postcondition check would
                           be reported as a defect instead of an answer.
  2. recovery rules     -- a known interruption we know how to clear. Handled
                           before it can be mistaken for breakage.
  3. environment signals-- the app itself said something went wrong. Classified
                           into the right failure, not a generic timeout.
  4. postcondition      -- only now: are we where the recording said we would be?

Get that order wrong and the system either hides real breakage inside "expected
outcomes" or pages someone at 2am because a member id had no match.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from playwright.sync_api import Error as PlaywrightError

from ..escalation.broker import EscalationBroker, Resolution
from ..evidence.recorder import EvidenceRecorder
from ..policy.engine import PolicyEngine, SecretNotConfigured, Verdict
from ..surface.base import AmbiguousTarget, ResolvedControl, Surface, SurfaceError
from ..types.actions import StepAction, describe_action
from ..types.artifact import Capability, OnExhausted, Param, RecoveryRule, Step
from ..types.conditions import describe
from ..types.core import ControlRef, ParamType, RiskTier, Sensitivity
from ..types.observation import Observation
from ..types.overlay import TenantOverlay
from ..types.results import (
    ESCALATABLE,
    DriftSignal,
    EscalationDetail,
    FailureClass,
    FailureDetail,
    LocatorUsed,
    OutcomeDetail,
    RecoveryEvent,
    ReplayResult,
    ReplayStatus,
    StepResult,
    StepStatus,
)
from .conditions import EvalContext, evaluate, observed_summary
from .extract import ExtractionError, extract


class ParamValidationError(ValueError):
    pass


def _first_line(message: str) -> str:
    """Playwright errors carry a long call log; the first line is the cause."""
    return message.strip().split("\n", 1)[0]


def validate_params(cap: Capability, supplied: Mapping[str, Any]) -> dict[str, Any]:
    """Enforce the declared input contract before touching the application.

    A bad member id should cost one millisecond and a clear message, not three
    screens of navigation and a confusing locator error.
    """
    unknown = set(supplied) - {p.name for p in cap.inputs}
    if unknown:
        raise ParamValidationError(f"unknown parameter(s): {sorted(unknown)}")
    out: dict[str, Any] = {}
    for p in cap.inputs:
        if p.name not in supplied or supplied[p.name] is None:
            if p.required and p.default is None:
                raise ParamValidationError(f"missing required parameter {p.name!r}")
            if p.default is not None:
                out[p.name] = _coerce(p, p.default)
            continue
        out[p.name] = _coerce(p, supplied[p.name])
    return out


def _coerce(p: Param, raw: Any) -> Any:
    try:
        if p.type == ParamType.INTEGER:
            value: Any = int(raw)
        elif p.type == ParamType.NUMBER:
            value = float(raw)
        elif p.type == ParamType.BOOLEAN:
            value = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
        else:
            value = str(raw)
    except (TypeError, ValueError) as exc:
        raise ParamValidationError(f"{p.name}: expected {p.type.value}, got {raw!r}") from exc

    if p.pattern and not re.fullmatch(p.pattern, str(value)):
        raise ParamValidationError(
            f"{p.name}: {value!r} does not match required format /{p.pattern}/")
    if p.enum and str(value) not in p.enum:
        raise ParamValidationError(f"{p.name}: must be one of {p.enum}")
    if p.minimum is not None and float(value) < p.minimum:
        raise ParamValidationError(f"{p.name}: below minimum {p.minimum}")
    if p.maximum is not None and float(value) > p.maximum:
        raise ParamValidationError(f"{p.name}: above maximum {p.maximum}")
    return value


# How many times one step may be interrupted-and-cleared before we stop
# believing it is a stray dialog and call it an unexpected state.
_MAX_INTERRUPTIONS = 3

# How many times one step may be handed to a human before the run gives up.
_MAX_ESCALATIONS_PER_STEP = 2

# Distinguishable from None, which is a meaningful "no jump target" answer.
_SENTINEL = object()


class _Terminal(Exception):
    """Internal control flow: this run is over, carry the result out."""

    def __init__(self, result: ReplayResult) -> None:
        self.result = result


@dataclass
class ReplayEngine:
    surface: Surface
    policy: PolicyEngine
    recorder: EvidenceRecorder
    app_id: str
    broker: EscalationBroker | None = None
    overlay: TenantOverlay | None = None
    allow_draft: bool = False
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    # per-run state
    _outputs: dict[str, Any] = field(default_factory=dict, init=False)
    _recovery_uses: dict[str, int] = field(default_factory=dict, init=False)
    _drift: list[DriftSignal] = field(default_factory=list, init=False)
    _recoveries: list[RecoveryEvent] = field(default_factory=list, init=False)
    _escalation: EscalationDetail | None = field(default=None, init=False)
    _current_values: dict[str, Any] = field(default_factory=dict, init=False)
    _escalations: dict[str, int] = field(default_factory=dict, init=False)
    _finishing_after_human: bool = field(default=False, init=False)

    # -- aliases ---------------------------------------------------------
    @property
    def control_aliases(self) -> dict[str, str]:
        return dict(self.overlay.control_aliases) if self.overlay else {}

    @property
    def text_aliases(self) -> dict[str, str]:
        return dict(self.overlay.text_aliases) if self.overlay else {}

    # -- entry point -----------------------------------------------------
    def run(self, cap: Capability, params: Mapping[str, Any], *,
            base_url: str, tenant_id: str | None = None) -> ReplayResult:
        started = time.monotonic()
        result = ReplayResult(
            status=ReplayStatus.FAILED, capability_id=cap.id,
            capability_version=cap.version, run_id=self.run_id,
            tenant_id=tenant_id, evidence_dir=str(self.recorder.dir))

        if self.overlay:
            cap = self.overlay.apply(cap)

        self.recorder.event(
            "replay_start", capability=cap.qualified_name, tenant=tenant_id,
            base_url=base_url, params=self._redact_params(cap, params),
            approval=cap.approval.state.value, overlay=bool(self.overlay))

        try:
            values = self._gate(cap, params, base_url, result)
            self._execute(cap, values, base_url, result)
        except _Terminal as stop:
            result = stop.result
        except SurfaceError as exc:
            result = self._fail(result, FailureClass.SURFACE_ERROR, str(exc))
        except PlaywrightError as exc:
            # A driver-level error -- the app is unreachable, the browser died,
            # a frame detached. Worth telling apart from a bug in this engine,
            # because the two go to different people.
            result = self._fail(
                result, FailureClass.SURFACE_ERROR, _first_line(str(exc)),
                remediation="the application or the browser was unreachable; "
                            "check the target is up and the base URL is right")
        except Exception as exc:                      # never leak a stack to the caller
            result = self._fail(result, FailureClass.INTERNAL,
                                f"{type(exc).__name__}: {exc}")

        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.finished_at = datetime.now(timezone.utc)
        result.drift = self._drift
        result.recoveries = self._recoveries
        result.escalation = self._escalation
        result.used_llm = False
        self.recorder.event("replay_end", status=result.status.value,
                            headline=result.headline(), duration_ms=result.duration_ms)
        self.recorder.write_result(result)
        return result

    # -- pre-flight ------------------------------------------------------
    def _gate(self, cap: Capability, params: Mapping[str, Any], base_url: str,
              result: ReplayResult) -> dict[str, Any]:
        try:
            values = validate_params(cap, params)
        except ParamValidationError as exc:
            raise _Terminal(self._reject(result, FailureClass.INPUT_INVALID, str(exc),
                                         remediation="fix the arguments and call again"))

        if (self.policy.unattended_replay_requires_approval
                and cap.approval.state.value != "approved" and not self.allow_draft):
            raise _Terminal(self._reject(
                result, FailureClass.POLICY_DENIED,
                f"capability is {cap.approval.state.value}; unattended replay requires "
                f"an approved capability",
                remediation="have a reviewer approve it, or pass --allow-draft"))

        decision = self.policy.check_url(self.app_id, base_url)
        if not decision.allowed:
            raise _Terminal(self._reject(result, FailureClass.POLICY_DENIED, decision.reason))
        return values

    # -- main loop -------------------------------------------------------
    def _execute(self, cap: Capability, values: dict[str, Any], base_url: str,
                 result: ReplayResult) -> None:
        self._current_values = values
        plan: list[Step] = [*cap.preconditions, *cap.steps]
        index = 0
        guard = 0
        while index < len(plan):
            guard += 1
            if guard > len(plan) * 4:
                raise _Terminal(self._fail(
                    result, FailureClass.INTERNAL,
                    "replay made no forward progress; aborting to avoid a loop"))
            step = plan[index]
            jump = self._run_step(cap, step, index, values, base_url, result)
            if jump is None:
                index += 1
                continue
            # `__first_step__` means "restart the business flow": re-auth lands
            # back on the console home, so there is nowhere local to resume to.
            target = cap.steps[0].id if (jump == "__first_step__" and cap.steps) else jump
            index = next((i for i, s in enumerate(plan) if s.id == target), index + 1)
            self.recorder.event("resume_jump", to=target, requested=jump)

        self._verify_checkpoint(cap, result)
        self._finalize_outputs(cap, result)
        result.status = ReplayStatus.SUCCESS
        result.outputs = self._outputs

    # -- one step --------------------------------------------------------
    def _run_step(self, cap: Capability, step: Step, index: int,
                  values: dict[str, Any], base_url: str,
                  result: ReplayResult) -> str | None:
        sr = StepResult(index=index, step_id=step.id, intent=step.intent,
                        action=describe_action(step.action), status=StepStatus.OK)
        result.steps.append(sr)
        began = time.monotonic()

        obs = self._observe(step.id)
        ctx = self._ctx(obs)

        if step.precondition is not None and not evaluate(step.precondition, ctx):
            sr.status = StepStatus.SKIPPED
            sr.note = f"precondition not met: {describe(step.precondition)}"
            self.recorder.event("step_skipped", step=step.id, reason=sr.note)
            return None

        budget = step.on_error.retries + 1
        attempt, interruptions = 0, 0
        while True:
            attempt += 1
            sr.attempts = attempt
            # Two nested handlers on purpose. The inner one deals with a retry;
            # the outer one deals with whatever comes back from escalating a
            # retry that ran out of attempts -- which is raised *from inside*
            # the inner handler and so cannot be caught there.
            try:
                pending: _Retry | None = None
                try:
                    jump = self._attempt(cap, step, index, values, base_url, sr,
                                         result, attempt)
                    sr.duration_ms = int((time.monotonic() - began) * 1000)
                    return jump
                except _Retry as again:
                    pending = again
                jump = self._handle_retry(cap, step, index, sr, result, pending,
                                          attempt, budget, began)
                if jump is not _SENTINEL:
                    return jump
                interruptions += 0 if not pending.from_recovery else 1
                if interruptions > _MAX_INTERRUPTIONS:
                    sr.duration_ms = int((time.monotonic() - began) * 1000)
                    self._stop(cap, result, sr, FailureClass.UNEXPECTED_STATE,
                               f"the screen was interrupted {interruptions} times on "
                               f"one step; something is wrong beyond a stray dialog",
                               expected=pending.expected, observed=pending.observed,
                               step=step, index=index)
                if pending.from_recovery:
                    attempt -= 1
                continue
            except _Continue as done:
                sr.duration_ms = int((time.monotonic() - began) * 1000)
                sr.status, sr.note = StepStatus.RECOVERED, done.note
                return None
            except _HumanResolved as resolved:
                sr.duration_ms = int((time.monotonic() - began) * 1000)
                sr.status, sr.note = StepStatus.RECOVERED, resolved.note
                self.recorder.event("handoff_resume", step=step.id,
                                    rerun=resolved.rerun, note=resolved.note)
                if not resolved.rerun:
                    return None
                attempt = 0        # the human cleared the way; give it a fresh budget
                continue

    def _handle_retry(self, cap, step, index, sr, result, again, attempt, budget,
                      began):
        """Decide what a retry means. Returns a jump target, or _SENTINEL to
        keep looping."""
        # A cleared interruption is not a failed attempt. Clearing a notice
        # modal must not consume the budget the step reserved for genuine
        # flakiness, or any capability with a recovery rule and the default
        # `retries: 0` would fail the moment one fires.
        if again.from_recovery:
            if again.jump_to:
                # Re-authentication drops us back at the console, so the
                # rule names where the flow has to pick up again.
                sr.duration_ms = int((time.monotonic() - began) * 1000)
                sr.status, sr.note = StepStatus.RECOVERED, again.detail
                return again.jump_to
            return _SENTINEL
        if attempt >= budget:
            sr.duration_ms = int((time.monotonic() - began) * 1000)
            if step.optional or step.on_error.on_exhausted == OnExhausted.SKIP:
                sr.status = StepStatus.SKIPPED
                sr.note = f"skipped after {attempt} attempt(s): {again.detail}"
                return None
            self._stop(cap, result, sr, again.failure_class, again.detail,
                       expected=again.expected, observed=again.observed,
                       step=step, index=index)
        self.recorder.event("step_retry", step=step.id, attempt=attempt,
                            reason=again.detail)
        time.sleep(step.on_error.backoff_ms / 1000)
        return _SENTINEL

    def _attempt(self, cap: Capability, step: Step, index: int, values: dict[str, Any],
                 base_url: str, sr: StepResult, result: ReplayResult,
                 attempt: int) -> str | None:
        action = step.action
        target_ref = getattr(action, "target", None)
        resolved: ResolvedControl | None = None
        obs = self._observe(step.id)

        if target_ref is not None:
            resolved = self._resolve(target_ref, obs, sr, step, index, cap, result)

        control_name = self._alias(target_ref.name) if target_ref else ""
        url = self._render(getattr(action, "url_template", ""), values, base_url) \
            if action.kind == "navigate" else None

        decision = self.policy.check(self.app_id, action, control_name=control_name, url=url)
        self.recorder.event("policy_check", step=step.id, action=action.kind,
                            control=control_name, verdict=decision.verdict.value,
                            risk=decision.risk.value, reason=decision.reason)
        if decision.verdict == Verdict.DENY:
            self._stop(cap, result, sr, FailureClass.POLICY_DENIED, decision.reason,
                       step=step, index=index, escalate=False)
        if decision.verdict == Verdict.REQUIRE_APPROVAL:
            self._request_approval(cap, step, index, decision.reason, obs, sr, result)

        self._perform(action, resolved, values, base_url, step)
        self.surface.settle(step.settle_ms)
        obs = self._observe(step.id, after=True)

        return self._after_action(cap, step, index, sr, result, obs, values, base_url, attempt)

    # -- the ordering that matters ---------------------------------------
    def _after_action(self, cap: Capability, step: Step, index: int, sr: StepResult,
                      result: ReplayResult, obs: Observation, values: dict[str, Any],
                      base_url: str, attempt: int) -> str | None:
        ctx = self._ctx(obs)

        # 1. A declared business outcome is an answer, and it ends the run cleanly.
        for outcome in cap.outcomes:
            if evaluate(outcome.when, ctx):
                message = ""
                if outcome.message_from:
                    try:
                        message = str(extract(outcome.message_from, obs,
                                              aliases=self.text_aliases) or "")
                    except ExtractionError:
                        message = ""
                detail = OutcomeDetail(code=outcome.code, title=outcome.title,
                                       message=message, detected_at_step=step.id)
                self.recorder.event("business_outcome", code=outcome.code,
                                    step=step.id, message=message)
                result.status = ReplayStatus.BUSINESS_OUTCOME
                result.outcome = detail
                result.outputs = dict(self._outputs)
                if outcome.terminal:
                    raise _Terminal(result)

        # 2. A known interruption we already know how to clear.
        fired = self._try_recovery(cap, step, sr, obs, values, base_url)
        if fired is not None:
            sr.status = StepStatus.RECOVERED
            rule = next(r for r in cap.recovery if r.id == fired)
            if rule.resume_at_step:
                raise _Retry(FailureClass.UNEXPECTED_STATE,
                             f"cleared by recovery rule {fired}; resuming at step "
                             f"{rule.resume_at_step}",
                             observed=observed_summary(obs), from_recovery=True,
                             jump_to=rule.resume_at_step)

            # Clearing the obstruction is not the same as undoing the step. An
            # interruption often appears *after* the action landed -- a notice
            # rendered into a frame by the very navigation we just made. Re-running
            # the step then would repeat it, and in this domain repeating a click
            # can mean submitting a transaction twice.
            #
            # So we only re-run when we can positively show the step did not take:
            # a postcondition that is defined and still false. No postcondition
            # means no evidence, and the safe reading of no evidence is "do not
            # do it again" -- the next step's own checks will catch a real
            # derailment.
            obs = self._observe(step.id, after=True)
            ctx = self._ctx(obs)
            if step.postcondition is not None and not evaluate(step.postcondition, ctx):
                raise _Retry(FailureClass.UNEXPECTED_STATE,
                             f"cleared by recovery rule {fired}; the step had not taken "
                             f"effect, so re-running it",
                             expected=describe(step.postcondition),
                             observed=observed_summary(obs), from_recovery=True)
            self.recorder.event(
                "recovery_no_rerun", step=step.id, rule=fired,
                reason="step had already taken effect when the interruption appeared")

        # 3. The app told us something went wrong. Classify it properly.
        for signal in cap.environment:
            if evaluate(signal.when, ctx):
                self.recorder.event("environment_signal", signal=signal.id, step=step.id,
                                    failure_class=signal.failure_class.value)
                self._stop(cap, result, sr, signal.failure_class, signal.description,
                           expected="", observed=observed_summary(obs),
                           remediation=signal.remediation, step=step, index=index)

        # 4. Only now: are we where the recording said we would be?
        if step.postcondition is not None and not evaluate(step.postcondition, ctx):
            raise _Retry(FailureClass.POSTCONDITION_FAILED,
                         f"postcondition not met after {describe_action(step.action)}",
                         expected=describe(step.postcondition),
                         observed=observed_summary(obs))

        if step.action.kind == "read":
            self._do_read(cap, step, index, sr, result, obs)

        sr.observed = observed_summary(obs)
        if step.postcondition:
            sr.expected = describe(step.postcondition)
        return None

    # -- pieces ----------------------------------------------------------
    def _resolve(self, ref: ControlRef, obs: Observation, sr: StepResult, step: Step,
                 index: int, cap: Capability, result: ReplayResult) -> ResolvedControl:
        try:
            found = self.surface.resolve(ref, observation=obs, aliases=self.control_aliases)
        except AmbiguousTarget as exc:
            self._stop(cap, result, sr, FailureClass.TARGET_AMBIGUOUS, str(exc),
                       expected=f'{ref.role} "{ref.name}"',
                       observed=observed_summary(obs), step=step, index=index)
            raise  # unreachable; _stop raises
        if found is None:
            tried = ", ".join(f"{c.strategy.value}" for c in ref.ranked()) or "none"
            raise _Retry(
                FailureClass.TARGET_NOT_FOUND,
                f'could not find {ref.role} "{self._alias(ref.name)}" (tried: {tried})',
                expected=f'{ref.role} "{self._alias(ref.name)}"',
                observed=observed_summary(obs))

        sr.locator = LocatorUsed(
            strategy=found.strategy, args={"detail": found.detail, "mode": found.match_mode},
            confidence=found.confidence, rank=found.rank,
            fallback=found.used_fallback, structural=found.is_structural)
        if found.used_fallback:
            self._drift.append(DriftSignal(
                kind="locator_fallback", step_id=step.id,
                detail=f"preferred locator missed; matched via {found.strategy.value}"))
        if found.is_structural:
            self._drift.append(DriftSignal(
                kind="structural_locator", step_id=step.id,
                detail=f"resolved only via {found.strategy.value}; the semantic "
                       f"locators no longer match -- review this capability"))
        return found

    def _perform(self, action: StepAction, resolved: ResolvedControl | None,
                 values: dict[str, Any], base_url: str, step: Step) -> None:
        k = action.kind
        if k == "navigate":
            self.surface.navigate(self._render(action.url_template, values, base_url))
        elif k == "click":
            self.surface.click(resolved)
        elif k == "fill":
            self.surface.fill(resolved, self._value(action.value))
        elif k == "select":
            self.surface.select(resolved, self._value(action.value))
        elif k == "press":
            self.surface.press(action.key, resolved)
        elif k == "wait_for":
            self._wait_for(action, step)
        elif k == "read":
            pass                       # handled after observing
        self.recorder.event("action", step=step.id, action=describe_action(action))

    def _wait_for(self, action, step: Step) -> None:
        deadline = time.monotonic() + action.timeout_ms / 1000
        while time.monotonic() < deadline:
            if evaluate(action.condition, self._ctx(self.surface.observe())):
                return
            time.sleep(0.2)
        raise _Retry(FailureClass.TIMEOUT,
                     f"waited {action.timeout_ms} ms for {describe(action.condition)}",
                     expected=describe(action.condition))

    def _do_read(self, cap: Capability, step: Step, index: int, sr: StepResult,
                 result: ReplayResult, obs: Observation) -> None:
        spec = step.action.extract
        try:
            value = extract(spec, obs, aliases=self.text_aliases)
        except ExtractionError as exc:
            self._stop(cap, result, sr, FailureClass.EXTRACTION_FAILED, str(exc),
                       expected=f"output {spec.output}", observed=observed_summary(obs),
                       remediation="the screen no longer exposes this value where the "
                                   "capability expects it; re-review the extraction",
                       step=step, index=index)
            return
        self._outputs[spec.output] = value
        declared = next((o for o in cap.outputs if o.name == spec.output), None)
        sens = declared.sensitivity if declared else Sensitivity.INTERNAL
        self.recorder.event("extracted", step=step.id, output=spec.output,
                            value=self.policy.redactor.value(value, sens))

    def _try_recovery(self, cap: Capability, step: Step, sr: StepResult, obs: Observation,
                      values: dict[str, Any], base_url: str) -> str | None:
        ctx = self._ctx(obs)
        for rule in cap.recovery:
            if self._recovery_uses.get(rule.id, 0) >= rule.max_uses:
                continue
            if not evaluate(rule.when, ctx):
                continue
            self._recovery_uses[rule.id] = self._recovery_uses.get(rule.id, 0) + 1
            self.recorder.event("recovery_start", rule=rule.id, step=step.id,
                                when=describe(rule.when))
            self._run_recovery(rule, values, base_url, obs)
            event = RecoveryEvent(
                rule_id=rule.id, description=rule.description, at_step=step.id,
                attempt=self._recovery_uses[rule.id])
            self._recoveries.append(event)
            sr.recoveries.append(event)
            self._drift.append(DriftSignal(
                kind="recovery_used", step_id=step.id,
                detail=f"recovery {rule.id} fired"))
            return rule.id
        return None

    def _run_recovery(self, rule: RecoveryRule, values: dict[str, Any], base_url: str,
                      obs: Observation) -> None:
        for act in rule.do:
            # Recovery actions are not a back door: they go through the same gate.
            target = getattr(act, "target", None)
            name = self._alias(target.name) if target else ""
            url = self._render(getattr(act, "url_template", ""), values, base_url) \
                if act.kind == "navigate" else None
            decision = self.policy.check(self.app_id, act, control_name=name, url=url)
            if not decision.allowed:
                self.recorder.event("recovery_blocked", rule=rule.id,
                                    action=describe_action(act), reason=decision.reason)
                raise SurfaceError(
                    f"recovery {rule.id} requires an action policy refuses: {decision.reason}")
            resolved = None
            if target is not None:
                fresh = self.surface.observe()
                resolved = self.surface.resolve(target, observation=fresh,
                                                aliases=self.control_aliases)
                if resolved is None:
                    raise SurfaceError(
                        f'recovery {rule.id} could not find {target.role} "{name}"')
            self._perform(act, resolved, values, base_url,
                          Step(id=f"recovery:{rule.id}", intent=rule.description, action=act))
            self.surface.settle(200)

    def _verify_checkpoint(self, cap: Capability, result: ReplayResult) -> None:
        if cap.checkpoint is None:
            return
        obs = self._observe("checkpoint", after=True)
        ctx = self._ctx(obs)
        if evaluate(cap.checkpoint, ctx):
            self.recorder.event("checkpoint_met", condition=describe(cap.checkpoint))
            return
        sr = StepResult(index=len(result.steps), step_id="__checkpoint__",
                        intent="verify the capability actually reached its goal",
                        action="checkpoint", status=StepStatus.FAILED,
                        expected=describe(cap.checkpoint), observed=observed_summary(obs))
        result.steps.append(sr)
        self._stop(cap, result, sr, FailureClass.CHECKPOINT_FAILED,
                   "every step ran, but the success condition is not satisfied",
                   expected=describe(cap.checkpoint), observed=observed_summary(obs),
                   remediation="the flow completed into an unexpected state; compare the "
                               "final screenshot against the recorded checkpoint")

    def _finalize_outputs(self, cap: Capability, result: ReplayResult) -> None:
        missing = [o.name for o in cap.outputs if o.required and o.name not in self._outputs]
        if missing:
            sr = StepResult(index=len(result.steps), step_id="__outputs__",
                            intent="return declared outputs", action="collect",
                            status=StepStatus.FAILED)
            result.steps.append(sr)
            self._stop(cap, result, sr, FailureClass.EXTRACTION_FAILED,
                       f"declared output(s) not produced: {missing}",
                       expected=f"outputs {missing}")

    # -- stopping --------------------------------------------------------
    def _stop(self, cap: Capability, result: ReplayResult, sr: StepResult,
              failure_class: FailureClass, message: str, *, expected: str = "",
              observed: str = "", remediation: str = "", step: Step | None = None,
              index: int | None = None, escalate: bool = True) -> None:
        """Record a hard failure, offer it to a human, and end the run."""
        sr.status = StepStatus.FAILED
        sr.expected, sr.observed = expected or sr.expected, observed or sr.observed
        evidence = self._capture_failure(sr.step_id)
        sr.evidence.extend(evidence)

        # Two bounds on escalation, both of which a naive implementation lacks.
        #
        # The first stops a loop: verifying the checkpoint after an operator
        # says they finished can itself fail, and escalating *that* would land
        # straight back here.
        #
        # The second stops a slower loop: an operator who keeps clicking
        # "resume" without actually clearing the problem would otherwise get a
        # fresh retry budget every time, forever.
        step_key = step.id if step else "__run__"
        too_many = self._escalations.get(step_key, 0) >= _MAX_ESCALATIONS_PER_STEP
        if escalate and self._finishing_after_human:
            escalate = False
        elif escalate and too_many:
            escalate = False
            message = (f"{message} -- already escalated "
                       f"{_MAX_ESCALATIONS_PER_STEP} times on this step")

        if escalate and self.broker is not None and failure_class in ESCALATABLE:
            self._escalations[step_key] = self._escalations.get(step_key, 0) + 1
            resolution = self._escalate(cap, failure_class, message, expected, observed,
                                        step, index, evidence)

            if resolution == Resolution.COMPLETED_BY_HUMAN:
                # The operator says they finished the job. Trust but verify: the
                # checkpoint still has to pass before this is reported a success.
                raise _Terminal(self._succeed_after_human(cap, result))

            if resolution == Resolution.RESUME:
                # The operator says they cleared the obstruction. Check the
                # screen rather than taking their word for it.
                rerun, note = self._resume_plan(cap, step)
                if rerun is not None:
                    raise _HumanResolved(rerun, note)
                # Cannot establish whether an irreversible step already ran.
                # Guessing either way is worse than stopping.
                message = (f"{message} -- after the handoff there is no way to tell "
                           f"whether this irreversible step already completed")

            if resolution == Resolution.TIMEOUT:
                result.status = ReplayStatus.ESCALATED
                result.failure = FailureDetail(
                    failure_class=failure_class, message=message,
                    step_index=index, step_id=step.id if step else None,
                    expected=expected, observed=observed,
                    remediation="no operator claimed the intervention in time",
                    evidence=evidence)
                raise _Terminal(result)

        raise _Terminal(self._fail(result, failure_class, message, expected=expected,
                                   observed=observed, remediation=remediation,
                                   step=step, index=index, evidence=evidence))

    def _escalate(self, cap: Capability, failure_class: FailureClass, message: str,
                  expected: str, observed: str, step: Step | None, index: int | None,
                  evidence: list[str]) -> Resolution:
        req = self.broker.raise_intervention(
            run_id=self.run_id, kind="replay", reason=message,
            subject=cap.qualified_name, failure_class=failure_class,
            step_id=step.id if step else None, step_index=index,
            expected=expected, observed=observed,
            url=getattr(self.surface, "page", None).url if hasattr(self.surface, "page") else "",
            screenshot=evidence[0] if evidence else "",
            suggested_action=self._suggestion(failure_class, step))
        resolution = self.broker.wait_for_resolution()
        self._escalation = EscalationDetail(
            intervention_id=req.id, reason=message,
            raised_at_step=step.id if step else None,
            resolved=resolution != Resolution.TIMEOUT,
            resolution=resolution.value, human_actions=len(req.human_actions))
        if resolution != Resolution.TIMEOUT:
            self.broker.resume_automation(self.run_id)
        return resolution

    @staticmethod
    def _suggestion(failure_class: FailureClass, step: Step | None) -> str:
        what = f' for step "{step.intent}"' if step else ""
        return {
            FailureClass.TARGET_NOT_FOUND:
                f"take the session, complete the action{what} by hand, then hand back",
            FailureClass.TARGET_AMBIGUOUS:
                "several controls matched; pick the right one and hand back",
            FailureClass.POSTCONDITION_FAILED:
                f"the screen did not reach the expected state{what}; correct it and hand back",
            FailureClass.CHECKPOINT_FAILED:
                "the flow ended somewhere unexpected; finish or abort",
            FailureClass.SESSION_LOST: "sign back on, then hand back",
            FailureClass.PERMISSION_DENIED:
                "this operator lacks rights on the record; escalate to a supervisor",
            FailureClass.UNEXPECTED_STATE:
                "clear whatever is blocking the screen and hand back",
        }.get(failure_class, "review the screen and decide")

    def _resume_plan(self, cap: Capability, step: Step | None) -> tuple[bool | None, str]:
        """After a handoff, decide whether to re-run the step. Verify, don't trust.

        Returns (rerun, note); `rerun is None` means it cannot be decided safely.

        Escalation here follows a step that did *not* complete -- that is what
        made it escalate -- so re-running is normally both correct and safe.
        The exception is an operator who went ahead and did the step themselves:
        a postcondition that is now satisfied is the evidence for that, and the
        step is not repeated.

        With no postcondition there is no evidence either way. For an ordinary
        step, re-running is the lesser risk. For an irreversible one it is not:
        repeating a posted transaction is the single worst thing this system
        could do, so it stops and says why instead.
        """
        obs = self._observe("post-handoff", after=True)
        ctx = self._ctx(obs)
        if step is not None and step.postcondition is not None:
            met = evaluate(step.postcondition, ctx)
            self.recorder.event("resume_check", step=step.id, met=met,
                                condition=describe(step.postcondition))
            return ((False, "operator satisfied the step; continuing from here")
                    if met else
                    (True, "step still not satisfied after handoff; retrying it"))
        if step is not None and step.risk == RiskTier.IRREVERSIBLE:
            self.recorder.event("resume_undecidable", step=step.id,
                                reason="irreversible step with no postcondition")
            return (None, "")
        return (True, "no postcondition to check; retrying the step")

    def _succeed_after_human(self, cap: Capability, result: ReplayResult) -> ReplayResult:
        """An operator says they finished the job. Verify before believing it."""
        self._finishing_after_human = True
        try:
            self._verify_checkpoint(cap, result)
            self._finalize_outputs(cap, result)
        finally:
            self._finishing_after_human = False
        result.status = ReplayStatus.SUCCESS
        result.outputs = self._outputs
        return result

    def _fail(self, result: ReplayResult, failure_class: FailureClass, message: str,
              *, expected: str = "", observed: str = "", remediation: str = "",
              step: Step | None = None, index: int | None = None,
              evidence: list[str] | None = None) -> ReplayResult:
        result.status = ReplayStatus.FAILED
        result.failure = FailureDetail(
            failure_class=failure_class, message=message, step_index=index,
            step_id=step.id if step else None, expected=expected, observed=observed,
            remediation=remediation, evidence=evidence or [])
        result.outputs = dict(self._outputs)
        self.recorder.event("failure", failure_class=failure_class.value, message=message,
                            expected=expected, observed=observed)
        return result

    def _reject(self, result: ReplayResult, failure_class: FailureClass, message: str,
                *, remediation: str = "") -> ReplayResult:
        result.status = ReplayStatus.REJECTED
        result.failure = FailureDetail(failure_class=failure_class, message=message,
                                       remediation=remediation)
        self.recorder.event("rejected", failure_class=failure_class.value, message=message)
        return result

    # -- helpers ---------------------------------------------------------
    def _observe(self, label: str, *, after: bool = False) -> Observation:
        path = None
        if self.recorder.should_capture(failure=False):
            path = self.recorder.screenshot_path(f"{label}{'-after' if after else ''}")
        obs = self.surface.observe(screenshot=bool(path), screenshot_path=path) \
            if path else self.surface.observe()
        return obs

    def _capture_failure(self, label: str) -> list[str]:
        out = []
        shot = self.surface.screenshot(self.recorder.screenshot_path(f"FAIL-{label}"))
        if shot:
            out.append(self.recorder.rel(shot))
        dom = self.surface.raw_snapshot(self.recorder.dom_path(f"FAIL-{label}"))
        if dom:
            out.append(self.recorder.rel(dom))
        return out

    def _ctx(self, obs: Observation) -> EvalContext:
        return EvalContext(observation=obs, outputs=self._outputs,
                           text_aliases=self.text_aliases,
                           control_aliases=self.control_aliases)

    def _alias(self, name: str) -> str:
        return self.control_aliases.get(name, name)

    def _value(self, spec) -> str:
        if spec.kind == "literal":
            return spec.value
        if spec.kind == "param":
            return str(self._current_values.get(spec.param, ""))
        if spec.kind == "secret":
            try:
                return self.policy.resolve_secret(self.app_id, spec.secret)
            except SecretNotConfigured as exc:
                raise SurfaceError(str(exc)) from exc
        raise ValueError(f"unknown value kind {spec.kind}")

    def _render(self, template: str, values: dict[str, Any], base_url: str) -> str:
        out = template.replace("{base_url}", base_url.rstrip("/"))
        for k, v in values.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def _request_approval(self, cap: Capability, step: Step, index: int, reason: str,
                          obs: Observation, sr: StepResult, result: ReplayResult) -> None:
        if self.broker is None:
            self._stop(cap, result, sr, FailureClass.POLICY_DENIED,
                       f"{reason}, and no approver is attached to this run",
                       step=step, index=index, escalate=False)
        shot = self.surface.screenshot(self.recorder.screenshot_path(f"approve-{step.id}"))
        req = self.broker.raise_intervention(
            run_id=self.run_id, kind="replay", reason=reason,
            subject=cap.qualified_name, step_id=step.id, step_index=index,
            approval_of=describe_action(step.action),
            observed=observed_summary(obs), url=obs.url,
            screenshot=self.recorder.rel(shot),
            suggested_action="approve only if this change is intended for this member")
        resolution = self.broker.wait_for_resolution()
        self._escalation = EscalationDetail(
            intervention_id=req.id, reason=reason, raised_at_step=step.id,
            resolved=resolution != Resolution.TIMEOUT, resolution=resolution.value,
            human_actions=len(req.human_actions))
        if resolution != Resolution.TIMEOUT:
            self.broker.resume_automation(self.run_id)
        if resolution != Resolution.APPROVED:
            result.status = (ReplayStatus.ESCALATED if resolution == Resolution.TIMEOUT
                             else ReplayStatus.REJECTED)
            result.failure = FailureDetail(
                failure_class=FailureClass.POLICY_DENIED,
                message=f"irreversible step was not approved ({resolution.value})",
                step_index=index, step_id=step.id)
            raise _Terminal(result)
        self.recorder.event("approval_granted", step=step.id,
                            action=describe_action(step.action))

    def _redact_params(self, cap: Capability, params: Mapping[str, Any]) -> dict:
        sens = {p.name: p.sensitivity for p in cap.inputs}
        return self.policy.redactor.mapping(dict(params), sens)


class _Retry(Exception):
    """Try this step again. `from_recovery` means an interruption was cleared,
    which does not count against the step's retry budget."""

    def __init__(self, failure_class: FailureClass, detail: str, *,
                 expected: str = "", observed: str = "",
                 from_recovery: bool = False, jump_to: str | None = None) -> None:
        super().__init__(detail)
        self.failure_class, self.detail = failure_class, detail
        self.expected, self.observed = expected, observed
        self.from_recovery, self.jump_to = from_recovery, jump_to


class _HumanResolved(Exception):
    """An operator handled the escalation. `rerun` says whether automation
    should attempt the step again, or treat it as already satisfied."""

    def __init__(self, rerun: bool, note: str) -> None:
        super().__init__(note)
        self.rerun, self.note = rerun, note


class _Continue(Exception):
    """This step is done -- a human satisfied it. Move to the next one."""

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note
