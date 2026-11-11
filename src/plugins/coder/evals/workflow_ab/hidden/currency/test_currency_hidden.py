"""Hidden from the run: every case here is stated in the task (suite.yaml, id currencies)."""
from datetime import date
from decimal import Decimal

import pytest

from ledger.currency import convert
from ledger.entry import Entry
from ledger.parse import parse_line, parse_text
from ledger.report import monthly, totals_by_category


def test_a_code_is_case_insensitive_and_kept_upper_case():
    assert parse_line("2026-01-05; Coffee; -3.50 usd; food").currency == "USD"


def test_several_spaces_before_the_code():
    entry = parse_line("2026-01-05; Coffee; -3.50   CHF; food")
    assert (entry.amount, entry.currency) == (Decimal("-3.50"), "CHF")


@pytest.mark.parametrize("amount", ["-3.50 US", "-3.50 USD1", "-3.50 U2D"])
def test_a_code_that_is_not_three_letters_is_refused(amount):
    with pytest.raises(ValueError):
        parse_line(f"2026-01-05; Coffee; {amount}; food")


def test_an_entry_keeps_working_without_a_currency():
    entry = Entry(date(2026, 1, 5), "Coffee", Decimal("-3.50"), "food")
    assert entry.currency is None


def test_a_code_without_a_rate_is_named():
    with pytest.raises(ValueError, match="CHF"):
        convert(parse_text("2026-01-05; Train; -20 CHF; travel"), {"USD": Decimal("0.9")})


def test_the_base_currency_needs_no_rate():
    [entry] = convert(parse_text("2026-01-05; Coffee; -5 EUR; food"), {})
    assert (entry.amount, entry.currency) == (Decimal("-5"), None)


def test_another_base():
    entries = parse_text("2026-01-05; Coffee; -10 USD; food\n2026-01-06; Tea; -2; food\n"
                         "2026-01-07; Cake; -4 EUR; food")
    converted = convert(entries, {"EUR": Decimal("1.10")}, base="USD")
    assert [e.amount for e in converted] == [Decimal("-10"), Decimal("-2"), Decimal("-4.40")]


def test_a_converted_amount_is_rounded_to_cents_half_even():
    entries = parse_text("2026-01-05; A; 0.125 USD; x\n2026-01-05; B; 0.135 USD; x")
    assert [e.amount for e in convert(entries, {"USD": Decimal("1")})] == [Decimal("0.12"), Decimal("0.14")]


def test_an_amount_in_the_base_currency_is_not_rounded():
    [entry] = convert(parse_text("2026-01-05; A; -2.005; x"), {})
    assert entry.amount == Decimal("-2.005")


def test_convert_keeps_the_order_and_the_rest_of_an_entry():
    entries = parse_text("2026-03-01; B; 1 USD; y\n2026-01-01; A; 2; x")
    converted = convert(entries, {"USD": Decimal("2")})
    assert [(e.day, e.text, e.category) for e in converted] == [(date(2026, 3, 1), "B", "y"),
                                                                 (date(2026, 1, 1), "A", "x")]


def test_a_report_without_rates_refuses_a_foreign_amount():
    entries = parse_text("2026-01-05; Coffee; -10 USD; food\n2026-01-06; Tea; -2; food")
    with pytest.raises(ValueError):
        totals_by_category(entries)
    with pytest.raises(ValueError):
        monthly(entries)


def test_a_report_takes_its_base():
    entries = parse_text("2026-01-05; Coffee; -10 USD; food\n2026-01-06; Tea; -2 EUR; food")
    assert totals_by_category(entries, rates={"EUR": Decimal("1.10")}, base="USD") == {"food": Decimal("-12.20")}
