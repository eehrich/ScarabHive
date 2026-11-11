"""Sums over entries."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from .entry import Entry


def totals_by_category(entries: Iterable[Entry]) -> dict[str, Decimal]:
    """Net amount per category, sorted by category."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for entry in entries:
        out[entry.category] += entry.amount
    return dict(sorted(out.items()))


def monthly(entries: Iterable[Entry]) -> dict[str, Decimal]:
    """Net amount per month (``YYYY-MM``), sorted by month."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for entry in entries:
        out[entry.day.strftime("%Y-%m")] += entry.amount
    return dict(sorted(out.items()))
