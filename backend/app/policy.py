"""Deterministic refund policy engine.

This module encodes the rules from ``data/refund_policy.md`` as code. It is the
**authoritative guardrail**: the agent consults it via the ``check_refund_eligibility``
tool, and the ``issue_refund`` tool re-runs it before mutating anything, so an
unauthorized refund cannot be pushed through even if the model is jailbroken by a
pleading or prompt-injecting customer.

"Today" is pinned to ``SUPPORT_TODAY`` (default 2026-06-24) so the demo is
deterministic regardless of the real wall-clock date. Keep the default in sync
with ``data/generate_crm.py``'s ``REFERENCE_DATE``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from datetime import date

APPROVE = "APPROVE"
DENY = "DENY"
ESCALATE = "ESCALATE"

STANDARD_WINDOW_DAYS = 30
DEFECTIVE_WINDOW_DAYS = 60
ESCALATION_THRESHOLD = 500.00


def support_today() -> date:
    raw = os.getenv("SUPPORT_TODAY", "2026-06-24")
    return date.fromisoformat(raw)


@dataclass
class Decision:
    decision: str  # APPROVE | DENY | ESCALATE
    rule: str  # short policy rule id/name behind the decision
    reason: str  # human-readable explanation for the customer/agent
    amount: float  # refund amount evaluated, USD
    window_days: int  # window that applied (30 or 60)
    days_since_delivery: int | None

    def to_dict(self) -> dict:
        return asdict(self)


def _amount(item: dict) -> float:
    return round(float(item["unit_price"]) * int(item.get("quantity", 1)), 2)


def evaluate(order: dict, item: dict, defective: bool) -> Decision:
    """Apply the refund policy to a resolved order+item.

    Caller (the tool layer) is responsible for verifying the customer/order/item
    exist and belong to the customer (Rule 1). This function applies Rules 2-7 in
    the precedence order documented in refund_policy.md.
    """
    amount = _amount(item)
    today = support_today()

    delivered_at = order.get("delivered_at")
    days_since = None
    if delivered_at:
        days_since = (today - date.fromisoformat(delivered_at)).days

    # Rule 4 (status): only delivered orders are refundable; otherwise escalate.
    if order.get("status") != "delivered" or not delivered_at:
        return Decision(
            ESCALATE, "R4_not_delivered",
            f"Order {order['id']} is '{order.get('status')}', not yet delivered. "
            "A cancellation must be handled by a human.",
            amount, STANDARD_WINDOW_DAYS, days_since,
        )

    # Rule 5 (duplicate): already refunded.
    if item.get("refunded"):
        return Decision(
            DENY, "R5_already_refunded",
            "This item has already been refunded and cannot be refunded again.",
            amount, STANDARD_WINDOW_DAYS, days_since,
        )

    # Rule 3 (final sale): never auto-refundable; escalate only if defective.
    if item.get("final_sale"):
        if defective:
            return Decision(
                ESCALATE, "R3_final_sale_defective",
                "This is a final-sale item reported defective. The agent cannot "
                "refund final-sale items; a human will review for a replacement.",
                amount, DEFECTIVE_WINDOW_DAYS, days_since,
            )
        return Decision(
            DENY, "R3_final_sale",
            "This is a final-sale item (e.g. gift card, digital, clearance, or "
            "personalized) and is never refundable.",
            amount, STANDARD_WINDOW_DAYS, days_since,
        )

    # Rule 2 (window): 30 days standard, 60 if defective.
    window = DEFECTIVE_WINDOW_DAYS if defective else STANDARD_WINDOW_DAYS
    if days_since is not None and days_since > window:
        return Decision(
            DENY, "R2_outside_window",
            f"The request is {days_since} days after delivery, outside the "
            f"{window}-day {'defective ' if defective else ''}return window.",
            amount, window, days_since,
        )

    # Rule 6 (high value): over $500 requires human approval.
    if amount > ESCALATION_THRESHOLD:
        return Decision(
            ESCALATE, "R6_high_value",
            f"The refund amount ${amount:.2f} exceeds the ${ESCALATION_THRESHOLD:.0f} "
            "limit and requires human approval.",
            amount, window, days_since,
        )

    # Rule 7: all conditions satisfied — agent may auto-approve.
    return Decision(
        APPROVE, "R7_approved",
        f"Eligible for an automatic refund of ${amount:.2f}: delivered, within "
        f"the {window}-day window, not final sale, not previously refunded, and "
        "at or under the $500 limit.",
        amount, window, days_since,
    )
