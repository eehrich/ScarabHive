from datetime import date
from decimal import Decimal

import pytest

from ledger.parse import parse_line, parse_text
from ledger.report import monthly, totals_by_category

TEXT = """
# January
2026-01-05; Coffee; -3.50; Food
2026-01-20; Salary; 2000; income
  # paid late
2026-02-01; Bakery; -4.20; food
"""


def test_a_line_is_read():
    entry = parse_line("2026-01-05; Coffee; -3.50; Food")
    assert (entry.day, entry.text, entry.amount, entry.category) == (date(2026, 1, 5), "Coffee", Decimal("-3.50"),
                                                                     "food")


def test_a_bad_amount_is_refused():
    with pytest.raises(ValueError):
        parse_line("2026-01-05; Coffee; lots; food")


def test_totals_and_months():
    entries = parse_text(TEXT)
    assert totals_by_category(entries) == {"food": Decimal("-7.70"), "income": Decimal("2000")}
    assert monthly(entries) == {"2026-01": Decimal("1996.50"), "2026-02": Decimal("-4.20")}
