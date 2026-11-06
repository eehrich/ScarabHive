"""HTTP pieces the provider batch clients (/v1/batches, /v1/messages/batches) share."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx

from agent_system.llm.models import LLMQuotaExhaustedError, LLMRateLimitError

from .http_status import retry_after_seconds
from .quota import quota_exhausted

__all__ = ["DateTimeEncoder", "raise_for_batch_status"]


class DateTimeEncoder(json.JSONEncoder):
    """JSON encoder that writes datetimes as ISO 8601."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def raise_for_batch_status(response: httpx.Response, operation: str, *,
                           provider: str, model: str = "unknown") -> None:
    """Raise the typed error for a non-200 batch API answer.

    429 is LLMQuotaExhaustedError when the body names an account cap (quota_exhausted), else
    LLMRateLimitError with the Retry-After the server sent (seconds or an
    HTTP date; an unreadable one is no hint, never a ValueError that hides the
    429). Anything else is a RuntimeError naming the operation.
    """
    if response.status_code == 200:
        return
    if response.status_code == 429:
        if quota_exhausted(response.text):
            raise LLMQuotaExhaustedError(
                provider=provider, model=model, message=f"{operation}: {response.text}")
        raise LLMRateLimitError(
            provider=provider, model=model,
            retry_after=retry_after_seconds(response.headers.get("retry-after")),
            message=f"{operation}: {response.text}")
    raise RuntimeError(f"{operation}: {response.status_code} {response.text}")
