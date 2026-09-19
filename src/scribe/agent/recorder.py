"""Turning a discovery run into a capability.

This is where a transcript becomes a contract, and most of the durability of
the final artifact is decided here rather than by the model:

  * values the caller supplied become parameters, whether or not the model
    remembered to use a placeholder;
  * concrete URLs become templates, so the recording is not welded to the
    tenant it was recorded on;
  * locators come from the observation's computed candidates, not from
    anything the model said;
  * per-step postconditions are *derived* from the state transition that was
    actually observed -- the model is not asked to invent an assertion for a
    screen it has already moved past;
  * anything input-specific is kept out of conditions, because a checkpoint
    that embeds this run's member id would pass exactly once.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..apps.meridian_core import AppProfile
from ..types.actions import Fill, Navigate, Read, Select, StepAction
from ..types.artifact import (
    Approval,
    ApprovalState,
    Capability,
    ErrorPolicy,
    OutputField,
    Param,
    Provenance,
    Step,
)
from ..types.conditions import AllOf, Condition, TextPresent, UrlMatches
from ..types.core import (
    ExtractSource,
    ExtractSpec,
    ParamType,
    ParamValue,
    Sensitivity,
    Transform,
)
from ..types.observation import Observation
from .loop import DiscoveryResult, TraceEntry
from .schema import AgentMove

_PARAM_PLACEHOLDER = re.compile(r"\{\{param:([a-zA-Z_][\w]*)\}\}")

# Output names that carry regulated data. A reviewer confirms these before the
# capability is approved -- the guess only sets the default.
_PII_HINTS = ("name", "ssn", "tax_id", "address", "phone", "email", "dob",
              "birth", "account_number", "card", "member_id")


def extract_spec_from_move(move: AgentMove) -> ExtractSpec | None:
    """Build an extraction spec from a `read` move, if it is well formed."""
    transform = {"none": Transform.NONE, "number": Transform.NUMBER,
                 "trim": Transform.TRIM, "upper": Transform.UPPER}[move.transform]
    if move.source == "labeled_field":
        if not move.label:
            return None
        return ExtractSpec(output=move.output or "value",
                           source=ExtractSource.LABELED_FIELD,
                           label=move.label, transform=transform)
    if move.source == "table_cell":
        if not (move.row_anchor and move.column_header):
            return None
        return ExtractSpec(output=move.output or "value",
                           source=ExtractSource.TABLE_CELL,
                           row_anchor=move.row_anchor, column_header=move.column_header,
                           transform=transform)
    if move.source == "page_regex":
        if not move.pattern:
            return None
        return ExtractSpec(output=move.output or "value",
                           source=ExtractSource.PAGE_REGEX,
                           pattern=move.pattern, transform=transform)
    return None


class ArtifactRecorder:
    def __init__(self, profile: AppProfile, *, base_url: str,
                 tenant_id: str | None = None) -> None:
        self.profile, self.base_url, self.tenant_id = profile, base_url, tenant_id

    # -- main ------------------------------------------------------------
    def build(self, result: DiscoveryResult, *, capability_id: str, name: str,
              params: dict[str, Any], model: str, version: str = "1.0.0") -> Capability:
        steps: list[Step] = []
        outputs: list[OutputField] = []
        used_ids: set[str] = set()
        # Values seen this run, so they can be scrubbed out of derived conditions.
        volatile = {str(v) for v in params.values() if str(v)}

        applied = [e for e in result.trace
                   if e.move and not e.error and e.policy_verdict == "allow"
                   and e.tool not in ("finish", "escalate")]

        for position, entry in enumerate(applied):
            move = entry.move
            action = self._action_for(move, entry, params)
            if action is None:
                continue
            step_id = self._step_id(move, entry, used_ids)
            after = self._observation_after(result, applied, position)
            post = None if move.tool == "read" else self._derive_postcondition(
                entry.observation, after, volatile)
            steps.append(Step(
                id=step_id,
                intent=entry.intent or self._default_intent(move, entry),
                action=action,
                postcondition=post,
                # Enterprise apps are slow before they are broken: one retry with
                # backoff absorbs a sluggish round trip without masking a defect.
                on_error=ErrorPolicy(retries=1, backoff_ms=600),
            ))
            if move.tool == "read":
                outputs.append(self._output_for(move))

        checkpoint = self._checkpoint(result, volatile)
        inputs = [self._param_for(k, v) for k, v in params.items()]

        return Capability(
            id=capability_id,
            version=version,
            name=name,
            description=result.summary or result.goal,
            app=self.profile.binding(self.tenant_id),
            inputs=inputs,
            outputs=outputs,
            preconditions=list(self.profile.login),
            steps=steps,
            recovery=list(self.profile.recovery),
            environment=list(self.profile.environment),
            outcomes=[],          # filled by the probe pass, if one was run
            checkpoint=checkpoint,
            approval=Approval(state=ApprovalState.DRAFT,
                              note="recorded by a discovery run; a human must review "
                                   "the steps, the declared sensitivity of each output, "
                                   "and the risk tier before unattended use"),
            provenance=Provenance(
                recorded_at=datetime.now(timezone.utc),
                recorded_by=model,
                discovery_run_id=result.run_id,
                discovery_goal=result.goal,
                evidence_path=result.evidence_dir,
                steps_proposed=len(result.trace),
                steps_recorded=len(steps)),
            tags=["discovered"],
        )

    # -- steps -----------------------------------------------------------
    def _action_for(self, move: AgentMove, entry: TraceEntry,
                    params: dict[str, Any]) -> StepAction | None:
        if move.tool == "read":
            spec = extract_spec_from_move(move)
            return Read(extract=spec) if spec else None
        if move.tool == "navigate":
            return Navigate(url_template=self._canonical_url(move.url or "", params))
        target = entry.element
        if target is None:
            return None
        ref = _control_ref(entry)
        if move.tool == "click":
            from ..types.actions import Click
            return Click(target=ref)
        if move.tool == "press":
            from ..types.actions import Press
            return Press(key=move.key or "Enter", target=ref)
        value = self._value_for(move.value or "", params)
        if move.tool == "fill":
            return Fill(target=ref, value=value)
        if move.tool == "select":
            return Select(target=ref, value=value)
        return None

    @staticmethod
    def _value_for(raw: str, params: dict[str, Any]):
        """Placeholder, or an exact match against a supplied input, becomes a param.

        The second half matters: a model that types the literal member id instead
        of the placeholder would otherwise produce a capability hard-coded to one
        member, and it would pass its own replay test.
        """
        from ..types.core import LiteralValue

        placeholder = _PARAM_PLACEHOLDER.fullmatch(raw.strip())
        if placeholder:
            return ParamValue(param=placeholder.group(1))
        for key, value in params.items():
            if str(value) and raw.strip() == str(value):
                return ParamValue(param=key)
        return LiteralValue(value=raw)

    def _canonical_url(self, url: str, params: dict[str, Any]) -> str:
        """`http://host/members/detail?f_mid=100412` ->
        `{base_url}/members/detail?f_mid={member_id}`."""
        out = url
        if out.startswith(self.base_url):
            out = "{base_url}" + out[len(self.base_url):]
        else:
            parsed = urlparse(out)
            if parsed.scheme:
                out = "{base_url}" + (parsed.path or "/") + (
                    f"?{parsed.query}" if parsed.query else "")
        for key, value in params.items():
            if str(value):
                out = out.replace(str(value), "{" + key + "}")
        return out

    @staticmethod
    def _step_id(move: AgentMove, entry: TraceEntry, used: set[str]) -> str:
        base = (entry.intent or "").strip().lower()
        if not base:
            noun = (entry.element.name if entry.element else move.output or move.tool)
            base = f"{move.tool} {noun}"
        slug = re.sub(r"[^a-z0-9]+", "_", base).strip("_")[:44] or move.tool
        candidate, n = slug, 2
        while candidate in used:
            candidate, n = f"{slug}_{n}", n + 1
        used.add(candidate)
        return candidate

    @staticmethod
    def _default_intent(move: AgentMove, entry: TraceEntry) -> str:
        what = entry.element.name if entry.element else (move.output or move.url or "")
        return {"click": f"click {what}", "fill": f"enter a value into {what}",
                "select": f"choose an option in {what}", "press": f"press {move.key}",
                "navigate": f"open {what}",
                "read": f"read {move.output}"}.get(move.tool, move.tool)

    @staticmethod
    def _observation_after(result: DiscoveryResult, applied: list[TraceEntry],
                           position: int) -> Observation | None:
        """The screen the *next* move saw -- i.e. the result of this one."""
        if position + 1 < len(applied):
            return applied[position + 1].observation
        return result.final_observation

    def _derive_postcondition(self, before: Observation | None,
                              after: Observation | None,
                              volatile: set[str]) -> Condition | None:
        """Assert the transition that was actually observed.

        Two signals, both taken from evidence rather than from the model: the
        URL moved, and text appeared that was not there before. Anything
        carrying a value from this run is discarded -- it would pin the
        capability to one input.
        """
        if after is None:
            return None
        parts: list[Condition] = []

        if before is not None and _path(before.url) != _path(after.url):
            pattern = re.escape(_path(after.url))
            for value in volatile:
                pattern = pattern.replace(re.escape(value), r"[^&/]+")
            parts.append(UrlMatches(pattern=pattern))

        marker = _new_marker_text(before, after, volatile)
        if marker:
            parts.append(TextPresent(text=marker))

        if not parts:
            return None
        return parts[0] if len(parts) == 1 else AllOf(conditions=parts)

    # -- contract --------------------------------------------------------
    @staticmethod
    def _param_for(name: str, value: Any) -> Param:
        text = str(value)
        pattern = None
        ptype = ParamType.STRING
        if text.isdigit():
            # Fixed-width numeric ids are the norm on these systems; pinning the
            # width turns a typo into a rejected call instead of a wasted session.
            pattern = rf"^\d{{{len(text)}}}$"
        sensitivity = (Sensitivity.PII if any(h in name.lower() for h in _PII_HINTS)
                       else Sensitivity.INTERNAL)
        return Param(name=name, type=ptype,
                     description=f"{name.replace('_', ' ')} supplied by the caller",
                     pattern=pattern, example=text, sensitivity=sensitivity)

    @staticmethod
    def _output_for(move: AgentMove) -> OutputField:
        otype = ParamType.NUMBER if move.transform == "number" else ParamType.STRING
        name = move.output or "value"
        sensitivity = (Sensitivity.PII if any(h in name.lower() for h in _PII_HINTS)
                       else Sensitivity.INTERNAL)
        return OutputField(name=name, type=otype,
                           description=move.description or name.replace("_", " "),
                           sensitivity=sensitivity)

    def _checkpoint(self, result: DiscoveryResult, volatile: set[str]) -> Condition | None:
        """Use the model's checkpoint only if it is visible and input-independent."""
        text = (result.checkpoint_text or "").strip()
        final = result.final_observation
        if text and final and text.lower() in final.text.lower():
            if not any(v and v in text for v in volatile):
                return TextPresent(text=text)
        marker = _new_marker_text(None, final, volatile) if final else None
        return TextPresent(text=marker) if marker else None


def _control_ref(entry: TraceEntry):
    element = entry.element
    from ..types.core import ControlRef

    return ControlRef(role=element.role, name=element.name,
                      frame_path=element.frame_path,
                      candidates=list(element.candidates),
                      description=f"{element.role} named {element.name!r}")


def _path(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")


def _new_marker_text(before: Observation | None, after: Observation | None,
                     volatile: set[str]) -> str | None:
    """Pick a short, stable line that the transition introduced.

    Headings win: they are what an operator would point at to say "I'm on the
    right screen", and they survive a tenant's data differing.
    """
    if after is None:
        return None
    old = set()
    if before is not None:
        old = {line.strip() for line in before.text.splitlines() if line.strip()}
    candidates: list[tuple[int, str]] = []
    for position, raw in enumerate(after.text.splitlines()):
        line = raw.strip()
        if not line or line in old or line.startswith("---"):
            continue
        if "\t" in raw:
            # Rendered text separates table cells with tabs, so a line with one
            # is a data row ("Member Name\tDolores Vance"), not a heading. Those
            # make terrible assertions: they are the values that change per input.
            continue
        if len(line) > 60 or len(line) < 4:
            continue
        if any(v and v in line for v in volatile):
            continue          # carries this run's input; useless as an assertion
        if sum(c.isdigit() for c in line) > 3:
            continue          # looks like data, not a label
        candidates.append((position, line))
    if not candidates:
        return None
    # Prefer a title-cased line, then the earliest one: headings render above
    # the content they head, so document order is a decent proxy for "this names
    # the screen" over "this is something on the screen".
    candidates.sort(key=lambda item: (not item[1].istitle(), item[0]))
    return candidates[0][1]
