"""Synthetic fixture data for MERIDIAN CORE.

Everything here is fabricated. No real member, account, or institution data is
used anywhere in this project.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Literal

AccountKind = Literal["SAVINGS", "CHECKING", "MONEY MARKET", "CERTIFICATE"]


@dataclass
class Account:
    kind: AccountKind
    number: str
    balance: float
    status: str = "OPEN"


@dataclass
class Member:
    member_id: str
    name: str
    status: str
    branch: str
    accounts: list[Account] = field(default_factory=list)
    # When true the app answers member lookups with a permission denial. Stands
    # in for the real thing: privacy holds, employee accounts, legal freezes.
    restricted: bool = False


_SEED_MEMBERS: list[Member] = [
    Member(
        member_id="100412",
        name="Dolores Vance",
        status="ACTIVE",
        branch="0042 - RIVER OAKS",
        accounts=[
            Account("SAVINGS", "SAV-0100412-01", 4182.55),
            Account("CHECKING", "CHK-0100412-01", 912.03),
        ],
    ),
    Member(
        member_id="100413",
        name="Marcus Oyelaran",
        status="ACTIVE",
        branch="0011 - DOWNTOWN",
        accounts=[
            Account("SAVINGS", "SAV-0100413-01", 27.19),
            Account("CHECKING", "CHK-0100413-01", 15340.00),
            Account("CERTIFICATE", "CRT-0100413-01", 25000.00, status="LOCKED"),
        ],
    ),
    Member(
        member_id="100414",
        name="Priya Raghunathan",
        status="ACTIVE",
        branch="0042 - RIVER OAKS",
        restricted=True,
        accounts=[Account("SAVINGS", "SAV-0100414-01", 63002.40)],
    ),
    Member(
        member_id="100415",
        name="Theo Brannigan",
        status="DORMANT",
        branch="0003 - NORTH GATE",
        accounts=[Account("SAVINGS", "SAV-0100415-01", 1.00, status="DORMANT")],
    ),
    Member(
        member_id="100416",
        name="Wen Li",
        status="ACTIVE",
        branch="0011 - DOWNTOWN",
        accounts=[
            Account("SAVINGS", "SAV-0100416-01", 8750.25),
            Account("MONEY MARKET", "MMK-0100416-01", 44100.00),
        ],
    ),
]

OPERATORS = {"svc_automation": "Tr0ubadour!", "jrivera": "hunter2-demo"}

SUB_ACCOUNT_TYPES = ["SAVINGS", "MONEY MARKET", "CERTIFICATE"]

MIN_INITIAL_DEPOSIT = 25.00


class MemberStore:
    """In-memory store. Reset between server runs so demos are repeatable."""

    def __init__(self) -> None:
        self._members = {m.member_id: m for m in copy.deepcopy(_SEED_MEMBERS)}
        self._next_suffix = 90

    def get(self, member_id: str) -> Member | None:
        return self._members.get((member_id or "").strip())

    def open_sub_account(self, member_id: str, kind: str, deposit: float) -> Account:
        member = self._members[member_id]
        self._next_suffix += 1
        prefix = {"SAVINGS": "SAV", "MONEY MARKET": "MMK", "CERTIFICATE": "CRT"}[kind]
        account = Account(kind, f"{prefix}-0{member_id}-{self._next_suffix}", deposit)
        member.accounts.append(account)
        return account
