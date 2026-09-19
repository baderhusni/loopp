"""Who is driving.

The hard part of a human handoff is not the UI, it is the invariant: exactly one
party may act on a live session at a time, and both parties must agree on which
one it is. Two writers on the same browser is how you get a half-typed form
submitted twice.

So control is an explicit token with a state machine, and *both* sides check it
before every single interaction -- the executor before each action, the operator
console before forwarding each click. A violation raises rather than racing.

    AUTOMATION ──stuck──► HANDOFF_PENDING ──operator claims──► HUMAN
         ▲                       │                               │
         │                       │ nobody claims in time         │ hand back
         │                       ▼                               ▼
         └───────── re-verify ── RESUMING ◄──────────────────────┘
                                   │
                                   └──► RELEASED (run over)

The lease matters: an operator who closes the tab must not wedge the session
forever, so HUMAN control expires and falls back to HANDOFF_PENDING.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class ControlOwner(str, Enum):
    AUTOMATION = "automation"
    HANDOFF_PENDING = "handoff_pending"   # automation stopped, nobody attached yet
    HUMAN = "human"
    RESUMING = "resuming"                 # handed back, automation re-verifying state
    RELEASED = "released"


class ControlViolation(RuntimeError):
    """Someone tried to act on the session without holding control."""


@dataclass
class ControlToken:
    owner: ControlOwner = ControlOwner.AUTOMATION
    holder: str = ""                      # "run:<id>" | "operator:<name>"
    since: float = field(default_factory=time.time)
    lease_seconds: float = 900.0
    lease_expires_at: float | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- inspection ------------------------------------------------------
    def state(self) -> tuple[ControlOwner, str]:
        with self._lock:
            self._expire_if_due()
            return self.owner, self.holder

    def held_by_automation(self) -> bool:
        return self.state()[0] == ControlOwner.AUTOMATION

    def assert_automation(self, run_id: str) -> None:
        owner, holder = self.state()
        if owner != ControlOwner.AUTOMATION:
            raise ControlViolation(
                f"automation tried to act while control is {owner.value}"
                + (f" (held by {holder})" if holder else ""))
        if holder and holder != f"run:{run_id}":
            raise ControlViolation(
                f"control is held by {holder}, not run:{run_id}")

    def assert_human(self, operator: str) -> None:
        owner, holder = self.state()
        if owner != ControlOwner.HUMAN:
            raise ControlViolation(
                f"operator {operator!r} tried to act while control is {owner.value}")
        if holder != f"operator:{operator}":
            raise ControlViolation(
                f"control is held by {holder}, not operator:{operator}")

    # -- transitions -----------------------------------------------------
    def begin_run(self, run_id: str) -> None:
        with self._lock:
            self.owner, self.holder = ControlOwner.AUTOMATION, f"run:{run_id}"
            self.since, self.lease_expires_at = time.time(), None

    def offer(self) -> None:
        """Automation stops and offers the session. Idempotent."""
        with self._lock:
            if self.owner in (ControlOwner.HUMAN, ControlOwner.HANDOFF_PENDING):
                return
            self.owner, self.holder = ControlOwner.HANDOFF_PENDING, ""
            self.since, self.lease_expires_at = time.time(), None

    def claim(self, operator: str, lease_seconds: float | None = None) -> None:
        with self._lock:
            self._expire_if_due()
            if self.owner != ControlOwner.HANDOFF_PENDING:
                raise ControlViolation(
                    f"cannot claim: control is {self.owner.value}, not handoff_pending")
            self.owner, self.holder = ControlOwner.HUMAN, f"operator:{operator}"
            self.since = time.time()
            self.lease_expires_at = self.since + (lease_seconds or self.lease_seconds)

    def renew(self, operator: str) -> None:
        with self._lock:
            self.assert_human(operator)
            self.lease_expires_at = time.time() + self.lease_seconds

    def hand_back(self, operator: str) -> None:
        with self._lock:
            self.assert_human(operator)
            self.owner, self.holder = ControlOwner.RESUMING, ""
            self.since, self.lease_expires_at = time.time(), None

    def resume(self, run_id: str) -> None:
        """Automation takes the session back after re-verifying where it is."""
        with self._lock:
            if self.owner not in (ControlOwner.RESUMING, ControlOwner.HANDOFF_PENDING):
                raise ControlViolation(f"cannot resume from {self.owner.value}")
            self.owner, self.holder = ControlOwner.AUTOMATION, f"run:{run_id}"
            self.since, self.lease_expires_at = time.time(), None

    def release(self) -> None:
        with self._lock:
            self.owner, self.holder = ControlOwner.RELEASED, ""
            self.since, self.lease_expires_at = time.time(), None

    # -- lease -----------------------------------------------------------
    def _expire_if_due(self) -> None:
        if (self.owner == ControlOwner.HUMAN and self.lease_expires_at
                and time.time() > self.lease_expires_at):
            # The operator walked away. Put the session back on offer rather
            # than leaving it locked to a browser tab nobody is looking at.
            self.owner, self.holder = ControlOwner.HANDOFF_PENDING, ""
            self.lease_expires_at = None

    def snapshot(self) -> dict:
        owner, holder = self.state()
        return {
            "owner": owner.value,
            "holder": holder,
            "since": self.since,
            "lease_expires_at": self.lease_expires_at,
            "lease_seconds_remaining": (
                None if not self.lease_expires_at
                else max(0.0, self.lease_expires_at - time.time())),
        }
