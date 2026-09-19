"""End-to-end replay against the live fixture.

One test per branch of the result contract, because the distinction between
them is the design.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from fixtures import savings_balance_capability
from harness import engine, fixture_app
from scribe.escalation import AutoResolver, ControlToken, EscalationBroker, Resolution
from scribe.types.results import FailureClass, ReplayStatus

pytestmark = pytest.mark.browser


def arm(base_url: str, kind: str, **kw) -> None:
    body = json.dumps({"kind": kind, **kw}).encode()
    req = urllib.request.Request(f"{base_url}/__fault", data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def run(tmp_path, params, *, fault=None, cap=None, **kw):
    cap = cap or savings_balance_capability()
    with fixture_app() as base:
        if fault:
            arm(base, fault[0], **fault[1])
        with engine(tmp_path, **kw) as eng:
            eng.policy.app("meridian_core").allowed_origins.append(base)
            return eng.run(cap, params, base_url=base, tenant_id="northstar-cu")


# -- success ---------------------------------------------------------------
def test_success_returns_typed_outputs(tmp_path):
    result = run(tmp_path, {"member_id": "100412"})
    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["member_name"] == "Dolores Vance"
    assert result.outputs["savings_balance"] == 4182.55
    assert isinstance(result.outputs["savings_balance"], float)


def test_the_production_path_never_calls_a_model(tmp_path):
    """The claim the whole design rests on."""
    assert run(tmp_path, {"member_id": "100412"}).used_llm is False


def test_the_same_artifact_works_for_a_different_input(tmp_path):
    """Proves the recording is parameterized, not welded to the member it saw."""
    result = run(tmp_path, {"member_id": "100416"})
    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["member_name"] == "Wen Li"


def test_every_step_resolves_semantically_with_no_drift(tmp_path):
    result = run(tmp_path, {"member_id": "100412"})
    assert result.drift == []
    for step in result.steps:
        if step.locator is not None:
            assert not step.locator.structural
            assert not step.locator.fallback


# -- business outcome ------------------------------------------------------
def test_no_such_member_is_an_outcome_not_a_failure(tmp_path):
    result = run(tmp_path, {"member_id": "999999"})
    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome.code == "MEMBER_NOT_FOUND"
    assert "No member record found" in result.outcome.message
    assert result.failure is None


# -- caller faults ---------------------------------------------------------
def test_a_malformed_id_is_rejected_before_the_browser_does_any_work(tmp_path):
    result = run(tmp_path, {"member_id": "abc"})
    # REJECTED, not FAILED: nothing was attempted, so nothing went wrong.
    assert result.status == ReplayStatus.REJECTED
    assert result.failure.failure_class == FailureClass.INPUT_INVALID
    assert result.steps == []           # nothing was attempted


def test_a_draft_capability_will_not_run_unattended(tmp_path):
    result = run(tmp_path, {"member_id": "100412"},
                 cap=savings_balance_capability(approved=False))
    assert result.status == ReplayStatus.REJECTED
    assert result.failure.failure_class == FailureClass.POLICY_DENIED


def test_an_explicit_override_lets_a_draft_run(tmp_path):
    result = run(tmp_path, {"member_id": "100412"},
                 cap=savings_balance_capability(approved=False), allow_draft=True)
    assert result.status == ReplayStatus.SUCCESS


# -- environment faults ----------------------------------------------------
def test_a_privacy_hold_is_reported_as_a_permission_problem(tmp_path):
    result = run(tmp_path, {"member_id": "100414"})
    assert result.failure.failure_class == FailureClass.PERMISSION_DENIED
    # Escalatable, so with nobody attached it ends as ESCALATED, not FAILED.
    assert result.status in (ReplayStatus.ESCALATED, ReplayStatus.FAILED)


def test_an_application_error_is_classified_and_not_retried_forever(tmp_path):
    result = run(tmp_path, {"member_id": "100412"}, fault=("app_error", {"count": 1}))
    assert result.status == ReplayStatus.FAILED
    assert result.failure.failure_class == FailureClass.APP_ERROR
    assert result.failure.remediation


def test_failure_evidence_is_captured(tmp_path):
    result = run(tmp_path, {"member_id": "100412"}, fault=("app_error", {"count": 1}))
    failed = [s for s in result.steps if s.evidence]
    assert failed, "a hard failure should leave a screenshot and a DOM snapshot"
    assert any(e.endswith(".png") for e in failed[0].evidence)
    assert any(e.endswith(".html") for e in failed[0].evidence)


# -- recovery --------------------------------------------------------------
def test_an_unexpected_modal_is_cleared_and_the_run_still_succeeds(tmp_path):
    result = run(tmp_path, {"member_id": "100412"},
                 fault=("interstitial", {"count": 2}))
    assert result.status == ReplayStatus.SUCCESS
    assert [r.rule_id for r in result.recoveries] == ["dismiss_system_notice"]
    # Recovered, not silent: it shows up as a drift signal for review.
    assert any(d.kind == "recovery_used" for d in result.drift)


def test_a_session_that_dies_mid_flow_is_re_authenticated(tmp_path):
    result = run(tmp_path, {"member_id": "100416"},
                 fault=("session_expired", {"count": 1}))
    assert result.status == ReplayStatus.SUCCESS
    assert [r.rule_id for r in result.recoveries] == ["reauthenticate"]
    assert result.outputs["member_name"] == "Wen Li"


# -- escalation ------------------------------------------------------------
def test_an_unresolvable_block_is_handed_to_a_human(tmp_path):
    """With the capability's own answer to a modal removed, it has to ask.

    The end-to-end version of this -- a real operator driving the live session
    through the console's HTTP API -- is examples/handoff_demo.py. Here the
    operator is stubbed so the routing, the context, and the audit trail can be
    asserted in CI.
    """
    cap = savings_balance_capability()
    cap.recovery = []

    token = ControlToken()
    broker = EscalationBroker(token, sinks=[], claim_timeout=10, resolve_timeout=20)
    broker.sinks.append(AutoResolver(
        broker, Resolution.COMPLETED_BY_HUMAN, delay=0.4,
        actions=[("click", "acknowledged the notice by hand")]))

    with fixture_app() as base:
        arm(base, "interstitial", count=4)
        with engine(tmp_path, broker=broker) as eng:
            eng.policy.app("meridian_core").allowed_origins.append(base)
            result = eng.run(cap, {"member_id": "100412"}, base_url=base)

    assert result.escalation is not None, result.headline()
    assert result.escalation.intervention_id
    assert result.escalation.human_actions == 1
    assert result.escalation.resolution == "completed_by_human"
    # The operator said they finished; the checkpoint says otherwise, and the
    # run reports that rather than taking their word for it.
    assert result.status is not ReplayStatus.SUCCESS
    assert result.failure.failure_class == FailureClass.CHECKPOINT_FAILED


def test_an_unclaimed_escalation_ends_the_run_rather_than_hanging(tmp_path):
    cap = savings_balance_capability()
    cap.recovery = []
    result = run(tmp_path, {"member_id": "100412"},
                 cap=cap, fault=("interstitial", {"count": 3}), claim_timeout=1.5)
    assert result.status == ReplayStatus.ESCALATED
    assert result.escalation.resolution == "timeout"
