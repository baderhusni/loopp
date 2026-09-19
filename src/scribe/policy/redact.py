"""Redaction.

Two rules, and the second is the one that actually holds the line:

1. Declared sensitivity. Parameters and outputs carry a `Sensitivity`, and
   anything marked `pii` or `secret` is masked on its way to disk.

2. Value scrubbing. Every secret the run resolves is registered here, and every
   string written anywhere -- log lines, artifacts, run summaries, error
   messages, model prompts -- is scrubbed for those literal values. Rule 1
   depends on somebody having labelled the field correctly. Rule 2 does not,
   which is why it exists: a passcode that leaks into an error string the app
   rendered was never labelled by anyone.

What this deliberately does *not* claim: screenshots. Pixels cannot be scrubbed
by a regex, so a screenshot inherits the sensitivity of whatever was on screen.
That is handled by capture policy (when screenshots are taken at all), not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..types.core import Sensitivity

# Patterns that matter for US bank back-office data. Ordered longest-first at
# match time so a card number is not partly eaten by a shorter pattern.
DEFAULT_PATTERNS: list[tuple[str, str]] = [
    ("account_number", r"\b(?:SAV|CHK|MMK|CRT|ACCT)-\d{5,9}-\d{2,4}\b"),
    ("ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("card_number", r"\b(?:\d[ -]?){13,19}\b"),
    ("routing_number", r"\b\d{9}\b"),
    ("email", r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    ("phone", r"\b\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b"),
]

MASK = "[REDACTED:{label}]"
SECRET_MASK = "[REDACTED:secret]"


@dataclass
class Redactor:
    patterns: list[tuple[str, str]] = field(default_factory=lambda: list(DEFAULT_PATTERNS))
    extra_terms: dict[str, str] = field(default_factory=dict)  # literal -> label
    enabled: bool = True

    _secrets: set[str] = field(default_factory=set, repr=False)
    _compiled: list[tuple[str, re.Pattern[str]]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._compiled = [(label, re.compile(rx)) for label, rx in self.patterns]

    # -- secret registry -------------------------------------------------
    def register_secret(self, value: str) -> None:
        """Remember a live secret so it can be scrubbed wherever it surfaces."""
        if value and len(value) >= 4:
            self._secrets.add(value)

    def forget_secrets(self) -> None:
        self._secrets.clear()

    # -- scrubbing -------------------------------------------------------
    def text(self, value: str | None) -> str:
        if not value:
            return value or ""
        if not self.enabled:
            return value
        out = value
        # Secrets first and unconditionally.
        for secret in sorted(self._secrets, key=len, reverse=True):
            out = out.replace(secret, SECRET_MASK)
        for literal, label in self.extra_terms.items():
            if literal:
                out = out.replace(literal, MASK.format(label=label))
        for label, rx in self._compiled:
            out = rx.sub(MASK.format(label=label), out)
        return out

    def value(self, raw, sensitivity: Sensitivity):
        """Mask a declared output/parameter value for persistence."""
        if sensitivity == Sensitivity.SECRET:
            return SECRET_MASK
        if sensitivity == Sensitivity.PII:
            return self._partial(str(raw))
        return self.text(str(raw)) if isinstance(raw, str) else raw

    @staticmethod
    def _partial(raw: str) -> str:
        """Keep just enough to correlate a record across logs, and no more."""
        if len(raw) <= 4:
            return "[REDACTED:pii]"
        return f"[REDACTED:pii:…{raw[-4:]}]"

    def mapping(self, data: dict, sensitivities: dict[str, Sensitivity]) -> dict:
        return {
            k: self.value(v, sensitivities.get(k, Sensitivity.INTERNAL))
            for k, v in data.items()
        }

    def structure(self, node):
        """Recursively scrub an arbitrary JSON-ish structure."""
        if isinstance(node, str):
            return self.text(node)
        if isinstance(node, dict):
            return {k: self.structure(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [self.structure(v) for v in node]
        return node
