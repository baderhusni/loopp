"""The capability artifact: what the model's discovery run turns into.

Design intent, in one line: this is a *contract*, not a macro recording.

A macro says "click here, then here". A contract additionally says what the
caller must supply, what it gets back, which failures are legitimate answers,
how far the thing is allowed to go without a person, and how to tell whether it
actually worked. That difference is what lets an agent invoke it unattended and
a reviewer approve it without watching a screen recording.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator

from .actions import StepAction, describe_action
from .conditions import Condition, describe
from .results import FailureClass
from .core import (
    ExtractSpec,
    ParamType,
    RiskTier,
    Sensitivity,
    Strict,
    SurfaceKind,
)

SCHEMA_VERSION = "1.1"


# --------------------------------------------------------------------------
# Typed call contract
# --------------------------------------------------------------------------
class Param(Strict):
    """One input the calling agent supplies per invocation.

    Constraints are enforced before the browser is touched. A malformed member
    id should cost nothing and fail as ``INPUT_INVALID`` -- not as a confusing
    mid-flow locator error three screens in.
    """

    name: str
    type: ParamType = ParamType.STRING
    description: str
    required: bool = True
    default: str | None = None
    pattern: str | None = None          # regex, strings only
    enum: list[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    example: str | None = None

    def json_schema(self) -> dict[str, Any]:
        node: dict[str, Any] = {"type": self.type.value, "description": self.description}
        if self.pattern:
            node["pattern"] = self.pattern
        if self.enum:
            node["enum"] = self.enum
        if self.minimum is not None:
            node["minimum"] = self.minimum
        if self.maximum is not None:
            node["maximum"] = self.maximum
        if self.example is not None:
            node["examples"] = [self.example]
        return node


class OutputField(Strict):
    """One value the capability returns. Shape is part of the contract."""

    name: str
    type: ParamType = ParamType.STRING
    description: str
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    required: bool = True

    def json_schema(self) -> dict[str, Any]:
        return {"type": self.type.value, "description": self.description}


class BusinessOutcome(Strict):
    """A legitimate answer that is not success.

    "No such member" is information the caller asked for, not an exception. It
    gets a stable code so an agent can branch on it without string-matching the
    app's wording -- which differs per tenant anyway.
    """

    code: str
    title: str
    when: Condition
    terminal: bool = True
    message_from: ExtractSpec | None = None  # capture the app's own wording


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------
class OnExhausted(str, Enum):
    FAIL = "fail"
    SKIP = "skip"
    ESCALATE = "escalate"


class ErrorPolicy(Strict):
    """What to do when a step will not go through."""

    retries: int = 0
    backoff_ms: int = 500
    on_exhausted: OnExhausted = OnExhausted.FAIL


class Step(Strict):
    id: str
    intent: str                     # what a person would say they were doing
    action: StepAction
    precondition: Condition | None = None   # false -> skip (optional steps)
    postcondition: Condition | None = None  # the per-step checkpoint
    optional: bool = False
    risk: RiskTier = RiskTier.SAFE
    timeout_ms: int = 10_000
    settle_ms: int = 150            # quiet period after acting, before observing
    on_error: ErrorPolicy = Field(default_factory=ErrorPolicy)

    def summary(self) -> str:
        return f"{self.id}: {self.intent} [{describe_action(self.action)}]"


class EnvironmentSignal(Strict):
    """A condition that is neither success nor a business outcome.

    "Application error SYS-0500", "session has expired", "not authorized" are
    properties of the *vendor product*, not of any one capability. They are
    authored once in an app profile and stamped onto every capability recorded
    against that product, so a reviewer sees them in the artifact and a tenant
    overlay can extend them -- without every capability author re-deriving how
    this particular core reports a failure.
    """

    id: str
    description: str
    when: Condition
    failure_class: FailureClass
    remediation: str = ""


class RecoveryRule(Strict):
    """Declarative handling for a known, recoverable interruption.

    These are what keep the happy path honest. A session that expires or a
    notice modal that appears is not drift and not a business outcome -- it is
    a condition the capability already knows how to clear. Recording them as
    data means a reviewer can see exactly what the automation will do on its
    own, and the policy engine still gates every action inside ``do``.
    """

    id: str
    description: str
    when: Condition
    do: list[StepAction]
    max_uses: int = 2
    # Recovery that re-enters the flow (re-auth) may need the run to resume from
    # a step rather than continue in place.
    resume_at_step: str | None = None


# --------------------------------------------------------------------------
# Binding, risk, provenance
# --------------------------------------------------------------------------
class AppBinding(Strict):
    """Which application this capability speaks to."""

    app_id: str
    vendor_product: str
    product_version: str = ""
    surface: SurfaceKind = SurfaceKind.LEGACY_WEB
    entrypoint: str = "{base_url}/"
    # product -> written against the vendor product, reusable across every
    # tenant running it (with an overlay). tenant -> genuinely one-off.
    scope: Literal["product", "tenant"] = "product"
    recorded_on_tenant: str | None = None


class RiskProfile(Strict):
    max_tier: RiskTier = RiskTier.SAFE
    # An irreversible capability does not run unattended on a whim.
    requires_approval_to_run: bool = False
    notes: str = ""


class ApprovalState(str, Enum):
    DRAFT = "draft"          # just recorded; replayable only with --allow-draft
    APPROVED = "approved"    # a human signed off; callable by agents
    DEPRECATED = "deprecated"


class Approval(Strict):
    state: ApprovalState = ApprovalState.DRAFT
    approved_by: str | None = None
    approved_at: datetime | None = None
    note: str = ""


class ReplayStats(Strict):
    """Cheap reliability signal. Fed by ``scribe replay --record-stats``.

    Failures are split by who is answerable. A dead locator says the recording
    has gone stale; the core returning HTTP 500 says nothing about the
    recording at all. Counting both against one number produces a score that
    drops when the *application* has a bad afternoon, which is exactly the
    signal you do not want when deciding whether a capability still works.
    """

    replays: int = 0
    successes: int = 0
    business_outcomes: int = 0
    failures: int = 0                # attributable to this capability
    environment_failures: int = 0     # the app, the session, the network
    last_replay_at: datetime | None = None

    @property
    def success_rate(self) -> float | None:
        """Over runs the capability could have got right."""
        graded = self.successes + self.business_outcomes + self.failures
        return None if graded == 0 else (
            (self.successes + self.business_outcomes) / graded)


class Provenance(Strict):
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_by: str = ""            # model id that drove discovery
    discovery_run_id: str = ""
    discovery_goal: str = ""
    evidence_path: str = ""
    steps_proposed: int = 0          # how much of the run survived into the artifact
    steps_recorded: int = 0


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------
class Capability(Strict):
    schema_version: str = SCHEMA_VERSION
    id: str                          # meridian_core.member_savings_balance
    version: str = "1.0.0"
    name: str                        # human title
    description: str                 # agent-facing: what it does, when to call it

    app: AppBinding
    inputs: list[Param] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)

    # A prologue that runs before `steps` and is skipped when already signed on.
    # Kept separate so credentials stay out of the flow proper and so one login
    # definition is shared by every capability on the app.
    preconditions: list[Step] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)

    recovery: list[RecoveryRule] = Field(default_factory=list)
    environment: list[EnvironmentSignal] = Field(default_factory=list)
    outcomes: list[BusinessOutcome] = Field(default_factory=list)
    checkpoint: Condition | None = None   # proves we really got there

    risk: RiskProfile = Field(default_factory=RiskProfile)
    approval: Approval = Field(default_factory=Approval)
    provenance: Provenance = Field(default_factory=Provenance)
    stats: ReplayStats = Field(default_factory=ReplayStats)
    tags: list[str] = Field(default_factory=list)

    @field_validator("steps")
    @classmethod
    def _unique_step_ids(cls, v: list[Step]) -> list[Step]:
        ids = [s.id for s in v]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate step ids: {sorted(dupes)}")
        return v

    # -- call contract ---------------------------------------------------
    @property
    def qualified_name(self) -> str:
        return f"{self.id}@{self.version}"

    def input_schema(self) -> dict[str, Any]:
        """JSON Schema for the inputs -- this is the agent-facing tool schema."""
        return {
            "type": "object",
            "properties": {p.name: p.json_schema() for p in self.inputs},
            "required": [p.name for p in self.inputs if p.required],
            "additionalProperties": False,
        }

    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {o.name: o.json_schema() for o in self.outputs},
            "required": [o.name for o in self.outputs if o.required],
            "additionalProperties": False,
        }

    def outcome_codes(self) -> list[str]:
        return [o.code for o in self.outcomes]

    def sensitive_outputs(self) -> set[str]:
        return {o.name for o in self.outputs
                if o.sensitivity in (Sensitivity.PII, Sensitivity.SECRET)}

    def max_step_risk(self) -> RiskTier:
        order = [RiskTier.SAFE, RiskTier.CAUTION, RiskTier.IRREVERSIBLE]
        worst = RiskTier.SAFE
        for s in [*self.preconditions, *self.steps]:
            if order.index(s.risk) > order.index(worst):
                worst = s.risk
        return worst

    def review_summary(self) -> str:
        """What a human reviewer reads before approving. Plain text on purpose."""
        lines = [
            f"{self.name}  ({self.qualified_name})",
            f"  app      : {self.app.vendor_product} {self.app.product_version} "
            f"[{self.app.surface.value}] scope={self.app.scope}",
            f"  purpose  : {self.description}",
            f"  inputs   : " + (", ".join(
                f"{p.name}:{p.type.value}{'' if p.required else '?'}" for p in self.inputs)
                or "(none)"),
            f"  outputs  : " + (", ".join(
                f"{o.name}:{o.type.value}[{o.sensitivity.value}]" for o in self.outputs)
                or "(none)"),
            f"  risk     : max={self.max_step_risk().value} "
            f"approval={self.approval.state.value}",
        ]
        if self.preconditions:
            lines.append("  prologue :")
            lines += [f"    - {s.summary()}" for s in self.preconditions]
        lines.append("  steps    :")
        for i, s in enumerate(self.steps):
            flag = " (optional)" if s.optional else ""
            risk = "" if s.risk == RiskTier.SAFE else f" !{s.risk.value}"
            lines.append(f"    {i:>2}. {s.summary()}{flag}{risk}")
            if s.postcondition:
                lines.append(f"        then: {describe(s.postcondition)}")
        if self.recovery:
            lines.append("  recovery :")
            lines += [f"    - {r.id}: when {describe(r.when)}" for r in self.recovery]
        if self.outcomes:
            lines.append("  outcomes :")
            lines += [f"    - {o.code}: when {describe(o.when)}" for o in self.outcomes]
        if self.checkpoint:
            lines.append(f"  checkpoint: {describe(self.checkpoint)}")
        return "\n".join(lines)
