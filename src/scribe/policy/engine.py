"""The guardrail.

One chokepoint. `Executor.perform` and the discovery loop both call
`PolicyEngine.check` before anything reaches the surface, and neither has a code
path that skips it. Prompt-level instructions to the model are not a guardrail;
they are a suggestion to a system whose whole job is to improvise.

Three answers: ALLOW, DENY, REQUIRE_APPROVAL. The third is what makes the human
handoff part of the safety model rather than a separate feature -- a risky step
does not fail, it goes to a person.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ..types.actions import MUTATING_KINDS, StepAction
from ..types.core import RiskTier
from .redact import DEFAULT_PATTERNS, Redactor


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class Decision:
    verdict: Verdict
    reason: str = ""
    risk: RiskTier = RiskTier.SAFE
    rule: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict == Verdict.ALLOW

    def __str__(self) -> str:
        return f"{self.verdict.value}({self.risk.value}){': ' + self.reason if self.reason else ''}"


class SecretNotConfigured(RuntimeError):
    pass


@dataclass
class AppPolicy:
    app_id: str
    allowed_origins: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)
    irreversible_controls: list[str] = field(default_factory=list)
    caution_controls: list[str] = field(default_factory=list)
    on_irreversible: str = "require_approval"
    on_caution: str = "allow"
    secrets: dict[str, str] = field(default_factory=dict)


@dataclass
class PolicyEngine:
    apps: dict[str, AppPolicy]
    max_steps: int = 24
    max_runtime_seconds: int = 240
    unattended_replay_requires_approval: bool = True
    screenshot_policy: str = "on_failure"
    redactor: Redactor = field(default_factory=Redactor)

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "PolicyEngine":
        raw = yaml.safe_load(Path(path).read_text()) or {}
        d = raw.get("defaults", {})
        apps: dict[str, AppPolicy] = {}
        for app_id, cfg in (raw.get("apps") or {}).items():
            risk = cfg.get("risk", {}) or {}
            apps[app_id] = AppPolicy(
                app_id=app_id,
                allowed_origins=cfg.get("allowed_origins", []),
                allowed_paths=cfg.get("allowed_paths", []),
                denied_paths=cfg.get("denied_paths", []),
                allowed_actions=cfg.get("allowed_actions", []),
                irreversible_controls=risk.get("irreversible_controls", []),
                caution_controls=risk.get("caution_controls", []),
                on_irreversible=risk.get("on_irreversible", "require_approval"),
                on_caution=risk.get("on_caution", "allow"),
                secrets=cfg.get("secrets", {}) or {},
            )
        red = raw.get("redaction", {}) or {}
        redactor = Redactor(
            patterns=[*DEFAULT_PATTERNS,
                      *[(p["name"], p["regex"]) for p in red.get("extra_patterns", [])]],
            enabled=red.get("enabled", True))
        return cls(
            apps=apps,
            max_steps=int(d.get("max_steps", 24)),
            max_runtime_seconds=int(d.get("max_runtime_seconds", 240)),
            unattended_replay_requires_approval=bool(
                d.get("unattended_replay_requires_approval", True)),
            screenshot_policy=d.get("screenshot_policy", "on_failure"),
            redactor=redactor,
        )

    def app(self, app_id: str) -> AppPolicy:
        if app_id not in self.apps:
            raise KeyError(
                f"no policy for app {app_id!r}; add it to the policy file before "
                f"running anything against it")
        return self.apps[app_id]

    # -- URL allowlist ---------------------------------------------------
    def check_url(self, app_id: str, url: str) -> Decision:
        pol = self.app(app_id)
        try:
            parsed = urlparse(url)
        except ValueError:
            return Decision(Verdict.DENY, f"unparseable URL {url!r}", rule="url.parse")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in pol.allowed_origins:
            return Decision(Verdict.DENY,
                            f"origin {origin} is not in the allowlist for {app_id}",
                            rule="url.origin")
        path = parsed.path or "/"
        for rx in pol.denied_paths:
            if re.search(rx, path):
                return Decision(Verdict.DENY, f"path {path} matches deny rule /{rx}/",
                                rule="url.denied_path")
        if pol.allowed_paths and not any(re.search(rx, path) for rx in pol.allowed_paths):
            return Decision(Verdict.DENY, f"path {path} is not in the allowlist",
                            rule="url.allowed_path")
        return Decision(Verdict.ALLOW, rule="url")

    # -- risk ------------------------------------------------------------
    def classify_risk(self, app_id: str, action: StepAction,
                      control_name: str = "") -> RiskTier:
        """Decide risk from what the control says on screen, at act time."""
        if action.kind not in MUTATING_KINDS:
            return RiskTier.SAFE
        pol = self.app(app_id)
        name = (control_name or getattr(getattr(action, "target", None), "name", "") or "")
        norm = name.strip().casefold()
        if not norm:
            return RiskTier.CAUTION if action.kind == "click" else RiskTier.SAFE
        for pattern in pol.irreversible_controls:
            if pattern.casefold() in norm:
                return RiskTier.IRREVERSIBLE
        for pattern in pol.caution_controls:
            if pattern.casefold() in norm:
                return RiskTier.CAUTION
        # Typing into a field is not, by itself, a state change on the far side.
        return RiskTier.SAFE if action.kind in ("fill", "select") else RiskTier.CAUTION

    # -- the gate --------------------------------------------------------
    def check(self, app_id: str, action: StepAction, *, control_name: str = "",
              url: str | None = None) -> Decision:
        pol = self.app(app_id)
        if pol.allowed_actions and action.kind not in pol.allowed_actions:
            return Decision(Verdict.DENY,
                            f"action {action.kind!r} is not permitted on {app_id}",
                            rule="action.kind")
        if action.kind == "navigate":
            target = url or getattr(action, "url_template", "")
            verdict = self.check_url(app_id, target)
            if not verdict.allowed:
                return verdict

        risk = self.classify_risk(app_id, action, control_name)
        if risk == RiskTier.IRREVERSIBLE:
            mode = pol.on_irreversible
            if mode == "block":
                return Decision(Verdict.DENY,
                                f'"{control_name}" is an irreversible control and policy '
                                f"blocks it", risk=risk, rule="risk.irreversible")
            if mode == "require_approval":
                return Decision(Verdict.REQUIRE_APPROVAL,
                                f'"{control_name}" commits an irreversible change; '
                                f"a human must approve it", risk=risk,
                                rule="risk.irreversible")
        if risk == RiskTier.CAUTION and pol.on_caution == "require_approval":
            return Decision(Verdict.REQUIRE_APPROVAL,
                            f'"{control_name}" changes state', risk=risk, rule="risk.caution")
        return Decision(Verdict.ALLOW, risk=risk, rule="risk")

    # -- secrets ---------------------------------------------------------
    def resolve_secret(self, app_id: str, name: str) -> str:
        """Resolve at act time, register for scrubbing, never persist."""
        pol = self.app(app_id)
        spec = pol.secrets.get(name)
        if not spec:
            raise SecretNotConfigured(
                f"secret {name!r} is not declared in policy for {app_id}")
        if spec.startswith("env:"):
            var = spec[4:]
            value = os.environ.get(var)
            if value is None:
                raise SecretNotConfigured(
                    f"secret {name!r} maps to ${var}, which is not set")
        else:
            raise SecretNotConfigured(
                f"unsupported secret source {spec!r}; only env: is allowed")
        self.redactor.register_secret(value)
        return value

    def summary(self) -> dict[str, Any]:
        return {
            "apps": sorted(self.apps),
            "max_steps": self.max_steps,
            "max_runtime_seconds": self.max_runtime_seconds,
            "unattended_replay_requires_approval": self.unattended_replay_requires_approval,
            "screenshot_policy": self.screenshot_policy,
        }
