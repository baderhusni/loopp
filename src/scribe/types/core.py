"""Shared value types used across artifacts, observations, and results."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    """Base model: reject unknown keys so a malformed artifact fails loudly."""

    model_config = ConfigDict(extra="forbid", frozen=False)


class Sensitivity(str, Enum):
    """Drives redaction. Applied to parameters, outputs, and captured text."""

    PUBLIC = "public"        # safe anywhere
    INTERNAL = "internal"    # fine in logs, not for third parties
    PII = "pii"              # names, addresses, account numbers -> masked in evidence
    SECRET = "secret"        # credentials/tokens -> never written down at all


class RiskTier(str, Enum):
    """How much damage an action can do if it fires when it should not."""

    SAFE = "safe"              # read-only navigation and inspection
    CAUTION = "caution"        # writes that a person can undo from the UI
    IRREVERSIBLE = "irreversible"  # moves money, opens/closes accounts, notifies members


class SurfaceKind(str, Enum):
    WEB = "web"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"
    TERMINAL = "terminal"


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


# --------------------------------------------------------------------------
# Values: what gets typed into a field.
# --------------------------------------------------------------------------
class LiteralValue(Strict):
    """A constant baked into the capability at record time."""

    kind: Literal["literal"] = "literal"
    value: str


class ParamValue(Strict):
    """Supplied by the caller on every invocation."""

    kind: Literal["param"] = "param"
    param: str


class SecretValue(Strict):
    """Resolved from the secret provider at act time.

    The name is all that is ever persisted. The value never enters an artifact,
    a log line, a screenshot annotation, or an LLM prompt.
    """

    kind: Literal["secret"] = "secret"
    secret: str


ValueSpec = Annotated[
    Union[LiteralValue, ParamValue, SecretValue], Field(discriminator="kind")
]


# --------------------------------------------------------------------------
# Targeting: how a control is found again on the next run.
# --------------------------------------------------------------------------
class LocatorStrategy(str, Enum):
    """Ordered roughly by how well each survives a redeploy of the same app.

    The first four are semantic: they describe the control the way an operator
    would. The last three are structural, recorded only as a last resort, and
    a replay that has to fall back to one of them is reported as a drift signal.
    """

    ROLE_NAME = "role_name"            # accessible role + accessible name
    LABEL_ADJACENT = "label_adjacent"  # legacy: the <td>/text sitting next to the field
    LINK_TEXT = "link_text"            # anchor by its visible text
    FIELD_NAME = "field_name"          # the form control's name= attribute
    TABLE_CELL = "table_cell"          # row anchor + column header
    CSS = "css"                        # brittle; ids here are often per-render
    XPATH = "xpath"                    # brittle
    COORDINATES = "coordinates"        # normalized viewport point; pixel-only surfaces


# Priors used when the recorder has no better evidence. Higher is more durable.
STRATEGY_CONFIDENCE: dict[LocatorStrategy, float] = {
    LocatorStrategy.ROLE_NAME: 0.95,
    LocatorStrategy.LABEL_ADJACENT: 0.85,
    LocatorStrategy.LINK_TEXT: 0.80,
    LocatorStrategy.FIELD_NAME: 0.70,
    LocatorStrategy.TABLE_CELL: 0.75,
    LocatorStrategy.CSS: 0.25,
    LocatorStrategy.XPATH: 0.20,
    LocatorStrategy.COORDINATES: 0.10,
}

# Anything at or below this is structural: usable, but a replay that lands on
# one records a drift warning so the capability gets looked at.
STRUCTURAL_CONFIDENCE_CEILING = 0.30


class LocatorCandidate(Strict):
    """One way to find a control, plus why we believe in it."""

    strategy: LocatorStrategy
    # Shape depends on the strategy: {"role","name"} for ROLE_NAME,
    # {"label"} for LABEL_ADJACENT, {"selector"} for CSS, and so on.
    args: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.5
    note: str = ""

    @property
    def is_structural(self) -> bool:
        return self.confidence <= STRUCTURAL_CONFIDENCE_CEILING


class ControlRef(Strict):
    """A control, described semantically first and structurally only as backup.

    ``frame_path`` matters more than it looks: on a frameset app "the page" is
    several documents, and the same control name can exist in more than one.
    """

    role: str
    name: str = ""
    frame_path: list[str] = Field(default_factory=list)
    candidates: list[LocatorCandidate] = Field(default_factory=list)
    description: str = ""

    def ranked(self) -> list[LocatorCandidate]:
        return sorted(self.candidates, key=lambda c: -c.confidence)


# --------------------------------------------------------------------------
# Extraction: how a value is read off the screen.
# --------------------------------------------------------------------------
class ExtractSource(str, Enum):
    CONTROL_TEXT = "control_text"      # visible text of a referenced control
    CONTROL_VALUE = "control_value"    # current value of an input
    TABLE_CELL = "table_cell"          # row anchor x column header
    LABELED_FIELD = "labeled_field"    # caption cell -> the value cell beside it
    PAGE_REGEX = "page_regex"          # regex over visible frame text


class Transform(str, Enum):
    NONE = "none"
    NUMBER = "number"        # strip thousands separators/currency -> float
    TRIM = "trim"
    UPPER = "upper"


class ExtractSpec(Strict):
    """Reads one declared output.

    ``TABLE_CELL`` is the interesting one. Legacy grids get re-columned between
    deployments of the same product, so a cell is addressed by the text of its
    row and the text of its column header -- never by index.
    """

    output: str
    source: ExtractSource
    control: ControlRef | None = None
    label: str | None = None           # for LABELED_FIELD
    row_anchor: str | None = None
    column_header: str | None = None
    table_hint: str | None = None
    pattern: str | None = None
    group: int = 1
    transform: Transform = Transform.NONE
    frame_path: list[str] = Field(default_factory=list)
    required: bool = True
