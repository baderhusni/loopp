"""The artifact is a contract. These tests hold it to that."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from fixtures import savings_balance_capability
from scribe.types.artifact import Capability, Step
from scribe.types.conditions import AllOf, Not, TextPresent, describe
from scribe.types.core import LocatorStrategy, ParamType, Sensitivity
from scribe.types.overlay import TenantOverlay


def test_capability_round_trips_through_json():
    cap = savings_balance_capability()
    again = Capability.model_validate_json(json.dumps(cap.model_dump(mode="json")))
    assert again.model_dump(mode="json") == cap.model_dump(mode="json")


def test_unknown_fields_are_rejected():
    """An artifact with a field we do not understand must fail loudly, not
    silently ignore whatever it was trying to say."""
    raw = savings_balance_capability().model_dump(mode="json")
    raw["steps"][0]["surprise"] = "hello"
    with pytest.raises(ValidationError):
        Capability.model_validate(raw)


def test_duplicate_step_ids_are_rejected():
    cap = savings_balance_capability()
    raw = cap.model_dump(mode="json")
    raw["steps"].append(raw["steps"][0])
    with pytest.raises(ValidationError, match="duplicate step ids"):
        Capability.model_validate(raw)


def test_input_schema_is_a_usable_tool_schema():
    schema = savings_balance_capability().input_schema()
    assert schema["type"] == "object"
    assert schema["required"] == ["member_id"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["member_id"]["pattern"] == r"^\d{6}$"


def test_secrets_are_referenced_by_name_never_by_value():
    """The one thing that must never be in an artifact."""
    cap = savings_balance_capability()
    blob = json.dumps(cap.model_dump(mode="json"))
    assert "core_passcode" in blob          # the reference is recorded
    assert "Tr0ubadour" not in blob          # the value never is
    for step in cap.preconditions:
        value = getattr(step.action, "value", None)
        if value is not None and value.kind == "secret":
            assert not hasattr(value, "value")


def test_pii_outputs_are_declared():
    cap = savings_balance_capability()
    assert cap.sensitive_outputs() == {"member_name", "savings_account_number"}
    balance = next(o for o in cap.outputs if o.name == "savings_balance")
    assert balance.type == ParamType.NUMBER
    assert balance.sensitivity == Sensitivity.INTERNAL


def test_semantic_locators_outrank_structural_ones():
    cap = savings_balance_capability()
    step = next(s for s in cap.steps if s.id == "enter_member_id")
    ranked = step.action.target.ranked()
    assert ranked[0].strategy == LocatorStrategy.ROLE_NAME
    assert all(not c.is_structural for c in ranked[:2])


def test_conditions_describe_themselves_for_review():
    cond = AllOf(conditions=[TextPresent(text="Summary"),
                            Not(condition=TextPresent(text="not found"))])
    text = describe(cond)
    assert "Summary" in text and "NOT" in text and "AND" in text


def test_review_summary_names_every_step():
    cap = savings_balance_capability()
    summary = cap.review_summary()
    for step in cap.steps:
        assert step.id in summary
    assert "MEMBER_NOT_FOUND" in summary
    assert "checkpoint" in summary


def test_overlay_never_mutates_the_base_capability():
    cap = savings_balance_capability()
    before = cap.model_dump(mode="json")
    overlay = TenantOverlay(tenant_id="t", app_id="meridian_core", base_url="http://x",
                            control_aliases={"Member ID": "Member Number"})
    specialized = overlay.apply(cap)
    assert cap.model_dump(mode="json") == before
    assert specialized is not cap
