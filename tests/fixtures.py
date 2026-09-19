"""Hand-authored capability used by the test suite.

This exists so the replay engine can be tested without spending a model call on
every run. The capability *shipped* in `capabilities/` is not this one -- it is
produced by a real discovery run, and `evidence/` carries the transcript.
"""

from __future__ import annotations

from scribe.apps import MERIDIAN_CORE
from scribe.types.actions import Click, Fill, Navigate, Read
from scribe.types.artifact import (
    Approval,
    ApprovalState,
    BusinessOutcome,
    Capability,
    ErrorPolicy,
    OutputField,
    Param,
    Provenance,
    Step,
)
from scribe.types.conditions import AllOf, Not, TextPresent
from scribe.types.core import (
    ExtractSource,
    ExtractSpec,
    ParamType,
    ParamValue,
    Sensitivity,
    Transform,
)
from scribe.apps.meridian_core import control

CONTENT = ["content"]

MEMBER_NOT_FOUND = BusinessOutcome(
    code="MEMBER_NOT_FOUND",
    title="no member record matches the supplied id",
    # Deliberately the shortest stable substring: the full sentence embeds the
    # tenant's own wording for the id field ("Member ID" vs "Member Number").
    when=TextPresent(text="No member record found"),
    message_from=ExtractSpec(output="_message", source=ExtractSource.PAGE_REGEX,
                             pattern=r"(No member record found[^\n]*)", group=1),
)


def savings_balance_capability(*, approved: bool = True) -> Capability:
    return Capability(
        id="meridian_core.member_savings_balance",
        version="1.0.0",
        name="Read a member's savings balance",
        description=(
            "Look up a member by id in MERIDIAN CORE and return the current balance "
            "of their savings share, along with the member's name and the account "
            "number. Read-only."),
        app=MERIDIAN_CORE.binding("northstar-cu"),
        inputs=[Param(name="member_id", type=ParamType.STRING,
                      description="The institution's member id.",
                      pattern=r"^\d{6}$", example="100412",
                      sensitivity=Sensitivity.PII)],
        outputs=[
            OutputField(name="member_name", type=ParamType.STRING,
                        description="Member's full name as held on the core.",
                        sensitivity=Sensitivity.PII),
            OutputField(name="savings_balance", type=ParamType.NUMBER,
                        description="Current savings share balance, in USD.",
                        sensitivity=Sensitivity.INTERNAL),
            OutputField(name="savings_account_number", type=ParamType.STRING,
                        description="Savings account number.",
                        sensitivity=Sensitivity.PII),
        ],
        preconditions=list(MERIDIAN_CORE.login),
        steps=[
            Step(id="open_member_search", intent="open member inquiry",
                 action=Navigate(url_template="{base_url}/members/search"),
                 postcondition=TextPresent(text="Member Inquiry"),
                 on_error=ErrorPolicy(retries=1, backoff_ms=500)),
            Step(id="enter_member_id", intent="type the member id into the inquiry field",
                 action=Fill(target=control("textbox", "Member ID", CONTENT),
                             value=ParamValue(param="member_id"))),
            Step(id="submit_search", intent="run the inquiry",
                 action=Click(target=control("button", "Search", CONTENT)),
                 on_error=ErrorPolicy(retries=1, backoff_ms=750),
                 postcondition=AllOf(conditions=[
                     TextPresent(text="Member Relationship Summary"),
                     Not(condition=TextPresent(text="No member record found"))])),
            Step(id="read_member_name", intent="read the member's name",
                 action=Read(extract=ExtractSpec(
                     output="member_name", source=ExtractSource.LABELED_FIELD,
                     label="Member Name", transform=Transform.TRIM))),
            Step(id="read_savings_balance", intent="read the savings share balance",
                 action=Read(extract=ExtractSpec(
                     output="savings_balance", source=ExtractSource.TABLE_CELL,
                     row_anchor="SAVINGS", column_header="Balance",
                     transform=Transform.NUMBER))),
            Step(id="read_savings_account", intent="read the savings account number",
                 action=Read(extract=ExtractSpec(
                     output="savings_account_number", source=ExtractSource.TABLE_CELL,
                     row_anchor="SAVINGS", column_header="Account Number",
                     transform=Transform.TRIM))),
        ],
        recovery=list(MERIDIAN_CORE.recovery),
        environment=list(MERIDIAN_CORE.environment),
        outcomes=[MEMBER_NOT_FOUND],
        checkpoint=AllOf(conditions=[
            TextPresent(text="Member Relationship Summary"),
            Not(condition=TextPresent(text="No member record found"))]),
        approval=Approval(state=ApprovalState.APPROVED if approved else ApprovalState.DRAFT,
                          approved_by="tests"),
        provenance=Provenance(recorded_by="hand-authored test fixture"),
        tags=["read-only", "member-servicing"],
    )
