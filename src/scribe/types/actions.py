"""The action vocabulary.

One vocabulary serves both halves of the system: it is what the discovery model
is allowed to emit, and it is what a recorded step contains. Keeping them
identical is what makes recording a transcription rather than a translation --
there is no second, lossier representation to keep in sync.

``finish`` and ``escalate`` are loop control, not steps; they never appear in a
saved capability.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field

from .core import ControlRef, ExtractSpec, Strict, ValueSpec
from .conditions import Condition


class Navigate(Strict):
    """Go to a URL. Templated with ``{base_url}`` and ``{param}`` placeholders.

    Recorded in canonical form: a concrete ``/members/detail?f_mid=100412``
    becomes ``{base_url}/members/detail?f_mid={member_id}`` so the capability is
    not welded to the record-time tenant or the record-time input.
    """

    kind: Literal["navigate"] = "navigate"
    url_template: str


class Click(Strict):
    kind: Literal["click"] = "click"
    target: ControlRef


class Fill(Strict):
    kind: Literal["fill"] = "fill"
    target: ControlRef
    value: ValueSpec
    clear_first: bool = True


class Select(Strict):
    kind: Literal["select"] = "select"
    target: ControlRef
    value: ValueSpec


class Press(Strict):
    kind: Literal["press"] = "press"
    key: str
    target: ControlRef | None = None


class WaitFor(Strict):
    kind: Literal["wait_for"] = "wait_for"
    condition: Condition
    timeout_ms: int = 10_000


class Read(Strict):
    """Pull a declared output off the current screen."""

    kind: Literal["read"] = "read"
    extract: ExtractSpec


StepAction = Annotated[
    Union[Navigate, Click, Fill, Select, Press, WaitFor, Read],
    Field(discriminator="kind"),
]

# Action kinds an allowlist can name. Kept as plain strings so policy files stay
# readable and do not import code.
ACTION_KINDS = ("navigate", "click", "fill", "select", "press", "wait_for", "read")

# Which action kinds can change state on the far side. Used by the policy engine
# to decide what needs a risk decision and what is just looking around.
MUTATING_KINDS = frozenset({"click", "fill", "select", "press"})


def describe_action(action: StepAction) -> str:
    k = action.kind
    if k == "navigate":
        return f"navigate to {action.url_template}"
    if k == "click":
        return f'click {action.target.role} "{action.target.name}"'
    if k in ("fill", "select"):
        v = action.value
        shown = {"literal": lambda: f'"{v.value}"', "param": lambda: f"<{v.param}>",
                 "secret": lambda: f"<secret:{v.secret}>"}[v.kind]()
        verb = "type into" if k == "fill" else "select in"
        return f'{verb} {action.target.role} "{action.target.name}" = {shown}'
    if k == "press":
        where = f' in "{action.target.name}"' if action.target else ""
        return f"press {action.key}{where}"
    if k == "wait_for":
        from .conditions import describe
        return f"wait for {describe(action.condition)}"
    if k == "read":
        return f"read {action.extract.output} via {action.extract.source.value}"
    return k
