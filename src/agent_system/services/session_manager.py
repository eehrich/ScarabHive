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
        
        logger.info("SessionManager initialized with storage_path=%s", self.storage_path)

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

    def _get_index_path(self, user_id: str) -> Path:
        """Get the path to the index file for a user.
        
        Args:
            user_id: User identifier
        
        Returns:
            Path to index.json file
        """
        safe_user_id = self._sanitize_user_id(user_id)
        return self.storage_path / safe_user_id / "index.json"

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

    async def _read_index_async(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        """Read session index file.
        
        Args:
            user_id: User identifier
        
        Returns:
            Dict mapping session_id -> metadata
        
        Raises:
            FileNotFoundError: If index doesn't exist
        """
        index_path = self._get_index_path(user_id)
        if not index_path.exists():
            raise FileNotFoundError(f"Index file not found: {index_path}")
        
        def read_index():
            with open(index_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        
        return await asyncio.to_thread(read_index)

    async def _write_index_async(self, user_id: str, index_data: Dict[str, Dict[str, Any]]) -> None:
        """Write session index file atomically with retry on Windows file lock conflicts.
        
        Args:
            user_id: User identifier
            index_data: Dict mapping session_id -> metadata
        """
        index_path = self._get_index_path(user_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Reuse the robust atomic write method that handles Windows file locks
        await self._atomic_write_async(index_path, index_data)

    async def _rebuild_index(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        """Rebuild index from session files.
        
        Args:
            user_id: User identifier
        
        Returns:
            Dict mapping session_id -> metadata
        """
        safe_user_id = self._sanitize_user_id(user_id)
        user_dir = self.storage_path / safe_user_id
        
        if not user_dir.exists():
            return {}
        
        index_data = {}
        
        for session_file in user_dir.glob("*.json"):
            # Skip index, backups, and temp files
            if session_file.name in ('index.json', 'index.tmp') or session_file.name.startswith('.'):
                continue
            
            try:
                session_data = await self._read_session_file_async(session_file)
                
                # Extract metadata
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
                    "depth": session_data.get("depth", 0)
                }
            except Exception as e:
                logger.warning("Failed to read session %s for index rebuild: %s", session_file, e)
                continue
        
        # Write rebuilt index
        if index_data:
            await self._write_index_async(user_id, index_data)
            logger.info("Rebuilt index for user %s with %d sessions", user_id, len(index_data))
        
        return index_data

    async def _update_index_entry(self, user_id: str, session_id: str, metadata: Dict[str, Any]) -> None:
        """Update a single entry in the index.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            metadata: Session metadata to store
        """
        try:
            index_data = await self._read_index_async(user_id)
        except FileNotFoundError:
            # Index doesn't exist, rebuild it
            index_data = await self._rebuild_index(user_id)
        
        # Update entry
        index_data[session_id] = metadata
        
        # Write back
        await self._write_index_async(user_id, index_data)

    async def _remove_index_entry(self, user_id: str, session_id: str) -> None:
        """Remove an entry from the index.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
        """
        try:
            index_data = await self._read_index_async(user_id)
            index_data.pop(session_id, None)
            await self._write_index_async(user_id, index_data)
        except FileNotFoundError:
            # Index doesn't exist, nothing to remove
            pass

    async def create_session(
        self,
        user_id: str,
        title: str = "New Conversation",
        agent_name: str = "basic_agent",
        llm_profile: str = "default",
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new session.
        
        Args:
            user_id: User identifier (from JWT)
            title: Session title
            agent_name: Agent to use
            llm_profile: LLM profile to use
            session_id: Optional custom session ID (generated if not provided)
        
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
                "depth": session_data.get("depth", 0)
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
            if time.time() - cached_time < self._cache_ttl:
                # Verify ownership
                if cached_data["user_id"] != user_id:
                    raise SessionPermissionError(f"User {user_id} doesn't own session {session_id}")
                logger.debug("Session %s loaded from cache", session_id)
                return cached_data.copy()
        
        # Load from disk
        async with self._lock:
            path = self._get_session_path(user_id, session_id)
            
            if not path.exists():
                # Before raising NotFoundError, check if session exists for another user
                # This prevents session ID conflicts across users
                for existing_user_dir in self.storage_path.iterdir():
                    if existing_user_dir.is_dir():
                        other_user_path = existing_user_dir / f"{session_id}.json"
                        if other_user_path.exists():
                            # Session exists but belongs to another user
                            raise SessionPermissionError(
                                f"Session {session_id} already exists and belongs to another user"
                            )
                # Session truly doesn't exist
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
        
        async with self._lock:
            user_id = session_data["user_id"]
            session_id = session_data["session_id"]
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
            
            # Update index
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
                "depth": session_data.get("depth", 0)
            }
            await self._update_index_entry(user_id, session_id, metadata)
            
            logger.debug("Saved session %s", session_id)

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
                        except (FileNotFoundError, json.JSONDecodeError):
                            continue
            
            if not actual_owner_found:
                if not path.exists():
                    raise SessionNotFoundError(f"Session {session_id} not found")
            
            # At this point, path exists and user owns it
            session_data = await self._read_session_file_async(path)
            
            # Create backup if requested
            if create_backup:
                backup_path = path.parent / f".backup_{session_id}_{int(time.time())}.json"
                shutil.copy2(path, backup_path)
                logger.info("Created backup at %s", backup_path)
            
            # Delete file
            path.unlink()
            
            # Remove from cache
            self._cache.pop(session_id, None)
            
            # Remove from index
            await self._remove_index_entry(user_id, session_id)
            
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
        
        # Try to read from index file
        try:
            index_data = await self._read_index_async(user_id)
            sessions = list(index_data.values())
            
            # Sort by updated_at (most recent first)
            sessions.sort(key=lambda s: s["updated_at"], reverse=True)
            
            logger.debug("Listed %d sessions for user %s from index", len(sessions), user_id)
            return sessions
            
        except FileNotFoundError:
            # Index doesn't exist, rebuild it
            logger.info("Index not found for user %s, rebuilding...", user_id)
            index_data = await self._rebuild_index(user_id)
            sessions = list(index_data.values())
            
            # Sort by updated_at (most recent first)
            sessions.sort(key=lambda s: s["updated_at"], reverse=True)
            
            logger.debug("Listed %d sessions for user %s after rebuild", len(sessions), user_id)
            return sessions
        
        except Exception as e:
            # Index is corrupt, rebuild it
            logger.warning("Corrupt index for user %s, rebuilding: %s", user_id, e)
            index_data = await self._rebuild_index(user_id)
            sessions = list(index_data.values())
            
            # Sort by updated_at (most recent first)
            sessions.sort(key=lambda s: s["updated_at"], reverse=True)
            
            logger.debug("Listed %d sessions for user %s after rebuild", len(sessions), user_id)
            return sessions

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

    async def update_session_metadata(
        self,
        user_id: str,
        session_id: str,
        **metadata: Any
    ) -> None:
        """Update session metadata.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            **metadata: Metadata fields to update (e.g., tags=['research', 'mcp'])
        
        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionPermissionError: If user doesn't own the session
        """
        session_data = await self.load_session(user_id, session_id)
        session_data["metadata"].update(metadata)
        await self.save_session(session_data)
        
        logger.debug("Updated metadata for session %s: %s", session_id, metadata)

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
