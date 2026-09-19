"""The agent-facing surface."""

from __future__ import annotations

import json
from pathlib import Path

from fixtures import savings_balance_capability
from scribe.catalog import Catalog, load_capability, save_capability
from scribe.types.artifact import ApprovalState


def build(tmp_path: Path, *, approved: bool = True) -> Catalog:
    cap = savings_balance_capability(approved=approved)
    save_capability(cap, tmp_path / "meridian_core" / "balance.json")
    return Catalog(tmp_path)


def test_a_capability_round_trips_through_the_store(tmp_path):
    cap = savings_balance_capability()
    path = save_capability(cap, tmp_path / "c.json")
    assert load_capability(path).model_dump(mode="json") == cap.model_dump(mode="json")


def test_tool_schemas_are_directly_usable_by_a_calling_agent(tmp_path):
    tools = build(tmp_path).as_anthropic_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "meridian_core__member_savings_balance"
    assert tool["input_schema"]["required"] == ["member_id"]
    # What it returns and how it can legitimately not succeed are part of the
    # description, so an agent can plan around both without calling it first.
    assert "savings_balance" in tool["description"]
    assert "MEMBER_NOT_FOUND" in tool["description"]
    json.dumps(tools)          # must be plain JSON


def test_drafts_are_not_offered_to_agents(tmp_path):
    catalog = build(tmp_path, approved=False)
    assert catalog.as_anthropic_tools() == []
    assert len(catalog.as_anthropic_tools(approved_only=False)) == 1


def test_lookup_by_id_prefers_the_highest_version(tmp_path):
    older = savings_balance_capability()
    older.version = "1.0.0"
    newer = savings_balance_capability()
    newer.version = "1.10.0"
    save_capability(older, tmp_path / "a.json")
    save_capability(newer, tmp_path / "b.json")
    catalog = Catalog(tmp_path)
    assert catalog.get(older.id).capability.version == "1.10.0"
    assert catalog.get(older.id, "1.0.0").capability.version == "1.0.0"


def test_overlays_are_not_mistaken_for_capabilities(tmp_path):
    build(tmp_path)
    overlays = tmp_path / "meridian_core" / "overlays"
    overlays.mkdir(parents=True)
    (overlays / "t.json").write_text(json.dumps({
        "tenant_id": "riverbend-fcu", "app_id": "meridian_core",
        "base_url": "http://127.0.0.1:8800",
        "control_aliases": {"Member ID": "Member Number"}}))
    catalog = Catalog(tmp_path)
    assert len(catalog.entries()) == 1
    assert catalog.overlay_for("meridian_core.member_savings_balance",
                               "riverbend-fcu").alias("Member ID") == "Member Number"


def test_the_shipped_catalog_is_loadable_and_approved():
    """The capability committed to this repo -- recorded by a real discovery run."""
    repo = Path(__file__).resolve().parents[1]
    catalog = Catalog(repo / "capabilities")
    entries = catalog.entries()
    assert entries, "no capability committed"
    cap = entries[0].capability
    assert cap.approval.state == ApprovalState.APPROVED
    assert cap.provenance.discovery_run_id
    assert cap.outcome_codes() == ["MEMBER_NOT_FOUND"]
    assert "Tr0ubadour" not in json.dumps(cap.model_dump(mode="json"))
