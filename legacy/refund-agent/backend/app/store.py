"""In-memory CRM data store.

Loads the synthetic CRM (``data/crm.json``) and the refund policy text
(``data/refund_policy.md``) once, and exposes read/lookup helpers plus the two
mutating actions the agent can take: recording a refund and opening an
escalation ticket.

State is in-memory and resets when the server restarts. That is intentional for
a local demo: a refund recorded during a session sticks for the process lifetime
so the "already refunded → deny" rule can be demonstrated live.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"
_CRM_PATH = _DATA_DIR / "crm.json"
_POLICY_PATH = _DATA_DIR / "refund_policy.md"

_lock = threading.Lock()
_seq = {"refund": 0, "ticket": 0}


def _load_crm() -> dict:
    return json.loads(_CRM_PATH.read_text())


# Mutable working copy (deep-copied from disk so we never write back to the file).
_state: dict = _load_crm()
_refunds: list[dict] = []
_escalations: list[dict] = []


def reset() -> None:
    """Reset all in-memory state to the on-disk dataset (used by tests)."""
    global _state, _refunds, _escalations
    with _lock:
        _state = _load_crm()
        _refunds = []
        _escalations = []
        _seq["refund"] = 0
        _seq["ticket"] = 0


def policy_text() -> str:
    return _POLICY_PATH.read_text()


def reference_date() -> str:
    return _state.get("reference_date", "")


# ----------------------------- lookups --------------------------------------

def all_customers() -> list[dict]:
    """Public, non-sensitive view of customers for the admin/demo panel."""
    out = copy.deepcopy(_state["customers"])
    for c in out:
        c.pop("phone", None)  # keep the public view free of PII-shaped fields
    return out


def find_customer(email: str) -> dict | None:
    if not email:
        return None
    needle = email.strip().lower()
    for c in _state["customers"]:
        if c["email"].lower() == needle:
            return c
    return None


def get_order(customer: dict, order_id: str) -> dict | None:
    if not order_id:
        return None
    needle = order_id.strip().upper()
    for o in customer["orders"]:
        if o["id"].upper() == needle:
            return o
    return None


def get_item(order: dict, item_id: str) -> dict | None:
    if not item_id:
        return None
    needle = item_id.strip().upper()
    for it in order["items"]:
        if it["id"].upper() == needle:
            return it
    return None


# ----------------------------- mutations ------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_refund(customer: dict, order: dict, item: dict, amount: float) -> dict | None:
    """Mark an item refunded and record the refund. Caller MUST have confirmed
    policy approval first (issue_refund re-checks before calling this).

    Re-checks the ``refunded`` flag *inside* the lock and returns ``None`` if the
    item was already refunded — this closes the read-then-write race so two
    concurrent approved requests for the same item cannot both record a refund."""
    with _lock:
        if item.get("refunded"):
            return None
        item["refunded"] = True
        _seq["refund"] += 1
        refund = {
            "refund_id": f"RFND-{8000 + _seq['refund']}",
            "customer_id": customer["id"],
            "order_id": order["id"],
            "item_id": item["id"],
            "item_name": item["name"],
            "amount": amount,
            "created_at": _now(),
        }
        _refunds.append(refund)
        return dict(refund)


def open_escalation(customer: dict, order: dict, item: dict | None, reason: str) -> dict:
    with _lock:
        _seq["ticket"] += 1
        ticket = {
            "ticket_id": f"ESC-{9000 + _seq['ticket']}",
            "customer_id": customer["id"],
            "order_id": order["id"] if order else None,
            "item_id": item["id"] if item else None,
            "reason": reason,
            "status": "open",
            "created_at": _now(),
        }
        _escalations.append(ticket)
        return dict(ticket)


def refunds() -> list[dict]:
    return list(_refunds)


def escalations() -> list[dict]:
    return list(_escalations)
