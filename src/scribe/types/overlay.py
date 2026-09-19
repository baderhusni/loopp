"""Tenant overlays: one recording, many institutions.

The premise from the brief is that hundreds of tenants run roughly twenty apps
each, and many of those are the *same vendor product* configured differently.
Re-recording a capability per tenant is the failure mode to avoid: it multiplies
the maintenance surface by the tenant count and guarantees the variants drift
apart.

So a capability is written against the *product* and a tenant supplies a thin,
reviewable overlay. The overlay is deliberately narrow -- it cannot add steps or
change what the capability does, only say how this deployment words things and
what extra interruptions it throws. Anything that needs more than this is a
signal the tenant is genuinely running a different flow, and should be a
separate capability rather than an ever-growing patch file.
"""

from __future__ import annotations

from pydantic import Field

from .artifact import Capability, RecoveryRule, Step
from .core import Strict

OVERLAY_SCHEMA_VERSION = "1.0"


class StepOverride(Strict):
    """Escape hatch for a single step. Used sparingly, and it shows up in review."""

    step_id: str
    reason: str
    step: Step


class TenantOverlay(Strict):
    schema_version: str = OVERLAY_SCHEMA_VERSION
    tenant_id: str
    app_id: str
    base_url: str

    # The common case: the same control, called something else here.
    #   {"Member ID": "Member Number", "Search": "Find Member"}
    # Applied when resolving control names and when matching condition text, so
    # a rename costs one line instead of a re-record.
    control_aliases: dict[str, str] = Field(default_factory=dict)

    # Same idea for text this deployment words differently.
    text_aliases: dict[str, str] = Field(default_factory=dict)

    # Deployment-specific interruptions (a regional notice, an SSO bounce).
    extra_recovery: list[RecoveryRule] = Field(default_factory=list)

    step_overrides: list[StepOverride] = Field(default_factory=list)
    notes: str = ""

    def alias(self, name: str) -> str:
        return self.control_aliases.get(name, name)

    def alias_text(self, text: str) -> str:
        return self.text_aliases.get(text, text)

    def apply(self, cap: Capability) -> Capability:
        """Return a tenant-specialized copy. The base artifact is never mutated."""
        out = cap.model_copy(deep=True)
        overrides = {o.step_id: o.step for o in self.step_overrides}
        out.steps = [overrides.get(s.id, s) for s in out.steps]
        out.preconditions = [overrides.get(s.id, s) for s in out.preconditions]
        out.recovery = [*out.recovery, *self.extra_recovery]
        return out
