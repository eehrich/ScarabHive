"""Session Manager for persistent agent conversation sessions.

Handles creation, loading, saving, and management of user sessions with
thread-safe file operations and automatic persistence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class SessionNotFoundError(Exception):
    """Raised when a session cannot be found."""
    pass


class SessionPermissionError(Exception):
    """Raised when user doesn't have permission to access a session."""
    pass


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
    """

    def __init__(self, storage_path: str = "data/sessions"):
        """Initialize SessionManager.
        
        Args:
            storage_path: Base directory for session storage (default: data/sessions)
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # In-memory cache: {session_id: (session_data, timestamp)}
        self._cache: Dict[str, tuple[Dict[str, Any], float]] = {}
        self._cache_ttl = 300  # 5 minutes
        self._max_cache_size = 200  # Maximum cached sessions to prevent memory leak
        self._lock = asyncio.Lock()
        
        # Per-session locks to prevent concurrent writes to the same session
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._session_locks_lock = asyncio.Lock()  # Lock for accessing _session_locks dict
        
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
        """Sanitize user_id to prevent directory traversal.
        
        Args:
            user_id: Raw user identifier
        
        Returns:
            Sanitized user ID safe for filesystem use
        """
        return user_id.replace('..', '_').replace('/', '_').replace('\\', '_')

    def _validate_session_id(self, session_id: str) -> str:
        """Validate and sanitize session ID.
        
        Args:
            session_id: Raw session ID
            
        Returns:
            Sanitized session ID
            
        Raises:
            ValueError: If session ID contains invalid characters
        """
        import re
        # Session IDs should be alphanumeric with underscores/hyphens only
        if not re.match(r'^[a-zA-Z0-9_-]+$', session_id):
            raise ValueError(f"Invalid session ID format: {session_id!r}")
        return session_id

    def _get_index_path(
        self,
        user_id: str,
        parent_session_id: Optional[str] = None,
    ) -> Path:
        """Index file path. Top-level: ``<user>/index.json``. Sub-agents:
        ``<user>/.subs.<parent>.index.json`` (per-parent partition so parallel
        ``agent-cli`` processes write to different files, no contention).
        """
        safe_user_id = self._sanitize_user_id(user_id)
        user_dir = self.storage_path / safe_user_id
        if parent_session_id:
            safe_parent = self._validate_session_id(parent_session_id)
            return user_dir / f".subs.{safe_parent}.index.json"
        return user_dir / "index.json"

    @staticmethod
    def _extract_parent_id(session_data_or_metadata: Dict[str, Any]) -> Optional[str]:
        """Pull the parent session_id from a session-data or index-metadata dict.

        Sessions track their parent via ``parent_session = {"session_id": ..., "created_at": ...}``.
        Returns None for top-level sessions.
        """
        parent = session_data_or_metadata.get("parent_session")
        if isinstance(parent, dict):
            pid = parent.get("session_id")
            if isinstance(pid, str) and pid:
                return pid
        return None

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
        # Sanitize user_id to prevent directory traversal
        safe_user_id = self._sanitize_user_id(user_id)
        # Validate session_id format
        safe_session_id = self._validate_session_id(session_id)
        
        user_dir = self.storage_path / safe_user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        
        return user_dir / f"{safe_session_id}.json"

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
        if session_id in self._cache:
            cached_data, cached_time = self._cache[session_id]
            if time.time() - cached_time < self._cache_ttl:
                return True

        # Check all user directories for this session ID
        for user_dir in self.storage_path.iterdir():
            if user_dir.is_dir():
                session_file = user_dir / f"{session_id}.json"
                if session_file.exists():
                    return True
        return False

    async def _find_session_owner_async(self, session_id: str) -> Optional[str]:
        """Find the user_id that owns a session (async version).
        
        Args:
            session_id: Session ID to find
        
        Returns:
            user_id if session found, None otherwise
        """
        # Check in-memory cache first (avoids filesystem race when session
        # was just created by another coroutine in the same process).
        if session_id in self._cache:
            cached_data, cached_time = self._cache[session_id]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data.get("user_id")

        for user_dir in self.storage_path.iterdir():
            if user_dir.is_dir():
                session_file = user_dir / f"{session_id}.json"
                if session_file.exists():
                    try:
                        session_data = await self._read_session_file_async(session_file)
                        return session_data.get("user_id")
                    except Exception as e:
                        # If file exists but can't be read (e.g., Windows file lock),
                        # fall back to directory name as user_id to avoid false "not exists"
                        logger.debug(
                            f"Could not read session file {session_file} ({e}), "
                            f"using directory name as owner: {user_dir.name}"
                        )
                        return user_dir.name
        return None

    def _find_session_owner(self, session_id: str) -> Optional[str]:
        """Find the user_id that owns a session (sync version - use _find_session_owner_async in async contexts).
        
        Args:
            session_id: Session ID to find
        
        Returns:
            user_id if session found, None otherwise
        """
        # Check in-memory cache first
        if session_id in self._cache:
            cached_data, cached_time = self._cache[session_id]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data.get("user_id")

        for user_dir in self.storage_path.iterdir():
            if user_dir.is_dir():
                session_file = user_dir / f"{session_id}.json"
                if session_file.exists():
                    try:
                        session_data = self._read_session_file(session_file)
                        return session_data.get("user_id")
                    except Exception as e:
                        # If file exists but can't be read (e.g., Windows file lock),
                        # fall back to directory name as user_id to avoid false "not exists"
                        logger.debug(
                            f"Could not read session file {session_file} ({e}), "
                            f"using directory name as owner: {user_dir.name}"
                        )
                        return user_dir.name
        return None

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
        """Write data to file atomically with retry on Windows file lock conflicts.
        
        NOTE: This is a synchronous method. Use _atomic_write_async() in async contexts
        to avoid blocking the event loop.
        
        Args:
            path: Target file path
            data: Data to write (will be JSON-serialized)
        
        Raises:
            IOError: If write fails after retries
        """
        # Use unique temp file per write to avoid conflicts between parallel writes
        temp_path = path.parent / f".{path.name}.{uuid4().hex[:8]}.tmp"
        
        max_retries = 5
        retry_delay = 0.01  # Start with 10ms
        
        for attempt in range(max_retries):
            try:
                # Write to temporary file
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                
                # Atomic move (overwrites target)
                # On Windows, this can fail with PermissionError if another process holds the file
                os.replace(temp_path, path)
                logger.debug("Atomically wrote session to %s", path)
                return  # Success!
                
            except (PermissionError, OSError) as e:
                # Windows file lock conflict - retry with exponential backoff
                if attempt < max_retries - 1:
                    logger.debug(
                        "File lock conflict writing %s (attempt %d/%d), retrying in %.2fms: %s",
                        path, attempt + 1, max_retries, retry_delay * 1000, e
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    # Final attempt failed
                    if temp_path.exists():
                        temp_path.unlink()
                    logger.error("Failed to write session %s after %d retries: %s", path, max_retries, e)
                    raise IOError(f"Failed to write session after {max_retries} retries: {e}") from e
            
            except Exception as e:
                # Other errors (JSON encoding, disk full, etc.) - fail immediately
                if temp_path.exists():
                    temp_path.unlink()
                logger.error("Failed to write session %s: %s", path, e)
                raise IOError(f"Failed to write session: {e}") from e

    async def _atomic_write_async(self, path: Path, data: Dict[str, Any]) -> None:
        """Async wrapper for atomic write to avoid blocking event loop."""
        await asyncio.to_thread(self._atomic_write, path, data)

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
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
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

    async def _read_index_async(
        self,
        user_id: str,
        parent_session_id: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Read a single session index file (main or per-parent sub index).
        
        Args:
            user_id: User identifier
        
        Returns:
            Dict mapping session_id -> metadata
        
        Raises:
            FileNotFoundError: If index doesn't exist
        """
        index_path = self._get_index_path(user_id, parent_session_id)
        if not index_path.exists():
            raise FileNotFoundError(f"Index file not found: {index_path}")

        def read_index():
            with open(index_path, 'r', encoding='utf-8') as f:
                return json.load(f)

        return await asyncio.to_thread(read_index)

    async def _read_all_indexes_async(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        """Read main index plus all per-parent sub indices and merge.

        Used by list_sessions for a unified view across partitions.
        Reads only the small index files, not every session blob.
        """
        safe_user_id = self._sanitize_user_id(user_id)
        user_dir = self.storage_path / safe_user_id
        if not user_dir.exists():
            return {}

        merged: Dict[str, Dict[str, Any]] = {}

        def read_one(path: Path) -> Dict[str, Dict[str, Any]]:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except Exception as e:  # noqa: BLE001 — skip broken index
                logger.warning("Failed to read index file %s: %s", path, e)
                return {}

        candidates = [user_dir / "index.json"] + sorted(user_dir.glob(".subs.*.index.json"))
        for path in candidates:
            if not path.exists():
                continue
            data = await asyncio.to_thread(read_one, path)
            merged.update(data)
        return merged

    async def _write_index_async(
        self,
        user_id: str,
        index_data: Dict[str, Dict[str, Any]],
        parent_session_id: Optional[str] = None,
    ) -> None:
        """Write a session index file atomically (main or per-parent sub index)."""
        index_path = self._get_index_path(user_id, parent_session_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        await self._atomic_write_async(index_path, index_data)

    async def _rebuild_index(
        self,
        user_id: str,
        parent_session_id: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Rebuild a single index file from session files on disk.

        Args:
            user_id: User identifier.
            parent_session_id: If set, rebuild only this parent's sub-index
                (entries whose ``parent_session.session_id`` matches). Otherwise
                rebuild the main index (entries without a parent).

        Returns:
            Dict mapping session_id -> metadata for the rebuilt index.
        """
        safe_user_id = self._sanitize_user_id(user_id)
        user_dir = self.storage_path / safe_user_id

        if not user_dir.exists():
            return {}

        index_data: Dict[str, Dict[str, Any]] = {}

        for session_file in user_dir.glob("*.json"):
            # Skip index files, backups, temp files
            if session_file.name in ('index.json', 'index.tmp') or session_file.name.startswith('.'):
                continue

            try:
                session_data = await self._read_session_file_async(session_file)
                pid = self._extract_parent_id(session_data)
                # Only include entries that belong in this index partition
                if pid != parent_session_id:
                    continue

                index_data[session_data["session_id"]] = {
                    "session_id": session_data["session_id"],
                    "user_id": session_data["user_id"],
                    "title": session_data["title"],
                    "created_at": session_data["created_at"],
                    "updated_at": session_data["updated_at"],
                    "agent_name": session_data["agent_name"],
                    "llm_profile": session_data["llm_profile"],
                    "message_count": session_data["metadata"].get("message_count", 0),
                    "last_agent_response": session_data["metadata"].get("last_agent_response", ""),
                    "tags": session_data["metadata"].get("tags", []),
                    "parent_session": session_data.get("parent_session"),
                    "depth": session_data.get("depth", 1),
                }
            except Exception as e:
                logger.warning("Failed to read session %s for index rebuild: %s", session_file, e)
                continue

        if index_data:
            await self._write_index_async(user_id, index_data, parent_session_id)
            logger.info(
                "Rebuilt %s index for user %s with %d sessions",
                ("sub-index for parent " + parent_session_id) if parent_session_id else "main",
                user_id, len(index_data),
            )

        return index_data

    async def _update_index_entry(
        self,
        user_id: str,
        session_id: str,
        metadata: Dict[str, Any],
        parent_session_id: Optional[str] = None,
    ) -> None:
        """Update a single entry in the appropriate index (main or per-parent sub).

        ``parent_session_id`` is auto-detected from ``metadata.parent_session``
        if not supplied — top-level sessions go to main index, sub-agent
        sessions go to ``.subs.<parent>.index.json``.
        """
        if parent_session_id is None:
            parent_session_id = self._extract_parent_id(metadata)

        try:
            index_data = await self._read_index_async(user_id, parent_session_id)
        except FileNotFoundError:
            index_data = await self._rebuild_index(user_id, parent_session_id)

        index_data[session_id] = metadata
        await self._write_index_async(user_id, index_data, parent_session_id)

        # Migration cleanup: when an entry is now in a sub-index, ensure it's
        # not still lingering in main from an earlier write (sub-agents are
        # created via create_session first → land in main → then save_session
        # adds parent_session and routes to sub here). One-off cost on the
        # first save with a parent; subsequent saves no-op the main read.
        if parent_session_id is not None:
            try:
                main_idx = await self._read_index_async(user_id)
            except FileNotFoundError:
                return
            if main_idx.pop(session_id, None) is not None:
                await self._write_index_async(user_id, main_idx)

    async def _remove_index_entry(
        self,
        user_id: str,
        session_id: str,
        parent_session_id: Optional[str],
    ) -> None:
        """Remove an entry from the index partition where it lives.

        ``parent_session_id`` MUST be the same value that was used when the
        entry was last written (None for top-level sessions, parent's id for
        sub-agents). The caller knows this from the session data — there's
        no fallback search to keep this on the fast path.
        """
        try:
            data = await self._read_index_async(user_id, parent_session_id)
        except FileNotFoundError:
            return
        if data.pop(session_id, None) is not None:
            if not data and parent_session_id:
                # Last child removed: delete the sub-index file instead of
                # persisting an empty {} — the file's existence doubles as the
                # sidebar's has_children signal, so a leftover empty partition
                # would render a phantom expand toggle forever.
                try:
                    self._get_index_path(user_id, parent_session_id).unlink(missing_ok=True)
                except OSError as e:
                    logger.warning(
                        "Failed to remove emptied sub-index for parent %s: %s",
                        parent_session_id, e,
                    )
                return
            await self._write_index_async(user_id, data, parent_session_id)

    async def create_session(
        self,
        user_id: str,
        title: str = "New Conversation",
        agent_name: str = "basic_agent",
        llm_profile: str = "default",
        session_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
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

            await self._atomic_write_async(path, session_data)
            
            # Cache the new session (with cleanup if needed)
            self._cleanup_cache()
            self._cache[sid] = (session_data, time.time())
            
            # Update index
            metadata = {
                "session_id": session_data["session_id"],
                "user_id": session_data["user_id"],
                "title": session_data["title"],
                "created_at": session_data["created_at"],
                "updated_at": session_data["updated_at"],
                "agent_name": session_data["agent_name"],
                "llm_profile": session_data["llm_profile"],
                "message_count": 0,
                "last_agent_response": "",
                "tags": session_data["metadata"].get("tags", []),
                "parent_session": session_data.get("parent_session"),
                "depth": session_data.get("depth", 1)
            }
            await self._update_index_entry(safe_user_id, sid, metadata)
            
            logger.info("Created session %s for user %s", sid, safe_user_id)
            return session_data
    
    def _cleanup_cache(self) -> None:
        """Clean up cache: remove expired entries and apply LRU eviction."""
        now = time.time()
        
        # Remove expired entries
        expired = [
            sid for sid, (_, ts) in self._cache.items()
            if now - ts > self._cache_ttl
        ]
        for sid in expired:
            del self._cache[sid]
        
        if expired:
            logger.debug(f"SessionManager: Cleaned up {len(expired)} expired cache entries")
        
        # LRU eviction if still over limit
        while len(self._cache) >= self._max_cache_size:
            # Find oldest entry
            oldest = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest]
            logger.debug(f"SessionManager: Evicted cache entry {oldest} (LRU)")

    def _written_since(self, user_id: str, session_id: str, since: float) -> bool:
        """Whether the session file changed on disk after ``since``."""
        try:
            return self._get_session_path(user_id, session_id).stat().st_mtime > since
        except (OSError, ValueError):
            return False

    def changed_on_disk(self, user_id: str, session_id: str) -> Optional[bool]:
        """Whether the file holds something this manager has not seen -- another
        process continued the session.

        Its own loads and saves both stamp the cache, so they are not a change:
        a caller that has newer messages in memory than on disk (between a run
        and its save) must not be sent back to the file for them. None where
        there is no stamp: the cache is bounded (TTL and LRU), so a session
        missing from it is not an answer either way.
        """
        cached = self._cache.get(session_id)
        if cached is None:
            return None
        return self._written_since(user_id, session_id, cached[1])

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
        if not bypass_cache and session_id in self._cache:
            cached_data, cached_time = self._cache[session_id]
            # Another process may have written the file since -- a woken
            # agent-cli run continuing a session this API process has cached.
            if (time.time() - cached_time < self._cache_ttl
                    and not self._written_since(user_id, session_id, cached_time)):
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
                for existing_user_dir in self.storage_path.iterdir():
                    if not existing_user_dir.is_dir():
                        continue
                    if existing_user_dir.name == safe_user_id:
                        continue  # never compare against own directory
                    other_user_path = existing_user_dir / f"{session_id}.json"
                    if other_user_path.exists():
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
            self._cleanup_cache()
            self._cache[session_id] = (session_data, time.time())
            
            logger.debug("Session %s loaded from disk", session_id)
            return session_data.copy()

    async def save_session(self, session_data: Dict[str, Any]) -> None:
        """Save a session (update existing).
        
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
            
            # Update metadata
            session_data["metadata"]["message_count"] = len(session_data["messages"])
            if session_data["messages"]:
                last_msg = session_data["messages"][-1]
                if last_msg["role"] == "assistant":
                    content = last_msg["content"]
                    if isinstance(content, str):
                        session_data["metadata"]["last_agent_response"] = content[:200]
            
            await self._atomic_write_async(path, session_data)
            
            # Update cache
            self._cache[session_id] = (session_data, time.time())
            
            # Update index (use global lock for index file)
            async with self._lock:
                metadata = {
                    "session_id": session_data["session_id"],
                    "user_id": session_data["user_id"],
                    "title": session_data["title"],
                    "created_at": session_data["created_at"],
                    "updated_at": session_data["updated_at"],
                    "agent_name": session_data["agent_name"],
                    "llm_profile": session_data["llm_profile"],
                    "message_count": session_data["metadata"].get("message_count", 0),
                    "last_agent_response": session_data["metadata"].get("last_agent_response", ""),
                    "tags": session_data["metadata"].get("tags", []),
                    "parent_session": session_data.get("parent_session"),
                    "depth": session_data.get("depth", 1)
                }
                await self._update_index_entry(user_id, session_id, metadata)
            
            logger.debug("Saved session %s", session_id)

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
            
            if not path.exists():
                raise SessionNotFoundError(f"Session {session_id} not found")
            
            # Load current session
            session_data = await self._read_session_file_async(path)
            
            # Verify ownership
            if session_data["user_id"] != user_id:
                raise SessionPermissionError(f"User {user_id} doesn't own session {session_id}")
            
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
            await self._atomic_write_async(path, session_data)
            
            # Update cache
            self._cache[session_id] = (session_data, time.time())
            
            logger.debug("Updated metadata for session %s: keys=%s", session_id, list(metadata_updates.keys()))

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
            await self._atomic_write_async(path, session_data)
            self._cache[session_id] = (session_data, time.time())
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
        async with self._lock:
            path = self._get_session_path(user_id, session_id)
            
            # Check if session exists anywhere first (to distinguish NotFound vs PermissionDenied)
            # Try to find it in the actual owner's directory
            actual_owner_found = False
            for user_dir in self.storage_path.iterdir():
                if user_dir.is_dir():
                    potential_path = user_dir / f"{session_id}.json"
                    if potential_path.exists():
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
            
            # Remove from cache
            self._cache.pop(session_id, None)
            
            # Remove from index (target the right partition based on parent)
            await self._remove_index_entry(
                user_id, session_id, self._extract_parent_id(session_data),
            )
            
            logger.info("Deleted session %s for user %s", session_id, user_id)

    async def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """List all sessions for a user using fast index lookup.
        
        Args:
            user_id: User identifier
        
        Returns:
            List of session metadata dictionaries (without full message history)
        """
        safe_user_id = self._sanitize_user_id(user_id)
        user_dir = self.storage_path / safe_user_id

        if not user_dir.exists():
            logger.debug("No sessions directory for user %s", user_id)
            return []

        # Read main index + every per-parent sub-index and merge.
        # Per-parent sub-indices live in ".subs.<parent>.index.json" — they
        # exist so that parallel agent-cli processes don't write-collide on
        # the main index. ``list_sessions`` is the unified view.
        try:
            index_data = await self._read_all_indexes_async(user_id)
            if not index_data:
                # Nothing read — either no index files at all (fresh user)
                # or all empty. Try a full rebuild as a last resort.
                index_data = await self._rebuild_index(user_id)

            sessions = list(index_data.values())
            sessions.sort(key=lambda s: s["updated_at"], reverse=True)
            logger.debug("Listed %d sessions for user %s", len(sessions), user_id)
            return sessions

        except Exception as e:
            # Read pipeline blew up — rebuild main index from disk.
            logger.warning("Index read failed for user %s, rebuilding main: %s", user_id, e)
            index_data = await self._rebuild_index(user_id)
            sessions = list(index_data.values())
            
            # Sort by updated_at (most recent first)
            sessions.sort(key=lambda s: s["updated_at"], reverse=True)
            
            logger.debug("Listed %d sessions for user %s after rebuild", len(sessions), user_id)
            return sessions

    async def _read_main_index_async(self, user_id: str) -> Optional[Dict[str, Dict[str, Any]]]:
        """Read ONLY the main index (top-level sessions).

        Deliberately skips the per-parent ``.subs.*.index.json`` partitions —
        those hold exclusively sub-sessions.

        Returns ``None`` when the index needs healing (file missing or
        unparseable) — callers decide whether to rebuild. A parsed-but-empty
        index returns ``{}`` and is trusted: deletes legitimately empty it.
        """
        path = self._get_index_path(user_id)
        if not path.exists():
            return None

        def read_one() -> Optional[Dict[str, Dict[str, Any]]]:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else None
            except Exception as e:  # noqa: BLE001 — a broken index must not 500
                logger.warning("Failed to read main index %s: %s", path, e)
                return None

        return await asyncio.to_thread(read_one)

    def _has_children(self, user_id: str, session_id: str) -> bool:
        """Whether a session has sub-sessions — one stat on its per-parent
        sub-index, no file read. The size guard (> 4 bytes; an empty index
        serialises to ``{}``) keeps a stale emptied sub-index from producing a
        phantom expand toggle."""
        try:
            st = self._get_index_path(user_id, session_id).stat()
        except (OSError, ValueError):
            return False
        return st.st_size > 4

    def _annotate_children_flag(self, user_id: str, sessions: List[Dict[str, Any]]) -> None:
        """Add ``has_children`` so the UI can render an expand toggle without
        shipping the descendants."""
        for s in sessions:
            sid = s.get("session_id")
            s["has_children"] = bool(sid) and self._has_children(user_id, sid)

    async def list_root_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """Top-level sessions only, newest first, each flagged ``has_children``.

        This is what the sidebar needs. ``list_sessions`` merges the main index
        with every per-parent sub-index — with tens of thousands of sub-sessions
        that means reading thousands of files and building a payload the caller
        then discards. Roots live exclusively in the main index, so one read is
        enough. Children are fetched on demand via ``list_child_sessions``.
        """
        user_dir = self.storage_path / self._sanitize_user_id(user_id)
        if not user_dir.exists():
            return []
        index_data = await self._read_main_index_async(user_id)
        if index_data is None:
            # Main index missing or unreadable — same self-heal as the old
            # pipeline: rebuild it from the session files on disk (expensive,
            # but only on actual index loss; _rebuild_index persists the
            # result, so the next call is fast again). A parsed-but-empty
            # index is trusted and does NOT trigger this (deletes empty it
            # legitimately — rebuilding every call would reintroduce the
            # full-scan cost this method exists to avoid).
            index_data = await self._rebuild_index(user_id)
        sessions = list(index_data.values())
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        self._annotate_children_flag(user_id, sessions)
        logger.debug("Listed %d root sessions for user %s", len(sessions), user_id)
        return sessions

    async def list_child_sessions(
        self, user_id: str, parent_session_id: str
    ) -> List[Dict[str, Any]]:
        """Direct children of one parent, newest first, each flagged ``has_children``.

        Reads exactly that parent's ``.subs.<parent>.index.json`` — one file.
        """
        path = self._get_index_path(user_id, parent_session_id)  # validates the id
        if not path.exists():
            return []

        def read_one() -> Dict[str, Dict[str, Any]]:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to read sub-index %s: %s", path, e)
                return {}

        index_data = await asyncio.to_thread(read_one)
        # A sub-index is a partition of ONE parent, but stay strict: only return
        # entries that actually name this parent.
        children = [
            meta for meta in index_data.values()
            if self._extract_parent_id(meta) == parent_session_id
        ]
        children.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        self._annotate_children_flag(user_id, children)
        return children

    async def find_sessions(
        self,
        user_id: str,
        *,
        session_id: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Server-side session lookup/search over the indices.

        ``session_id``: existence + metadata for one session in O(1) (the
        session file itself is the proof; no index scan). Lets the UI resolve a
        session that is not currently loaded in the sidebar tree.

        ``query``: case-insensitive substring match on the title across ALL
        partitions. This is the expensive path (reads every sub-index), so it
        only runs when the caller actually searches.
        """
        if session_id:
            path = self._get_session_path(user_id, session_id)  # validates the id
            if not path.exists():
                return []
            main = (await self._read_main_index_async(user_id)) or {}
            meta = main.get(session_id)
            if meta is None:
                def read_meta() -> Optional[Dict[str, Any]]:
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Failed to read session %s: %s", path, e)
                        return None
                    # Metadata only — never ship the message history here.
                    return {k: v for k, v in data.items() if k != "messages"}
                meta = await asyncio.to_thread(read_meta)
            if not meta:
                return []
            meta = dict(meta)
            meta["has_children"] = self._has_children(user_id, session_id)
            return [meta]

        if not query:
            return []
        needle = query.strip().lower()
        if not needle:
            return []
        index_data = await self._read_all_indexes_async(user_id)
        matches = [
            meta for meta in index_data.values()
            if needle in str(meta.get("title", "")).lower()
        ]
        matches.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        matches = matches[: max(1, limit)]
        self._annotate_children_flag(user_id, matches)
        return matches

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
        return {
            "size": len(self._cache),
            "max_size": self._max_cache_size,
            "ttl_seconds": self._cache_ttl,
            "entries": list(self._cache.keys())
        }
