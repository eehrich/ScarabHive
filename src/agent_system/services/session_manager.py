"""Session Manager for persistent agent conversation sessions.

Handles creation, loading, saving, and management of user sessions with
thread-safe file operations and automatic persistence.

``SessionManager`` is the facade: every caller goes through it, and it alone
decides when a session file is written and under which lock. Two parts with
rules of their own are components it owns -- the per-user index of the
sessions (``services/session_index.py``) and the in-memory cache with what
this process has seen of each file (``services/session_cache.py``); the
rules that turn ids into paths are ``services/session_paths.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..paths import data_path
from ..utils.io import atomic_write_json, read_json_retrying
from .session_cache import SessionCache
from .session_index import INDEX_LOCK_TIMEOUT  # noqa: F401 - importable from here, as before the split
from .session_index import SessionIndex, extract_parent_id
from .session_paths import sanitize_user_id, session_files_anywhere, validate_session_id

logger = logging.getLogger(__name__)


class SessionNotFoundError(Exception):
    """Raised when a session cannot be found."""
    pass


class SessionPermissionError(Exception):
    """Raised when user doesn't have permission to access a session."""
    pass


class SessionDeletedError(Exception):
    """Raised when a write would bring back a session this manager has deleted."""


class SessionManager:
    """Manages persistent agent conversation sessions.
    
    Features:
    - Per-user session isolation via directory structure
    - Thread-safe file operations with atomic writes
    - Auto-save on every message
    - Session metadata management
    - LRU cache for recently accessed sessions
    
    Directory structure:
        data/sessions/{user_id}/{session_id}.json
    
    Each session file contains:
    - session_id, user_id, created_at, updated_at, title
    - agent_name, llm_profile
    - messages (conversation history)
    - metadata (message_count, token_count, tags, etc.)

    Locks, outermost first: a session's own lock, then ``_lock``, then the
    index's file lock of one partition (taken inside ``SessionIndex``).
    """

    def __init__(self, storage_path: Optional[str] = None):
        """Initialize SessionManager.

        Args:
            storage_path: Base directory for session storage (default:
                ``sessions`` in the data directory, agent_system/paths.py)
        """
        self.storage_path = Path(storage_path) if storage_path is not None else data_path("sessions")
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # Recently used sessions, and what this process has seen of their files (changed_on_disk)
        self._cache = SessionCache()
        self._lock = asyncio.Lock()

        # Per-session locks to prevent concurrent writes to the same session
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._session_locks_lock = asyncio.Lock()  # Lock for accessing _session_locks dict

        # Sessions deleted in this process. A run that was still going -- or its request handler, or a checkpoint
        # -- saves its session after the delete; that save must not bring the session back.
        self._deleted: set[str] = set()

        # The index partitions: read and written as the session files are.
        self._index = SessionIndex(
            self.storage_path,
            read_session=self._read_session_file_async,
            write=self._atomic_write,
        )

        logger.info("SessionManager initialized with storage_path=%s", self.storage_path)
    
    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a lock for a specific session.
        
        Args:
            session_id: Session ID to get lock for
            
        Returns:
            asyncio.Lock for this session
        """
        async with self._session_locks_lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = asyncio.Lock()
            return self._session_locks[session_id]

    def _sanitize_user_id(self, user_id: str) -> str:
        """Sanitize user_id to prevent directory traversal (``session_paths.sanitize_user_id``)."""
        return sanitize_user_id(user_id)

    def _validate_session_id(self, session_id: str) -> str:
        """Validate and sanitize session ID (``session_paths.validate_session_id``).

        The check callers outside make before they hand an id on: the app, the
        sub-agent manager, the tool approvals.

        Raises:
            ValueError: If session ID contains invalid characters
        """
        return validate_session_id(session_id)

    def belongs_to(self, user_id: str, session_id: str) -> bool:
        """Whether ``session_id`` is one of ``user_id``'s sessions.

        One stat call, for callers that must not answer about a session before
        knowing it is the asker's -- ``load_session`` decides the same thing, but
        reads and parses the whole file for it. Used by the poll behind the
        sessions pane, which asks about every row on screen, several times a minute.

        Goes through ``_session_file`` rather than ``_get_session_path``: the latter
        CREATES the user directory on its way to the path, and a question is not a
        reason to write to disk.

        Weaker than ``load_session``'s check in one way, and deliberately: that one
        also holds the file's own ``user_id`` against the asker, because a directory
        is not the last word on who owns a session. Two accounts can share a
        directory where the filesystem ignores case -- ``Alice`` and ``alice`` are
        two users to SQLite and one directory to Windows. Every listing in this class
        already answers from that shared directory, so this adds no exposure that a
        session list does not; it is the pre-existing case-folding gap, not a new one.

        False for a session that exists only in memory (one a run is creating right
        now, before its first save). Say "not yours" rather than "not there": the
        caller is deciding what to hand out.
        """
        try:
            return self._session_file(user_id, session_id).exists()
        except ValueError:  # a session id that cannot name a file cannot be anyone's
            return False

    def _get_session_path(self, user_id: str, session_id: str) -> Path:
        """Get the file path for a session.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
        
        Returns:
            Path object for the session file
            
        Raises:
            ValueError: If session_id contains invalid characters
        """
        path = self._session_file(user_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _session_file(self, user_id: str, session_id: str) -> Path:
        """Where a session's file would be -- without making anything.

        Split out of ``_get_session_path`` for the readers: that one creates the
        user directory, which is right for a save and wrong for a question. Same
        validation either way, so the two can never name different files.

        Raises:
            ValueError: If session_id contains invalid characters
        """
        # Sanitize user_id to prevent directory traversal
        safe_user_id = self._sanitize_user_id(user_id)
        # Validate session_id format
        safe_session_id = self._validate_session_id(session_id)

        return self.storage_path / safe_user_id / f"{safe_session_id}.json"

    def _generate_session_id(self) -> str:
        """Generate a unique session ID.
        
        Returns:
            Short alphanumeric session ID (10 characters)
        """
        return uuid4().hex[:10]

    def _session_id_exists_globally(self, session_id: str) -> bool:
        """Check if a session ID exists for ANY user (global collision check).
        
        Args:
            session_id: Session ID to check
        
        Returns:
            True if session exists for any user, False otherwise
        """
        # Check in-memory cache first (avoids filesystem race)
        if self._cache.fresh(session_id) is not None:
            return True

        # Check all user directories for this session ID
        return any(session_files_anywhere(self.storage_path, session_id))

    async def _find_session_owner_async(self, session_id: str) -> Optional[str]:
        """Find the user_id that owns a session (async version).

        Args:
            session_id: Session ID to find

        Returns:
            user_id if session found, None otherwise
        """
        # Check in-memory cache first (avoids filesystem race when session
        # was just created by another coroutine in the same process).
        cached = self._cache.fresh(session_id)
        if cached is not None:
            return cached[0].get("user_id")

        for session_file in session_files_anywhere(self.storage_path, session_id):
            try:
                session_data = await self._read_session_file_async(session_file)
                return session_data.get("user_id")
            except Exception as e:
                return self._owner_by_directory(session_file, e)
        return None

    def _find_session_owner(self, session_id: str) -> Optional[str]:
        """Find the user_id that owns a session (sync version - use _find_session_owner_async in async contexts).

        Args:
            session_id: Session ID to find

        Returns:
            user_id if session found, None otherwise
        """
        # Check in-memory cache first
        cached = self._cache.fresh(session_id)
        if cached is not None:
            return cached[0].get("user_id")

        for session_file in session_files_anywhere(self.storage_path, session_id):
            try:
                session_data = self._read_session_file(session_file)
                return session_data.get("user_id")
            except Exception as e:
                return self._owner_by_directory(session_file, e)
        return None

    @staticmethod
    def _owner_by_directory(session_file: Path, error: Exception) -> str:
        """The owner of a session file that is there but cannot be read: the directory it is in."""
        # If file exists but can't be read (e.g., Windows file lock),
        # fall back to directory name as user_id to avoid false "not exists"
        logger.debug(
            f"Could not read session file {session_file} ({error}), "
            f"using directory name as owner: {session_file.parent.name}"
        )
        return session_file.parent.name

    def _validate_session_data(self, data: Dict[str, Any]) -> None:
        """Validate session data structure.
        
        Args:
            data: Session data dictionary
        
        Raises:
            ValueError: If required fields are missing or invalid
        """
        required_fields = ["session_id", "user_id", "created_at", "updated_at", 
                          "title", "agent_name", "llm_profile", "messages"]
        
        for field in required_fields:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")
        
        if not isinstance(data["messages"], list):
            raise ValueError("messages must be a list")
        
        # Validate message structure
        for i, msg in enumerate(data["messages"]):
            if not isinstance(msg, dict):
                raise ValueError(f"Message {i} must be a dict")
            if "role" not in msg or "content" not in msg:
                raise ValueError(f"Message {i} missing role or content")

    def _atomic_write(self, path: Path, data: Dict[str, Any]) -> None:
        """Write a session or index file atomically (``utils.io.atomic_write_json``).

        NOTE: This is a synchronous method. Use _atomic_write_async() in async contexts
        to avoid blocking the event loop.
        """
        atomic_write_json(path, data)
        logger.debug("Atomically wrote session to %s", path)

    def is_deleted(self, session_id: str) -> bool:
        """Whether this manager has deleted the session and its file is still gone: nothing here writes it again.

        Written again by another process -- agent-cli, a woken run -- it is a session again. With session presence
        on, that process could only take it once the runs of this one had let it go, so none of their saves is still
        to come. Without presence, or for a run started with force, a run of this process still going may save over
        it -- as two processes on one session overwrite each other's saves anyway.
        """
        if session_id not in self._deleted:
            return False
        if self._session_id_exists_globally(session_id):
            self._deleted.discard(session_id)
            return False
        return True

    def lift_tombstone(self, session_id: str) -> None:
        """Let this manager write *session_id* again after it deleted it (is_deleted).

        For an id that comes back on purpose -- the session of an agent called as a tool has the same id for every
        call in one caller session (Agent.tool_session_id), and tombstoned, it was never stored again in this
        process (Agent._open_tool_session makes it afresh) -- by a caller that knows no run of this process still
        has it: the tombstone is what keeps such a run's late save from bringing it back.
        """
        self._deleted.discard(session_id)

    async def _write_session_file(self, path: Path, session_data: Dict[str, Any]) -> None:
        """Write a session file -- never one of a deleted session (see ``is_deleted``).

        Every caller holds the session's lock or ``_lock``; delete_session holds both, so no write slips between
        the check and the delete.
        """
        if self.is_deleted(session_data["session_id"]):
            raise SessionDeletedError(f"Session {session_data['session_id']} has been deleted")
        await self._atomic_write_async(path, session_data)

    async def _atomic_write_async(self, path: Path, data: Dict[str, Any]) -> None:
        """Async wrapper for atomic write to avoid blocking event loop.

        A cancel does not stop the thread, so it does not end the wait either: the write finishes first, then the
        cancel goes on. Ended at once, it let go of the caller's locks (the session's, SessionService.save_lock)
        while the thread still wrote -- and a checkpoint stopped before a run's final save, or a failed run's save
        cancelled before its turn is put back, landed after the save that followed it, older data over newer.
        """
        write = asyncio.ensure_future(asyncio.to_thread(self._atomic_write, path, data))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            while not write.done():
                try:
                    await asyncio.wait({write})
                except asyncio.CancelledError:
                    pass  # cancelled again; the thread still writes
            if not write.cancelled():
                write.exception()  # retrieved: _atomic_write logged its own failure, the cancel is what goes on
            raise

    def _read_session_file(self, path: Path) -> Dict[str, Any]:
        """Read and validate session file.
        
        NOTE: This is a synchronous method. Use _read_session_file_async() in async contexts
        to avoid blocking the event loop.
        
        Args:
            path: Session file path
        
        Returns:
            Session data dictionary
        
        Raises:
            SessionNotFoundError: If file doesn't exist
            ValueError: If JSON is invalid or data is corrupt
        """
        if not path.exists():
            raise SessionNotFoundError(f"Session file not found: {path}")
        
        try:
            data = read_json_retrying(path)
            self._validate_session_data(data)
            return data
            
        except json.JSONDecodeError as e:
            logger.error("Corrupt session file %s: %s", path, e)
            raise ValueError(f"Corrupt session file: {path.name}") from e
        except Exception as e:
            logger.error("Failed to read session %s: %s", path, e)
            raise

    async def _read_session_file_async(self, path: Path) -> Dict[str, Any]:
        """Async wrapper for reading session file to avoid blocking event loop."""
        return await asyncio.to_thread(self._read_session_file, path)

    async def _rebuild_index(
        self,
        user_id: str,
        parent_session_id: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Rebuild one index partition from the session files on disk (``SessionIndex.rebuild``).

        Here for the session archive, which repairs the partition of a tree it
        could not delete whole.
        """
        return await self._index.rebuild(user_id, parent_session_id)

    async def create_session(
        self,
        user_id: str,
        title: str = "New Conversation",
        agent_name: str = "basic_agent",
        llm_profile: str = "default",
        session_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        depth: Optional[int] = None,
        depth_budget: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a new session.

        Args:
            user_id: User identifier (from JWT)
            title: Session title
            agent_name: Agent to use
            llm_profile: LLM profile to use
            session_id: Optional custom session ID (generated if not provided)
            parent_session_id: If set, the new session is a sub-agent of this
                parent. ``parent_session`` is filled into the session data
                immediately so the index entry lands in
                ``.subs.<parent>.index.json`` on first write — no migration
                cleanup, no main-index contention from parallel sub-spawns.
            depth, depth_budget: the session's place in a sub-agent tree (see
                the sub-agent manager), written with the record -- not by a
                second save that can fail on its own.

        Returns:
            Session data dictionary

        Raises:
            ValueError: If session_id already exists (after max retries)
        """
        async with self._lock:
            # Sanitize user_id before storing
            safe_user_id = self._sanitize_user_id(user_id)
            
            # Generate unique session ID with collision detection (global across all users)
            if session_id:
                sid = session_id
                # Check if this custom ID exists for ANY user (global check)
                if self._session_id_exists_globally(sid):
                    raise ValueError(f"Session {sid} already exists")
            else:
                # Generate new ID with retry on collision
                max_retries = 10
                for attempt in range(max_retries):
                    sid = self._generate_session_id()
                    if not self._session_id_exists_globally(sid):
                        break
                    logger.warning(
                        f"Session ID collision detected: {sid} (attempt {attempt + 1}/{max_retries})"
                    )
                else:
                    # Extremely unlikely - fallback to longer ID
                    sid = uuid4().hex
                    logger.error(f"Failed to generate unique session ID after {max_retries} attempts, using full UUID")
            
            path = self._get_session_path(safe_user_id, sid)
            
            now = datetime.now(timezone.utc).isoformat()
            
            session_data = {
                "session_id": sid,
                "user_id": safe_user_id,  # Store sanitized version
                "created_at": now,
                "updated_at": now,
                "title": title,
                "agent_name": agent_name,
                "llm_profile": llm_profile,
                "messages": [],
                "metadata": {
                    "message_count": 0,
                    "token_count": 0,
                    "last_agent_response": "",
                    "tags": []
                }
            }

            # Set parent link upfront so the first index write goes straight
            # to the per-parent sub-index. Avoids 12+ sub-agents racing on
            # main index.json during parallel spawn.
            if parent_session_id:
                safe_parent = self._validate_session_id(parent_session_id)
                session_data["parent_session"] = {
                    "session_id": safe_parent,
                    "created_at": now,
                }
            if depth is not None:
                session_data["depth"] = depth
            if depth_budget is not None:
                session_data["depth_budget"] = depth_budget

            await self._write_session_file(path, session_data)
            
            # Cache the new session (with cleanup if needed)
            self._cache.cleanup()
            self._cache.wrote(sid, session_data)
            
            # Update index
            await self._index.update_entry(
                safe_user_id, sid, SessionIndex.row(session_data),
            )
            
            logger.info("Created session %s for user %s", sid, safe_user_id)
            return session_data
    
    def _written_since(self, user_id: str, session_id: str, since: float) -> bool:
        """Whether the session file changed on disk after ``since``."""
        try:
            return self._get_session_path(user_id, session_id).stat().st_mtime > since
        except (OSError, ValueError):
            return False

    def changed_on_disk(self, user_id: str, session_id: str) -> Optional[bool]:
        """Whether the file holds something this process has not seen -- another
        process continued the session.

        Seen is what this manager wrote, and a load read into a tracker
        (SessionService.load_and_restore_session marks it): a caller that has
        newer messages in memory than on disk (between a run and its save)
        must not be sent back to the file for them. A load that only shows or
        asks the session (the web UI opening it) is not seen: counted, what
        another process wrote before it was hidden from the next claim, which
        ran on its stale copy and saved it over that turn. None where there is
        no stamp: it is bounded (SessionCache.SEEN_KEPT), so a session missing from it is
        not an answer either way.
        """
        seen = self._cache.seen.get(session_id)
        if seen is None:
            return None
        return self._written_since(user_id, session_id, seen)

    def mark_seen(self, session_id: str) -> None:
        """The session's last load went into a tracker: what it read counts as seen (changed_on_disk)."""
        self._cache.mark_seen(session_id)

    async def peek_session(self, user_id: str, session_id: str) -> dict[str, Any]:
        """The session as it lies on disk, read past the cache and without a trace in it.

        For a caller that only asks the record something (which agent it ran
        with) before it claims the session: nothing of the answer may count as
        seen (changed_on_disk), and none of it needs caching.

        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionPermissionError: If user doesn't own the session
        """
        session_data = await self._read_session_file_async(self._session_file(user_id, session_id))
        if session_data["user_id"] != user_id:
            raise SessionPermissionError(f"User {user_id} doesn't own session {session_id}")
        return session_data

    async def load_session(self, user_id: str, session_id: str, bypass_cache: bool = False) -> Dict[str, Any]:
        """Load a session.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            bypass_cache: If True, always load from disk (for testing corrupt files)
        
        Returns:
            Session data dictionary
        
        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionPermissionError: If user doesn't own the session
        """
        # Check cache first (unless bypassing)
        cached = None if bypass_cache else self._cache.fresh(session_id)
        if cached is not None:
            cached_data, cached_time = cached
            # Another process may have written the file since -- a woken
            # agent-cli run continuing a session this API process has cached.
            if not self._written_since(user_id, session_id, cached_time):
                # Verify ownership
                if cached_data["user_id"] != user_id:
                    raise SessionPermissionError(f"User {user_id} doesn't own session {session_id}")
                logger.debug("Session %s loaded from cache", session_id)
                return cached_data.copy()
        
        # Use per-session lock to prevent reading while another task is writing
        session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            path = self._get_session_path(user_id, session_id)
            safe_user_id = self._sanitize_user_id(user_id)

            if not path.exists():
                # Before raising NotFoundError, check if session exists for ANOTHER user.
                # This prevents session ID conflicts across users.
                # IMPORTANT: skip the caller's own directory — otherwise a concurrent
                # create_session by the same user that completes between path.exists()
                # and this iterdir loop would be misreported as "belongs to another
                # user" (race condition: the file appears in cli_user/ during the loop,
                # we see it, and falsely flag it as cross-user).
                if any(session_files_anywhere(
                        self.storage_path, session_id,
                        skip_user=safe_user_id)):  # never compare against own directory
                    # Session exists but belongs to another user
                    raise SessionPermissionError(
                        f"Session {session_id} already exists and belongs to another user"
                    )
                # Re-check own path after the iterdir scan: a concurrent writer in
                # the same user-dir may have completed the atomic write between our
                # initial path.exists() and now. If the file is now there, fall
                # through and read it instead of raising NotFound.
                if not path.exists():
                    raise SessionNotFoundError(f"Session {session_id} not found")
            
            session_data = await self._read_session_file_async(path)
            
            # Verify ownership (security check)
            if session_data["user_id"] != user_id:
                logger.warning(
                    "User %s attempted to access session %s owned by %s",
                    user_id, session_id, session_data["user_id"]
                )
                raise SessionPermissionError(f"User {user_id} doesn't own session {session_id}")
            
            # Update cache (with cleanup if needed)
            self._cache.cleanup()
            self._cache.put(session_id, session_data)
            
            logger.debug("Session %s loaded from disk", session_id)
            return session_data.copy()

    async def save_session(self, session_data: Dict[str, Any]) -> None:
        """Save a session (update existing).

        Metadata already in the file wins over the caller's: it is written by
        ``update_session_metadata`` too, and a caller loaded its copy before this
        lock -- written back whole, that copy dropped what was merged meanwhile.
        A key the file does not have yet is taken from the caller; to change
        one, use ``update_session_metadata``.
        
        Args:
            session_data: Complete session data dictionary
        
        Raises:
            ValueError: If session data is invalid
            SessionNotFoundError: If session doesn't exist
        """
        self._validate_session_data(session_data)
        
        session_id = session_data["session_id"]
        
        # Use per-session lock to prevent concurrent writes to the same session
        session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            user_id = session_data["user_id"]
            path = self._get_session_path(user_id, session_id)
            
            # Update timestamp
            session_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            
            # What update_session_metadata merged since the caller loaded its copy (a
            # sub-agent linked during a checkpoint save) stays: the file's metadata wins.
            try:
                on_disk = (await self._read_session_file_async(path)).get("metadata") or {}
            except (SessionNotFoundError, ValueError):
                on_disk = {}  # nothing readable to keep: the caller's copy is all there is
            session_data["metadata"] = {**session_data["metadata"], **on_disk}

            # Update metadata
            session_data["metadata"]["message_count"] = len(session_data["messages"])
            if session_data["messages"]:
                last_msg = session_data["messages"][-1]
                if last_msg["role"] == "assistant":
                    content = last_msg["content"]
                    if isinstance(content, str):
                        session_data["metadata"]["last_agent_response"] = content[:200]
            
            await self._write_session_file(path, session_data)
            
            # Update cache
            self._cache.wrote(session_id, session_data)
            
            # Update index (use global lock for index file)
            async with self._lock:
                await self._index.update_entry(
                    user_id, session_id, SessionIndex.row(session_data),
                )
            
            logger.debug("Saved session %s", session_id)

    async def _read_to_change(self, path: Path, user_id: str, session_id: str) -> Dict[str, Any]:
        """The session's file as a change by its owner starts from -- the caller holds the session's lock.

        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionPermissionError: If user doesn't own the session
        """
        if not path.exists():
            raise SessionNotFoundError(f"Session {session_id} not found")
        
        # Load current session
        session_data = await self._read_session_file_async(path)
        
        # Verify ownership
        if session_data["user_id"] != user_id:
            raise SessionPermissionError(f"User {user_id} doesn't own session {session_id}")
        return session_data

    async def update_session_metadata(
        self, 
        user_id: str, 
        session_id: str, 
        metadata_updates: Dict[str, Any]
    ) -> None:
        """Atomically update only the metadata field of a session.
        
        This method is safe for concurrent access - it loads the current session,
        merges the metadata updates, and saves back, all under a per-session lock.
        This prevents "lost update" problems when multiple tasks need to update
        different metadata fields (e.g., sub_agents, activity).
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            metadata_updates: Dict of metadata fields to update (merged with existing)
        
        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionPermissionError: If user doesn't own the session
        """
        session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            path = self._get_session_path(user_id, session_id)
            session_data = await self._read_to_change(path, user_id, session_id)
            
            # Merge metadata updates (deep merge for nested dicts like sub_agents)
            if "metadata" not in session_data:
                session_data["metadata"] = {}
            
            for key, value in metadata_updates.items():
                if isinstance(value, dict) and key in session_data["metadata"] and isinstance(session_data["metadata"][key], dict):
                    # Deep merge for nested dicts
                    session_data["metadata"][key].update(value)
                else:
                    session_data["metadata"][key] = value
            
            # Update timestamp
            session_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            
            # Save back
            await self._write_session_file(path, session_data)
            
            # Update cache
            self._cache.wrote(session_id, session_data)
            
            logger.debug("Updated metadata for session %s: keys=%s", session_id, list(metadata_updates.keys()))

    async def set_session_place(self, user_id: str, session_id: str, *, depth: Optional[int] = None,
                                depth_budget: Optional[int] = None) -> None:
        """Set a session's place in a sub-agent tree (``depth``, ``depth_budget``), atomically as
        update_session_metadata changes its metadata: read, changed and written back under the session's lock, so a
        write that landed since a caller loaded the record is not written over -- as it was by a caller that set
        the two on its copy and saved the whole record (save_session merges only the metadata on disk). The index
        row carries ``depth`` too.

        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionPermissionError: If user doesn't own the session
        """
        session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            path = self._get_session_path(user_id, session_id)
            session_data = await self._read_to_change(path, user_id, session_id)
            if depth is not None:
                session_data["depth"] = depth
            if depth_budget is not None:
                session_data["depth_budget"] = depth_budget
            session_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self._write_session_file(path, session_data)
            self._cache.wrote(session_id, session_data)
            async with self._lock:
                await self._index.update_entry(user_id, session_id, SessionIndex.row(session_data))

    async def drop_session_metadata_entry(self, user_id: str, session_id: str, key: str, entry: str) -> bool:
        """Remove one entry of a dict in a session's metadata (``metadata[key][entry]``), atomically as
        ``update_session_metadata`` merges -- which can add and change entries, not remove one. False when
        there was nothing to remove. A save keeps the file's metadata (save_session), so it stays removed.

        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionPermissionError: If user doesn't own the session
        """
        session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            path = self._get_session_path(user_id, session_id)
            session_data = await self._read_to_change(path, user_id, session_id)
            entries = (session_data.get("metadata") or {}).get(key)
            if not isinstance(entries, dict) or entries.pop(entry, None) is None:
                return False
            session_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self._write_session_file(path, session_data)
            self._cache.wrote(session_id, session_data)
            return True

    async def replace_session_context_vars(
        self,
        user_id: str,
        session_id: str,
        context_vars: Dict[str, Any],
    ) -> bool:
        """Atomically REPLACE a session's persisted context_vars.

        Replace, not merge -- and that is the whole point. Everything else in
        this system only ever adds variables (agent defaults, ``--vars``,
        plugins), so ``save_session`` syncs the runtime set with ``update()``.
        The ``/vars`` command is the first writer that can REMOVE one, and a
        merge cannot express removal: the key stays on disk, the next
        ``load_and_restore_session`` merges it back into the tracker, and the
        variable the person just deleted is rendered into the prompt again.
        Persisting the resulting set here is what makes the removal stick.

        Returns False when the session has no file yet -- the common case for
        a conversation whose first turn has not been saved. Nothing is lost:
        the runtime set is written by the next ``save_session``, and with an
        empty file to merge into, merging and replacing are the same thing.

        Raises SessionPermissionError if *user_id* does not own the session.
        """
        session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            path = self._get_session_path(user_id, session_id)
            if not path.exists():
                return False

            session_data = await self._read_session_file_async(path)
            if session_data.get("user_id") != user_id:
                raise SessionPermissionError(
                    f"User {user_id} doesn't own session {session_id}")

            session_data["context_vars"] = dict(context_vars)
            session_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self._write_session_file(path, session_data)
            self._cache.wrote(session_id, session_data)
            logger.debug("Replaced context_vars for session %s: keys=%s",
                         session_id, sorted(context_vars))
            return True

    async def delete_session(self, user_id: str, session_id: str, create_backup: bool = True) -> None:
        """Delete a session.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            create_backup: Create backup before deletion (default: True)
        
        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionPermissionError: If user doesn't own the session
        """
        # the session's lock first, as save_session takes it: a save of this session waits until it is gone
        session_lock = await self._get_session_lock(session_id)
        async with session_lock, self._lock:
            path = self._get_session_path(user_id, session_id)

            # Check if session exists anywhere first (to distinguish NotFound vs PermissionDenied)
            # Try to find it in the actual owner's directory
            actual_owner_found = False
            for potential_path in session_files_anywhere(self.storage_path, session_id):
                try:
                    session_data = await self._read_session_file_async(potential_path)
                    if session_data["user_id"] != user_id:
                        # Session exists but belongs to different user
                        raise SessionPermissionError(f"User {user_id} doesn't own session {session_id}")
                    actual_owner_found = True
                    break
                except (SessionNotFoundError, ValueError, OSError):
                    # _read_session_file converts JSONDecodeError to
                    # ValueError and missing files to
                    # SessionNotFoundError -- the old clause caught
                    # exactly the two types that never arrive here.
                    continue
            
            if not actual_owner_found:
                if not path.exists():
                    raise SessionNotFoundError(f"Session {session_id} not found")
            
            # At this point, path exists and user owns it. Best-effort read:
            # the content is only needed for the index partition and the
            # backup -- a corrupt file must still be DELETABLE, otherwise the
            # API can never get rid of it.
            try:
                session_data = await self._read_session_file_async(path)
            except (SessionNotFoundError, ValueError, OSError) as e:
                logger.warning(
                    "Deleting session %s despite unreadable file (%s)",
                    session_id, e,
                )
                session_data = {}
            
            # Create backup if requested
            if create_backup:
                backup_path = path.parent / f".backup_{session_id}_{int(time.time())}.json"
                shutil.copy2(path, backup_path)
                logger.info("Created backup at %s", backup_path)
            
            # Delete file
            path.unlink()
            self._deleted.add(session_id)

            # Remove from cache
            self._cache.drop(session_id)
            
            # Remove from index (target the right partition based on parent)
            await self._index.remove_entry(
                user_id, session_id, extract_parent_id(session_data),
            )
            
            logger.info("Deleted session %s for user %s", session_id, user_id)

    async def reinstate_session(self, session_data: Dict[str, Any]) -> None:
        """Put a session back on disk exactly as it was -- the inverse of ``delete_session``.

        Used by the session archive to restore a tree. What sets it apart from
        ``save_session``: it does NOT touch ``updated_at`` or recount the
        messages. A conversation that comes back out of the archive is the
        conversation it was, not one that was written today -- and its age is
        what decides whether the sweep takes it again.

        The tombstone in ``_deleted`` is deliberately NOT lifted here. It does
        not have to be: this writes through ``_atomic_write_async``, which does
        not consult it, and ``is_deleted`` drops the entry by itself as soon as
        the file is back. Lifting it before the write would open the hole the
        tombstone exists to close -- a save still in flight from the run that
        owned the session could recreate what was archived.

        Refuses to overwrite a session that is live again: two sessions under
        one id is corruption, and the caller has to see it rather than lose
        whichever copy loses the race. Live under ANOTHER user counts too --
        archiving freed the id, create_session checks every user for it, and
        the cache and every owner lookup here assume one owner per id.

        Raises:
            ValueError: If the session data is invalid or the session is live.
        """
        self._validate_session_data(session_data)
        session_id = session_data["session_id"]
        user_id = session_data["user_id"]

        session_lock = await self._get_session_lock(session_id)
        async with session_lock, self._lock:
            path = self._get_session_path(user_id, session_id)
            if path.exists():
                raise ValueError(f"Session {session_id} is live -- refusing to overwrite it")
            if self._session_id_exists_globally(session_id):
                raise ValueError(f"Session {session_id} is live for another user -- refusing a second one")

            await self._atomic_write_async(path, session_data)
            self._cache.wrote(session_id, session_data)
            await self._index.update_entry(
                user_id, session_id, SessionIndex.row(session_data),
            )
            logger.info("Reinstated session %s for user %s", session_id, user_id)

    async def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """List all sessions for a user using fast index lookup.
        
        Args:
            user_id: User identifier
        
        Returns:
            List of session metadata dictionaries (without full message history)
        """
        return await self._index.list_all(user_id)

    def _has_children(self, user_id: str, session_id: str) -> bool:
        """Whether a session has sub-sessions -- one stat, no file read (``SessionIndex.has_children``).

        Asked by the sub-agent manager's panel for a node it is about to show.
        """
        return self._index.has_children(user_id, session_id)

    async def list_root_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """Top-level sessions only, newest first, each flagged ``has_children``.

        This is what the sidebar needs. ``list_sessions`` merges the main index
        with every per-parent sub-index — with tens of thousands of sub-sessions
        that means reading thousands of files and building a payload the caller
        then discards. Roots live exclusively in the main index, so one read is
        enough. Children are fetched on demand via ``list_child_sessions``.
        """
        return await self._index.list_roots(user_id)

    async def list_child_sessions(
        self, user_id: str, parent_session_id: str, annotate_children: bool = True
    ) -> List[Dict[str, Any]]:
        """Direct children of one parent, newest first, each flagged ``has_children``.

        Reads exactly that parent's ``.subs.<parent>.index.json`` — one file.

        ``annotate_children=False`` leaves off ``has_children``, which costs one stat
        per child. The sidebar needs it to draw an expand toggle; a caller that walks
        the whole subtree itself already knows and pays hundreds of stats for nothing.
        """
        return await self._index.list_children(user_id, parent_session_id, annotate_children)

    async def resolve_session_ref(self, user_id: str, ref: str, *,
                                  others: Optional[list] = None) -> Optional[str]:
        """The session ``ref`` names: an id, or the TITLE of one.

        A session id is machine-made (``2332j2kj22k``) and cannot be renamed:
        it is the key the usage tracker, the message debugger, the context
        stores, the sub-session indexes and the presence locks all file their
        rows under. The name a person remembers is the title, so the title is
        what they may type -- ``--session "FPGA Quartus"`` and ``/resume
        FPGA Quartus`` find the same session the id would.

        An existing id always wins over a title that looks like one. Then an
        exact title -- where several sessions carry the same one (a pipeline
        writes hundreds of "Bewerte Kapitel 3"), the most recently updated is
        meant, because that is the one its person worked in. A prefix counts
        only when it names exactly ONE session: ``--session build`` creates a
        session called "build" unless a single stored title starts that way,
        and joining a stranger's conversation because the first letters
        matched is worse than starting a new one. None when nothing matches.

        *others*, when given, receives the ids of the other sessions that
        carry the same exact title -- so the caller can say it took the newest
        of several instead of letting the choice pass unseen.

        Titles are looked up among the TOP-LEVEL sessions only, the ones the
        listings show and /title names: a sub-agent's title is the first 100
        characters of its task, repeated pipeline tasks share them by the
        hundred, and a title typed by a person must neither land in one of
        those nor count them as namesakes the listing then cannot show. An id
        still reaches any session, sub-agents' included.
        """
        ref = (ref or "").strip()
        if not ref:
            return None
        if self.belongs_to(user_id, ref):
            return ref
        wanted = ref.casefold()
        rows = await self.list_root_sessions(user_id)

        def newest(matches: list) -> Optional[str]:
            if not matches:
                return None
            best = max(matches, key=lambda r: str(r.get("updated_at") or ""))
            return best.get("session_id")

        titled = [(r, str(r.get("title") or "").strip().casefold()) for r in rows]
        same = [r for r, title in titled if title == wanted]
        exact = newest(same)
        if exact:
            if others is not None:
                others.extend(r.get("session_id") for r in same if r.get("session_id") != exact)
            return exact
        starting = [r for r, title in titled if title.startswith(wanted)]
        return starting[0].get("session_id") if len(starting) == 1 else None

    async def rename_session(self, user_id: str, session_id: str, new_title: str) -> None:
        """Rename a session.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            new_title: New session title
        
        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionPermissionError: If user doesn't own the session
        """
        session_data = await self.load_session(user_id, session_id)
        session_data["title"] = new_title
        await self.save_session(session_data)
        
        logger.info("Renamed session %s to '%s'", session_id, new_title)

    def clear_cache(self) -> None:
        """Clear the in-memory session cache."""
        self._cache.clear()
        logger.debug("Session cache cleared")

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics.
        
        Returns:
            Dictionary with cache stats (size, hit_rate, etc.)
        """
        return self._cache.stats()
