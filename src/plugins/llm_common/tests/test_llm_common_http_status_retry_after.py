"""Retry-After as the batch clients and the httpx client read it.

The batch clients called float() on the header: an HTTP-date Retry-After
(RFC 9110) raised ValueError and hid the 429 from the rate-limit backoff.
The httpx client took any number: `Retry-After: 3600` parked a turn for an
hour, `inf` forever.
"""
from datetime import datetime, timezone

import httpx
import pytest

from agent_system.llm.models import LLMQuotaExhaustedError, LLMRateLimitError
from plugins.llm_common.batch_http import raise_for_batch_status
from plugins.llm_common.http_status import retry_after_seconds


@pytest.mark.parametrize("value,expected", [
    ("20", 20.0), ("1.5", 1.5), ("1.5s", 1.5), ("200ms", 0.2), ("6m0s", 360.0)])
def test_numbers_and_durations(value, expected):
    assert retry_after_seconds(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", [None, "", "soon", "inf", "nan", "-5", 12])
def test_unusable_is_no_hint(value):
    assert retry_after_seconds(value) is None


def test_http_date_is_read_against_now():
    now = datetime(2026, 10, 21, 7, 27, 30, tzinfo=timezone.utc)
    assert retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT", now=now) == 30.0
    assert retry_after_seconds("Wed, 21 Oct 2026 07:00:00 GMT", now=now) == 0.0


def test_cap():
    assert retry_after_seconds("3600", cap=60) == 60


def _response(status, text="", headers=None):
    return httpx.Response(status, text=text, headers=headers or {})


def test_batch_429_with_http_date_is_still_a_rate_limit():
    with pytest.raises(LLMRateLimitError):
        raise_for_batch_status(
            _response(429, "slow down", {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            "op", provider="openai_batch")


def test_batch_quota_and_other_errors():
    with pytest.raises(LLMQuotaExhaustedError):
        raise_for_batch_status(_response(429, "insufficient_quota"), "op", provider="p")
    with pytest.raises(RuntimeError, match="op: 500"):
        raise_for_batch_status(_response(500, "boom"), "op", provider="p")
    raise_for_batch_status(_response(200), "op", provider="p")



def test_a_batch_per_minute_429_naming_quota_is_a_rate_limit():
    """Google's per-minute throttle reads "Resource has been exhausted (e.g. check quota)": a short wait helps."""
    with pytest.raises(LLMRateLimitError) as limited:
        raise_for_batch_status(_response(429, "Resource has been exhausted (e.g. check quota). "
                                              "quotaId: GenerateRequestsPerMinutePerProjectPerModel"), "op", provider="p")
    assert type(limited.value) is LLMRateLimitError
