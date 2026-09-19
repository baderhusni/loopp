"""Guardrails and data handling.

The point of these is not that the happy path is allowed; it is that the
refusals actually refuse, in the places where a model or a mis-edited artifact
would otherwise get through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scribe.policy import PolicyEngine, SecretNotConfigured, Verdict
from scribe.policy.redact import Redactor
from scribe.types.actions import Click, Fill, Navigate
from scribe.types.core import ControlRef, LiteralValue, RiskTier, Sensitivity

POLICY = Path(__file__).resolve().parents[1] / "policies" / "policy.yaml"


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine.load(POLICY)


def button(name: str) -> Click:
    return Click(target=ControlRef(role="button", name=name))


# -- allowlist -------------------------------------------------------------
def test_allowed_origin_and_path(policy):
    assert policy.check_url("meridian_core",
                            "http://127.0.0.1:8799/members/search").allowed


def test_offsite_origin_is_denied(policy):
    decision = policy.check_url("meridian_core", "https://evil.example/members/search")
    assert decision.verdict == Verdict.DENY
    assert "allowlist" in decision.reason


def test_control_plane_is_denied_even_on_an_allowed_origin(policy):
    """The fixture's own fault-injection endpoint is not part of the app."""
    decision = policy.check_url("meridian_core", "http://127.0.0.1:8799/__fault")
    assert decision.verdict == Verdict.DENY


def test_unknown_app_is_refused_rather_than_defaulted(policy):
    with pytest.raises(KeyError):
        policy.check_url("some_other_core", "http://127.0.0.1:8799/")


def test_action_kinds_outside_the_allowlist_are_denied(policy):
    policy.app("meridian_core").allowed_actions = ["navigate", "read"]
    assert policy.check("meridian_core", button("Search"),
                        control_name="Search").verdict == Verdict.DENY


# -- risk ------------------------------------------------------------------
def test_irreversible_control_requires_a_human(policy):
    decision = policy.check("meridian_core", button("Confirm & Open Account"),
                            control_name="Confirm & Open Account")
    assert decision.verdict == Verdict.REQUIRE_APPROVAL
    assert decision.risk == RiskTier.IRREVERSIBLE


def test_risk_is_decided_by_policy_not_by_the_artifact(policy):
    """An artifact that claims a dangerous step is 'safe' must not get through.

    Risk is classified at act time from what the control is called on screen,
    so editing a recorded risk tier buys nothing.
    """
    assert policy.classify_risk("meridian_core", button("Post Transaction"),
                                "Post Transaction") == RiskTier.IRREVERSIBLE


def test_typing_into_a_field_is_not_a_state_change(policy):
    action = Fill(target=ControlRef(role="textbox", name="Member ID"),
                  value=LiteralValue(value="100412"))
    assert policy.classify_risk("meridian_core", action, "Member ID") == RiskTier.SAFE


def test_blocking_mode_denies_outright(policy):
    policy.app("meridian_core").on_irreversible = "block"
    assert policy.check("meridian_core", button("Close Account"),
                        control_name="Close Account").verdict == Verdict.DENY


# -- secrets ---------------------------------------------------------------
def test_secret_resolution_registers_for_scrubbing(policy, monkeypatch):
    monkeypatch.setenv("MERIDIAN_PASSCODE", "hunter2-not-real")
    value = policy.resolve_secret("meridian_core", "core_passcode")
    assert value == "hunter2-not-real"
    assert policy.redactor.text("logged in with hunter2-not-real") == \
        "logged in with [REDACTED:secret]"


def test_undeclared_secret_is_refused(policy):
    with pytest.raises(SecretNotConfigured):
        policy.resolve_secret("meridian_core", "some_other_password")


def test_secret_from_an_unset_env_var_fails_loudly(policy, monkeypatch):
    monkeypatch.delenv("MERIDIAN_PASSCODE", raising=False)
    with pytest.raises(SecretNotConfigured, match="not set"):
        policy.resolve_secret("meridian_core", "core_passcode")


# -- redaction -------------------------------------------------------------
def test_regulated_patterns_are_masked():
    r = Redactor()
    masked = r.text("acct SAV-0100412-01 ssn 123-45-6789 mail a.b@c.com")
    assert "SAV-0100412-01" not in masked
    assert "123-45-6789" not in masked
    assert "a.b@c.com" not in masked


def test_secret_scrubbing_does_not_depend_on_anyone_labelling_the_field():
    """The backstop: a secret that leaks into unrelated text is still caught."""
    r = Redactor()
    r.register_secret("Tr0ubadour!")
    leaked = "core said: invalid passcode 'Tr0ubadour!' for operator svc"
    assert "Tr0ubadour!" not in r.text(leaked)


def test_pii_values_keep_only_a_correlation_tail():
    r = Redactor()
    assert r.value("Dolores Vance", Sensitivity.PII).startswith("[REDACTED:pii")
    assert r.value("secret", Sensitivity.SECRET) == "[REDACTED:secret]"
    assert r.value(4182.55, Sensitivity.INTERNAL) == 4182.55


def test_nested_structures_are_scrubbed_recursively():
    r = Redactor()
    r.register_secret("Tr0ubadour!")
    out = r.structure({"a": ["Tr0ubadour!", {"b": "SAV-0100412-01"}], "n": 1})
    assert out["a"][0] == "[REDACTED:secret]"
    assert out["a"][1]["b"] == "[REDACTED:account_number]"
    assert out["n"] == 1
