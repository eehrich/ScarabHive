"""Reading the timestamps the system writes: ISO 8601 strings, in UTC."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def parse_iso_utc(value: Any) -> Optional[datetime]:
    """*value* as an aware datetime in UTC, or None when it is not a timestamp.

    *value* is an ISO 8601 string (a trailing ``Z`` included, which
    ``fromisoformat`` before 3.11 did not take) or a datetime already. One
    without a zone is taken to be UTC -- what every writer here means by it;
    one with another offset is converted. None, an empty string, anything
    else, and a string that is no timestamp all give None.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
