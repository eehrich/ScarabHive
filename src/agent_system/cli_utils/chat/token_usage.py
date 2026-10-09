"""What the chat's turns use: tokens and what they cost.

Per call as the turn sees them (_call_pricing_key, _accumulate_usage), added
up over the chat (_merge_totals) and shown as the footer under every answer
and at the end (_format_usage); /costs asks the usage tracker instead, which
also sees what sub-agents spent.

Its own module because the turn writes these numbers, the REPL sums and
shows them, and /costs reports the same arithmetic from a wider source.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from ...llm.pricing import normalize_usage, resolve_call_cost

if TYPE_CHECKING:
    from .context import _ChatContext
    from .display import ChatRenderer
    from .repl import _Repl

logger = logging.getLogger(__name__)


def _call_pricing_key(agent: Any, override: Any = None,
                      event: Any = None) -> tuple[Optional[str], bool]:
    """(model, is_batch) of the client that just ran -- read per call.

    Pricing the whole session with one model was wrong as soon as a fallback
    switched profiles or a step used a different client.

    The event names the model that answered the call (a fallback, or a step
    walking around a blocked LLM, runs on neither the override nor the
    agent's own client). Without that, the override comes first, and that is
    not cosmetic: --llm and /model hand the turn a different client while
    ``agent.llm`` stays the agent's own, so reading the agent alone quoted the
    price of the model that did NOT run.

    The isinstance-str guard on the provider mirrors the usage tracker: a
    plain mock must not look batchy and halve the estimate.
    """
    model = event.get("model") if isinstance(event, dict) else None
    if isinstance(model, str) and model:
        return model, event.get("batch") is True
    client = override or getattr(agent, "llm", None)
    model = getattr(client, "model", None)
    # Who answered, not which client ran: a batch client's sync fallback is
    # priced in full. `is True`: a bare mock must not look batchy.
    return (str(model) if model else None,
            getattr(client, "last_was_batch", None) is True)


def _merge_totals(total: dict, turn: dict) -> None:
    """Fold a finished turn's already-resolved totals into the session sum."""
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens",
                "cache_write_tokens", "cost", "cost_unpriced_calls"):
        value = turn.get(key)
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
    if turn.get("cost_is_estimate"):
        total["cost_is_estimate"] = True


def _accumulate_usage(total: dict, usage: Any, model: Optional[str] = None,
                      is_batch: bool = False) -> None:
    """Add one LLM call's usage AND its resolved cost into the running total.

    Cost is resolved per call, not once over the summed tokens: a session can
    span several models, and mixing a billed figure from one provider with
    estimated tokens from another produced a number that was both too low and
    labelled as exact.
    """
    if not isinstance(usage, dict):
        return
    call = normalize_usage(usage)
    total["prompt_tokens"] = total.get("prompt_tokens", 0) + call.prompt_tokens
    total["completion_tokens"] = (
        total.get("completion_tokens", 0) + call.completion_tokens)
    total["cached_tokens"] = total.get("cached_tokens", 0) + call.cached_tokens
    total["cache_write_tokens"] = (
        total.get("cache_write_tokens", 0) + call.cache_write_tokens)

    cost, estimated = resolve_call_cost(usage, model, is_batch=is_batch)
    if cost is not None:
        total["cost"] = total.get("cost", 0.0) + cost
        # One estimated call makes the SUM an estimate -- anything else would
        # present a partly guessed total as billing.
        total["cost_is_estimate"] = total.get("cost_is_estimate", False) or estimated
    else:
        # Tokens counted, price unknown: the total is incomplete and has to say so.
        total["cost_unpriced_calls"] = total.get("cost_unpriced_calls", 0) + 1


def _format_usage(totals: dict, elapsed: float, sym: dict,
                  context: Optional[tuple[int, Optional[int]]] = None) -> str:
    """One dim footer line: context fill, tokens, cache hit rate, cost, time.

    The cost is already resolved per call by _accumulate_usage -- this only
    renders it. An estimated total carries a leading ~, and calls whose price
    could not be determined are named rather than silently omitted.

    `context` is (tokens_in_window, window_size) for a single turn; the
    session total has no such thing and passes None.
    """
    def _short(n: float) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"

    prompt = totals.get("prompt_tokens", 0) or 0
    completion = totals.get("completion_tokens", 0) or 0
    cached = totals.get("cached_tokens", 0) or 0
    written = totals.get("cache_write_tokens", 0) or 0

    parts = []
    if context:
        used, window = context
        if window:
            # The window is a round number by nature -- "272k" reads better
            # than the "272.0k" the generic short form would give it.
            parts.append(f"ctx {_short(used)}/{window // 1000}k "
                         f"({used * 100 // window}%)")
        else:
            parts.append(f"ctx {_short(used)}")

    if prompt or completion:
        head = f"{sym['up']}{_short(prompt)}"
        # Always shown once anything was sent: 0% is the interesting case --
        # it means the prefix cache is not being hit at all. Writes are named
        # separately because "0% cached" alone cannot tell a broken cache from
        # the first turn of a working one, which is exactly the confusion that
        # cost an hour when Claude's cache_control was being dropped.
        if prompt:
            rate = f"{cached * 100 // prompt}% cached"
            if written:
                rate += f", +{_short(written)} written"
            head += f" ({rate})"
        parts.append(f"{head} {sym['down']}{_short(completion)}")

    cost = totals.get("cost")
    if cost is not None:
        marker = "~$" if totals.get("cost_is_estimate") else "$"
        text = f"{marker}{cost:.4f}"
        unpriced = totals.get("cost_unpriced_calls", 0)
        if unpriced:
            text += f" +{unpriced} unpriced"
        parts.append(text)

    mins, secs = divmod(int(elapsed), 60)
    parts.append(f"{mins}m{secs:02d}s" if mins else f"{secs}s")
    # Output speed, the number people compare between models: generated
    # tokens over wall time. Prompt tokens are not "generated" and would
    # inflate it by the whole history on every turn.
    # Only per turn (`context` marks one): across a whole session the elapsed
    # time includes the user thinking and typing, so the rate would say more
    # about the human than about the model.
    if context and completion and elapsed > 0:
        parts.append(f"{completion / elapsed:.0f} tok/s")
    return sym["sep"].join(parts)


def _usage_tracker(ctx: "_ChatContext") -> Any:
    """The registered context_usage_tracker's UsageTracker, or None.

    It hooks every LLM call in the process, so it is the ONLY source that also
    sees sub-agent calls -- those run in their own sub-sessions and never
    appear in the coordinator's run_events stream.
    """
    registry = getattr(ctx.agent, "registry", None)
    if registry is None:
        return None
    try:
        server = registry.get("context_usage_tracker")
    except Exception:
        logger.debug("Usage tracker not registered", exc_info=True)
        return None
    tracker = getattr(server, "tracker", None)
    return tracker if hasattr(tracker, "get_statistics") else None


def _show_costs(ctx: "_ChatContext", renderer: ChatRenderer) -> None:
    """Session cost INCLUDING sub-agents, from the usage tracker.

    The turn footer only sums what the coordinator's own event stream carries.
    Sub-agents bill against the same wallet but report through their own
    sub-sessions, so a number built from the stream alone is silently too low.
    """
    tracker = _usage_tracker(ctx)
    if tracker is None:
        print("The context_usage_tracker plugin is not active -- no per-call "
              "records to add up.")
        print(f"This chat's own turns: {_format_usage(ctx.total_usage, 0, renderer.sym)}")
        return
    try:
        stats = tracker.get_statistics(session_id=ctx.session_id)
    except Exception as e:
        logger.error("Failed to read usage statistics: %s", e, exc_info=True)
        print(f"Could not read usage statistics: {e}")
        return
    totals = (stats or {}).get("totals")
    if not totals:
        print("No LLM calls recorded for this session yet.")
        return

    def _short(n: float) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"

    known = totals.get("cost_known_calls", 0)
    estimated = totals.get("cost_estimated_calls", 0)
    samples = (stats.get("timespan") or {}).get("sample_count", 0)
    # Calls the tracker saw but could price neither way -- naming them keeps
    # the total from looking complete when it is not.
    unpriced = max(0, samples - known)

    print(f"Session {ctx.session_id} (incl. sub-agents):")
    renderer.println(
        f"  calls        {samples}"
        + (f"  ({estimated} estimated)" if estimated else ""), color="90")
    renderer.println(
        f"  tokens       {renderer.sym['up']}{_short(totals.get('prompt_tokens', 0))}"
        f"  {renderer.sym['down']}{_short(totals.get('completion_tokens', 0))}"
        f"  cache {totals.get('cache_hit_rate', 0.0):.0f}%", color="90")
    marker = "~$" if estimated else "$"
    renderer.println(f"  cost         {marker}{totals.get('cost', 0.0):.4f}"
                     + (f"  ({unpriced} unpriced)" if unpriced else ""), color="90")
    renderer.commit()
    if estimated:
        print("  ~ = estimated from config/llm_pricing.yaml, not provider billing")


def _on_costs(repl: _Repl, payload: str) -> None:
    _show_costs(repl.ctx, repl.renderer)
