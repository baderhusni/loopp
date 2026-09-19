"""The replay result contract.

The single most important distinction in this file is between an *outcome* and
a *failure*. "No member record found" is an answer. A locator that no longer
resolves is a defect. Collapsing the two is what makes UI automation
untrustworthy: callers start treating every non-success as retryable noise, and
real breakage hides inside it.

So the top-level status is a closed set, and only ``FAILED`` means "something is
wrong with the system".
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import Field

from .core import LocatorStrategy, Strict


class ReplayStatus(str, Enum):
    SUCCESS = "success"                    # checkpoint met, outputs returned
    BUSINESS_OUTCOME = "business_outcome"  # a declared, legitimate non-success
    FAILED = "failed"                      # defect: stopped, evidence captured
    ESCALATED = "escalated"                # handed to a human, may resume
    REJECTED = "rejected"                  # refused before touching the app


class FailureClass(str, Enum):
    """Why a run stopped. Chosen so the right team gets paged.

    Roughly: the first two are the caller's problem, the middle block is the
    capability's problem, and the last block is the environment's.
    """

    INPUT_INVALID = "input_invalid"              # params failed the declared contract
    POLICY_DENIED = "policy_denied"              # guardrail refused the action

    TARGET_NOT_FOUND = "target_not_found"        # every locator candidate missed
    TARGET_AMBIGUOUS = "target_ambiguous"        # several controls matched
    POSTCONDITION_FAILED = "postcondition_failed"
    CHECKPOINT_FAILED = "checkpoint_failed"      # steps ran, we are not where we should be
    EXTRACTION_FAILED = "extraction_failed"      # output declared but not readable

    PERMISSION_DENIED = "permission_denied"      # app refused this operator
    SESSION_LOST = "session_lost"                # expired and could not re-auth
    APP_ERROR = "app_error"                      # app returned its own error
    UNEXPECTED_STATE = "unexpected_state"        # blocked by something unmodelled
    TIMEOUT = "timeout"

    SURFACE_ERROR = "surface_error"              # browser/driver failed
    INTERNAL = "internal"


# Failures the *capability* is answerable for: its locators, its assertions, its
# reads. These are the ones that should move a reliability score, because they
# are the ones a better recording would have avoided.
CAPABILITY_FAULTS = frozenset({
    FailureClass.TARGET_NOT_FOUND,
    FailureClass.TARGET_AMBIGUOUS,
    FailureClass.POSTCONDITION_FAILED,
    FailureClass.CHECKPOINT_FAILED,
    FailureClass.EXTRACTION_FAILED,
})

# Failure classes worth putting in front of a person rather than just logging.
ESCALATABLE = frozenset({
    FailureClass.TARGET_NOT_FOUND,
    FailureClass.TARGET_AMBIGUOUS,
    FailureClass.POSTCONDITION_FAILED,
    FailureClass.CHECKPOINT_FAILED,
    FailureClass.UNEXPECTED_STATE,
    FailureClass.SESSION_LOST,
    FailureClass.PERMISSION_DENIED,
})


class StepStatus(str, Enum):
    OK = "ok"
    SKIPPED = "skipped"            # precondition false, or optional and absent
    RECOVERED = "recovered"        # a recovery rule fired, then the step went through
    FAILED = "failed"
    NOT_REACHED = "not_reached"


class RecoveryEvent(Strict):
    """A recoverable condition that was detected and cleared."""

    rule_id: str
    description: str
    at_step: str
    attempt: int
    detail: str = ""


class LocatorUsed(Strict):
    strategy: LocatorStrategy
    args: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    rank: int = 0                  # 0 = the preferred candidate resolved
    fallback: bool = False         # a lower-ranked candidate had to be used
    structural: bool = False       # resolved only via css/xpath/coordinates


class StepResult(Strict):
    index: int
    step_id: str
    intent: str
    action: str                    # human rendering
    status: StepStatus
    attempts: int = 1
    duration_ms: int = 0
    locator: LocatorUsed | None = None
    recoveries: list[RecoveryEvent] = Field(default_factory=list)
    expected: str = ""             # what the postcondition asked for
    observed: str = ""             # what was actually on screen
    evidence: list[str] = Field(default_factory=list)
    note: str = ""


class FailureDetail(Strict):
    """Everything needed to debug without re-running."""

    failure_class: FailureClass
    message: str
    step_index: int | None = None
    step_id: str | None = None
    expected: str = ""
    observed: str = ""
    remediation: str = ""          # the concrete next move, where we know it
    evidence: list[str] = Field(default_factory=list)


class OutcomeDetail(Strict):
    code: str
    title: str
    message: str = ""              # the app's own wording, when captured
    detected_at_step: str | None = None


class EscalationDetail(Strict):
    intervention_id: str
    reason: str
    raised_at_step: str | None = None
    console_url: str = ""
    resolved: bool = False
    resolution: str = ""           # resumed | aborted | completed_by_human
    human_actions: int = 0


class DriftSignal(Strict):
    """Soft warning: it worked, but not the way it was recorded.

    Not a failure. This is the early-warning channel for a capability that is
    slowly going stale -- a preferred locator that stopped resolving, or a
    recovery rule that started firing every run.
    """

    kind: str                      # locator_fallback | structural_locator | recovery_used
    step_id: str
    detail: str


class ReplayResult(Strict):
    status: ReplayStatus
    capability_id: str
    capability_version: str
    run_id: str
    tenant_id: str | None = None

    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    duration_ms: int = 0

    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: OutcomeDetail | None = None
    failure: FailureDetail | None = None
    escalation: EscalationDetail | None = None

    steps: list[StepResult] = Field(default_factory=list)
    recoveries: list[RecoveryEvent] = Field(default_factory=list)
    drift: list[DriftSignal] = Field(default_factory=list)
    evidence_dir: str = ""
    used_llm: bool = False         # must stay False on the production path

    def ok(self) -> bool:
        return self.status == ReplayStatus.SUCCESS

    def headline(self) -> str:
        if self.status == ReplayStatus.SUCCESS:
            return f"SUCCESS  {self.capability_id}  ({self.duration_ms} ms)"
        if self.status == ReplayStatus.BUSINESS_OUTCOME and self.outcome:
            return f"OUTCOME  {self.outcome.code}  {self.outcome.message or self.outcome.title}"
        if self.status == ReplayStatus.ESCALATED and self.escalation:
            return f"ESCALATED  {self.escalation.reason}"
        if self.status == ReplayStatus.REJECTED and self.failure:
            return f"REJECTED  {self.failure.failure_class.value}: {self.failure.message}"
        if self.failure:
            at = f" at step {self.failure.step_id}" if self.failure.step_id else ""
            return f"FAILED  {self.failure.failure_class.value}{at}: {self.failure.message}"
        return self.status.value.upper()
