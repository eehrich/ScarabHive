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
from pathlib import Path
from typing import Dict, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_PRICING_PATH = Path("config/llm_pricing.yaml")

#: mtime-based cache — the table is read at most once per file change.
_cache: dict = {"path": None, "mtime": None, "table": {}}


def load_pricing(path: Path = DEFAULT_PRICING_PATH) -> Dict[str, Dict[str, float]]:
    """Per-model rates from the pricing yaml; ``{}`` if missing/unreadable."""
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
    is_batch: bool = False,
    path: Path = DEFAULT_PRICING_PATH,
) -> Optional[float]:
    """Estimated USD cost for one call; ``None`` when the model has no entry.

    Callers should mark such costs as ESTIMATES in any display — they come
    from the maintained table, not from provider billing.
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
        + (completion_tokens or 0) * p.get("output", 0.0)
    ) / 1_000_000
    if is_batch:
        cost *= p.get("batch_discount", 1.0)
    return cost
