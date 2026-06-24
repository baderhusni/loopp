"""Agent tools — the only way the agent can touch CRM data or take an action.

Each tool is a plain Python function returning a JSON-serialisable dict. The SDK
binding (in ``agent.py``) wraps these with the ``@tool`` decorator; the mock
engine calls them directly. Keeping the logic SDK-free means the backend and its
tests run with zero external dependencies.

The refund guardrail lives here, not just in the prompt: ``check_refund_eligibility``
and ``issue_refund`` both call the deterministic policy engine, and ``issue_refund``
*refuses* (returns an error) for anything the engine does not APPROVE. A pleading
or prompt-injecting customer therefore cannot force an unauthorized refund — the
model can ask for it, but the tool will not perform it.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from . import policy, store, trace


# ----------------------------- helpers --------------------------------------

def _resolve(email: str, order_id: str | None = None, item_id: str | None = None):
    """Resolve customer/order/item, returning (objs, error_dict|None)."""
    customer = store.find_customer(email)
    if not customer:
        return (None, None, None), {"error": f"No customer found with email '{email}'. "
                                             "Ask the customer to confirm the email on their account."}
    order = None
    if order_id is not None:
        order = store.get_order(customer, order_id)
        if not order:
            return (customer, None, None), {
                "error": f"Order '{order_id}' was not found under {customer['email']}. "
                         "Ask the customer to re-check the order number."}
    item = None
    if order is not None and item_id is not None:
        item = store.get_item(order, item_id)
        if not item:
            return (customer, order, None), {
                "error": f"Item '{item_id}' was not found in order {order['id']}."}
    return (customer, order, item), None


def _order_summary(order: dict) -> dict:
    total = round(sum(float(i["unit_price"]) * int(i.get("quantity", 1)) for i in order["items"]), 2)
    return {
        "id": order["id"],
        "status": order["status"],
        "placed_at": order["placed_at"],
        "delivered_at": order.get("delivered_at"),
        "item_count": len(order["items"]),
        "order_total": total,
    }


def _item_view(item: dict) -> dict:
    return {
        "id": item["id"],
        "name": item["name"],
        "sku": item["sku"],
        "category": item["category"],
        "unit_price": item["unit_price"],
        "quantity": item.get("quantity", 1),
        "line_amount": round(float(item["unit_price"]) * int(item.get("quantity", 1)), 2),
        "final_sale": item.get("final_sale", False),
        "refunded": item.get("refunded", False),
    }


# ----------------------------- tool logic -----------------------------------

def find_customer(email: str) -> dict:
    (customer, _, _), err = _resolve(email)
    if err:
        return err
    return {
        "customer": {
            "id": customer["id"],
            "name": customer["name"],
            "email": customer["email"],
            "tier": customer["tier"],
            "member_since": customer["member_since"],
            "orders": [_order_summary(o) for o in customer["orders"]],
        }
    }


def get_order(email: str, order_id: str) -> dict:
    (customer, order, _), err = _resolve(email, order_id)
    if err:
        return err
    return {
        "order": {
            **_order_summary(order),
            "items": [_item_view(i) for i in order["items"]],
        }
    }


def check_refund_eligibility(email: str, order_id: str, item_id: str, defective: bool = False) -> dict:
    """Read-only policy check. A DENY/ESCALATE here is a *successful* check, not
    a tool error — it is the policy verdict the agent must follow."""
    (customer, order, item), err = _resolve(email, order_id, item_id)
    if err:
        return err
    decision = policy.evaluate(order, item, bool(defective))
    return {
        "item": {"id": item["id"], "name": item["name"], "line_amount": decision.amount},
        "defective_claimed": bool(defective),
        "decision": decision.to_dict(),
    }


def issue_refund(email: str, order_id: str, item_id: str, defective: bool = False) -> dict:
    """Perform a refund — but ONLY if the policy engine approves. Any non-APPROVE
    verdict is returned as an error so the refund cannot be forced."""
    (customer, order, item), err = _resolve(email, order_id, item_id)
    if err:
        return err
    decision = policy.evaluate(order, item, bool(defective))
    if decision.decision != policy.APPROVE:
        return {
            "error": f"REFUND BLOCKED by policy [{decision.rule}]: {decision.reason} "
                     f"(verdict: {decision.decision}). The agent must not refund this item; "
                     f"{'escalate to a human' if decision.decision == policy.ESCALATE else 'deny the request'}.",
            "decision": decision.to_dict(),
        }
    refund = store.record_refund(customer, order, item, decision.amount)
    return {"refunded": True, "refund": refund, "decision": decision.to_dict()}


def escalate_to_human(email: str, order_id: str, item_id: str | None = None, reason: str = "") -> dict:
    (customer, order, item), err = _resolve(email, order_id, item_id)
    # item is optional for escalation; ignore an item-not-found error if no item_id given.
    if err and not (item_id is None and err["error"].startswith("Item")):
        if customer is None or order is None:
            return err
    ticket = store.open_escalation(customer, order, item, reason or "Customer refund request requires human review.")
    return {"escalated": True, "ticket": ticket}


# ----------------------------- registry -------------------------------------

ToolFn = Callable[..., dict]


class ToolSpec:
    def __init__(self, name: str, description: str, input_schema: dict, fn: ToolFn):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.fn = fn


TOOLS: list[ToolSpec] = [
    ToolSpec(
        "find_customer",
        "Look up a customer by their email address and list their orders. Use this "
        "first to verify the customer's identity before discussing any order.",
        {
            "type": "object",
            "properties": {"email": {"type": "string", "description": "Customer email on the account"}},
            "required": ["email"],
        },
        find_customer,
    ),
    ToolSpec(
        "get_order",
        "Get the items and details of one order belonging to a verified customer, "
        "including price, quantity, final-sale status, and whether each item was already refunded.",
        {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "order_id": {"type": "string", "description": "e.g. ORD-5001"},
            },
            "required": ["email", "order_id"],
        },
        get_order,
    ),
    ToolSpec(
        "check_refund_eligibility",
        "Apply the refund policy to one item and get the verdict (APPROVE, DENY, or "
        "ESCALATE) with the rule behind it. ALWAYS call this before issuing a refund. "
        "Set defective=true only if the customer credibly reports the item is defective/damaged.",
        {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "order_id": {"type": "string"},
                "item_id": {"type": "string", "description": "e.g. ITEM-1"},
                "defective": {"type": "boolean", "description": "True if the customer reports the item defective/damaged"},
            },
            "required": ["email", "order_id", "item_id"],
        },
        check_refund_eligibility,
    ),
    ToolSpec(
        "issue_refund",
        "Process a refund for an item. This will SUCCEED only if the policy engine "
        "approves; otherwise it returns an error and no refund is made. Never tell a "
        "customer a refund is done unless this tool returned refunded=true.",
        {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "order_id": {"type": "string"},
                "item_id": {"type": "string"},
                "defective": {"type": "boolean"},
            },
            "required": ["email", "order_id", "item_id"],
        },
        issue_refund,
    ),
    ToolSpec(
        "escalate_to_human",
        "Open a human-review ticket for cases the agent cannot resolve itself: refunds "
        "over $500, defective final-sale items, or cancelling an undelivered order.",
        {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "order_id": {"type": "string"},
                "item_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why this needs a human"},
            },
            "required": ["email", "order_id", "reason"],
        },
        escalate_to_human,
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def run_tool(name: str, tool_input: dict[str, Any]) -> tuple[dict, bool]:
    """Execute a tool by name, timing it and recording the call to the active run
    trace. Returns (result_dict, is_error)."""
    spec = TOOLS_BY_NAME.get(name)
    started = time.perf_counter()
    if spec is None:
        result = {"error": f"Unknown tool '{name}'."}
        is_error = True
    else:
        try:
            result = spec.fn(**tool_input)
            is_error = bool(result.get("error"))
        except TypeError as exc:
            result = {"error": f"Invalid arguments for {name}: {exc}"}
            is_error = True
        except Exception as exc:  # defensive: never crash the agent loop
            result = {"error": f"Internal error in {name}: {exc}"}
            is_error = True
    latency_ms = (time.perf_counter() - started) * 1000
    trace.record_tool(name, tool_input, result, latency_ms, is_error)
    return result, is_error
