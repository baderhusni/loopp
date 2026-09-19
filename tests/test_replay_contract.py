"""Replay semantics that do not need a browser.

The end-to-end browser runs live in test_replay_live.py; these pin the parts of
the contract that should be checkable in milliseconds -- input validation,
condition evaluation, and extraction.
"""

from __future__ import annotations

import pytest

from fixtures import savings_balance_capability
from scribe.replay.conditions import EvalContext, evaluate
from scribe.replay.engine import ParamValidationError, validate_params
from scribe.replay.extract import ExtractionError, extract
from scribe.types.conditions import AllOf, AnyOf, Not, TextPresent, UrlMatches
from scribe.types.core import ExtractSource, ExtractSpec, Transform
from scribe.types.observation import FieldPair, Observation, TableSnapshot
from scribe.types.results import ESCALATABLE, FailureClass, ReplayStatus


# -- the input contract ----------------------------------------------------
def test_valid_params_pass():
    assert validate_params(savings_balance_capability(),
                           {"member_id": "100412"}) == {"member_id": "100412"}


@pytest.mark.parametrize("bad", ["abc", "1", "1004121", ""])
def test_malformed_ids_are_rejected_before_the_browser_opens(bad):
    with pytest.raises(ParamValidationError, match="format"):
        validate_params(savings_balance_capability(), {"member_id": bad})


def test_missing_required_param_is_rejected():
    with pytest.raises(ParamValidationError, match="missing required"):
        validate_params(savings_balance_capability(), {})


def test_unknown_param_is_rejected_rather_than_ignored():
    with pytest.raises(ParamValidationError, match="unknown"):
        validate_params(savings_balance_capability(),
                        {"member_id": "100412", "sneaky": "1"})


# -- conditions ------------------------------------------------------------
def observation(text: str = "", url: str = "http://x/console", **kw) -> Observation:
    return Observation(url=url, text=text, frame_texts={"(top)": text}, **kw)


def test_text_conditions_are_case_insensitive_by_default():
    ctx = EvalContext(observation=observation("Member Relationship Summary"))
    assert evaluate(TextPresent(text="member relationship summary"), ctx)
    assert evaluate(TextAbsent := Not(condition=TextPresent(text="nope")), ctx)


def test_boolean_combinators():
    ctx = EvalContext(observation=observation("Summary\nno errors here"))
    assert evaluate(AllOf(conditions=[TextPresent(text="Summary"),
                                      Not(condition=TextPresent(text="denied"))]), ctx)
    assert evaluate(AnyOf(conditions=[TextPresent(text="absent"),
                                      TextPresent(text="Summary")]), ctx)
    assert not evaluate(AllOf(conditions=[TextPresent(text="Summary"),
                                          TextPresent(text="absent")]), ctx)


def test_url_condition():
    ctx = EvalContext(observation=observation(url="http://x/members/detail?f_mid=1"))
    assert evaluate(UrlMatches(pattern=r"/members/detail"), ctx)
    assert not evaluate(UrlMatches(pattern=r"/members/search"), ctx)


def test_tenant_text_aliases_apply_to_conditions():
    """One recording, a deployment that words the message differently."""
    ctx = EvalContext(observation=observation("No member record found for Member Number 9."),
                      text_aliases={"No member record found for Member ID":
                                    "No member record found for Member Number"})
    assert evaluate(TextPresent(text="No member record found for Member ID"), ctx)


# -- extraction ------------------------------------------------------------
GRID = TableSnapshot(headers=["Type", "Account Number", "Balance", "Status"],
                     rows=[["SAVINGS", "SAV-0100412-01", "4,182.55", "OPEN"],
                           ["CHECKING", "CHK-0100412-01", "912.03", "OPEN"]])
# Same data, the column order a different institution configures.
REORDERED = TableSnapshot(headers=["Account Number", "Type", "Status", "Balance"],
                          rows=[["SAV-0100412-01", "SAVINGS", "OPEN", "4,182.55"]])


def balance_spec() -> ExtractSpec:
    return ExtractSpec(output="savings_balance", source=ExtractSource.TABLE_CELL,
                       row_anchor="SAVINGS", column_header="Balance",
                       transform=Transform.NUMBER)


def test_table_read_is_typed():
    obs = observation(tables=[GRID])
    assert extract(balance_spec(), obs) == 4182.55


def test_table_read_survives_reordered_columns():
    """The whole reason cells are addressed by heading and not by index."""
    assert extract(balance_spec(), observation(tables=[REORDERED])) == 4182.55


def test_labeled_field_read():
    obs = observation(pairs=[FieldPair(label="Member Name", value="Dolores Vance")])
    spec = ExtractSpec(output="member_name", source=ExtractSource.LABELED_FIELD,
                       label="Member Name")
    assert extract(spec, obs) == "Dolores Vance"


def test_missing_value_says_what_was_actually_there():
    obs = observation(tables=[GRID])
    spec = balance_spec()
    spec.row_anchor = "CERTIFICATE"
    with pytest.raises(ExtractionError) as exc:
        extract(spec, obs)
    assert "CERTIFICATE" in str(exc.value)
    assert "Balance" in str(exc.value)      # names the grid it did look at


def test_optional_output_returns_none_rather_than_raising():
    spec = balance_spec()
    spec.required = False
    spec.row_anchor = "CERTIFICATE"
    assert extract(spec, observation(tables=[GRID])) is None


# -- the taxonomy itself ---------------------------------------------------
def test_business_outcome_is_not_a_failure_status():
    assert ReplayStatus.BUSINESS_OUTCOME != ReplayStatus.FAILED


def test_caller_and_capability_faults_are_not_escalated_to_a_human():
    """Waking someone at 2am for a malformed argument is the wrong design."""
    assert FailureClass.INPUT_INVALID not in ESCALATABLE
    assert FailureClass.POLICY_DENIED not in ESCALATABLE
    assert FailureClass.APP_ERROR not in ESCALATABLE


def test_conditions_a_person_can_actually_fix_are_escalated():
    for cls in (FailureClass.TARGET_NOT_FOUND, FailureClass.CHECKPOINT_FAILED,
                FailureClass.PERMISSION_DENIED, FailureClass.SESSION_LOST):
        assert cls in ESCALATABLE
