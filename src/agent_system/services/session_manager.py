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

    def _get_session_path(self, user_id: str, session_id: str) -> Path:
        """Get the file path for a session.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
        
        Returns:
            Path object for the session file
        """
        # Sanitize user_id to prevent directory traversal
        safe_user_id = self._sanitize_user_id(user_id)
        user_dir = self.storage_path / safe_user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        
        return user_dir / f"{session_id}.json"

    def _generate_session_id(self) -> str:
        """Generate a unique session ID.
        
        Returns:
            Short alphanumeric session ID (12 characters)
        """
        return uuid4().hex[:12]

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
        """Write data to file atomically.
        
        Args:
            path: Target file path
            data: Data to write (will be JSON-serialized)
        
        Raises:
            IOError: If write fails
        """
        # Write to temporary file first
        temp_path = path.parent / f".{path.name}.tmp"
        
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            # Atomic move (overwrites target)
            os.replace(temp_path, path)
            logger.debug("Atomically wrote session to %s", path)
            
        except Exception as e:
            # Clean up temp file on failure
            if temp_path.exists():
                temp_path.unlink()
            logger.error("Failed to write session %s: %s", path, e)
            raise IOError(f"Failed to write session: {e}") from e

    def _read_session_file(self, path: Path) -> Dict[str, Any]:
        """Read and validate session file.
        
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
            ValueError: If session_id already exists
        """
        async with self._lock:
            sid = session_id or self._generate_session_id()
            
            # Sanitize user_id before storing
            safe_user_id = self._sanitize_user_id(user_id)
            
            path = self._get_session_path(safe_user_id, sid)
            
            if path.exists():
                raise ValueError(f"Session {sid} already exists")
            
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
            
            self._atomic_write(path, session_data)
            
            # Cache the new session
            self._cache[sid] = (session_data, time.time())
            
            logger.info("Created session %s for user %s", sid, safe_user_id)
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
                raise SessionNotFoundError(f"Session {session_id} not found")
            
            session_data = self._read_session_file(path)
            
            # Verify ownership (security check)
            if session_data["user_id"] != user_id:
                logger.warning(
                    "User %s attempted to access session %s owned by %s",
                    user_id, session_id, session_data["user_id"]
                )
                raise SessionPermissionError(f"User {user_id} doesn't own session {session_id}")
            
            # Update cache
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
            
            self._atomic_write(path, session_data)
            
            # Update cache
            self._cache[session_id] = (session_data, time.time())
            
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
                            session_data = self._read_session_file(potential_path)
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
            session_data = self._read_session_file(path)
            
            # Create backup if requested
            if create_backup:
                backup_path = path.parent / f".backup_{session_id}_{int(time.time())}.json"
                shutil.copy2(path, backup_path)
                logger.info("Created backup at %s", backup_path)
            
            # Delete file
            path.unlink()
            
            # Remove from cache
            self._cache.pop(session_id, None)
            
            logger.info("Deleted session %s for user %s", session_id, user_id)

    async def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """List all sessions for a user.
        
        Args:
            user_id: User identifier
        
        Returns:
            List of session metadata dictionaries (without full message history)
        """
        safe_user_id = user_id.replace('/', '_').replace('\\', '_').replace('..', '_')
        user_dir = self.storage_path / safe_user_id
        
        if not user_dir.exists():
            logger.debug("No sessions directory for user %s", user_id)
            return []
        
        sessions = []
        
        for session_file in user_dir.glob("*.json"):
            # Skip backups and temp files
            if session_file.name.startswith('.'):
                continue
            
            try:
                session_data = self._read_session_file(session_file)
                
                # Return metadata only (not full message history)
                sessions.append({
                    "session_id": session_data["session_id"],
                    "user_id": session_data["user_id"],  # Added for API compatibility
                    "title": session_data["title"],
                    "created_at": session_data["created_at"],
                    "updated_at": session_data["updated_at"],
                    "agent_name": session_data["agent_name"],
                    "llm_profile": session_data["llm_profile"],
                    "message_count": session_data["metadata"].get("message_count", 0),
                    "last_agent_response": session_data["metadata"].get("last_agent_response", ""),
                    "tags": session_data["metadata"].get("tags", [])  # Added for API compatibility
                })
            except Exception as e:
                logger.warning("Failed to load session %s: %s", session_file, e)
                continue
        
        # Sort by updated_at (most recent first)
        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        
        logger.debug("Listed %d sessions for user %s", len(sessions), user_id)
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
            "ttl_seconds": self._cache_ttl,
            "entries": list(self._cache.keys())
        }
