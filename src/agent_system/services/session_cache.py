"""What a ``SessionManager`` keeps of its sessions in memory, and what it has seen of their files.

Two things, each bounded on its own: copies of recently used sessions (an LRU
with a TTL, so a load need not read the file again), and for each session the
moment this process last had its file as it is -- what
``SessionManager.changed_on_disk`` holds the file's mtime against.

Its own module because it is state with rules of its own -- how old a copy
may be, which one goes when the cache is full, why the stamps are not tied to
the copies -- that the manager's methods only use: they put what they read or
wrote, take what is still fresh, and drop what they delete. It takes no lock:
every method is synchronous, so on the event loop no other task gets between
its steps.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SessionCache:
    """The session copies and seen stamps of one ``SessionManager``."""

    #: How many sessions keep what was seen of them (changed_on_disk). A stamp does not grow old -- the file is
    #: newer than it or not -- so it goes only when newer ones push it out.
    SEEN_KEPT = 10000

    def __init__(self) -> None:
        # In-memory cache: {session_id: (session_data, timestamp)}
        self.entries: Dict[str, tuple[Dict[str, Any], float]] = {}
        # When this process last had a session's file as it is: its own writes, and a load read into a tracker
        # (mark_seen). Apart from the cache: a load only to show or ask a session caches it, and counted as seen
        # it hid what another process wrote from changed_on_disk. Bounded on its own (SEEN_KEPT): tied to the
        # cache, sessions only browsed pushed out the stamps of sessions that sit in a tracker.
        self.seen: dict[str, float] = {}
        self.ttl = 300  # 5 minutes
        self.max_size = 200  # Maximum cached sessions to prevent memory leak

    def fresh(self, session_id: str) -> Optional[tuple[Dict[str, Any], float]]:
        """The cached copy of a session and when it was cached -- None when there is none younger than the TTL."""
        if session_id in self.entries:
            cached_data, cached_time = self.entries[session_id]
            if time.time() - cached_time < self.ttl:
                return cached_data, cached_time
        return None

    def put(self, session_id: str, session_data: Dict[str, Any]) -> None:
        """Cache a copy just read from the file. Not seen: see ``mark_seen``."""
        self.entries[session_id] = (session_data, time.time())

    def wrote(self, session_id: str, session_data: Dict[str, Any]) -> None:
        """Cache a copy this process just wrote -- and what it wrote is seen."""
        self.entries[session_id] = (session_data, time.time())
        self.saw(session_id, self.entries[session_id][1])  # written here: seen

    def drop(self, session_id: str) -> None:
        """A deleted session: neither its copy nor its stamp stays."""
        self.entries.pop(session_id, None)
        self.seen.pop(session_id, None)

    def cleanup(self) -> None:
        """Clean up cache: remove expired entries and apply LRU eviction."""
        now = time.time()

        # Remove expired entries
        expired = [
            sid for sid, (_, ts) in self.entries.items()
            if now - ts > self.ttl
        ]
        for sid in expired:
            del self.entries[sid]

        if expired:
            logger.debug(f"SessionManager: Cleaned up {len(expired)} expired cache entries")

        # LRU eviction if still over limit
        while len(self.entries) >= self.max_size:
            # Find oldest entry
            oldest = min(self.entries.keys(), key=lambda k: self.entries[k][1])
            del self.entries[oldest]
            logger.debug(f"SessionManager: Evicted cache entry {oldest} (LRU)")

    def mark_seen(self, session_id: str) -> None:
        """The session's last load went into a tracker: what it read counts as seen (changed_on_disk)."""
        cached = self.entries.get(session_id)
        if cached is not None:
            self.saw(session_id, cached[1])

    def saw(self, session_id: str, when: float) -> None:
        self.seen.pop(session_id, None)
        self.seen[session_id] = when
        while len(self.seen) > self.SEEN_KEPT:
            del self.seen[next(iter(self.seen))]

    def clear(self) -> None:
        """Forget every copy and every stamp."""
        self.entries.clear()
        self.seen.clear()

    def stats(self) -> Dict[str, Any]:
        """Size, bounds and the cached session ids (SessionManager.get_cache_stats)."""
        return {
            "size": len(self.entries),
            "max_size": self.max_size,
            "ttl_seconds": self.ttl,
            "entries": list(self.entries.keys())
        }
