"""The backend that answered an agent type last, per model.

A gateway such as OpenRouter serves one model from several backends, and a
prompt cache lives on one of them. Within a run the history says which one
holds it (``served_by`` on the assistant turns). A run's FIRST call has no
such history, yet its instructions and tools are those of every other run of
the same agent type, so the backend that answered that type last holds them.

Measured on the server on 22.09.2026, first calls of v4/v6 runs (72 h):

    gap to the type's previous call  same backend    another backend
    < 5 min                           57.8 % cached   27.8 %
    5-30 min                          22.5 %           9.0 %
    30-60 min                          1.1 %           8.3 %

Past about half an hour the cache is cold either way, and the gateway may
choose as configured again — hence the default window.

Process-wide on purpose: one client is built per agent, escalation and
fallback, and parallel sub-agents of one type use clients of their own. The
key is the agent type, not a client. Nothing is persisted: after a restart
the first call of each type follows the configured order, as it did before.
"""
from __future__ import annotations

import time
from typing import Optional

#: Window in minutes when a model entry sets none (see the table above).
DEFAULT_WINDOW_MINUTES = 30.0

_last: dict[tuple[str, str], tuple[str, float]] = {}


def remember(agent: str, model: str, backend: str) -> None:
    """Note that *backend* answered *agent*'s call to *model* just now."""
    _last[(agent, model)] = (backend, time.monotonic())


def recent(agent: Optional[str], model: Optional[str],
           window_minutes: Optional[float]) -> Optional[str]:
    """The backend that answered *agent* on *model* within the window, else None.

    ``window_minutes`` None means the default; 0 or less turns it off.
    """
    if not agent or not model:
        return None
    window = DEFAULT_WINDOW_MINUTES if window_minutes is None else window_minutes
    entry = _last.get((agent, model))
    if entry is None or window <= 0 or time.monotonic() - entry[1] > window * 60:
        return None
    return entry[0]


def forget(agent: Optional[str], model: Optional[str]) -> None:
    """Drop what is remembered: that backend just refused this agent's call."""
    if agent and model:
        _last.pop((agent, model), None)


def clear() -> None:
    """Forget every backend (test isolation)."""
    _last.clear()
