"""The discovery run: observe, decide, act -- once, with a model in the loop.

This is the only place a model ever touches the application, and it is an
authoring activity, not a production one. Everything the run produces is
distilled into an artifact that runs without it.

Three things are deliberately kept away from the model:

  credentials  -- sign-on is executed deterministically from the app profile
                  before the model is handed the session. It never sees, types,
                  or could log a passcode.
  the guardrail-- every move it proposes goes through the same policy engine
                  that gates replay. A refusal is fed back as an observation so
                  it can try another route, rather than ending the run.
  locators     -- it points at controls by the ref it was just shown. How to
                  find that control again is the recorder's job, from evidence
                  the model does not have.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..apps.meridian_core import AppProfile
from ..escalation.broker import EscalationBroker, Resolution
from ..evidence.recorder import EvidenceRecorder
from ..policy.engine import PolicyEngine, Verdict
from ..surface.base import AmbiguousTarget, Surface
from ..types.actions import Click, Fill, Navigate, Press, Select
from ..types.artifact import Capability
from ..types.core import ControlRef, LiteralValue
from ..types.observation import Observation, UIElement
import re

from .llm import LLMClient, LLMError
from .prompts import SYSTEM, task_briefing
from .schema import AgentMove

# Keep this many full observations in the conversation; older ones are elided.
# Legacy screens are small, but a long run would otherwise resend the same
# frameset chrome twenty times.
_FULL_OBSERVATIONS = 3

# Stuck detectors.
_MAX_REPEATS = 3            # identical move, three times running
_MAX_STALLS = 3             # screen unchanged after acting, three times running
_MAX_DENIALS = 2            # consecutive policy refusals

_PLACEHOLDER = re.compile(r"\{\{param:([a-zA-Z_][\w]*)\}\}")

# Moves that ought to move the screen on. Everything else can legitimately
# leave it untouched.
_STATE_CHANGING = frozenset({"navigate", "click", "fill", "select", "press"})


@dataclass
class TraceEntry:
    index: int
    thought: str
    tool: str
    raw: str
    url: str
    intent: str = ""
    element: UIElement | None = None
    move: AgentMove | None = None
    policy_verdict: str = "allow"
    error: str = ""
    observation: Observation | None = None


@dataclass
class DiscoveryResult:
    run_id: str
    goal: str
    success: bool
    stop_reason: str
    trace: list[TraceEntry] = field(default_factory=list)
    capability: Capability | None = None
    escalations: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    evidence_dir: str = ""
    final_observation: Observation | None = None
    checkpoint_text: str = ""
    summary: str = ""

    def headline(self) -> str:
        state = "RECORDED" if self.success else "NOT RECORDED"
        return (f"{state}  {self.stop_reason}  "
                f"({len(self.trace)} model moves, {self.llm_calls} calls)")


class DiscoveryAgent:
    def __init__(
        self,
        *,
        surface: Surface,
        policy: PolicyEngine,
        recorder: EvidenceRecorder,
        llm: LLMClient,
        profile: AppProfile,
        goal: str,
        params: dict[str, Any],
        base_url: str,
        broker: EscalationBroker | None = None,
        max_steps: int = 24,
        run_id: str | None = None,
    ) -> None:
        self.surface, self.policy, self.recorder = surface, policy, recorder
        self.llm, self.profile = llm, profile
        self.goal, self.params, self.base_url = goal, params, base_url
        self.broker = broker
        self.max_steps = max_steps
        self.run_id = run_id or uuid.uuid4().hex[:10]
        self.app_id = profile.app_id

    # -- entry point -----------------------------------------------------
    def run(self) -> DiscoveryResult:
        started = time.monotonic()
        result = DiscoveryResult(run_id=self.run_id, goal=self.goal, success=False,
                                 stop_reason="not started",
                                 evidence_dir=str(self.recorder.dir))
        self.recorder.event("discovery_start", goal=self.goal, base_url=self.base_url,
                            app=self.profile.vendor_product, model=self.llm.model,
                            backend=self.llm.name,
                            params=self._redacted_params())
        try:
            self._sign_on()
            self._loop(result)
        except LLMError as exc:
            result.stop_reason = f"model backend failed: {exc}"
        except Exception as exc:
            result.stop_reason = f"{type(exc).__name__}: {exc}"
        finally:
            result.llm_calls = self.llm.usage.calls
            result.cost_usd = self.llm.usage.cost_usd
            result.duration_ms = int((time.monotonic() - started) * 1000)
            self.recorder.event("discovery_end", success=result.success,
                                stop_reason=result.stop_reason,
                                moves=len(result.trace), llm_calls=result.llm_calls,
                                cost_usd=round(result.cost_usd, 4))
        return result

    # -- deterministic prologue ------------------------------------------
    def _sign_on(self) -> None:
        """Run the app profile's sign-on without the model. Credentials stay out
        of the transcript entirely."""
        from ..replay.engine import ReplayEngine
        from ..types.artifact import AppBinding, Capability as Cap

        shell = Cap(id=f"{self.app_id}.__signon__", name="sign on",
                    description="app profile sign-on prologue",
                    app=self.profile.binding(), steps=list(self.profile.login),
                    recovery=list(self.profile.recovery),
                    environment=list(self.profile.environment))
        engine = ReplayEngine(surface=self.surface, policy=self.policy,
                              recorder=self.recorder, app_id=self.app_id,
                              broker=None, allow_draft=True, run_id=self.run_id)
        outcome = engine.run(shell, {}, base_url=self.base_url)
        if outcome.status.value not in ("success",):
            raise RuntimeError(f"could not sign on to {self.app_id}: {outcome.headline()}")
        self.recorder.event("signed_on", note="credentials resolved from policy and "
                                              "never exposed to the model")

    # -- the loop --------------------------------------------------------
    def _loop(self, result: DiscoveryResult) -> None:
        messages: list[dict[str, Any]] = []
        briefing = task_briefing(self.goal, self.params, self.base_url,
                                 f"{self.profile.vendor_product} {self.profile.product_version}")
        repeats = stalls = denials = 0
        last_move_key: tuple | None = None
        last_screen_key: tuple | None = None
        # A `read` is a pure observation: it is *supposed* to leave the screen
        # exactly as it was. Only a move that should have changed something
        # counts towards "we have stopped making progress".
        last_move_changed_state = False

        for index in range(self.max_steps):
            shot = self.recorder.screenshot_path(f"step-{index:02d}")
            obs = self.surface.observe(screenshot=True, screenshot_path=shot)
            screen_key = self._screen_key(obs)
            if (last_screen_key is not None and screen_key == last_screen_key
                    and last_move_changed_state):
                stalls += 1
            elif screen_key != last_screen_key:
                stalls = 0
            last_screen_key = screen_key

            if stalls >= _MAX_STALLS:
                result.stop_reason = ("the screen stopped changing -- the run is not "
                                      "making progress")
                self._escalate(result, result.stop_reason, obs)
                return

            content = (f"{briefing}\n\n{obs.render()}" if index == 0 else
                       f"CURRENT SCREEN\n{obs.render()}")
            messages.append({"role": "user", "content": content})
            self._elide_old(messages)

            try:
                move, raw = self.llm.decide(SYSTEM, messages)
            except LLMError as exc:
                result.stop_reason = f"model backend failed: {exc}"
                return

            entry = TraceEntry(index=index, thought=move.thought, tool=move.tool, raw=raw,
                               url=obs.url, intent=move.intent or "", move=move,
                               observation=obs)
            result.trace.append(entry)
            self.recorder.event("model_move", index=index, tool=move.tool,
                                thought=move.thought, raw=raw, url=obs.url,
                                screenshot=self.recorder.rel(obs.screenshot_path))
            messages.append({"role": "assistant", "content": raw})

            move_key = (move.tool, move.ref, move.value, move.url)
            repeats = repeats + 1 if move_key == last_move_key else 0
            last_move_key = move_key
            if repeats >= _MAX_REPEATS:
                result.stop_reason = (f"the model repeated the same action "
                                      f"{repeats + 1} times without progress")
                self._escalate(result, result.stop_reason, obs)
                return

            if move.tool == "finish":
                result.success = True
                result.stop_reason = "the model reported the goal was met"
                result.final_observation = obs
                result.checkpoint_text = move.checkpoint_text or ""
                result.summary = move.summary or self.goal
                return

            if move.tool == "escalate":
                result.stop_reason = f"the model asked for help: {move.reason}"
                self._escalate(result, move.reason or "model escalated", obs)
                if not result.success:
                    return
                messages.append({"role": "user",
                                 "content": "A human operator took the session, acted, "
                                            "and handed it back. Re-read the screen."})
                continue

            last_move_changed_state = move.tool in _STATE_CHANGING
            feedback = self._apply(move, obs, entry)
            if feedback.startswith("REFUSED"):
                denials += 1
                if denials >= _MAX_DENIALS:
                    result.stop_reason = ("policy refused the model's approach "
                                          "repeatedly")
                    self._escalate(result, result.stop_reason, obs)
                    return
            else:
                denials = 0
            messages.append({"role": "user", "content": feedback})

        result.stop_reason = f"step budget exhausted after {self.max_steps} moves"
        self._escalate(result, result.stop_reason, self.surface.observe())

    # -- applying one move ------------------------------------------------
    def _apply(self, move: AgentMove, obs: Observation, entry: TraceEntry) -> str:
        if move.tool == "read":
            return self._apply_read(move, obs, entry)

        element: UIElement | None = None
        if move.needs_ref() or (move.tool == "press" and move.ref):
            element = obs.by_ref(move.ref or "")
            if element is None:
                entry.error = f"unknown ref {move.ref!r}"
                return (f"ERROR: there is no control {move.ref!r} on this screen. "
                        f"Pick a ref from the CONTROLS list you were just shown.")
            entry.element = element

        if move.value:
            try:
                live_value = self._substitute(move.value)
            except KeyError as exc:
                entry.error = f"unknown parameter {exc.args[0]!r}"
                known = ", ".join(self.params) or "(none)"
                return (f"ERROR: there is no parameter {exc.args[0]!r}. "
                        f"Available parameters: {known}.")
        else:
            live_value = move.value

        action = self._to_action(move, element)
        if action is None:
            entry.error = f"unsupported tool {move.tool!r}"
            return f"ERROR: {move.tool!r} is not something you can do here."

        control_name = element.name if element else ""
        url = self._absolute(move.url) if move.tool == "navigate" else None
        decision = self.policy.check(self.app_id, action, control_name=control_name, url=url)
        entry.policy_verdict = decision.verdict.value
        self.recorder.event("policy_check", index=entry.index, action=move.tool,
                            control=control_name, verdict=decision.verdict.value,
                            risk=decision.risk.value, reason=decision.reason)

        if decision.verdict == Verdict.DENY:
            entry.error = decision.reason
            return (f"REFUSED by policy: {decision.reason}. That route is not available "
                    f"to you. Try a different one, or escalate.")

        if decision.verdict == Verdict.REQUIRE_APPROVAL:
            approved = self._approve(decision.reason, move, obs, control_name)
            if not approved:
                entry.error = f"approval refused: {decision.reason}"
                return (f"REFUSED: a human declined to approve this "
                        f"({decision.reason}). Do not retry it.")

        try:
            self._execute(action, element, url, live_value)
        except AmbiguousTarget as exc:
            entry.error = str(exc)
            return f"ERROR: {exc}. Identify the control more precisely."
        except Exception as exc:
            entry.error = f"{type(exc).__name__}: {exc}"
            return f"ERROR: that action failed: {exc}"

        self.surface.settle(250)
        return "OK. The new screen follows."

    def _apply_read(self, move: AgentMove, obs: Observation, entry: TraceEntry) -> str:
        """Reads are validated against the live screen immediately.

        A `read` that cannot be satisfied right now would become a capability
        that fails on its first production call, so it is rejected here while
        the model can still choose a different anchor.
        """
        from ..replay.extract import ExtractionError, extract
        from .recorder import extract_spec_from_move

        spec = extract_spec_from_move(move)
        if spec is None:
            return "ERROR: that read is missing the fields its source needs."
        try:
            value = extract(spec, obs)
        except ExtractionError as exc:
            entry.error = str(exc)
            return f"ERROR: {exc}. Choose an anchor that is actually on this screen."
        self.recorder.event("model_read", output=spec.output, source=spec.source.value,
                            value=self.policy.redactor.text(str(value)))
        return (f"OK. Captured {spec.output} = {value!r}. That read is recorded. "
                f"The screen is unchanged.")

    def _substitute(self, text: str) -> str:
        """Swap `{{param:name}}` for the live value, for execution only.

        The move keeps the placeholder, because that is what gets recorded: the
        capability must type whatever the caller supplies, not the id used on
        the day it was authored. But the browser has to receive a real value now,
        or the discovery run searches for the literal string "{{param:member_id}}"
        and concludes the member does not exist.
        """
        def swap(match: "re.Match[str]") -> str:
            key = match.group(1)
            if key not in self.params:
                raise KeyError(key)
            return str(self.params[key])

        return _PLACEHOLDER.sub(swap, text)

    def _to_action(self, move: AgentMove, element: UIElement | None):
        ref = self._control_ref(element) if element else None
        if move.tool == "navigate":
            return Navigate(url_template=self._absolute(move.url or ""))
        if move.tool == "click":
            return Click(target=ref)
        if move.tool == "fill":
            return Fill(target=ref, value=LiteralValue(value=move.value or ""))
        if move.tool == "select":
            return Select(target=ref, value=LiteralValue(value=move.value or ""))
        if move.tool == "press":
            return Press(key=move.key or "Enter", target=ref)
        return None

    @staticmethod
    def _control_ref(element: UIElement) -> ControlRef:
        """Durable targeting, built from the observation -- not from the model."""
        return ControlRef(role=element.role, name=element.name,
                          frame_path=element.frame_path,
                          candidates=list(element.candidates),
                          description=f"{element.role} named {element.name!r}")

    def _execute(self, action, element: UIElement | None, url: str | None,
                 live_value: str | None = None) -> None:
        if action.kind == "navigate":
            self.surface.navigate(self._substitute(url or action.url_template))
            return
        target = self.surface.resolve(action.target, observation=None)
        if target is None:
            raise RuntimeError(f'could not act on {action.target.role} '
                               f'"{action.target.name}"')
        if action.kind == "click":
            self.surface.click(target)
        elif action.kind == "fill":
            self.surface.fill(target, live_value or "")
        elif action.kind == "select":
            self.surface.select(target, live_value or "")
        elif action.kind == "press":
            self.surface.press(action.key, target)

    def _absolute(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return self.base_url.rstrip("/") + "/" + url.lstrip("/")

    # -- humans ----------------------------------------------------------
    def _approve(self, reason: str, move: AgentMove, obs: Observation,
                 control_name: str) -> bool:
        if self.broker is None:
            return False
        req = self.broker.raise_intervention(
            run_id=self.run_id, kind="discovery", reason=reason, subject=self.goal,
            approval_of=f'{move.tool} "{control_name}"', url=obs.url,
            observed=(obs.text or "")[:400],
            screenshot=self.recorder.rel(obs.screenshot_path),
            suggested_action="approve only if this step is genuinely required by the goal")
        resolution = self.broker.wait_for_resolution()
        if resolution != Resolution.TIMEOUT:
            self.broker.resume_automation(self.run_id)
        return resolution == Resolution.APPROVED

    def _escalate(self, result: DiscoveryResult, reason: str,
                  obs: Observation | None) -> None:
        result.escalations += 1
        if self.broker is None:
            self.recorder.event("escalation_unrouted", reason=reason)
            return
        shot = self.recorder.rel(obs.screenshot_path) if obs else ""
        req = self.broker.raise_intervention(
            run_id=self.run_id, kind="discovery", reason=reason, subject=self.goal,
            url=obs.url if obs else "", observed=(obs.text or "")[:400] if obs else "",
            screenshot=shot,
            suggested_action="take the session, move the flow forward, then hand back")
        resolution = self.broker.wait_for_resolution()
        if resolution != Resolution.TIMEOUT:
            self.broker.resume_automation(self.run_id)
        if resolution == Resolution.RESUME:
            result.success = True          # the loop continues; not a final verdict
            result.stop_reason = f"{reason} (operator intervened and handed back)"
        elif resolution == Resolution.COMPLETED_BY_HUMAN:
            result.stop_reason = f"{reason} (operator completed the task by hand)"
        else:
            result.stop_reason = f"{reason} (no operator resolution: {resolution.value})"

    # -- housekeeping ----------------------------------------------------
    @staticmethod
    def _screen_key(obs: Observation) -> tuple:
        return (obs.url, tuple((e.role, e.name) for e in obs.elements),
                len(obs.text))

    @staticmethod
    def _elide_old(messages: list[dict[str, Any]]) -> None:
        seen = 0
        for msg in reversed(messages):
            if msg["role"] != "user" or "CURRENT SCREEN" not in str(msg["content"]):
                continue
            seen += 1
            if seen > _FULL_OBSERVATIONS:
                head = str(msg["content"]).split("\n", 2)[:2]
                msg["content"] = "\n".join(head) + "\n[earlier screen elided]"

    def _redacted_params(self) -> dict:
        return {k: self.policy.redactor.text(str(v)) for k, v in self.params.items()}
