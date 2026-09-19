"""The capability catalog.

An artifact is only useful if an agent can find it and call it, so the catalog
does double duty: it is the store on disk, and it is the tool surface an LLM
agent sees. `as_anthropic_tools()` emits exactly what you would pass in a
`tools=[...]` list -- the artifact's own declared inputs become the tool schema,
because they were designed to be a call contract rather than a step list.

Draft capabilities are listed but flagged, and the replay engine refuses to run
them unattended. Discovery produces drafts; a human promotes them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..types.artifact import ApprovalState, Capability
from ..types.overlay import TenantOverlay


def save_capability(cap: Capability, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cap.model_dump(mode="json"), indent=2), encoding="utf-8")
    return out


def load_capability(path: str | Path) -> Capability:
    return Capability.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_overlay(path: str | Path) -> TenantOverlay:
    return TenantOverlay.model_validate_json(Path(path).read_text(encoding="utf-8"))


@dataclass
class CatalogEntry:
    capability: Capability
    path: Path

    @property
    def callable_unattended(self) -> bool:
        return self.capability.approval.state == ApprovalState.APPROVED


class Catalog:
    """Capabilities on disk, plus per-tenant overlays under `overlays/`."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def entries(self) -> list[CatalogEntry]:
        out: list[CatalogEntry] = []
        for path in sorted(self.root.rglob("*.json")):
            if "overlays" in path.parts:
                continue
            try:
                out.append(CatalogEntry(load_capability(path), path))
            except Exception:
                continue        # not a capability; the catalog is not a validator
        return out

    def __iter__(self) -> Iterator[CatalogEntry]:
        return iter(self.entries())

    def get(self, capability_id: str, version: str | None = None) -> CatalogEntry:
        matches = [e for e in self.entries() if e.capability.id == capability_id
                   and (version is None or e.capability.version == version)]
        if not matches:
            raise KeyError(f"no capability {capability_id!r}"
                           + (f" at version {version}" if version else ""))
        matches.sort(key=lambda e: _semver(e.capability.version), reverse=True)
        return matches[0]

    def overlays(self, capability_id: str) -> dict[str, TenantOverlay]:
        out: dict[str, TenantOverlay] = {}
        for path in sorted(self.root.rglob("overlays/*.json")):
            try:
                overlay = load_overlay(path)
            except Exception:
                continue
            out[overlay.tenant_id] = overlay
        return out

    def overlay_for(self, capability_id: str, tenant_id: str) -> TenantOverlay | None:
        return self.overlays(capability_id).get(tenant_id)

    # -- agent-facing surface --------------------------------------------
    def as_anthropic_tools(self, *, approved_only: bool = True) -> list[dict[str, Any]]:
        """Capabilities as tool definitions an agent can be handed directly."""
        tools = []
        for entry in self.entries():
            cap = entry.capability
            if approved_only and not entry.callable_unattended:
                continue
            outcomes = ", ".join(cap.outcome_codes()) or "none declared"
            tools.append({
                "name": cap.id.replace(".", "__"),
                "description": (
                    f"{cap.description}\n\n"
                    f"Application: {cap.app.vendor_product} {cap.app.product_version}. "
                    f"Returns: "
                    + ", ".join(f"{o.name} ({o.type.value})" for o in cap.outputs)
                    + f". Known non-success outcomes: {outcomes}."),
                "input_schema": cap.input_schema(),
            })
        return tools

    def describe(self) -> str:
        rows = []
        for entry in self.entries():
            cap = entry.capability
            state = cap.approval.state.value
            mark = "*" if entry.callable_unattended else "-"
            rows.append(
                f" {mark} {cap.id}@{cap.version}  [{state}] "
                f"risk={cap.max_step_risk().value}\n"
                f"     {cap.description[:100]}\n"
                f"     in: {', '.join(p.name for p in cap.inputs) or '-'}"
                f"  out: {', '.join(o.name for o in cap.outputs) or '-'}"
                f"  outcomes: {', '.join(cap.outcome_codes()) or '-'}")
        if not rows:
            return "(no capabilities recorded yet)"
        return ("capabilities (* = approved for unattended use)\n"
                + "\n".join(rows))


def _semver(v: str) -> tuple:
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return (0,)
