"""Ledger lines: ``YYYY-MM-DD;text;amount;category``. Blank lines and comment lines (``#``) are skipped."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from .entry import Entry


def parse_line(line: str) -> Entry:
    parts = [part.strip() for part in line.split(";")]
    if len(parts) != 4:
        raise ValueError(f"expected 4 fields, got {len(parts)}: {line!r}")
    day, text, amount, category = parts
    try:
        value = Decimal(amount)
    except InvalidOperation:
        raise ValueError(f"not an amount: {amount!r}") from None
    return Entry(date.fromisoformat(day), text, value, category)


def parse_text(text: str) -> list[Entry]:
    return [parse_line(line) for line in text.splitlines()
            if line.strip() and not line.startswith("#")]
