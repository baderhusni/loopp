"""Evaluating the condition language against a live observation.

Pure and side-effect free: give it an observation and it answers true or false,
and explains itself. The explanation is not decoration -- when a checkpoint
fails, `explain()` is what ends up in the failure report, and it is the
difference between "step 4 failed" and "expected the member summary heading,
found the sign-on screen".

Tenant text aliases are applied here, so a deployment that words a message
differently does not need its conditions rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

from ..types.conditions import Condition, describe
from ..types.observation import Observation


@dataclass
class EvalContext:
    observation: Observation
    outputs: dict[str, Any] = field(default_factory=dict)
    text_aliases: Mapping[str, str] = field(default_factory=dict)
    control_aliases: Mapping[str, str] = field(default_factory=dict)

    def scope_text(self, frame_path: list[str]) -> str:
        if not frame_path:
            return self.observation.text
        key = "/".join(frame_path)
        # Exact frame first, then any frame whose path ends with the requested
        # name -- frame nesting differs between deployments more often than
        # frame names do.
        if key in self.observation.frame_texts:
            return self.observation.frame_texts[key]
        for k, v in self.observation.frame_texts.items():
            if k.endswith(frame_path[-1]):
                return v
        return self.observation.text


def _contains(haystack: str, needle: str, *, regex: bool, case_sensitive: bool) -> bool:
    if regex:
        return re.search(needle, haystack, 0 if case_sensitive else re.I) is not None
    return (needle in haystack) if case_sensitive else (needle.casefold() in haystack.casefold())


def evaluate(cond: Condition, ctx: EvalContext) -> bool:
    k = cond.kind
    obs = ctx.observation

    if k == "url_matches":
        return re.search(cond.pattern, obs.url) is not None

    if k in ("text_present", "text_absent"):
        needle = ctx.text_aliases.get(cond.text, cond.text)
        hit = _contains(ctx.scope_text(cond.frame_path), needle,
                        regex=cond.regex, case_sensitive=cond.case_sensitive)
        return hit if k == "text_present" else not hit

    if k == "control_present":
        want_role = cond.control.role
        want_name = ctx.control_aliases.get(cond.control.name, cond.control.name)
        want_frame = cond.control.frame_path
        from ..surface.base import normalize_name
        target = normalize_name(want_name)
        for e in obs.elements:
            if want_role and e.role != want_role:
                continue
            if want_frame and e.frame_path != want_frame:
                continue
            if target and normalize_name(e.name) != target:
                continue
            if cond.enabled is not None and e.enabled != cond.enabled:
                continue
            return True
        return False

    if k == "output_matches":
        value = ctx.outputs.get(cond.output)
        return value is not None and re.search(cond.pattern, str(value)) is not None

    if k == "http_status_in":
        return obs.http_status in cond.statuses

    if k == "all_of":
        return all(evaluate(c, ctx) for c in cond.conditions)
    if k == "any_of":
        return any(evaluate(c, ctx) for c in cond.conditions)
    if k == "not":
        return not evaluate(cond.condition, ctx)
    raise ValueError(f"unknown condition kind {k!r}")


def explain(cond: Condition, ctx: EvalContext) -> str:
    """Why the condition came out the way it did, in one line."""
    ok = evaluate(cond, ctx)
    return f"{'MET' if ok else 'NOT MET'}: {describe(cond)}"


def observed_summary(obs: Observation, limit: int = 220) -> str:
    """A short 'what was actually on screen' for failure reports."""
    head = " / ".join(
        line.strip() for line in obs.text.splitlines()
        if line.strip() and not line.startswith("---"))[:limit]
    return f"url={obs.url} title={obs.title!r} :: {head}"
