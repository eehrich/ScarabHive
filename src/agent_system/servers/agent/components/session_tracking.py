"""
Session and request tracking for agent execution.

This module handles:
- Request-to-session mapping
- Message appending to active requests
- Message appending to persisted sessions
- Draining appended messages during execution
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from ....llm.message_roles import DEVELOPER
from ....llm.models import ChatMessage
from ....llm.text_sanitizer import sanitize_for_llm


def is_volatile_note(msg: object) -> bool:
    """A developer note bound to the one call it was built for.

    Deliberately NOT ``message_roles.is_injected_note``, which also matches a
    marked ``user`` message. One of those is a hook's scripted follow-up -- a
    turn somebody is meant to have said, and v4 reads it back out of the STORED
    transcript by its configured text. Widening this predicate would delete it.
    """
    return (getattr(msg, "role", None) == DEVELOPER
            and bool(getattr(msg, "injected_by", None)))


logger = logging.getLogger(__name__)


#: Every tracker of this process -- one per agent. A run's messages wait in the
#: tracker of the agent that runs it, and a question asked in a sub-run (another
#: agent) looks up whether the person wrote to a run above it (ask_user). A
#: session discarded takes the sessions other agents run below it along
#: (discard_session).
_trackers: "weakref.WeakSet[SessionTracker]" = weakref.WeakSet()


#: The sets watch_appended_sessions hands out, each collecting the ids of the sessions a message is appended to.
_append_watches: list[set[str]] = []


def watch_appended_sessions() -> set[str]:
    """A set that collects the id of every session a message is appended to from now on, in any agent's tracker --
    beside its runs (append_to_session) or handed to a run of it (append_user_message) -- until
    unwatch_appended_sessions. For a request that puts back sessions it does not know yet: openai_api's turn, for
    the sessions of the agents its run calls as tools (a message the person appended there is theirs). Kept only
    while somebody watches."""
    seen: set[str] = set()
    _append_watches.append(seen)
    return seen


def unwatch_appended_sessions(seen: set[str]) -> None:
    _append_watches[:] = [other for other in _append_watches if other is not seen]


def session_chain(session_id: str) -> list[str]:
    """*session_id* and every session above it, nearest first: an agent called as a tool runs on one of its
    own below its caller's (Agent.tool_session_id; its metadata names the caller's as its parent), through
    every level, whichever agent's tracker holds each. The last one is where the chain started; just
    *session_id* for any other session (none for an empty id)."""
    chain: list[str] = []
    while session_id and session_id not in chain:
        chain.append(session_id)
        session_id = next((metadata["parent_session_id"] for tracker in list(_trackers)
                           for metadata in (tracker._session_metadata.get(session_id),)
                           if isinstance(metadata, dict) and metadata.get("parent_session_id")), None)
    return chain


def callers_session(session_id: str) -> str:
    """The session a session run below another belongs to (the top of its session_chain), through every
    level. Itself for any other session."""
    chain = session_chain(session_id)
    return chain[-1] if chain else session_id


def _children(session_id: str, agent_name: Optional[str]) -> list[tuple["SessionTracker", str]]:
    """The sessions run below *session_id* of *agent_name* -- the agents it called as tools: their metadata names
    that session and that agent as their parent (Agent._open_tool_session) -- with the tracker of each. The agent
    counts: another agent's run on the same session id has children of its own."""
    return [(tracker, child) for tracker in list(_trackers)
            for child, metadata in list(tracker._session_metadata.items())
            if isinstance(metadata, dict) and metadata.get("parent_session_id") == session_id
            and metadata.get("parent_agent") == agent_name]


def held_below(session_id: str, agent_name: Optional[str]) -> list[tuple["SessionTracker", str]]:
    """Every session run below *session_id* of *agent_name*, through every level, with the tracker that holds
    it: what goes with it (discard_session), and what openai_api's turn deletes or puts back with it."""
    below: list[tuple["SessionTracker", str]] = []
    for tracker, child in _children(session_id, agent_name):
        below.append((tracker, child))
        below.extend(held_below(child, tracker.agent_name))
    return below


def sessions_below(session_id: str, agent_name: Optional[str]) -> list[str]:
    """The ids of held_below."""
    return [child for _, child in held_below(session_id, agent_name)]


def a_run_has(session_id: str) -> bool:
    """Whether a run of this process has *session_id* right now, in whichever agent's tracker (run_has)."""
    return any(tracker.run_has(session_id) for tracker in list(_trackers))


def _parent_held(metadata: dict) -> bool:
    """Whether the session a child session runs below is still in its agent's tracker."""
    parent, agent_name = metadata.get("parent_session_id"), metadata.get("parent_agent")
    return any(tracker.agent_name == agent_name and parent in tracker._session_metadata
               for tracker in list(_trackers))


def message_waits_for(request_id: str) -> bool:
    """Whether a message the person wrote into the active run ``request_id``
    waits for that run's next step, whichever agent runs it."""
    return bool(request_id) and any(tracker.has_appended(request_id) for tracker in list(_trackers))


class SessionTracker:
    """Manages request/session lifecycle and message appending.

    Responsibilities:
    - Track active requests and their associated sessions
    - Allow appending messages to active requests (for multi-turn conversations)
    - Allow appending messages directly to sessions
    - Drain pending appended messages during execution loops

    Concurrency: the append/drain/session-lock paths guard their check-then-act
    sequences with an asyncio.Lock; the remaining accessors are plain dict
    operations (atomic per event-loop step, no lock needed as long as callers
    don't await between check and act).

    Note: The _active_requests dict is shared with AgentRequestManager to ensure
    both components work with the same request entries.
    """

    def __init__(self, active_requests: Optional[Dict[str, Dict[str, Any]]] = None):
        """Initialize the session tracker.

        Args:
            active_requests: Shared active requests dict (from AgentRequestManager).
                           If None, creates its own dict (for testing).
        """
        # Active requests: request_id -> {'cancel': Event(), 'message_event': Event(), 'appended': List[ChatMessage]}
        # Note: We only manage the 'appended' list and 'message_event' here
        # The 'cancel' event is managed by AgentRequestManager
        # This dict is SHARED with AgentRequestManager for coordination
        self._active_requests: Dict[str, Dict[str, Any]] = active_requests if active_requests is not None else {}

        # The agent this tracker belongs to (Agent.__init__): the sessions its runs call agents below name it
        # as their parent_agent, and only its own discard of a session takes them along (discard_session).
        self.agent_name: Optional[str] = None
        # Sessions below a session that was discarded while their run had them: they go once it lets go of
        # them (release_session_lock), unless their parent is back by then.
        self._orphans: set[str] = set()
        # Sessions discarded while a run had them (discard_session): they go once that run lets go.
        self._discard_on_release: set[str] = set()

        # Persisted sessions: session_id -> List[ChatMessage]
        self._sessions: Dict[str, List[ChatMessage]] = {}

        # Session metadata: session_id -> Dict[str, Any] (user_id, etc.)
        self._session_metadata: Dict[str, Dict[str, Any]] = {}

        # Titles the caller gave a session its run starts: session_id -> title,
        # until a save has written it. Not in the metadata -- the API sets that
        # dict anew on every event of a new session's run.
        self._titles_to_write: Dict[str, str] = {}

        # Session template vars: session_id -> Dict[str, Any] (workflow_phase, book_id, etc.)
        # CRITICAL: These are SESSION-SCOPED, not shared across sessions using same agent
        self._session_template_vars: Dict[str, Dict[str, Any]] = {}

        # Request-to-session mapping: request_id -> session_id
        self._request_to_session: Dict[str, str] = {}

        # Session-level locks: session_id -> asyncio.Lock
        # Prevents multiple parallel requests from modifying the same session simultaneously
        self._session_locks: Dict[str, asyncio.Lock] = {}

        # Track which request owns which session lock: session_id -> request_id
        self._session_lock_owners: Dict[str, str] = {}
        # The sessions whose lock a writer owns (acquire_session_lock(writer=True)): waited for, not refused
        self._session_lock_writers: set[str] = set()
        # Set when a session's writer is done: what waits for it asks again then
        self._session_lock_written: dict[str, asyncio.Event] = {}

        # How many requests hold or wait on a session's lock: session_id -> count.
        # The lock goes with the last of them (_let_go_of_session_lock). Kept for
        # good, every session ever run -- each one-shot call's ephemeral session
        # included -- left its lock for the life of the process.
        self._session_lock_users: Dict[str, int] = {}

        # Compacted messages pending to be applied: session_id -> List[ChatMessage]
        # When a compaction tool runs mid-request, it stores the compacted messages here.
        # The agent will use these instead of the request's local messages when persisting.
        self._compacted_messages: Dict[str, List[ChatMessage]] = {}

        # Sessions whose start hooks have run: one taken back to no messages (/undo) is not new
        self._started: set[str] = set()
        # Sessions that held messages in this process: only such a one is empty on purpose
        self._held: set[str] = set()
        # When a request last opened a session for a run of its own (mark_opened): a serial number
        self._openings: Dict[str, int] = {}
        self._opening_serial = 0
        # Who collects the messages appended to a session (watch_appends): one list per watcher
        self._append_watchers: dict[str, list[list[ChatMessage]]] = {}

        # Lock for concurrent access (appends/drains/lock bookkeeping)
        self._lock = asyncio.Lock()
        _trackers.add(self)

    async def is_request_active(self, request_id: str) -> bool:
        """Check if a request is currently active (still running).
        
        Used by the status endpoint to determine if a request is still processing.
        
        Args:
            request_id: The request ID to check
            
        Returns:
            True if the request is active, False otherwise
        """
        async with self._lock:
            return request_id in self._active_requests

    async def append_user_message(self, request_id: str, content: str) -> bool:
        """
        Append a user message to an active request's conversation.

        Args:
            request_id: The request ID to append to
            content: The message content

        Returns:
            True if appended successfully, False if request not found
        """
        logger.debug("Append request received for request_id=%s: %s", request_id, content[:50])
        async with self._lock:
            if request_id in self._active_requests:
                entry = self._active_requests[request_id]
                if isinstance(entry, dict):
                    try:
                        msg = ChatMessage(
                            role="user",
                            content=sanitize_for_llm(content),
                            timestamp=datetime.now(timezone.utc)
                        )
                        entry.setdefault('appended', []).append(msg)
                        self._tell_watchers(self._request_to_session.get(request_id), msg)
                        # Notify run_events if it's waiting
                        try:
                            entry['message_event'].set()
                        except Exception as e:
                            logger.debug(f"Failed to set message event: {e}")
                        logger.debug("Message appended to active request %s", request_id)
                        return True
                    except Exception as e:
                        logger.debug("Failed to append message to request %s: %s", request_id, e)
                        return False
        logger.debug("Request %s not found for append", request_id)
        return False

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

    def active_sessions(self) -> Dict[str, str]:
        """Every session this server is running right now: session_id -> request_id.

        The same ownership ``check_session_locked`` answers for one session, for
        all of them at once -- the sidebar asks "which of these is busy", and
        asking per row would be one call per visible node.

        A plain snapshot, taken without the async lock on purpose: the caller is
        a UI poll, and a session that starts or ends between two polls is shown
        one tick later either way. Blocking the run loop's lock for that would
        buy nothing.
        """
        return dict(self._session_lock_owners)

    async def append_to_session(self, session_id: str, content: str) -> bool:
        """
        Append a user message directly to a persisted session.

        Args:
            session_id: The session ID to append to
            content: The message content

        Returns:
            True if appended successfully, False if session not found
        """
        logger.debug("Session append request for session_id=%s: %s", session_id, content[:50])
        async with self._lock:
            if session_id in self._sessions:
                try:
                    msg = ChatMessage(
                        role="user",
                        content=sanitize_for_llm(content),
                        timestamp=datetime.now(timezone.utc)
                    )
                    self._sessions[session_id].append(msg)
                    self._tell_watchers(session_id, msg)
                    logger.debug("Message appended to session %s", session_id)
                    return True
                except Exception as e:
                    logger.debug("Failed to append message to session %s: %s", session_id, e)
                    return False
        logger.debug("Session %s not found for append", session_id)
        return False

    async def drain_appended_messages(self, request_id: str, messages: List[ChatMessage]) -> List[ChatMessage]:
        """
        Drain any appended messages for a request and add them to the conversation.

        Args:
            request_id: The request ID to drain messages from
            messages: The current message list to extend

        Returns:
            The updated messages list with appended messages added
        """
        async with self._lock:
            entry = self._active_requests.get(request_id)
            if isinstance(entry, dict):
                appended = entry.get('appended', [])
                if appended:
                    messages.extend(appended)
                    entry['appended'] = []
                    logger.debug("Drained %d appended messages for request %s", len(appended), request_id)
                    # Clear message_event
                    try:
                        entry['message_event'].clear()
                    except Exception as e:
                        logger.debug(f"Failed to clear message event: {e}")
        return messages

    def has_appended(self, request_id: str) -> bool:
        """Whether a message appended to the active request waits for its next
        step (append_user_message, not yet drained): a person wrote into the
        run. A tool that waits for that person (ask_user) stops waiting then."""
        entry = self._active_requests.get(request_id)
        return isinstance(entry, dict) and bool(entry.get('appended'))

    def register_request(self, request_id: str, session_id: str, request_entry: Dict[str, Any]) -> None:
        """
        Register a new active request's session mapping.

        Args:
            request_id: The request ID
            session_id: The associated session ID
            request_entry: The request entry dict (should already be in shared _active_requests)

        Note: The request_entry should already be registered in the shared _active_requests
        dict by AgentRequestManager. This method only sets up the session mapping.
        """
        # Verify the entry exists in shared dict (defensive check)
        if request_id not in self._active_requests:
            # If not already there, add it (shouldn't happen in normal flow)
            self._active_requests[request_id] = request_entry
            logger.debug("Request entry added to shared dict for %s (unexpected)", request_id)

        # Ensure session exists
        self._sessions.setdefault(session_id, [])
        # Map request to session
        self._request_to_session[request_id] = session_id
        self._forget_ended_requests()

    #: How many ended requests keep their request -> session mapping. It outlives the request on purpose -- an
    #: append to a run that ended a moment ago still finds its session (app: /events/{id}/append falls back to
    #: it), and the admin view and the job manager name a finished job's session -- but kept for every request,
    #: a one-shot call's included (hundreds per book), it grew for the life of the process.
    ENDED_REQUESTS_KEPT = 1000

    def _forget_ended_requests(self) -> None:
        """The oldest mappings of ended requests, past ENDED_REQUESTS_KEPT. An active request's never goes: a run
        and whatever waits on its lock (the sub-agent manager) look it up while it lasts."""
        excess = len(self._request_to_session) - self.ENDED_REQUESTS_KEPT
        if excess <= 0:
            return
        for request_id in list(self._request_to_session):
            if excess <= 0:
                break
            if request_id not in self._active_requests:
                del self._request_to_session[request_id]
                excess -= 1

    def unregister_request(self, request_id: str) -> None:
        """
        Unregister an active request and release session lock if held.

        Args:
            request_id: The request ID to remove
        """
        # Release session lock if this request owns it
        session_id = self._request_to_session.get(request_id)
        if session_id:
            # Check if this request owns the lock and release it synchronously
            # (called during cleanup, async not needed here)
            if session_id in self._session_lock_owners and self._session_lock_owners[session_id] == request_id:
                del self._session_lock_owners[session_id]
                self._written(session_id)
                if session_id in self._session_locks:
                    lock = self._session_locks[session_id]
                    if lock.locked():
                        lock.release()
                        logger.info("Released session lock for %s during unregister of request %s", 
                                  session_id, request_id)
                self._let_go_of_session_lock(session_id)
        
        # Remove request tracking
        self._active_requests.pop(request_id, None)
        self._request_to_session.pop(request_id, None)
        if session_id:
            self._discard_once_let_go(session_id)

    def get_session_for_request(self, request_id: str) -> Optional[str]:
        """
        Get the session ID associated with a request.

        Args:
            request_id: The request ID

        Returns:
            The session ID, or None if not found
        """
        return self._request_to_session.get(request_id)

    def get_session_messages(self, session_id: str) -> List[ChatMessage]:
        """
        Get the persisted messages for a session.

        Args:
            session_id: The session ID

        Returns:
            The session's message history (empty list if session not found)
        """
        return self._sessions.get(session_id, [])

    def set_session_messages(self, session_id: str, messages: List[ChatMessage]) -> None:
        """
        Set the persisted messages for a session.

        A volatile developer note never goes in. It is what the RUN told the
        model for THIS call (a budget, a deadline, the state a job reached) and
        it carries ``injected_by``: it is bound to the moment it was built for
        and must not outlive the run. Some are rebuilt every call (the step
        budget) and would pile up stale beside their successors; the one-offs
        (the continue nudge, a loop warning) would be read on the next resume
        as an instruction that still holds -- "provide your final answer NOW,
        do NOT use any tools" is the last thing every capped run adds. A
        developer note WITHOUT that marker was placed deliberately and is kept.

        The rule sits HERE, in the one funnel, and not in the caller that
        filters system messages: five places write session messages, and only
        one of them filters anything.

        Args:
            session_id: The session ID
            messages: The messages to persist
        """
        kept = [msg for msg in messages if not is_volatile_note(msg)]
        self._sessions[session_id] = kept
        if kept:
            self._held.add(session_id)

    def set_compacted_messages(self, session_id: str, messages: List[ChatMessage] | None) -> None:
        """
        Set compacted messages to be used instead of request messages when persisting.
        
        When a compaction/summarization tool runs mid-request, the local request
        messages list cannot be directly modified. This stores the compacted messages
        so the agent can use them when persisting the session at end of request.

        Args:
            session_id: The session ID
            messages: The compacted messages to use, or None to clear
        """
        if messages is None:
            # Clear by removing from dict
            self._compacted_messages.pop(session_id, None)
            logger.debug(f"Cleared compacted messages for session {session_id}")
        else:
            self._compacted_messages[session_id] = messages
            logger.debug(f"Set {len(messages)} compacted messages for session {session_id}")

    def get_compacted_messages(self, session_id: str) -> Optional[List[ChatMessage]]:
        """
        Get pending compacted messages for a session.

        Args:
            session_id: The session ID

        Returns:
            The compacted messages if any, None otherwise
        """
        return self._compacted_messages.get(session_id)

    def clear_compacted_messages(self, session_id: str) -> None:
        """
        Clear pending compacted messages for a session.
        Called after the compacted messages have been applied.

        Args:
            session_id: The session ID
        """
        self._compacted_messages.pop(session_id, None)

    def discard_session(self, session_id: str) -> None:
        """
        Drop every trace of a session.

        Used for the ephemeral sessions that stateless one-shot calls create:
        without this the tracker would keep one growing entry per call (a
        per-segment agent runs hundreds of times per book).

        Args:
            session_id: The session ID
        """
        if self.run_has(session_id):
            # A run has it: taken from under it, the run went on without its metadata, and its final save found
            # nothing to save. It goes once that run lets go (release_session_lock).
            self._discard_on_release.add(session_id)
            return
        self._sessions.pop(session_id, None)
        self._session_metadata.pop(session_id, None)
        self._titles_to_write.pop(session_id, None)
        self._session_template_vars.pop(session_id, None)
        self._compacted_messages.pop(session_id, None)
        self._started.discard(session_id)
        self._held.discard(session_id)
        self._openings.pop(session_id, None)
        # The sessions this agent's runs on it called agents below as tools (Agent.tool_session_id) go with it,
        # from whichever agent's tracker -- a throwaway caller leaves nothing in any agent's memory -- and
        # theirs in turn with them. Not one whose run still has it (the caller's run was cancelled, its tool
        # call runs on): taken from under it, its final save found no metadata. It goes once that run lets go.
        # Nor one a write has (an append: it reads the session back after it appended, to save it) -- any
        # holder of the lock, for a moment only.
        self._orphans.discard(session_id)
        for tracker, child in _children(session_id, self.agent_name):
            if tracker.check_session_locked(child)[0]:
                tracker._orphans.add(child)
            else:
                tracker.discard_session(child)

    def mark_opened(self, session_id: str) -> None:
        """A request opened the session for a run of its own (SessionService.open_for_run) -- also one that left
        the tracker as it was, because a run of this agent had it: that request runs on what the tracker holds
        once its run gets the session lock, so nobody may drop the session from here meanwhile (last_opened)."""
        self._opening_serial += 1
        self._openings[session_id] = self._opening_serial

    def last_opened(self, session_id: str) -> Optional[int]:
        """The serial number of the session's last opening (mark_opened), None when it was not opened since it
        came into the tracker. Another number than the one a request saw after its own opening: somebody opened
        it since."""
        return self._openings.get(session_id)

    def watch_appends(self, session_id: str) -> list[ChatMessage]:
        """A list that collects every message appended to the session from now on until unwatch_appends: beside
        its runs (append_to_session) and handed to a run of it (append_user_message). A request that settles the
        session after its run -- openai_api's AgentTurn, which puts back a turn its client never got -- keeps
        them: they are the user's, not the turn's. Kept only while somebody watches, so nothing grows with the
        sessions of the process."""
        seen: list[ChatMessage] = []
        self._append_watchers.setdefault(session_id, []).append(seen)
        return seen

    def unwatch_appends(self, session_id: str, seen: list[ChatMessage]) -> None:
        watchers = self._append_watchers.get(session_id, [])
        self._append_watchers[session_id] = [other for other in watchers if other is not seen]
        if not self._append_watchers[session_id]:
            del self._append_watchers[session_id]

    def watches_appends(self, session_id: str) -> bool:
        """Whether somebody collects what is appended to the session: a request settling it after its run."""
        return bool(self._append_watchers.get(session_id))

    def _tell_watchers(self, session_id: str | None, message: ChatMessage) -> None:
        for seen in self._append_watchers.get(session_id or "", ()):
            seen.append(message)
        if session_id:
            for appended in _append_watches:
                appended.add(session_id)

    def start_session(self, session_id: str) -> bool:
        """
        Mark a session as started.

        Returns:
            True the first time: the session has neither started before nor holds
            messages (a history loaded from the store is a started session too).
        """
        new = session_id not in self._started and not self._sessions.get(session_id)
        self._started.add(session_id)
        return new

    def emptied(self, session_id: str) -> bool:
        """Whether the session holds no messages although it held some in this process (/undo)."""
        return session_id in self._held and not self._sessions.get(session_id)

    def carry_title(self, session_id: str, title: str) -> None:
        """A title for the next save of *session_id* to write -- then dropped
        (title_written), so a rename after it is not put back."""
        self._titles_to_write[session_id] = title

    def title_to_write(self, session_id: str) -> Optional[str]:
        return self._titles_to_write.get(session_id)

    def title_written(self, session_id: str, title: str) -> None:
        """*title* is on disk: dropped -- unless another was carried meanwhile."""
        if self._titles_to_write.get(session_id) == title:
            del self._titles_to_write[session_id]

    def set_session_metadata(self, session_id: str, metadata: Dict[str, Any]) -> None:
        """
        Set metadata for a session (e.g., user_id).

        Args:
            session_id: The session ID
            metadata: Metadata dict (user_id, agent_name, etc.)
        """
        self._session_metadata[session_id] = metadata

    def get_session_metadata(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get metadata for a session.

        Args:
            session_id: The session ID

        Returns:
            Metadata dict or None if not found
        """
        return self._session_metadata.get(session_id)

    def set_session_template_vars(self, session_id: str, template_vars: Dict[str, Any]) -> None:
        """
        Set session-scoped template variables (e.g., workflow_phase, book_id).
        
        CRITICAL: These are isolated per session, not shared across sessions
        using the same agent singleton. This prevents cross-session contamination.

        Args:
            session_id: The session ID
            template_vars: Template variables dict
        """
        if session_id not in self._session_template_vars:
            self._session_template_vars[session_id] = {}
        self._session_template_vars[session_id].update(template_vars)
        logger.debug(f"Set session template vars for {session_id}: {list(template_vars.keys())}")

    def get_session_template_vars(self, session_id: str) -> Dict[str, Any]:
        """
        Get session-scoped template variables.

        Args:
            session_id: The session ID

        Returns:
            Template variables dict (empty dict if not found)
        """
        return self._session_template_vars.get(session_id, {})

    def clear_session_template_vars(self, session_id: str) -> None:
        """
        Clear session-scoped template variables.

        Args:
            session_id: The session ID
        """
        self._session_template_vars.pop(session_id, None)

    def has_session(self, session_id: str) -> bool:
        """
        Check if a session exists.

        Args:
            session_id: The session ID

        Returns:
            True if session exists, False otherwise
        """
        return session_id in self._sessions

    def get_all_session_ids(self) -> List[str]:
        """
        Get all session IDs.

        Returns:
            List of session IDs
        """
        return list(self._sessions.keys())

    def delete_session(self, session_id: str) -> bool:
        """
        Delete a session completely.

        Args:
            session_id: The session ID to delete

        Returns:
            True if session was deleted, False if it didn't exist
        """
        deleted = False
        if session_id in self._sessions:
            del self._sessions[session_id]
            deleted = True
        
        # Also clear any pending compacted messages
        self._compacted_messages.pop(session_id, None)
        
        # Clear metadata to prevent memory leak
        self._session_metadata.pop(session_id, None)
        self._titles_to_write.pop(session_id, None)
        
        # Clear session template vars to prevent memory leak
        self._session_template_vars.pop(session_id, None)
        self._started.discard(session_id)
        self._held.discard(session_id)
        self._openings.pop(session_id, None)
        self._orphans.discard(session_id)  # gone already: nothing is left to drop once its run lets go
        self._discard_on_release.discard(session_id)
        
        # Clear session locks to prevent memory leak
        if session_id in self._session_lock_owners:
            del self._session_lock_owners[session_id]
        self._written(session_id)
        # And who counted on the lock: a run still holding it releases into an
        # owner that is gone and forgets nothing, so a count left here kept every
        # later lock of this session for good.
        self._session_lock_users.pop(session_id, None)
        if session_id in self._session_locks:
            lock = self._session_locks.pop(session_id)
            # Release lock if still held (defensive)
            if lock.locked():
                try:
                    lock.release()
                except RuntimeError:
                    pass  # Already released
        
        return deleted

    def clear(self) -> None:
        """
        Clear all sessions and request mappings.
        Used during shutdown or reset operations.
        """
        self._sessions.clear()
        self._request_to_session.clear()
        self._compacted_messages.clear()
        self._session_metadata.clear()
        self._titles_to_write.clear()
        self._session_template_vars.clear()
        self._started.clear()
        self._held.clear()
        self._openings.clear()
        self._orphans.clear()
        self._discard_on_release.clear()
        self._append_watchers.clear()
        self._session_locks.clear()
        self._session_lock_owners.clear()
        for session_id in list(self._session_lock_writers):
            self._written(session_id)
        self._session_lock_users.clear()
