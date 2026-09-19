"""A small, serializable predicate language.

Checkpoints, business-outcome detectors, step pre/postconditions, and recovery
triggers are all conditions. They are *data*, never code or expressions to be
evaluated: an artifact is something a compliance reviewer reads and a machine
replays, and neither of those survives ``eval`` in a JSON file. The cost is
expressiveness; the payoff is that a capability is auditable and cannot smuggle
behaviour past the guardrails.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field

from .core import ControlRef, Strict


class UrlMatches(Strict):
    """Regex against the top-level document URL."""

    kind: Literal["url_matches"] = "url_matches"
    pattern: str


class TextPresent(Strict):
    """Visible text somewhere in the document tree.

    ``frame_path`` empty means "any frame" -- the right default on a frameset
    app, where the content you care about is never in the top document.
    """

    kind: Literal["text_present"] = "text_present"
    text: str
    regex: bool = False
    case_sensitive: bool = False
    frame_path: list[str] = Field(default_factory=list)


class TextAbsent(Strict):
    kind: Literal["text_absent"] = "text_absent"
    text: str
    regex: bool = False
    case_sensitive: bool = False
    frame_path: list[str] = Field(default_factory=list)


class ControlPresent(Strict):
    """A control with this role and name exists (optionally: is enabled)."""

    kind: Literal["control_present"] = "control_present"
    control: ControlRef
    enabled: bool | None = None


class OutputMatches(Strict):
    """An already-extracted output matches a pattern. Used for value checkpoints."""

    kind: Literal["output_matches"] = "output_matches"
    output: str
    pattern: str


class HttpStatusIn(Strict):
    """Status of the last main-frame response."""

    kind: Literal["http_status_in"] = "http_status_in"
    statuses: list[int]


class AllOf(Strict):
    kind: Literal["all_of"] = "all_of"
    conditions: list["Condition"]


class AnyOf(Strict):
    kind: Literal["any_of"] = "any_of"
    conditions: list["Condition"]


class Not(Strict):
    kind: Literal["not"] = "not"
    condition: "Condition"


Condition = Annotated[
    Union[
        UrlMatches,
        TextPresent,
        TextAbsent,
        ControlPresent,
        OutputMatches,
        HttpStatusIn,
        AllOf,
        AnyOf,
        Not,
    ],
    Field(discriminator="kind"),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()


def describe(cond: "Condition") -> str:
    """One-line human rendering, for logs, failure reports, and review diffs."""
    k = cond.kind
    if k == "url_matches":
        return f"URL matches /{cond.pattern}/"
    if k == "text_present":
        where = "/".join(cond.frame_path) or "any frame"
        return f'text "{cond.text}" present in {where}'
    if k == "text_absent":
        where = "/".join(cond.frame_path) or "any frame"
        return f'text "{cond.text}" absent from {where}'
    if k == "control_present":
        state = "" if cond.enabled is None else (
            " and enabled" if cond.enabled else " and disabled")
        return f'control {cond.control.role} "{cond.control.name}" present{state}'
    if k == "output_matches":
        return f"output {cond.output} matches /{cond.pattern}/"
    if k == "http_status_in":
        return f"HTTP status in {cond.statuses}"
    if k == "all_of":
        return "(" + " AND ".join(describe(c) for c in cond.conditions) + ")"
    if k == "any_of":
        return "(" + " OR ".join(describe(c) for c in cond.conditions) + ")"
    if k == "not":
        return f"NOT {describe(cond.condition)}"
    return k
