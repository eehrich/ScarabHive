"""Shared access to the central LLM pricing table (config/llm_pricing.yaml).

Single source of truth for per-model rates (per 1M tokens). Used as a
FALLBACK to estimate call costs when the provider response carries no billed
cost field — direct APIs (DeepSeek, Gemini SDK, native Anthropic/OpenAI) send
none; OpenRouter sends ``cost`` and needs no estimate.

The formula mirrors scripts/session_costs.compute_cost: uncached input at the
``input`` rate, cached input at ``cached_input`` (falls back to ``input``),
output at ``output``, optional ``batch_discount`` multiplier.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# Resolved against the repo root, not the process CWD: a relative default made
# the whole estimate silently disappear (load_pricing returns {} on OSError)
# whenever a tool ran from another directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PRICING_PATH = _REPO_ROOT / "config" / "llm_pricing.yaml"

#: mtime-based cache — the table is read at most once per file change.
_cache: dict = {"path": None, "mtime": None, "table": {}}


def load_pricing(path: Optional[Path] = None) -> Dict[str, Dict[str, float]]:
    """Per-model rates from the pricing yaml; ``{}`` if missing/unreadable."""
    # Resolved at CALL time: a default bound at def time could be
    # neither reconfigured nor monkeypatched in a test.
    path = path or DEFAULT_PRICING_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _cache["path"] == path and _cache["mtime"] == mtime:
        return _cache["table"]
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        table = {
            model: {k: float(v) for k, v in prices.items()}
            for model, prices in data.items()
            if isinstance(prices, dict)
        }
    except Exception as e:  # noqa: BLE001 — pricing must never break a caller
        logger.warning("Failed to load pricing table %s: %s", path, e)
        return {}
    _cache.update(path=path, mtime=mtime, table=table)
    return table


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    *,
    cache_write_tokens: int = 0,
    is_batch: bool = False,
    path: Optional[Path] = None,
) -> Optional[float]:
    """Estimated USD cost for one call; ``None`` when the model has no entry.

    Callers should mark such costs as ESTIMATES in any display — they come
    from the maintained table, not from provider billing.

    ``cached_tokens`` are cache READS and are assumed to be a subset of
    ``prompt_tokens`` (true for OpenAI and Gemini, and what the repo's own
    docs/tests assume for Anthropic). ``cache_write_tokens`` are billed ON TOP
    at the ``cache_write`` rate — providers charge a premium for writing a
    cache entry, and without a rate in the table they cost nothing, exactly as
    before.
    """
    p = load_pricing(path).get(model)
    if not p:
        return None
    prompt_tokens = prompt_tokens or 0
    cached_tokens = cached_tokens or 0
    uncached = max(0, prompt_tokens - cached_tokens)
    cached_rate = p.get("cached_input", p.get("input", 0.0))
    cost = (
        uncached * p.get("input", 0.0)
        + cached_tokens * cached_rate
        + (cache_write_tokens or 0) * p.get("cache_write", 0.0)
        + (completion_tokens or 0) * p.get("output", 0.0)
    ) / 1_000_000
    if is_batch:
        cost *= p.get("batch_discount", 1.0)
    return cost


@dataclass(frozen=True)
class CallUsage:
    """One LLM call's token counts in ONE canonical shape.

    Every client reports usage slightly differently (OpenAI names, Anthropic
    names, Gemini camelCase, Responses-API names). Normalising once here is
    what stops each consumer from re-deriving it -- and getting it subtly
    different, which is how the same call ended up with different amounts in
    different tools.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0        # cache READS
    cache_write_tokens: int = 0   # cache WRITES (billed extra)
    provider_cost: Optional[float] = None  # billed figure, when the API sends one

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _first_int(source: Dict[str, Any], *names: str) -> int:
    for name in names:
        value = source.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def normalize_usage(usage: Any) -> CallUsage:
    """Map any client's usage dict onto :class:`CallUsage`.

    Handles the shapes actually produced in this repo: OpenAI/httpx
    (prompt_tokens + prompt_tokens_details.cached_tokens), the Responses API
    (input_tokens/output_tokens + input_tokens_details), Anthropic
    (cache_read_input_tokens / cache_creation_input_tokens), Gemini in both
    snake_case and camelCase, and OpenRouter's extra ``cost``.
    """
    if not isinstance(usage, dict):
        return CallUsage()

    prompt = _first_int(usage, "prompt_tokens", "input_tokens", "promptTokenCount")
    completion = _first_int(usage, "completion_tokens", "output_tokens",
                            "candidatesTokenCount")

    details: Dict[str, Any] = {}
    for key in ("prompt_tokens_details", "input_tokens_details"):
        candidate = usage.get(key)
        if isinstance(candidate, dict):
            details = candidate
            break

    cached = _first_int(details, "cached_tokens", "cache_read_input_tokens")
    if not cached:
        cached = _first_int(usage, "cached_tokens", "cache_read_input_tokens",
                            "cachedContentTokenCount")

    writes = _first_int(details, "cache_write_tokens", "cache_creation_tokens",
                        "cache_creation_input_tokens")
    if not writes:
        writes = _first_int(usage, "cache_write_tokens", "cache_creation_tokens",
                            "cache_creation_input_tokens")

    cost = usage.get("cost")
    return CallUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        cache_write_tokens=writes,
        # `is None`, not falsy: a genuine 0.0 from a free/BYOK model is a real
        # billed figure and must not be replaced by an estimate.
        provider_cost=float(cost) if isinstance(cost, (int, float)) else None,
    )


def resolve_call_cost(
    usage: Any,
    model: Optional[str],
    *,
    is_batch: bool = False,
    path: Optional[Path] = None,
) -> Tuple[Optional[float], bool]:
    """USD cost of one call as ``(cost, is_estimate)``; ``(None, False)`` if unknown.

    The house rule, in one place: a figure billed by the provider always wins;
    otherwise the central table estimates it, and the caller must mark that as
    an estimate. Every consumer re-implemented this rule, and they disagreed --
    on whether ``cost == 0.0`` counts, on whether cache writes are priced, and
    on what happens for an unknown model.
    """
    call = normalize_usage(usage)
    if call.provider_cost is not None:
        return call.provider_cost, False
    if not model:
        return None, False
    estimate = estimate_cost(
        model, call.prompt_tokens, call.completion_tokens, call.cached_tokens,
        cache_write_tokens=call.cache_write_tokens, is_batch=is_batch, path=path,
    )
    return (estimate, True) if estimate is not None else (None, False)
