"""Turning a run into a contract.

The recorder is where durability is decided, mostly by not trusting the model:
it re-derives parameterization, canonicalizes URLs, and refuses assertions that
would only ever pass for the input used on the day.
"""

from __future__ import annotations

from scribe.agent.loop import DiscoveryResult, TraceEntry
from scribe.agent.recorder import ArtifactRecorder, _new_marker_text
from scribe.agent.schema import AgentMove
from scribe.apps import MERIDIAN_CORE
from scribe.types.core import LocatorCandidate, LocatorStrategy, Sensitivity
from scribe.types.observation import Observation, UIElement

BASE = "http://127.0.0.1:8799"
PARAMS = {"member_id": "100412"}


def recorder() -> ArtifactRecorder:
    return ArtifactRecorder(MERIDIAN_CORE, base_url=BASE, tenant_id="northstar-cu")


def element(ref="e1", role="textbox", name="Member ID") -> UIElement:
    return UIElement(
        ref=ref, role=role, name=name, frame_path=["content"],
        name_source="table_adjacent",
        candidates=[LocatorCandidate(
            strategy=LocatorStrategy.ROLE_NAME,
            args={"role": role, "name": name}, confidence=0.95)])


def observation(text: str, url: str = f"{BASE}/console") -> Observation:
    return Observation(url=url, text=text, frame_texts={"(top)": text})


# -- parameterization ------------------------------------------------------
def test_a_placeholder_becomes_a_parameter():
    value = recorder()._value_for("{{param:member_id}}", PARAMS)
    assert value.kind == "param" and value.param == "member_id"


def test_a_literal_that_matches_an_input_also_becomes_a_parameter():
    """The safety net.

    A model that types the real id instead of the placeholder would otherwise
    produce a capability hard-coded to one member -- and it would pass its own
    replay test, because it was recorded and replayed with that member.
    """
    value = recorder()._value_for("100412", PARAMS)
    assert value.kind == "param" and value.param == "member_id"


def test_an_unrelated_literal_stays_literal():
    value = recorder()._value_for("SAVINGS", PARAMS)
    assert value.kind == "literal" and value.value == "SAVINGS"


def test_urls_are_canonicalized_away_from_the_recording_environment():
    url = recorder()._canonical_url(f"{BASE}/members/detail?f_mid=100412", PARAMS)
    assert url == "{base_url}/members/detail?f_mid={member_id}"


def test_an_absolute_url_from_another_host_is_still_rebased():
    url = recorder()._canonical_url("http://other.host:9000/members/search", PARAMS)
    assert url == "{base_url}/members/search"


# -- derived assertions ----------------------------------------------------
def test_a_derived_marker_prefers_a_heading():
    before = observation("Member Inquiry\nEnter the member id")
    after = observation("Member Inquiry\nMember Relationship Summary\n"
                        "Member Name\tDolores Vance")
    assert _new_marker_text(before, after, {"100412"}) == "Member Relationship Summary"


def test_a_derived_marker_never_embeds_this_run_s_input():
    """An assertion containing the member id would pass exactly once."""
    before = observation("Member Inquiry")
    after = observation("Member Inquiry\nRecord 100412 loaded")
    assert _new_marker_text(before, after, {"100412"}) != "Record 100412 loaded"


def test_a_postcondition_is_derived_from_the_observed_transition():
    before = observation("Member Inquiry", url=f"{BASE}/members/search")
    after = observation("Member Relationship Summary",
                        url=f"{BASE}/members/detail?f_mid=100412")
    cond = recorder()._derive_postcondition(before, after, {"100412"})
    assert cond is not None
    rendered = cond.model_dump_json()
    assert "Member Relationship Summary" in rendered
    assert "100412" not in rendered          # the URL pattern is generalized too


# -- the contract ----------------------------------------------------------
def test_inferred_parameter_shape_is_pinned_and_flagged():
    param = recorder()._param_for("member_id", "100412")
    assert param.pattern == r"^\d{6}$"
    assert param.sensitivity == Sensitivity.PII


def test_a_numeric_read_is_typed_as_a_number():
    move = AgentMove(tool="read", output="savings_balance", source="table_cell",
                     row_anchor="SAVINGS", column_header="Balance",
                     transform="number", description="savings balance")
    field = recorder()._output_for(move)
    assert field.type.value == "number"
    assert field.sensitivity == Sensitivity.INTERNAL


def test_a_recorded_capability_is_a_draft_carrying_its_provenance():
    move = AgentMove(tool="fill", ref="e1", value="{{param:member_id}}",
                     intent="enter the member id", thought="t")
    entry = TraceEntry(index=0, thought="t", tool="fill", raw="{}", url=f"{BASE}/console",
                       intent="enter the member id", element=element(), move=move,
                       observation=observation("Member Inquiry"))
    result = DiscoveryResult(run_id="r1", goal="read a balance", success=True,
                             stop_reason="done", trace=[entry],
                             final_observation=observation("Member Relationship Summary"),
                             checkpoint_text="Member Relationship Summary",
                             summary="reads a balance")
    cap = recorder().build(result, capability_id="meridian_core.x", name="X",
                           params=PARAMS, model="claude-opus-5 (cli)")

    assert cap.approval.state.value == "draft"
    assert cap.provenance.recorded_by == "claude-opus-5 (cli)"
    assert cap.provenance.discovery_run_id == "r1"
    assert cap.steps[0].action.value.kind == "param"
    # App-level knowledge is attached, not re-derived per capability.
    assert [r.id for r in cap.recovery] == [r.id for r in MERIDIAN_CORE.recovery]
    assert [s.id for s in cap.preconditions] == [s.id for s in MERIDIAN_CORE.login]


def test_locators_come_from_the_observation_not_from_the_model():
    move = AgentMove(tool="click", ref="e1", thought="t")
    entry = TraceEntry(index=0, thought="t", tool="click", raw="{}", url=BASE,
                       element=element(role="button", name="Search"), move=move,
                       observation=observation("x"))
    action = recorder()._action_for(move, entry, PARAMS)
    assert action.target.name == "Search"
    assert action.target.frame_path == ["content"]
    assert action.target.candidates[0].strategy == LocatorStrategy.ROLE_NAME
