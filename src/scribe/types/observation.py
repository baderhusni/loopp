"""What the system sees, on any surface.

This is the seam. A web surface fills these in from the accessibility tree plus
the DOM; a desktop surface would fill the same shapes from UI Automation or the
AX API; a pixel-only surface from OCR plus coordinates. Everything above this
file -- the agent loop, the recorder, the replay engine, the policy checks --
is written against `Observation` and `UIElement` and has never heard of a CSS
selector.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .core import LocatorCandidate, Strict


class UIElement(Strict):
    """One interactable or readable control, described semantically."""

    ref: str                       # stable within one observation, e.g. "e12"
    role: str                      # textbox | button | link | combobox | cell | ...
    name: str                      # accessible name, as a person would read it
    value: str = ""
    enabled: bool = True
    focused: bool = False
    frame_path: list[str] = Field(default_factory=list)
    # Where the name came from. Matters on legacy markup, where a name that had
    # to be inferred from a neighbouring table cell is weaker evidence than one
    # the app actually declared.
    name_source: str = ""          # aria | label | text | value | table_adjacent | placeholder
    bbox: tuple[float, float, float, float] | None = None
    candidates: list[LocatorCandidate] = Field(default_factory=list)

    def render(self) -> str:
        """Compact one-line form -- this is what the model actually reads."""
        bits = [f"[{self.ref}]", self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.value:
            bits.append(f"value={self.value!r}")
        if not self.enabled:
            bits.append("(disabled)")
        if self.frame_path:
            bits.append(f"frame={'/'.join(self.frame_path)}")
        return " ".join(bits)


class TableSnapshot(Strict):
    """Grids get their own representation.

    Flattening a table into prose loses exactly the structure an extraction
    needs, and legacy apps put everything in tables.
    """

    caption: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    frame_path: list[str] = Field(default_factory=list)

    def render(self, max_rows: int = 12) -> str:
        out = [f"TABLE {self.caption}".rstrip(), "  | " + " | ".join(self.headers) + " |"]
        for r in self.rows[:max_rows]:
            out.append("  | " + " | ".join(r) + " |")
        if len(self.rows) > max_rows:
            out.append(f"  ... {len(self.rows) - max_rows} more rows")
        return "\n".join(out)


class FieldPair(Strict):
    """A caption and the value rendered next to it."""

    label: str
    value: str
    frame_path: list[str] = Field(default_factory=list)


class Observation(Strict):
    """A single perception of the application state."""

    url: str
    title: str = ""
    http_status: int | None = None
    elements: list[UIElement] = Field(default_factory=list)
    tables: list[TableSnapshot] = Field(default_factory=list)
    pairs: list[FieldPair] = Field(default_factory=list)
    text: str = ""                 # visible text, all frames, newline-joined
    frame_texts: dict[str, str] = Field(default_factory=dict)
    frames: list[str] = Field(default_factory=list)
    screenshot_path: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    def by_ref(self, ref: str) -> UIElement | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def render(self, max_text: int = 1800) -> str:
        """The textual view handed to the model.

        Deliberately not a DOM dump. The model gets what an operator gets: where
        it is, what it can touch, what the grids say, and what the screen reads.
        """
        parts = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.frames:
            parts.append(f"FRAMES: {', '.join(self.frames)}")
        if self.elements:
            parts.append("CONTROLS:")
            parts += [f"  {e.render()}" for e in self.elements]
        if self.tables:
            parts.append("TABLES:")
            parts += [t.render() for t in self.tables]
        if self.pairs:
            parts.append("FIELDS:")
            parts += [f"  {p.label}: {p.value}" for p in self.pairs if p.value]
        body = self.text.strip()
        if len(body) > max_text:
            body = body[:max_text] + f"\n... [{len(self.text) - max_text} more chars]"
        parts += ["SCREEN TEXT:", body]
        return "\n".join(parts)
