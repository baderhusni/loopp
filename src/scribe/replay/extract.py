"""Reading declared outputs off the screen.

The rule that matters: never address a value by position. Legacy grids get
re-columned and rows get reordered between deployments of the same product, so
a cell is found by the text of its row and the text of its column header, and a
field is found by the caption rendered beside it. An index-based read is a
capability that silently returns the wrong number after a tenant upgrade, which
is worse than one that fails.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..surface.base import normalize_name
from ..types.core import ExtractSource, ExtractSpec, Transform
from ..types.observation import Observation


class ExtractionError(RuntimeError):
    def __init__(self, spec: ExtractSpec, detail: str) -> None:
        super().__init__(f"could not read {spec.output!r}: {detail}")
        self.spec, self.detail = spec, detail


_NUM = re.compile(r"-?[\d,]+(?:\.\d+)?")


def apply_transform(raw: str, transform: Transform) -> Any:
    if transform == Transform.NUMBER:
        m = _NUM.search(raw or "")
        if not m:
            raise ValueError(f"no number in {raw!r}")
        return float(m.group(0).replace(",", ""))
    if transform == Transform.TRIM:
        return (raw or "").strip()
    if transform == Transform.UPPER:
        return (raw or "").strip().upper()
    return raw


def extract(spec: ExtractSpec, obs: Observation, *,
            aliases: Mapping[str, str] | None = None) -> Any:
    aliases = aliases or {}
    raw = _raw_value(spec, obs, aliases)
    if raw is None:
        if spec.required:
            raise ExtractionError(spec, _why_missing(spec, obs))
        return None
    try:
        return apply_transform(raw, spec.transform)
    except ValueError as exc:
        raise ExtractionError(spec, f"{exc} (source text {raw!r})") from exc


def _raw_value(spec: ExtractSpec, obs: Observation, aliases: Mapping[str, str]) -> str | None:
    src = spec.source

    if src == ExtractSource.LABELED_FIELD:
        want = normalize_name(aliases.get(spec.label or "", spec.label or ""))
        for p in obs.pairs:
            if spec.frame_path and p.frame_path != spec.frame_path:
                continue
            if normalize_name(p.label) == want:
                return p.value
        for p in obs.pairs:                       # looser second pass
            if want and want in normalize_name(p.label):
                return p.value
        return None

    if src == ExtractSource.TABLE_CELL:
        row_anchor = normalize_name(aliases.get(spec.row_anchor or "", spec.row_anchor or ""))
        column = normalize_name(aliases.get(spec.column_header or "", spec.column_header or ""))
        for table in obs.tables:
            if spec.table_hint and normalize_name(spec.table_hint) not in normalize_name(
                    table.caption):
                continue
            headers = [normalize_name(h) for h in table.headers]
            if column not in headers:
                continue
            col = headers.index(column)           # by header text, never by index
            for row in table.rows:
                if any(normalize_name(cell) == row_anchor for cell in row):
                    if col < len(row):
                        return row[col]
        return None

    if src in (ExtractSource.CONTROL_TEXT, ExtractSource.CONTROL_VALUE):
        ref = spec.control
        if ref is None:
            return None
        want = normalize_name(aliases.get(ref.name, ref.name))
        for e in obs.elements:
            if ref.role and e.role != ref.role:
                continue
            if ref.frame_path and e.frame_path != ref.frame_path:
                continue
            if want and normalize_name(e.name) != want:
                continue
            return e.value if src == ExtractSource.CONTROL_VALUE else e.name
        return None

    if src == ExtractSource.PAGE_REGEX:
        if not spec.pattern:
            return None
        scope = obs.text
        if spec.frame_path:
            key = "/".join(spec.frame_path)
            scope = obs.frame_texts.get(key, obs.text)
        m = re.search(spec.pattern, scope, re.I | re.M)
        if not m:
            return None
        try:
            return m.group(spec.group)
        except (IndexError, re.error):
            return m.group(0)
    return None


def _why_missing(spec: ExtractSpec, obs: Observation) -> str:
    """Say what *was* there, so the failure is actionable without a re-run."""
    if spec.source == ExtractSource.TABLE_CELL:
        seen = "; ".join(
            f"[{', '.join(t.headers)}] rows={len(t.rows)}" for t in obs.tables) or "no grids"
        return (f"no row matching {spec.row_anchor!r} with column {spec.column_header!r}; "
                f"grids on screen: {seen}")
    if spec.source == ExtractSource.LABELED_FIELD:
        seen = ", ".join(sorted({p.label for p in obs.pairs})[:12]) or "none"
        return f"no field captioned {spec.label!r}; captions on screen: {seen}"
    if spec.source == ExtractSource.PAGE_REGEX:
        return f"pattern /{spec.pattern}/ did not match the visible text"
    ref = spec.control
    return f"control {ref.role if ref else '?'} {ref.name if ref else '?'!r} not found"
