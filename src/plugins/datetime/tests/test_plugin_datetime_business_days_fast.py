"""Business days in O(1), with the answers of the day-by-day loop it replaced."""
from datetime import date, timedelta

import pytest

from plugins.datetime.server import add_business_days, business_days_between


def _walk_add(start, days):
    step, current, added = (1 if days > 0 else -1), start, 0
    while added < abs(days):
        current += timedelta(days=step)
        if current.weekday() < 5:
            added += 1
    return current


def _walk_between(start, end):
    count, current = 0, start
    while current < end:
        count += current.weekday() < 5
        current += timedelta(days=1)
    return count


@pytest.mark.parametrize("offset", range(14))
@pytest.mark.parametrize("days", [0, 1, 4, 5, 6, 9, 10, 23, -1, -5, -6, -23])
def test_adding_matches_the_walk(offset, days):
    start = date(2026, 11, 2) + timedelta(days=offset)
    assert add_business_days(start, days) == _walk_add(start, days)


@pytest.mark.parametrize("offset", range(7))
@pytest.mark.parametrize("span", [0, 1, 5, 6, 7, 8, 13, 30, 365])
def test_counting_matches_the_walk(offset, span):
    start = date(2026, 11, 2) + timedelta(days=offset)
    assert business_days_between(start, start + timedelta(days=span)) == _walk_between(
        start, start + timedelta(days=span))


def test_a_huge_count_is_instant():
    assert business_days_between(date(1, 1, 1), date(9999, 12, 31)) > 2_000_000
    with pytest.raises(OverflowError):
        add_business_days(date(2026, 1, 1), 10**7)
