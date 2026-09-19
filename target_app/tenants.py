"""Per-tenant configuration for MERIDIAN CORE.

Both tenants run the *same vendor product* (MERIDIAN CORE 4.2) with different
branding, field wording, and table column order -- the situation the brief
describes, where hundreds of institutions run the same underlying software
configured differently. `riverbend` is deliberately the awkward one: it renames
the two controls a capability depends on and reorders the accounts grid.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    institution: str
    accent: str
    # Wording that differs between deployments of the same product.
    member_id_label: str
    search_button: str
    accounts_heading: str
    # Column order of the accounts grid, by header text.
    account_columns: tuple[str, ...]
    # Some deployments bolt on a post-login notice the operator must dismiss.
    login_interstitial: str | None = None
    product: str = "MERIDIAN CORE"
    product_version: str = "4.2"
    extras: dict[str, str] = field(default_factory=dict)


NORTHSTAR = Tenant(
    tenant_id="northstar-cu",
    institution="NORTHSTAR CREDIT UNION",
    accent="#1f3864",
    member_id_label="Member ID",
    search_button="Search",
    accounts_heading="Share &amp; Deposit Accounts",
    account_columns=("Type", "Account Number", "Balance", "Status"),
)

RIVERBEND = Tenant(
    tenant_id="riverbend-fcu",
    institution="RIVERBEND FEDERAL CU",
    accent="#5b2333",
    member_id_label="Member Number",
    search_button="Find Member",
    accounts_heading="Deposit Relationships",
    account_columns=("Account Number", "Type", "Status", "Balance"),
    login_interstitial="Regional compliance notice: member data access is logged.",
)

TENANTS = {t.tenant_id: t for t in (NORTHSTAR, RIVERBEND)}
DEFAULT_TENANT = NORTHSTAR.tenant_id
