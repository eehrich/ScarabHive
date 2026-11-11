"""One ledger entry."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Entry:
    day: date
    text: str
    amount: Decimal
    category: str
