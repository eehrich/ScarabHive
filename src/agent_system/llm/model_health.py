"""Which LLMs are blocked -- for every agent in this process.

A rate limit, an exhausted quota or a refused key is a fact about the LLM, not
about the agent that ran into it. So the block lives here, keyed by the LLM
(its endpoint, credential and model name), and every agent reads the same answer: an LLM
blocked by one agent is skipped by all of them, an LLM that answers again is
free for all of them, and an agent that picks another, unblocked LLM is not
held back by someone else's block.

What an agent does with a blocked LLM -- take the next one of its chain, or
call it anyway because nothing else is left -- is the agent's decision; this
module only keeps the facts.

A block that has run out is not simply open again: the first request to ask
probes the LLM, the others keep treating it as blocked until that probe
answers (release or a new, longer block) or its lease runs out. Twenty agents
waiting on the same model would otherwise walk into the same 429 together.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# The first rate-limit block: long enough for a provider's per-minute window,
# short enough that a blip costs a minute on the fallback, not an hour. Every
# further failure without an answer in between doubles it, up to max_pause.
FIRST_RATE_LIMIT_PAUSE = 60.0

# How long a probe holds a run-out block for its request. A probe that never
# reports back (cancelled, 5xx, crashed) frees the LLM for the next prober
# after this.
PROBE_LEASE = 120.0

ModelKey = Tuple[str, str, str]


def model_key(client: Any) -> Optional[ModelKey]:
    """The LLM behind a client: (endpoint, credential, model).

    Two profiles naming the same model at the same endpoint with the same key
    are one LLM, whatever client class talks to them at that URL. The endpoint
    is the client's base_url; a client without one (the SDK clients, the batch
    client) is its own endpoint, named by its class -- a batch quota is not the
    sync quota of the same model. The credential is a fingerprint of the API
    key, never the key: a refused or exhausted key says nothing about another.
    None for a client without a model name -- nothing to block it by.

    ponytail: the key has no provider routing, so a 429 from one OpenRouter
    backend blocks the model for profiles routed to another backend too. Add the
    routing to the key if that ever costs more than a minute on the fallback.
    Likewise a client without a URL of its own (the SDK and batch clients) is
    one endpoint per class: two such clients of the same model with the same
    key share a block whatever server they reach.
    """
    model = getattr(client, "model", None)
    if not isinstance(model, str) or not model:
        return None
    base_url = getattr(client, "base_url", None) or getattr(client, "_base_url", None)
    endpoint = base_url.rstrip("/") if isinstance(base_url, str) and base_url else type(client).__name__
    api_key = getattr(client, "api_key", None)
    credential = (hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
                  if isinstance(api_key, str) and api_key else "")
    return endpoint, credential, model


@dataclass
class _Block:
    since: float
    until: float
    pause: float
    prober: Optional[str] = None
    probe_until: float = 0.0


class ModelHealth:
    """The blocks of one process. Use the module's ``model_health``.

    ponytail: per process. The writer's job workers run their own; a block
    seen by the API does not reach them. Share it (a table in a database both
    open) if cross-process 429s show up in the logs.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._blocks: Dict[ModelKey, _Block] = {}
        self._lock = threading.Lock()
        # Bumped by every block and release: a caller that picked an LLM before
        # a long wait (the pre-LLM hooks) sees whether its pick may be stale.
        self.version = 0

    def available(self, client: Any, owner: str) -> bool:
        """Whether *owner* (a request) may call *client* now.

        Asking about a block that has run out makes *owner* its prober, so ask
        only about the LLM you are going to call (or hand the probe back with
        :meth:`drop_probe`). Asking again does not renew the lease: a request
        whose probes keep ending without a verdict (5xx) holds the LLM for one
        lease, not for as long as it keeps asking.
        """
        key = model_key(client)
        if key is None:
            return True
        with self._lock:
            block = self._blocks.get(key)
            if block is None:
                return True
            now = self._clock()
            if now < block.until:
                return False
            if block.prober is not None and now < block.probe_until:
                return block.prober == owner
            block.prober, block.probe_until = owner, now + PROBE_LEASE
            return True

    def drop_probe(self, client: Any, owner: str) -> None:
        """*owner* will not call the LLM it holds the probe of after all: the
        next request may probe it at once, not only after the lease."""
        key = model_key(client)
        if key is None:
            return
        with self._lock:
            block = self._blocks.get(key)
            if block is not None and block.prober == owner:
                block.prober, block.probe_until = None, 0.0

    def block(self, client: Any, *, max_pause: float, rate_limit: bool,
              retry_after: Optional[float] = None, asked_at: Optional[float] = None,
              reason: str = "") -> Optional[float]:
        """Block the LLM behind *client* for every agent; returns how many
        seconds it stays blocked.

        rate_limit=True: FIRST_RATE_LIMIT_PAUSE, doubled on every failure since
        the LLM last answered, up to *max_pause*. rate_limit=False (quota
        exhausted, key or model refused): *max_pause* at once -- that does not
        get better in a minute. A provider's *retry_after* is the lower bound
        either way. None when the client has no model name to block by.

        *asked_at* is when the failed call went out (:meth:`now`). A rate limit
        on a call that went out before the running block was set belongs to
        the burst that block already answers: seven requests in flight when a
        window closes are one failure, not six doublings to the cap. And no
        block shortens a longer one still running -- an agent with a short
        cap does not cut another agent's quota block to its own.
        """
        key = model_key(client)
        if key is None:
            return None
        with self._lock:
            now = self._clock()
            previous = self._blocks.get(key)
            same_burst = previous is not None and asked_at is not None and previous.since >= asked_at
            if not rate_limit:
                pause = max_pause
            elif previous is None:
                pause = min(FIRST_RATE_LIMIT_PAUSE, max_pause)
            elif same_burst:
                # One failure, answered by the block already set: no doubling.
                # Once that block has run out, a straggler of the burst says
                # nothing at all -- re-blocking would undo a probe that is
                # finding the LLM healthy right now.
                if previous.until <= now:
                    return 0.0
                pause = previous.pause
            else:
                pause = min(previous.pause * 2, max_pause)
            pause = max(pause, retry_after or 0.0)
            if pause <= 0:
                return 0.0                           # a zero cap blocks nothing
            if previous is not None and previous.until >= now + pause:
                return previous.until - now          # the running block covers it
            self._blocks[key] = _Block(since=now, until=now + pause, pause=pause)
            self.version += 1
        logger.warning("LLM %s blocked for %.0fs for every agent%s",
                       key[2], pause, f" ({reason})" if reason else "")
        return pause

    def now(self) -> float:
        """This registry's clock: what to pass to :meth:`release` as *asked_at*."""
        return self._clock()

    def release(self, client: Any, asked_at: Optional[float] = None) -> None:
        """The LLM behind *client* answered: free for every agent again.

        *asked_at* is when the answered call went out (:meth:`now`). A block
        set after that stays: an answer to a call that started before another
        request's 429 says nothing about the LLM since. A block from the same
        clock tick stays too (time.monotonic ticks in ~16 ms on Windows):
        keeping it costs one probe, lifting it a wave of 429s.
        """
        key = model_key(client)
        if key is None:
            return
        with self._lock:
            block = self._blocks.get(key)
            if block is None or (asked_at is not None and block.since >= asked_at):
                return
            del self._blocks[key]
            self.version += 1
        logger.info("LLM %s answers again: unblocked for every agent", key[2])

    def remaining(self, client: Any) -> float:
        """Seconds the block on *client*'s LLM still runs; 0 when there is none."""
        key = model_key(client)
        with self._lock:
            block = self._blocks.get(key) if key else None
            return max(0.0, block.until - self._clock()) if block else 0.0

    def clear(self) -> None:
        with self._lock:
            self._blocks.clear()
            self.version += 1


model_health = ModelHealth()
