"""Auto-escalation to the advanced LLM profile when an agent is stuck.

A weak model that is stuck usually does not KNOW it is stuck, so self-reported
escalation ("I give up, use the big model") is unreliable — agents won't admit
they're too weak. Instead the run loop watches OBJECTIVE signals and escalates
on its behalf:

- the tool-call loop detector fires (same call/sequence repeated), or
- several consecutive steps whose tool calls ALL returned an error.

Escalation is deliberately time-boxed and budget-capped, not sticky-until-end:
staying on the expensive model for a whole run is wasteful. Each trigger opens a
window of ``rounds`` steps on the advanced model; the agent then drops back to
the standard model and only re-escalates on a fresh signal, until the total
``max_calls`` advanced-call budget for the run is spent.

The signals live in the run loop (where the step counters already are); this
class only owns the window + budget bookkeeping so it can be unit-tested in
isolation.
"""

from __future__ import annotations

from typing import Optional


class StuckEscalator:
    """Window + budget bookkeeping for stuck-triggered model escalation."""

    def __init__(self, *, enabled: bool, rounds: int, max_calls: int) -> None:
        self.rounds = max(0, int(rounds))
        self.max_calls = max(0, int(max_calls))
        # Escalation is only meaningful with a positive window AND budget.
        self.enabled = bool(enabled) and self.rounds > 0 and self.max_calls > 0
        self._remaining = 0   # advanced steps left in the current window
        self._used = 0        # advanced calls consumed this run

    @property
    def active(self) -> bool:
        """Whether an escalation window is currently open."""
        return self._remaining > 0

    def disable(self) -> None:
        """Turn escalation off for the rest of this run and close any open
        window. Called when the advanced client cannot be built, so the run
        loop stops re-attempting it every step (the window would otherwise stay
        open forever, since consume() only runs on an actual advanced call)."""
        self.enabled = False
        self._remaining = 0

    @property
    def calls_used(self) -> int:
        return self._used

    def consume(self) -> None:
        """Spend one advanced call from the current window. Call when the advanced
        client is actually SELECTED for the step's LLM call — i.e. the budget
        counts advanced calls issued. A step where no advanced client could be
        built spends nothing (the caller skips consume). A step where the advanced
        call is issued but then rate-limited into a fallback still counts (the
        advanced endpoint was hit); this is self-limiting, since the persistent
        fallback then suppresses escalation on following steps. No-op when no
        window is open."""
        if self._remaining > 0:
            self._remaining -= 1
            self._used += 1

    def trigger(self, reason: str) -> Optional[str]:
        """Report a stuck signal. Opens an escalation window for the next
        ``rounds`` steps when escalation is enabled, no window is currently open,
        and budget remains. The window never exceeds the remaining budget.

        Returns the reason string when a window opens (so the caller can log it
        once), else None (disabled, already escalated, or budget exhausted).
        """
        if not self.enabled or self._remaining > 0 or self._used >= self.max_calls:
            return None
        self._remaining = min(self.rounds, self.max_calls - self._used)
        return reason if self._remaining > 0 else None
