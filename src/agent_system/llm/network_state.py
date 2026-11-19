"""Is the network gone, as the agent loops of this process see it?

Every agent loop already learns the answer the hard way: a call that could not even reach its
provider (connect timed out, no route, a name that does not resolve) is the evidence of an outage,
an answer from any LLM is the evidence against it. This module keeps that evidence for the whole
process, so that what one run has seen helps another:

- a run whose chain cannot tell (a coding CLI that fails without saying why) waits for the network
  when other runs have just found it gone;
- a run waiting out an outage does not spend its network_outage_seconds while other runs find the
  network gone too -- the budget is for providers that are down while the network is up;
- a sub-agent wait does not time out on a sub-agent that is only waiting for the network.

The network counts as gone when, since the last answer, at least two different hosts could not be
reached and the latest of those failures is recent. One host is not enough: a base_url with a typo,
or one provider's name gone, is not the network. And a run asks with ``exclude`` = itself: its own
failures are not evidence for its own wait, or a chain of two hosts behind a firewall would keep
its outage alive and wait forever. A process with a single run, or with no traffic that can tell
(only coding CLIs), therefore behaves as before. Even with the network gone a run's budget stands
still for at most LONGEST_OUTAGE_S, then runs again: the evidence ends with the runs that give it.

No probe of its own: nothing is sent anywhere, so a proxy or a captive portal cannot fool it. An
answer of a local model clears the evidence like any other: a process that runs local models
during an internet outage falls back to the per-run budget.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

#: How long an unreachable host counts as evidence. Runs waiting for the network ask again every
#: 10 s to 2 min and a round of a long chain takes a few minutes, so while the network is gone the
#: evidence is renewed well within this.
EVIDENCE_S = 600.0
#: How long a run's budget stands still for the network, however sure the evidence: past it the
#: budget runs again and the run ends -- an outage that long is something a person has to look at.
LONGEST_OUTAGE_S = 24 * 3600.0
#: The clock (tests replace it).
_now = time.monotonic

_lock = threading.Lock()   # the agent loops of a process can run on more than one event loop
#: host -> {who found it unreachable -> when}, since the last answer
_unreachable: dict[str, dict[str, float]] = {}


def note_answer() -> None:
    """An LLM answered: the network is there, the evidence so far is spent."""
    with _lock:
        _unreachable.clear()


def note_unreachable(host: str, seen_by: str = "") -> None:
    """A call to *host* could not reach it at all (*seen_by*: the run that found it)."""
    with _lock:
        _unreachable.setdefault(host or "?", {})[seen_by] = _now()


def network_down(exclude: Optional[str] = None) -> bool:
    """Two or more hosts unreachable since the last answer, the latest within EVIDENCE_S --
    counting only what runs other than *exclude* found."""
    with _lock:
        latest = [max(when for who, when in seen.items() if who != exclude)
                  for seen in _unreachable.values() if any(who != exclude for who in seen)]
    return len(latest) >= 2 and _now() - max(latest) <= EVIDENCE_S


def reset() -> None:
    """Forget what was seen (tests)."""
    with _lock:
        _unreachable.clear()
