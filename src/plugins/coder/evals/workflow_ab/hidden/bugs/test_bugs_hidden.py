"""Hidden from the run: what the README and the docstrings say, beyond the cases tests/ shows."""
from decimal import Decimal

import pytest

from ledger.parse import parse_line, parse_text
from ledger.report import monthly, totals_by_category


def test_a_comment_indented_with_a_tab_is_skipped():
    assert parse_text("\t# a note\n2026-01-05; Coffee; -3.50; food") == [parse_line("2026-01-05; Coffee; -3.50; food")]


def test_categories_are_one_whatever_their_case():
    entries = parse_text("2026-01-05; A; -1; FOOD\n2026-01-06; B; -2; Food\n2026-01-07; C; -3; food")
    assert totals_by_category(entries) == {"food": Decimal("-6")}


def test_the_same_month_of_two_years_is_two_months():
    entries = parse_text("2025-12-03; A; -1; x\n2026-12-03; B; -2; x\n2026-12-28; C; -4; x")
    assert monthly(entries) == {"2025-12": Decimal("-1"), "2026-12": Decimal("-6")}


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-inf", "sNaN"])
def test_an_amount_that_is_no_finite_number_is_refused(amount):
    with pytest.raises(ValueError):
        parse_line(f"2026-01-05; Coffee; {amount}; food")
