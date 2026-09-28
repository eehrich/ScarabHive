"""What a run's calls are held to, and what the runs it starts inherit.

A sub-run -- a sub-agent, an agent called as a tool, a stategraph activity --
has a request id that extends the id of the run that started it
(``<run>_003_sub_x1``, ``<run>_004``). The spawn call itself passes the parent's
hook, so the parent's policy is recorded before the child starts. A child whose
agent has the hook on looks up every recorded run above it and is held to all
of them: never looser than any run above it (a stale entry under a reused id
can only add rules).

Combining a run's own settings with the policy it inherits:

* mode: the stricter of the two (off < auto < ask) -- a parent in ask makes
  the sub-run ask.
* deny: every rule of every level.
* allow: in ask mode a call runs without a question only when it matches an
  allow rule of EVERY level that asks -- a child cannot widen what its parent
  lets through unasked.
* unattended: block when any level that asks says block.
* "allow for this session": a grant counts only where it was given for EVERY
  asking level -- each level is the session (and owner) of a run that asks. A
  grant in one session cannot reach a chain it was not given in.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from agent_system.servers.agent.components.status_forwarding import run_is_live

from .rules import Rule, first_match

_STRICTNESS = {"off": 0, "auto": 1, "ask": 2}

#: Runs whose policy is kept; beyond this the oldest one is forgotten.
MAX_RUNS = 10000
#: Seconds between sweeps that drop the policies of runs that ended.
SWEEP_SECONDS = 60.0


@dataclass(frozen=True)
class AskLevel:
    """A run in ask mode, as the runs under it are held to it: its allow rules,
    its unattended answer, and whose session it asks in (owner, session id) --
    where a person's "allow for this session" for it is kept."""

    allow: Tuple[Rule, ...]
    unattended: str
    session: Tuple[Optional[str], str] = (None, "")


@dataclass(frozen=True)
class Policy:
    """The effective rules of one run: its own, and every run's above it."""

    mode: str
    deny: Tuple[Rule, ...]
    ask_levels: Tuple[AskLevel, ...]

    @classmethod
    def own(cls, mode: str, deny: Tuple[Rule, ...], allow: Tuple[Rule, ...], unattended: str,
            session: Tuple[Optional[str], str] = (None, "")) -> "Policy":
        return cls(mode=mode, deny=deny,
                   ask_levels=(AskLevel(allow, unattended, session),) if mode == "ask" else ())

    def under(self, parent: Optional["Policy"]) -> "Policy":
        """This run's own policy held to ``parent``'s as well."""
        if parent is None:
            return self
        mode = self.mode if _STRICTNESS[self.mode] >= _STRICTNESS[parent.mode] else parent.mode
        return Policy(mode=mode, deny=tuple(dict.fromkeys(self.deny + parent.deny)),
                      ask_levels=tuple(dict.fromkeys(self.ask_levels + parent.ask_levels)))

    def allows(self, name: str, server: str, arguments: Mapping[str, Any]) -> bool:
        """Whether an allow rule of every level that asks names the call."""
        return bool(self.ask_levels) and all(
            first_match(level.allow, name, server, arguments) is not None for level in self.ask_levels)

    @property
    def grant_sessions(self) -> Tuple[Tuple[Optional[str], str], ...]:
        """The sessions a grant must hold in: one per asking level."""
        return tuple(dict.fromkeys(level.session for level in self.ask_levels))

    @property
    def unattended(self) -> str:
        return "block" if any(level.unattended == "block" for level in self.ask_levels) else "allow"


class PolicyStore:
    """The policy of every run under approval, by request id.

    Kept while the run or a run under it streams (``run_is_live``), and for
    SWEEP_SECONDS after its last call: an async sub-agent may start after the
    run that started it ended -- its own stream begins only once the manager
    has set it up -- and must still find what it inherits. Bounded, and swept
    of runs that ended.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._policies: "OrderedDict[str, Tuple[Policy, float]]" = OrderedDict()
        self._clock = clock
        self._last_sweep = clock()

    def record(self, run_id: str, policy: Policy) -> None:
        if not run_id:
            return
        now = self._clock()
        self._policies.pop(run_id, None)
        self._policies[run_id] = (policy, now)
        if now - self._last_sweep >= SWEEP_SECONDS or len(self._policies) > MAX_RUNS:
            self._last_sweep = now
            for ended in [rid for rid, (_, at) in self._policies.items()
                          if now - at >= SWEEP_SECONDS and not run_is_live(rid)]:
                del self._policies[ended]
        if len(self._policies) > MAX_RUNS:
            # the ended runs first, oldest first -- a parent waiting on a long child
            # ages too, and must not be the one forgotten; and only as many as the
            # cap needs: one look per entry, stopping at the first ended one
            for rid in list(self._policies):
                if len(self._policies) <= MAX_RUNS:
                    break
                if not run_is_live(rid):
                    del self._policies[rid]
        while len(self._policies) > MAX_RUNS:
            self._policies.popitem(last=False)

    def inherited(self, request_id: str) -> Optional[Policy]:
        """The policies of every run above ``request_id`` -- its id with levels
        taken off the right at each ``_`` -- held together. Every one of them,
        not just the nearest: an entry left by an ended run under a reused id
        must only ever add rules, never stand in for a stricter run above it.
        Not the id itself -- a run is not its own ancestor."""
        combined: Optional[Policy] = None
        candidate = request_id or ""
        while "_" in candidate:
            candidate = candidate.rsplit("_", 1)[0]
            entry = self._policies.get(candidate)
            if entry is not None:
                combined = entry[0] if combined is None else combined.under(entry[0])
        return combined

    def __len__(self) -> int:
        return len(self._policies)
