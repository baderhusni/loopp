"""The action vocabulary the discovery model is allowed to emit.

One list of tools, shared by both LLM backends, so swapping the Anthropic SDK
for the local CLI cannot silently change what the model is able to do.

The model points at controls by the ephemeral `ref` it just saw in the
observation (`e6`), never by a selector. That split is deliberate and it is
what makes the recording durable: the model says *which thing on screen*, and
the recorder -- which also holds that observation, including every locator
candidate computed for that element -- writes down *how to find it again*.
A model that guessed at CSS would be inventing locators it has no way to
evaluate.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TOOL_NAMES = ("navigate", "click", "fill", "select", "press", "read",
              "finish", "escalate")


class AgentMove(BaseModel):
    """One validated decision from the model."""

    model_config = ConfigDict(extra="ignore")

    thought: str = ""
    tool: Literal[TOOL_NAMES]  # type: ignore[valid-type]

    # targeting
    ref: str | None = None
    url: str | None = None
    value: str | None = None
    key: str | None = None

    # read
    output: str | None = None
    source: Literal["labeled_field", "table_cell", "page_regex"] | None = None
    label: str | None = None
    row_anchor: str | None = None
    column_header: str | None = None
    pattern: str | None = None
    transform: Literal["none", "number", "trim", "upper"] = "none"
    description: str | None = None

    # finish / escalate
    intent: str | None = None
    checkpoint_text: str | None = None
    summary: str | None = None
    reason: str | None = None

    def needs_ref(self) -> bool:
        return self.tool in ("click", "fill", "select")


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    base = {
        "thought": {"type": "string",
                    "description": "One sentence: why this action, right now."},
        "intent": {"type": "string",
                   "description": "What an operator would say they are doing, e.g. "
                                  "'run the member inquiry'. Recorded as the step name."},
    }
    return {"type": "object", "properties": {**base, **props},
            "required": ["thought", *required], "additionalProperties": False}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "navigate",
        "description": "Go straight to a URL. Prefer clicking what is on screen; "
                       "use this only for an entry point you can see is correct.",
        "input_schema": _schema(
            {"url": {"type": "string", "description": "Absolute URL."}}, ["url"]),
    },
    {
        "name": "click",
        "description": "Click a control listed in CONTROLS, by its ref.",
        "input_schema": _schema(
            {"ref": {"type": "string", "description": "A ref from CONTROLS, e.g. 'e6'."}},
            ["ref"]),
    },
    {
        "name": "fill",
        "description": "Type into a text field. When the text is one of the task "
                       "parameters, pass the placeholder {{param:name}} rather than "
                       "the literal value, so the recording works for any input.",
        "input_schema": _schema(
            {"ref": {"type": "string"},
             "value": {"type": "string",
                       "description": "Literal text, or {{param:member_id}}."}},
            ["ref", "value"]),
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown, by its visible label.",
        "input_schema": _schema(
            {"ref": {"type": "string"}, "value": {"type": "string"}}, ["ref", "value"]),
    },
    {
        "name": "press",
        "description": "Press a key, optionally focused on a control.",
        "input_schema": _schema(
            {"key": {"type": "string", "description": "e.g. 'Enter'."},
             "ref": {"type": "string"}}, ["key"]),
    },
    {
        "name": "read",
        "description": (
            "Capture a value the task is supposed to return. Address it the way a "
            "person would: by the caption beside it (labeled_field) or by its row "
            "and column headings (table_cell). Never by position -- other "
            "institutions running this product reorder the columns."),
        "input_schema": _schema(
            {"output": {"type": "string",
                        "description": "snake_case name for the returned field."},
             "description": {"type": "string",
                             "description": "What this value means, for the caller."},
             "source": {"type": "string",
                        "enum": ["labeled_field", "table_cell", "page_regex"]},
             "label": {"type": "string",
                       "description": "labeled_field: the caption shown beside the value."},
             "row_anchor": {"type": "string",
                            "description": "table_cell: text identifying the row, "
                                           "e.g. 'SAVINGS'."},
             "column_header": {"type": "string",
                               "description": "table_cell: the column heading, "
                                              "e.g. 'Balance'."},
             "pattern": {"type": "string",
                         "description": "page_regex: a regex with one capture group."},
             "transform": {"type": "string", "enum": ["none", "number", "trim", "upper"]}},
            ["output", "source", "description"]),
    },
    {
        "name": "finish",
        "description": "The goal is met. Give the condition that proves it.",
        "input_schema": _schema(
            {"checkpoint_text": {
                "type": "string",
                "description": "Text visible on this screen, and only on this screen, "
                               "that proves the goal was reached. Prefer a stable "
                               "heading over a sentence containing a specific value."},
             "summary": {"type": "string",
                         "description": "One line describing what this capability does."}},
            ["checkpoint_text", "summary"]),
    },
    {
        "name": "escalate",
        "description": "You cannot safely proceed. Hand off to a human operator.",
        "input_schema": _schema(
            {"reason": {"type": "string",
                        "description": "What is blocking you, and what a person should do."}},
            ["reason"]),
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def parse_move(tool: str, payload: dict[str, Any]) -> AgentMove:
    return AgentMove(tool=tool, **{k: v for k, v in payload.items() if k != "tool"})


def tools_as_text() -> str:
    """The tool menu rendered for a backend that has no native tool calling."""
    import json

    lines = []
    for t in TOOLS:
        lines.append(f"- {t['name']}: {t['description']}")
        lines.append(f"  input: {json.dumps(t['input_schema']['properties'])}")
        lines.append(f"  required: {t['input_schema']['required']}")
    return "\n".join(lines)
