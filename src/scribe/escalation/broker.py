"""Raising a human, and getting the session back afterwards.

An intervention request is not a log line saying "stuck". It carries what a
person needs to act without going back to the engineer who wrote the capability:
which capability and which goal, which step, what was expected, what was on
screen, a screenshot, and -- where the system has an opinion -- what it thinks
should happen next.

Routing is a seam, not a hardcoded UI. `InterventionSink` is the integration
point: in this build the operator console is one sink and a terminal prompt is
another. A production deployment would add one that opens a ticket or pages the
on-call servicing queue; nothing else changes.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from ..types.results import FailureClass
from .control import ControlOwner, ControlToken


class InterventionStatus(str, Enum):
    OPEN = "open"               # raised, waiting for a human
    CLAIMED = "claimed"         # a human is driving
    RESOLVED = "resolved"
    ABANDONED = "abandoned"     # nobody came


class Resolution(str, Enum):
    RESUME = "resume"                       # human fixed it; automation continues
    COMPLETED_BY_HUMAN = "completed_by_human"  # human finished the job
    ABORT = "abort"
    APPROVED = "approved"                   # risky step signed off; automation proceeds
    DENIED = "denied"                       # risky step refused
    TIMEOUT = "timeout"


@dataclass
class HumanAction:
    """Something a person did while holding the session. Part of the audit trail."""

    at: datetime
    kind: str                   # click | type | key | navigate | note
    detail: str
    url: str = ""
    operator: str = ""

    def to_dict(self) -> dict:
        return {"at": self.at.isoformat(), "kind": self.kind,
                "detail": self.detail, "url": self.url, "operator": self.operator}


@dataclass
class InterventionRequest:
    id: str
    run_id: str
    kind: str                   # discovery | replay
    reason: str
    subject: str                # capability id, or the discovery goal
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    failure_class: FailureClass | None = None
    step_id: str | None = None
    step_index: int | None = None
    expected: str = ""
    observed: str = ""
    url: str = ""
    screenshot: str = ""
    suggested_action: str = ""
    # For an approval request rather than a stuck run.
    approval_of: str = ""

    status: InterventionStatus = InterventionStatus.OPEN
    claimed_by: str = ""
    claimed_at: datetime | None = None
    resolution: Resolution | None = None
    resolution_note: str = ""
    human_actions: list[HumanAction] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "run_id": self.run_id, "kind": self.kind,
            "reason": self.reason, "subject": self.subject,
            "created_at": self.created_at.isoformat(),
            "failure_class": self.failure_class.value if self.failure_class else None,
            "step_id": self.step_id, "step_index": self.step_index,
            "expected": self.expected, "observed": self.observed,
            "url": self.url, "screenshot": self.screenshot,
            "suggested_action": self.suggested_action,
            "approval_of": self.approval_of,
            "status": self.status.value, "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "resolution": self.resolution.value if self.resolution else None,
            "resolution_note": self.resolution_note,
            "human_actions": [a.to_dict() for a in self.human_actions],
        }

    def briefing(self) -> str:
        """The plain-text version a person reads. Deliberately short."""
        lines = [
            f"INTERVENTION {self.id}  ({self.kind})",
            f"  subject : {self.subject}",
            f"  reason  : {self.reason}",
        ]
        if self.approval_of:
            lines.append(f"  approve : {self.approval_of}")
        if self.step_id:
            lines.append(f"  step    : #{self.step_index} {self.step_id}")
        if self.expected:
            lines.append(f"  expected: {self.expected}")
        if self.observed:
            lines.append(f"  observed: {self.observed}")
        if self.url:
            lines.append(f"  at      : {self.url}")
        if self.suggested_action:
            lines.append(f"  suggest : {self.suggested_action}")
        return "\n".join(lines)


class InterventionSink(Protocol):
    """Where an intervention request gets routed."""

    def publish(self, request: InterventionRequest) -> None: ...
    def describe(self) -> str: ...


class FileSink:
    """Drops the request as JSON. Stands in for a ticket queue / pager hook."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def publish(self, request: InterventionRequest) -> None:
        (self.dir / f"{request.id}.json").write_text(
            json.dumps(request.to_dict(), indent=2), encoding="utf-8")

    def describe(self) -> str:
        return f"file://{self.dir}"


class LogSink:
    """Prints the briefing. Useful when running headless with no console."""

    def __init__(self, stream=None) -> None:
        import sys
        self.stream = stream or sys.stderr

    def publish(self, request: InterventionRequest) -> None:
        print("\n" + request.briefing() + "\n", file=self.stream, flush=True)

    def describe(self) -> str:
        return "log"


class EscalationBroker:
    """Owns the control token and the open intervention for one live session."""

    def __init__(
        self,
        token: ControlToken,
        *,
        sinks: list[InterventionSink] | None = None,
        claim_timeout: float = 300.0,
        resolve_timeout: float = 1800.0,
        on_event=None,
    ) -> None:
        self.token = token
        self.sinks = sinks or []
        self.claim_timeout = claim_timeout
        self.resolve_timeout = resolve_timeout
        self.on_event = on_event or (lambda *a, **k: None)
        # Set when a live session is attached. While automation waits for a
        # human it must keep draining the session command queue, or the
        # operator's clicks would sit unexecuted -- the browser can only be
        # driven from the thread that owns it, and that thread is this one.
        self._pump: Any = None
        self.current: InterventionRequest | None = None
        self.history: list[InterventionRequest] = []
        self._resolved = threading.Event()
        self._updates: queue.Queue = queue.Queue()
        self._lock = threading.RLock()

    def attach_pump(self, pump) -> None:
        """Give the waiting side something to do: drain the session queue."""
        self._pump = pump

    # -- raising ---------------------------------------------------------
    def raise_intervention(self, **fields: Any) -> InterventionRequest:
        with self._lock:
            req = InterventionRequest(id=f"iv_{uuid.uuid4().hex[:10]}", **fields)
            self.current = req
            self._resolved.clear()
            # Automation stops *before* the request goes out, so there is no
            # window where a human could attach while the executor is still acting.
            self.token.offer()
            self.history.append(req)
        self.on_event("escalation_raised", **req.to_dict())
        for sink in self.sinks:
            try:
                sink.publish(req)
            except Exception as exc:                      # a broken pager must not
                self.on_event("sink_error", sink=sink.describe(), error=str(exc))
        return req

    # -- operator side ---------------------------------------------------
    def claim(self, operator: str) -> InterventionRequest:
        with self._lock:
            req = self._require_open()
            self.token.claim(operator)
            req.status = InterventionStatus.CLAIMED
            req.claimed_by = operator
            req.claimed_at = datetime.now(timezone.utc)
        self.on_event("escalation_claimed", intervention_id=req.id, operator=operator)
        return req

    def record_human_action(self, operator: str, kind: str, detail: str,
                            url: str = "") -> None:
        """Every human interaction is recorded -- this is a regulated system."""
        with self._lock:
            req = self.current
            if req is None:
                return
            action = HumanAction(datetime.now(timezone.utc), kind, detail, url, operator)
            req.human_actions.append(action)
        self.on_event("human_action", intervention_id=req.id, **action.to_dict())

    def resolve(self, operator: str, resolution: Resolution, note: str = "") -> None:
        with self._lock:
            req = self.current
            if req is None:
                raise RuntimeError("no open intervention")
            if resolution in (Resolution.RESUME, Resolution.COMPLETED_BY_HUMAN,
                              Resolution.APPROVED, Resolution.DENIED):
                if self.token.state()[0] == ControlOwner.HUMAN:
                    self.token.hand_back(operator)
            req.status = InterventionStatus.RESOLVED
            req.resolution = resolution
            req.resolution_note = note
        self.on_event("escalation_resolved", intervention_id=req.id,
                      resolution=resolution.value, note=note,
                      human_actions=len(req.human_actions))
        self._resolved.set()

    # -- automation side -------------------------------------------------
    def wait_for_resolution(self, timeout: float | None = None) -> Resolution:
        """Block the run until a human decides, or give up cleanly.

        Never waits forever: an unattended run that escalates at 2am and is
        never claimed has to end as ESCALATED/unresolved, not as a hung process
        holding a browser and a core-banking session open.
        """
        deadline = time.monotonic() + (timeout or self.resolve_timeout)
        claim_deadline = time.monotonic() + self.claim_timeout
        while time.monotonic() < deadline:
            if self._pump is not None:
                # Execute whatever the operator has queued, then check again.
                self._pump(0.2)
                if self._resolved.is_set():
                    return self.current.resolution or Resolution.RESUME
            elif self._resolved.wait(timeout=0.25):
                return self.current.resolution or Resolution.RESUME
            with self._lock:
                claimed = self.current and self.current.status == InterventionStatus.CLAIMED
            if not claimed and time.monotonic() > claim_deadline:
                break
        with self._lock:
            if self.current:
                self.current.status = InterventionStatus.ABANDONED
                self.current.resolution = Resolution.TIMEOUT
        self.on_event("escalation_timeout",
                      intervention_id=self.current.id if self.current else None)
        return Resolution.TIMEOUT

    def resume_automation(self, run_id: str) -> None:
        self.token.resume(run_id)
        self.on_event("control_resumed", run_id=run_id)

    def _require_open(self) -> InterventionRequest:
        if self.current is None or self.current.status not in (
                InterventionStatus.OPEN, InterventionStatus.CLAIMED):
            raise RuntimeError("no open intervention to claim")
        return self.current

    def snapshot(self) -> dict:
        return {
            "control": self.token.snapshot(),
            "current": self.current.to_dict() if self.current else None,
            "history": len(self.history),
            "sinks": [s.describe() for s in self.sinks],
        }


class AutoResolver:
    """Test double: answers interventions on a timer, with a fixed verdict.

    Used by the demo scripts and tests so the end-to-end escalation path can run
    unattended. It is not a fallback that could ever fire in production -- it has
    to be constructed and passed in explicitly.
    """

    def __init__(self, broker: EscalationBroker, resolution: Resolution,
                 operator: str = "auto_operator", delay: float = 0.5,
                 actions: list[tuple[str, str]] | None = None) -> None:
        self.broker, self.resolution = broker, resolution
        self.operator, self.delay = operator, delay
        self.actions = actions or []

    def publish(self, request: InterventionRequest) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        time.sleep(self.delay)
        try:
            self.broker.claim(self.operator)
            for kind, detail in self.actions:
                self.broker.record_human_action(self.operator, kind, detail)
            self.broker.resolve(self.operator, self.resolution,
                                note="resolved by AutoResolver (test double)")
        except Exception:
            pass

    def describe(self) -> str:
        return f"auto:{self.resolution.value}"
