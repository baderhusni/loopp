"""Generate the synthetic CRM dataset (15 customers + order histories).

Reproducible: all dates are anchored to ``REFERENCE_DATE`` (the project's pinned
"today") so the policy windows behave identically every run, regardless of the
real wall-clock date. Re-run with:

    python backend/app/data/generate_crm.py

The hand-curated "hero" customers (C1001-C1007) deliberately cover every branch
of the refund policy so the agent can be demoed against each rule. The remaining
customers are filler with ordinary, in-window orders.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

# The project pins "today" to this date so the demo is deterministic.
# Keep in sync with backend/app/policy.py (SUPPORT_TODAY default).
REFERENCE_DATE = date(2026, 6, 24)
OUT_PATH = Path(__file__).with_name("crm.json")


def days_ago(n: int) -> str:
    return (REFERENCE_DATE - timedelta(days=n)).isoformat()


def item(item_id, name, sku, category, unit_price, qty=1, *, final_sale=False, refunded=False):
    return {
        "id": item_id,
        "name": name,
        "sku": sku,
        "category": category,
        "unit_price": unit_price,
        "quantity": qty,
        "final_sale": final_sale,
        "refunded": refunded,
    }


def order(order_id, placed_days_ago, status, delivered_days_ago, items):
    return {
        "id": order_id,
        "placed_at": days_ago(placed_days_ago),
        "status": status,
        "delivered_at": days_ago(delivered_days_ago) if delivered_days_ago is not None else None,
        "items": items,
    }


def customer(cid, name, email, phone, tier, member_since_days_ago, orders):
    return {
        "id": cid,
        "name": name,
        "email": email,
        "phone": phone,
        "tier": tier,
        "member_since": days_ago(member_since_days_ago),
        "orders": orders,
    }


def build() -> list[dict]:
    customers: list[dict] = []

    # C1001 — Alice: normal in-window item (APPROVE) + a final-sale clearance item (DENY).
    customers.append(customer(
        "CUST-1001", "Alice Nguyen", "alice@example.com", "+1-202-555-0101", "gold", 700,
        [order("ORD-5001", 16, "delivered", 12, [
            item("ITEM-1", "Wireless Headphones", "WH-220", "electronics", 129.00),
            item("ITEM-2", "Clearance Phone Case", "PC-CLR-9", "accessories", 19.99, final_sale=True),
        ])],
    ))

    # C1002 — Bob: high-value laptop, in window (ESCALATE > $500).
    customers.append(customer(
        "CUST-1002", "Bob Martinez", "bob@example.com", "+1-202-555-0102", "platinum", 1200,
        [order("ORD-5002", 20, "delivered", 14, [
            item("ITEM-1", "UltraBook Pro 14", "LAP-1299", "electronics", 1299.00),
        ])],
    ))

    # C1003 — Carol: delivered 70 days ago (DENY: outside 30-day and 60-day windows).
    customers.append(customer(
        "CUST-1003", "Carol Smith", "carol@example.com", "+1-202-555-0103", "standard", 900,
        [order("ORD-5003", 74, "delivered", 70, [
            item("ITEM-1", "Cotton Throw Blanket", "HOME-88", "home", 79.50),
        ])],
    ))

    # C1004 — David: delivered 40 days ago (DENY normally, APPROVE only if defective ≤60d).
    customers.append(customer(
        "CUST-1004", "David Lee", "david@example.com", "+1-202-555-0104", "standard", 500,
        [order("ORD-5004", 44, "delivered", 40, [
            item("ITEM-1", "Desk Lamp", "LAMP-12", "home", 59.00),
        ])],
    ))

    # C1005 — Emma: item already refunded (DENY: duplicate refund).
    customers.append(customer(
        "CUST-1005", "Emma Wilson", "emma@example.com", "+1-202-555-0105", "gold", 800,
        [order("ORD-5005", 18, "delivered", 13, [
            item("ITEM-1", "Bluetooth Speaker", "SPK-50", "electronics", 49.99, refunded=True),
        ])],
    ))

    # C1006 — Frank: order shipped, not yet delivered (ESCALATE: cancellation).
    customers.append(customer(
        "CUST-1006", "Frank Brown", "frank@example.com", "+1-202-555-0106", "standard", 300,
        [order("ORD-5006", 3, "shipped", None, [
            item("ITEM-1", "Running Shoes", "SHOE-200", "apparel", 119.99),
        ])],
    ))

    # C1007 — Grace: gift card, final sale (DENY; ESCALATE only if defective).
    customers.append(customer(
        "CUST-1007", "Grace Park", "grace@example.com", "+1-202-555-0107", "standard", 220,
        [order("ORD-5007", 10, "delivered", 7, [
            item("ITEM-1", "$100 Digital Gift Card", "GC-100", "gift_card", 100.00, final_sale=True),
            item("ITEM-2", "Phone Charger", "CHG-9", "electronics", 24.99),
        ])],
    ))

    # ---- Filler customers (C1008-C1015): ordinary, mostly in-window orders. ----
    fillers = [
        ("CUST-1008", "Hannah Kim", "hannah@example.com", "+1-202-555-0108", "standard",
         "ORD-5008", 14, 9, [("Yoga Mat", "YOGA-7", "fitness", 34.99, 1, False, False)]),
        ("CUST-1009", "Ian Foster", "ian@example.com", "+1-202-555-0109", "gold",
         "ORD-5009", 22, 18, [("Coffee Grinder", "GRND-3", "kitchen", 89.00, 1, False, False),
                              ("Travel Mug", "MUG-2", "kitchen", 18.50, 2, False, False)]),
        ("CUST-1010", "Julia Reyes", "julia@example.com", "+1-202-555-0110", "platinum",
         "ORD-5010", 9, 5, [("Smart Watch", "WATCH-9", "electronics", 249.00, 1, False, False)]),
        ("CUST-1011", "Kevin O'Neil", "kevin@example.com", "+1-202-555-0111", "standard",
         "ORD-5011", 30, 25, [("Backpack", "BAG-4", "apparel", 64.00, 1, False, False)]),
        ("CUST-1012", "Lena Petrov", "lena@example.com", "+1-202-555-0112", "gold",
         "ORD-5012", 12, 8, [("Cookware Set", "COOK-1", "kitchen", 199.00, 1, False, False)]),
        ("CUST-1013", "Marcus Hall", "marcus@example.com", "+1-202-555-0113", "standard",
         "ORD-5013", 6, 2, [("Wireless Mouse", "MOU-6", "electronics", 29.99, 1, False, False)]),
        ("CUST-1014", "Nina Castro", "nina@example.com", "+1-202-555-0114", "standard",
         "ORD-5014", 27, 21, [("Sunglasses", "SUN-3", "accessories", 89.00, 1, False, False)]),
        ("CUST-1015", "Omar Haddad", "omar@example.com", "+1-202-555-0115", "platinum",
         "ORD-5015", 11, 6, [("Tablet 10\"", "TAB-10", "electronics", 399.00, 1, False, False)]),
    ]
    for (cid, name, email, phone, tier, oid, placed, delivered, items_spec) in fillers:
        items = [
            item(f"ITEM-{i+1}", nm, sku, cat, price, qty, final_sale=fs, refunded=rf)
            for i, (nm, sku, cat, price, qty, fs, rf) in enumerate(items_spec)
        ]
        customers.append(customer(cid, name, email, phone, tier, 400, [
            order(oid, placed, "delivered", delivered, items),
        ]))

    return customers


def main() -> None:
    data = {
        "reference_date": REFERENCE_DATE.isoformat(),
        "currency": "USD",
        "customers": build(),
    }
    OUT_PATH.write_text(json.dumps(data, indent=2) + "\n")
    n = len(data["customers"])
    print(f"Wrote {n} customers to {OUT_PATH}")
    assert n == 15, f"expected 15 customers, got {n}"


if __name__ == "__main__":
    main()
