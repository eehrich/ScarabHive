from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_system.utils.timeutil import parse_iso_utc

UTC_MIDNIGHT = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", [
    "2026-01-01T00:00:00Z",
    "2026-01-01T00:00:00+00:00",
    "2026-01-01T02:00:00+02:00",
    "2026-01-01T00:00:00",  # no zone: UTC
    datetime(2026, 1, 1),
    datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2))),
])
def test_a_timestamp_comes_back_aware_in_utc(value):
    parsed = parse_iso_utc(value)
    assert parsed == UTC_MIDNIGHT and parsed.tzinfo == timezone.utc


@pytest.mark.parametrize("value", [None, "", "yesterday", "2026-13-01", 1767225600, {}])
def test_anything_else_is_none(value):
    assert parse_iso_utc(value) is None
