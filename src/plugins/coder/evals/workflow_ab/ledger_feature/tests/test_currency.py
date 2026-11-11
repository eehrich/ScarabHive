from decimal import Decimal

from ledger.currency import convert
from ledger.parse import parse_line, parse_text
from ledger.report import monthly, totals_by_category

RATES = {"USD": Decimal("0.90"), "CHF": Decimal("1.05")}


def test_an_amount_carries_its_currency():
    entry = parse_line("2026-01-05; Coffee; -3.50 USD; food")
    assert entry.amount == Decimal("-3.50") and entry.currency == "USD"


def test_an_amount_without_a_code_is_in_the_base_currency():
    assert parse_line("2026-01-05; Coffee; -3.50; food").currency is None


def test_convert_turns_every_entry_into_the_base_currency():
    entries = parse_text("2026-01-05; Coffee; -10 USD; food\n2026-01-06; Tea; -2; food")
    converted = convert(entries, RATES)
    assert [e.amount for e in converted] == [Decimal("-9.00"), Decimal("-2")]
    assert all(e.currency is None for e in converted)


def test_the_reports_convert_first():
    entries = parse_text("2026-01-05; Coffee; -10 USD; food\n2026-02-06; Train; -20 CHF; travel")
    assert totals_by_category(entries, rates=RATES) == {"food": Decimal("-9.00"), "travel": Decimal("-21.00")}
    assert monthly(entries, rates=RATES) == {"2026-01": Decimal("-9.00"), "2026-02": Decimal("-21.00")}
