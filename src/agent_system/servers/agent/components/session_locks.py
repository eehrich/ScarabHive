"""The session lock protocol of a SessionTracker: one run (or one short write) per session at a time.

A mixin, not an object of its own: the lock state lives on the tracker
(SessionTracker.__init__ sets ``_lock``, ``_session_locks``,
``_session_lock_owners``, ``_session_lock_writers``, ``_session_lock_written``,
``_session_lock_users``), because the rest of the tracker reads it -- which
request a session's owner is, whether a run has it, what goes once the run lets
go (discard_session).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from .session_tree import _parent_held

logger = logging.getLogger(__name__)


class SessionLocks:
    # Set by SessionTracker.__init__ (see the module docstring).
    _lock: asyncio.Lock
    _session_locks: Dict[str, asyncio.Lock]
    _session_lock_owners: Dict[str, str]
    _session_lock_writers: set[str]
    _session_lock_written: dict[str, asyncio.Event]
    _session_lock_users: Dict[str, int]
    _request_to_session: Dict[str, str]
    _session_metadata: Dict[str, Dict[str, Any]]
    _discard_on_release: set[str]
    _orphans: set[str]

    def discard_session(self, session_id: str) -> None:  # SessionTracker's
        raise NotImplementedError

    async def acquire_session_lock(self, session_id: str, request_id: str, timeout: float = 5.0,
                                   writer: bool = False) -> bool:
        """Acquire exclusive lock for a session.
        
        Args:
            session_id: The session ID to lock
            request_id: The request ID acquiring the lock
            timeout: Maximum time to wait for lock (seconds)
            writer: A write no run makes -- an append, /undo's cut (api/session_writes.beside_the_runs) --
                held from before it reads the session until its save is done. A request that
                meets one waits for it (up to ``timeout``) rather than being refused: it is
                over in a moment, while a run is not. A writer that meets a run is refused.
            
        Returns:
            True if lock acquired, False if timeout or already locked by another request
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            async with self._lock:
                owner = self._session_lock_owners.get(session_id)
                if owner == request_id:
                    # Same request already owns the lock (re-entrant)
                    logger.debug("Request %s already owns lock for session %s", request_id, session_id)
                    return True
                if owner is not None and session_id not in self._session_lock_writers:
                    logger.warning("Session %s is already locked by request %s (request %s waiting)",
                                 session_id, owner, request_id)
                    return False
                if owner is None:
                    # Create lock if it doesn't exist
                    if session_id not in self._session_locks:
                        self._session_locks[session_id] = asyncio.Lock()
                    # Counted in the same step the lock is looked up: from here on this
                    # request holds or waits on it, and it is not taken away meanwhile.
                    lock = self._session_locks[session_id]
                    others = self._session_lock_users.get(session_id, 0)  # holding it, or waiting on it
                    self._session_lock_users[session_id] = others + 1
                    break
                # A writer has it. Waited for until it is done, not in the lock's queue: a run
                # may take the lock next, and whoever meets a run is refused at once -- queued
                # behind it, they waited out their whole timeout.
                written = self._session_lock_written.setdefault(session_id, asyncio.Event())
                logger.debug("Session %s is being written by %s; request %s waits for it",
                             session_id, owner, request_id)
            try:
                await asyncio.wait_for(written.wait(), timeout=max(deadline - loop.time(), 0))
            except TimeoutError:
                logger.warning("Request %s timed out waiting for the write of session %s", request_id, session_id)
                return False

        # Try to acquire the lock with timeout
        acquired = owned = False
        try:
            if others:
                # Taken and not yet owned, or handed on to one that waited -- a moment between
                # the two: waited for while time is left.
                await asyncio.wait_for(lock.acquire(), timeout=max(deadline - loop.time(), 0))
            else:
                # Nobody holds or waits on it: taken at once. Through wait_for, a timeout of 0 --
                # or none left after a writer -- refused a free lock, and before Python 3.12 its
                # task paused between taking and owning, where the next asker queued behind a run.
                # No timeout, and none needed while the tracker's own lock is never held across
                # an await: then a count of 0 means the lock is free, and this does not wait.
                await lock.acquire()
            acquired = True
            async with self._lock:
                self._session_lock_owners[session_id] = request_id
                if writer:
                    self._session_lock_writers.add(session_id)
            owned = True
            logger.info("Request %s acquired lock for session %s", request_id, session_id)
            return True
        except asyncio.TimeoutError:
            logger.warning("Request %s timed out waiting for lock on session %s", request_id, session_id)
            return False
        finally:
            # Gave up waiting (timed out, cancelled), or cancelled on the way to owning it.
            # Only for the lock this request counted on: delete_session or clear() may have
            # taken it meanwhile -- they released it and dropped its count themselves, and a
            # count there now belongs to the session's NEXT lock.
            if not owned and self._session_locks.get(session_id) is lock:
                if acquired:
                    # Taken but never owned: release goes by the owner, so nobody else would
                    # release it. (A window nothing opens today -- the tracker's lock is never
                    # held across an await -- kept closed all the same.)
                    lock.release()
                self._let_go_of_session_lock(session_id)
    
    async def release_session_lock(self, session_id: str, request_id: str) -> None:
        """Release exclusive lock for a session.
        
        Args:
            session_id: The session ID to unlock
            request_id: The request ID releasing the lock
        """
        async with self._lock:
            if session_id not in self._session_lock_owners:
                logger.debug("No lock owner for session %s (request %s trying to release)",
                           session_id, request_id)
                return
            
            owner = self._session_lock_owners[session_id]
            if owner != request_id:
                logger.warning("Request %s tried to release lock owned by %s for session %s",
                             request_id, owner, session_id)
                return
            
            # Remove ownership
            del self._session_lock_owners[session_id]
            self._written(session_id)
        
        # Release the actual lock
        if session_id in self._session_locks:
            lock = self._session_locks[session_id]
            if lock.locked():
                lock.release()
                logger.info("Request %s released lock for session %s", request_id, session_id)
        self._let_go_of_session_lock(session_id)
        self._discard_once_let_go(session_id)

    def _discard_once_let_go(self, session_id: str) -> None:
        """What was discarded while a run had the session, once nobody holds it: the session itself
        (discard_session), or one below a session discarded meanwhile -- unless its parent is back in its agent's
        tracker."""
        if session_id in self._session_lock_owners:
            return
        if session_id in self._discard_on_release:
            self._discard_on_release.discard(session_id)
            self._orphans.discard(session_id)
            self.discard_session(session_id)
        elif session_id in self._orphans:
            self._orphans.discard(session_id)
            if not _parent_held(self._session_metadata.get(session_id) or {}):
                self.discard_session(session_id)

    def _let_go_of_session_lock(self, session_id: str) -> None:
        """One request fewer holding or waiting on the session's lock. The lock goes
        with the last of them -- and only then: while anybody holds it, waits on it
        or owns the session, a new request must meet the SAME lock, or two runs
        would write one session at once.

        Synchronous on purpose: it runs in a finally of a cancelled acquire too,
        where an await could be cut short, and without an await nothing interleaves.
        """
        users = self._session_lock_users.get(session_id, 0) - 1
        if users > 0:
            self._session_lock_users[session_id] = users
            return
        self._session_lock_users.pop(session_id, None)
        lock = self._session_locks.get(session_id)
        if lock is not None and not lock.locked() and session_id not in self._session_lock_owners:
            del self._session_locks[session_id]
    
    def check_session_locked(self, session_id: str) -> tuple[bool, Optional[str]]:
        """Check if a session is currently locked.
        
        Args:
            session_id: The session ID to check
            
        Returns:
            Tuple of (is_locked, owner_request_id)
        """
        if session_id in self._session_lock_owners:
            return True, self._session_lock_owners[session_id]
        return False, None

    def _written(self, session_id: str) -> None:
        """The session's lock is no writer's any more: what waits for the writer asks again."""
        self._session_lock_writers.discard(session_id)
        written = self._session_lock_written.pop(session_id, None)
        if written is not None:
            written.set()

    def run_has(self, session_id: str) -> bool:
        """Whether a run has the session right now: its lock is owned by the request registered for the session
        (register_request) -- not by a write no run makes (held_by_a_writer), nor by an opening or by a request
        that holds the lock for a run not started yet. From the lock's taking to its release, also after the run
        let go of its entry in the active requests (its finalize, before the last save)."""
        owner = self._session_lock_owners.get(session_id)
        return (owner is not None and session_id not in self._session_lock_writers
                and self._request_to_session.get(owner) == session_id)

    def held_by_a_writer(self, session_id: str) -> bool:
        """Whether the session's lock is owned by a write no run makes (acquire_session_lock(writer=True)): no run
        has it, whatever check_session_locked says -- a moment from now, nobody does."""
        return session_id in self._session_lock_writers
