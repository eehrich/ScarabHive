"""Dynamic workflows: a machine an agent writes for the task at hand and starts without saving it
(service.start_workflow, the tool ``run_workflow``).

What is theirs alone lives here: the instance's ``workflows:`` settings, the terms written into the machine before
it runs, the rules an inline machine keeps, the process-wide gate on their agents, and the results that wait for
their caller. A run without the ``workflow`` column -- every machine run that is not a dynamic workflow, the
writer's among them -- meets none of it.
"""
from __future__ import annotations

import asyncio
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Optional

#: The instance setting, and the keys it must have: without them no workflow starts (the user sets the caps).
SETTINGS_KEY = "workflows"
REQUIRED = ("default_max_output_tokens", "max_output_tokens_ceiling", "default_concurrency")
#: Top-level keys an inline machine may not use: they read files next to the machine, which it has not.
NOT_INLINE = ("imports", "python")


@dataclass(frozen=True)
class WorkflowSettings:
    #: Caps in LLM output tokens, not USD: a token means the same on every model and route -- a subscription's
    #: window fills with them too -- and output is what costs (input is mostly cached).
    default_max_output_tokens: int
    max_output_tokens_ceiling: int
    default_concurrency: int
    #: Agent activities of every workflow run of this process at once; None: no bound beyond each run's.
    max_concurrent_agents: Optional[int] = None
    #: The most a workflow may set as its own concurrency; None: default_concurrency.
    max_concurrency: Optional[int] = None


def _number(raw: dict[str, Any], key: str, *, whole: bool = False) -> Any:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or (
            whole and not isinstance(value, int)):
        raise ValueError(f"{SETTINGS_KEY}.{key} must be a {'whole ' if whole else ''}number above 0, not {value!r}")
    return value


def settings_of(server_config: Any) -> WorkflowSettings:
    """The instance's settings; ValueError names what is missing or wrong -- the answer the caller reads."""
    raw = getattr(server_config, SETTINGS_KEY, None)
    if raw is None:
        raise ValueError(f"dynamic workflows are off: the stategraph instance has no {SETTINGS_KEY}: block "
                         f"({', '.join(REQUIRED)}) -- an operator sets the caps there")
    if not isinstance(raw, dict):
        raise ValueError(f"{SETTINGS_KEY} must be a mapping, not {type(raw).__name__}")
    missing = [key for key in REQUIRED if key not in raw]
    if missing:
        raise ValueError(f"dynamic workflows are off: {SETTINGS_KEY} lacks {', '.join(missing)}")
    settings = WorkflowSettings(
        default_max_output_tokens=_number(raw, "default_max_output_tokens", whole=True),
        max_output_tokens_ceiling=_number(raw, "max_output_tokens_ceiling", whole=True),
        default_concurrency=_number(raw, "default_concurrency", whole=True),
        max_concurrent_agents=(_number(raw, "max_concurrent_agents", whole=True)
                               if raw.get("max_concurrent_agents") is not None else None),
        max_concurrency=(_number(raw, "max_concurrency", whole=True)
                         if raw.get("max_concurrency") is not None else None))
    if settings.default_max_output_tokens > settings.max_output_tokens_ceiling:
        raise ValueError(f"{SETTINGS_KEY}.default_max_output_tokens ({settings.default_max_output_tokens}) is above "
                         f"max_output_tokens_ceiling ({settings.max_output_tokens_ceiling})")
    return settings


def inline_problems(data: Any) -> list[str]:
    """Why a parsed machine cannot run inline: it reads files beside it, which an inline machine has not."""
    if not isinstance(data, dict):
        return ["the YAML is not a machine (a mapping with stategraph: 1)"]
    return [f"{key}: an inline workflow is one self-contained text -- put submachines under machines:, "
            "and no companion module" for key in NOT_INLINE if key in data]


def with_terms(text: str, settings: WorkflowSettings, max_output_tokens: Optional[int]) -> tuple[str, int, int]:
    """The machine's text with the limits it runs under written in, and those limits (max_output_tokens,
    concurrency).

    max_output_tokens: the smaller of the caller's and the machine's own, else either, else the default -- never
    above the ceiling (refused, not lowered: the caller learns its plan is too big). A limits.max_cost the machine
    sets stays, as one bound more. concurrency: the machine's, else the default, at most max_concurrency (else the
    default): the cap is checked before an activity starts, so what runs at once is what can still go past it.
    Written into the text, the limits are in the run's snapshot: the panel shows them, a replay reads them."""
    from .model.loader import parse_yaml
    from .model.yamledit import apply_op

    data = parse_yaml(text)
    limits = data.get("limits") if isinstance(data, dict) and isinstance(data.get("limits"), dict) else {}
    own = limits.get("max_output_tokens")
    if own is not None and (isinstance(own, bool) or not isinstance(own, int) or own <= 0):
        raise ValueError(f"limits.max_output_tokens must be a whole number above 0, not {own!r}")
    if max_output_tokens is not None and (isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int)
                                          or max_output_tokens <= 0):
        raise ValueError(f"max_output_tokens must be a whole number above 0 (LLM output tokens), "
                         f"not {max_output_tokens!r}")
    given = [v for v in (own, max_output_tokens) if v is not None]
    cap = min(given) if given else settings.default_max_output_tokens
    if cap > settings.max_output_tokens_ceiling:
        raise ValueError(f"max_output_tokens {cap} is above this installation's ceiling of "
                         f"{settings.max_output_tokens_ceiling} ({SETTINGS_KEY}.max_output_tokens_ceiling)")
    concurrency = limits.get("concurrency")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        concurrency = settings.default_concurrency
    concurrency = min(concurrency, settings.max_concurrency or settings.default_concurrency)
    terms = {**limits, "max_output_tokens": cap, "concurrency": concurrency}
    return apply_op(text, {"op": "update_machine", "fields": {"limits": terms}}), cap, concurrency


# --------------------------------------------------------------------- the places of their agents

#: One semaphore per event loop (a semaphore serves one; weakly, so a closed loop's go with it -- an id would be
#: reused), shared by every workflow run of the process whatever capacity its row carries: the first one sizes it.
_places: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_places_guard = threading.Lock()


def agent_places(capacity: Optional[int]) -> Optional[asyncio.Semaphore]:
    """The places an agent activity of a workflow run waits for (``max_concurrent_agents``); None without one.
    ponytail: sized by the first run that asks -- a changed setting takes effect with the next process."""
    if not capacity:
        return None
    with _places_guard:
        return _places.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(int(capacity)))


# --------------------------------------------------------------------- results waiting for their caller

class WaitingResults:
    """The workflow runs that ended and whose caller has not read them yet, by the caller's session.

    Kept in runs.db (RunStore.park_result): a woken caller often runs in another process -- a wake of an idle
    session starts one -- and reads them there. This process's own sessions are also noted in memory, so its hook
    finds a result that ended during a turn without asking the database on every call; a turn's first call asks it
    (``waiting_for(..., look_up=True)``).

    A result counts as delivered once its turn stands in a history a call was made with, not when the hook appends
    it: a later hook of the same chain can still fail or be cancelled, and the turn never reaches the history. Until
    then it is ``handed`` (this process's memory) and still waiting in the store -- the next call appends it again
    if it is missing there. Delivering is one atomic update: two processes never mark the same result."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._here: set[str] = set()
        self._handed: dict[str, dict[str, str]] = {}
        self._guard = threading.Lock()

    def add(self, session_id: str, run_id: str, ended_at: Optional[str] = None) -> None:
        self.store.park_result(run_id, session_id, ended_at)
        with self._guard:
            self._here.add(session_id)

    def hand(self, session_id: str, run_id: str, head: str) -> None:
        """Its turn (starting with ``head``) was appended to a call's messages; delivered once a history holds it."""
        with self._guard:
            self._handed.setdefault(session_id, {})[run_id] = head

    def handed(self, session_id: str) -> dict[str, str]:
        with self._guard:
            return dict(self._handed.get(session_id) or {})

    def waiting_for(self, session_id: str, *, look_up: bool = False) -> list[str]:
        """The results waiting for ``session_id``: from the store when this process ran one for it since, or with
        ``look_up``. Nothing is delivered yet -- ``deliver`` is the one that counts."""
        with self._guard:
            here = session_id in self._here
            self._here.discard(session_id)
        return self.store.waiting_results(session_id) if here or look_up else []

    def deliver(self, run_id: str, session_id: Optional[str] = None) -> bool:
        """Mark it delivered (and no longer handed to ``session_id``); True for the one call of any process that did."""
        if session_id is not None:
            with self._guard:
                self._handed.get(session_id, {}).pop(run_id, None)
        return self.store.deliver_result(run_id)

    def read(self, run_id: str, session_id: str) -> None:
        self.store.read_result(run_id, session_id)

    def waiting(self, run_id: str) -> bool:
        return self.store.result_waiting(run_id)
