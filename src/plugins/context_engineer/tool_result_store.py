"""Tool Result Store - Store and reference tool outputs.

This module provides storage for tool call outputs with compact references.
Old tool results are replaced with small reference strings, while the full
content is stored externally and can be retrieved on demand.

Key features:
- SQLite-based persistent storage
- Compact reference format: [Tool:name ref:id hash:xxxx]
- Full content retrieval via reference ID
- Automatic cleanup of old entries
"""

from __future__ import annotations

import functools
import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_system.llm.token_utils import estimate_content_tokens

logger = logging.getLogger(__name__)


def _synchronized(method):
    """Serialize a store method on the instance's reentrant lock.

    The single sqlite3.Connection (check_same_thread=False) is hit both
    synchronously from the event loop (handler reads) and from worker threads
    via asyncio.to_thread (compaction writes). Without serialization a
    loop-thread read and a worker-thread write race the same connection ->
    'recursive use of cursors', interleaved writes, corrupted reads.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper

# Length of the hash portion (in hex chars) of a short_id.
# 10 hex chars = 40 bits → ~1M IDs before 50% birthday-collision chance.
_SHORT_ID_HASH_LEN = 10


def _compute_short_id(tool_call_id: str) -> str:
    """Stable, collision-resistant short reference for a tool_call_id.

    Uses a SHA-256-derived suffix so distinct call IDs always map to distinct
    short IDs — independent of the prefix (`call_`, `tool_`, internal, …).
    """
    digest = hashlib.sha256(tool_call_id.encode()).hexdigest()[:_SHORT_ID_HASH_LEN]
    return f"TR_{digest}"


@dataclass
class ToolResultEntry:
    """A stored tool result entry."""
    id: str  # tool_call_id
    tool_name: str
    content: str
    content_hash: str
    token_count: int
    timestamp: datetime
    session_id: str
    summary: str | None = None  # Optional LLM-generated summary
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "content": self.content,
            "content_hash": self.content_hash,
            "token_count": self.token_count,
            "timestamp": self.timestamp.isoformat(),
            "session_id": self.session_id,
            "summary": self.summary
        }
    
    @classmethod
    def from_row(cls, row: tuple) -> "ToolResultEntry":
        """Create from SQLite row."""
        return cls(
            id=row[0],
            tool_name=row[1],
            content=row[2],
            content_hash=row[3],
            token_count=row[4],
            timestamp=datetime.fromisoformat(row[5]),
            session_id=row[6],
            summary=row[7]
        )


class ToolResultStore:
    """Store tool results externally, replace with compact references.
    
    When tool results are cleared from the context, they are stored here
    with a compact reference. The LLM can request the full content via
    the get_tool_result tool if needed.
    
    Reference format: [Tool:{tool_name} ref:{short_id} hash:{content_hash}]
    
    Usage:
        store = ToolResultStore(storage_path)
        
        # Store and get reference
        reference = store.store_and_reference(
            tool_call_id="tc_abc123xyz",
            tool_name="read_file",
            content="def main():\\n    print('hello')...",
            session_id="session_123"
        )
        # Returns: "[Tool:read_file ref:tc_abc12 hash:a1b2c3d4]"
        
        # Retrieve full content later
        entry = store.retrieve("tc_abc12")
        print(entry.content)
    """
    
    def __init__(self, storage_path: Path, session_id: str | None = None):
        """Initialize the tool result store.
        
        Args:
            storage_path: Path to SQLite database file
            session_id: Optional session ID for filtering
        """
        self.storage_path = storage_path
        self.session_id = session_id
        self._db: sqlite3.Connection | None = None
        # Serializes access to the shared connection across event-loop and
        # to_thread worker threads (see _synchronized). Reentrant so a
        # synchronized method can call another without deadlocking.
        self._lock = threading.RLock()

        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize SQLite database."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = sqlite3.connect(str(self.storage_path), check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS tool_results (
                id TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                token_count INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                session_id TEXT NOT NULL,
                summary TEXT,
                short_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: add short_id column to existing databases (idempotent)
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(tool_results)").fetchall()}
        if "short_id" not in cols:
            self._db.execute("ALTER TABLE tool_results ADD COLUMN short_id TEXT")
        # Backfill short_id for any legacy rows that don't have it yet
        legacy = self._db.execute(
            "SELECT id FROM tool_results WHERE short_id IS NULL OR short_id = ''"
        ).fetchall()
        for (legacy_id,) in legacy:
            self._db.execute(
                "UPDATE tool_results SET short_id = ? WHERE id = ?",
                (_compute_short_id(legacy_id), legacy_id),
            )
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tool_results_session
            ON tool_results(session_id)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tool_results_hash
            ON tool_results(content_hash)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tool_results_short_id
            ON tool_results(short_id)
        """)
        self._db.commit()
    
    @_synchronized
    def store_and_reference(
        self,
        tool_call_id: str,
        tool_name: str,
        content: str,
        session_id: str | None = None,
        summary: str | None = None
    ) -> str:
        """Store tool result and return compact reference.
        
        Args:
            tool_call_id: The original tool call ID
            tool_name: Name of the tool that was called
            content: Full tool output content
            session_id: Session ID for isolation
            summary: Optional summary of the content
            
        Returns:
            Compact reference string to replace content in context
        """
        session_id = session_id or self.session_id or "default"

        # Generate content hash
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:8]

        # Estimate tokens
        token_count = estimate_content_tokens(content)

        # Create short ID for reference (collision-resistant hash of full tool_call_id —
        # independent of prefix style: 'call_', 'tool_', or anything else).
        short_id = _compute_short_id(tool_call_id)

        # Store in database
        entry = ToolResultEntry(
            id=tool_call_id,
            tool_name=tool_name,
            content=content,
            content_hash=content_hash,
            token_count=token_count,
            timestamp=datetime.now(),
            session_id=session_id,
            summary=summary
        )

        self._db.execute("""
            INSERT OR REPLACE INTO tool_results
            (id, tool_name, content, content_hash, token_count, timestamp, session_id, summary, short_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.id,
            entry.tool_name,
            entry.content,
            entry.content_hash,
            entry.token_count,
            entry.timestamp.isoformat(),
            entry.session_id,
            entry.summary,
            short_id,
        ))
        self._db.commit()
        
        logger.debug(
            f"Stored tool result: id={short_id}, tool={tool_name}, "
            f"tokens={token_count}, hash={content_hash}"
        )
        
        # Return compact reference as JSON (preserves structure, valid for tool messages)
        import json
        return json.dumps({
            "type": "tool_result_ref",
            "tool_name": tool_name,
            "ref_id": short_id,
            "content_hash": content_hash,
            "token_count": token_count
        })
    
    @_synchronized
    def retrieve(self, reference_id: str) -> ToolResultEntry | None:
        """Retrieve full tool result by reference ID.

        Resolution order:
          1. ``TR_xxx`` reference → exact match on the indexed ``short_id`` column.
          2. Otherwise treat ``reference_id`` as a (possibly partial) raw ``tool_call_id``:
             try exact match, then prefix match.

        Args:
            reference_id: Short reference (``TR_<10 hex>``) or raw tool_call_id.

        Returns:
            ToolResultEntry if found, None otherwise.
        """
        select_cols = (
            "SELECT id, tool_name, content, content_hash, token_count, "
            "timestamp, session_id, summary FROM tool_results "
        )

        # Path 1: TR_<hash> reference → exact lookup on short_id column.
        if reference_id.startswith("TR_"):
            row = self._db.execute(
                select_cols + "WHERE short_id = ? LIMIT 1",
                (reference_id,),
            ).fetchone()
            if row:
                return ToolResultEntry.from_row(row)
            return None  # No TR_xxx → don't fall through to a wrong prefix match

        # Path 2: raw tool_call_id (or its prefix) — exact then prefix match.
        row = self._db.execute(
            select_cols + "WHERE id = ?",
            (reference_id,),
        ).fetchone()
        if row:
            return ToolResultEntry.from_row(row)

        row = self._db.execute(
            select_cols + "WHERE id LIKE ? LIMIT 1",
            (f"{reference_id}%",),
        ).fetchone()
        if row:
            return ToolResultEntry.from_row(row)

        return None
    
    @_synchronized
    def retrieve_by_hash(self, content_hash: str) -> ToolResultEntry | None:
        """Retrieve tool result by content hash.
        
        Args:
            content_hash: The content hash from the reference
            
        Returns:
            ToolResultEntry if found, None otherwise
        """
        cursor = self._db.execute(
            "SELECT id, tool_name, content, content_hash, token_count, timestamp, session_id, summary "
            "FROM tool_results WHERE content_hash = ? ORDER BY timestamp DESC LIMIT 1",
            (content_hash,)
        )
        row = cursor.fetchone()
        
        return ToolResultEntry.from_row(row) if row else None
    
    @_synchronized
    def get_stats(self, session_id: str | None = None) -> dict[str, Any]:
        """Get statistics about stored tool results.
        
        Args:
            session_id: Optional session filter
            
        Returns:
            Dictionary with stats
        """
        if session_id:
            cursor = self._db.execute(
                "SELECT COUNT(*), SUM(token_count) FROM tool_results WHERE session_id = ?",
                (session_id,)
            )
        else:
            cursor = self._db.execute(
                "SELECT COUNT(*), SUM(token_count) FROM tool_results"
            )
        
        row = cursor.fetchone()
        count = row[0] or 0
        total_tokens = row[1] or 0
        
        # Get by tool name
        if session_id:
            cursor = self._db.execute(
                "SELECT tool_name, COUNT(*), SUM(token_count) FROM tool_results "
                "WHERE session_id = ? GROUP BY tool_name",
                (session_id,)
            )
        else:
            cursor = self._db.execute(
                "SELECT tool_name, COUNT(*), SUM(token_count) FROM tool_results "
                "GROUP BY tool_name"
            )
        
        by_tool = {}
        for tool_row in cursor.fetchall():
            by_tool[tool_row[0]] = {
                "count": tool_row[1],
                "tokens": tool_row[2]
            }
        
        return {
            "total_entries": count,
            "total_tokens_stored": total_tokens,
            "by_tool": by_tool
        }
    
    @_synchronized
    def cleanup_old(self, max_age_hours: int = 24, session_id: str | None = None) -> int:
        """Remove old entries to save storage space.
        
        Args:
            max_age_hours: Remove entries older than this
            session_id: Optional session filter
            
        Returns:
            Number of entries removed
        """
        cutoff = datetime.now().isoformat()
        # Calculate cutoff (simplified - SQLite datetime comparison)
        from datetime import timedelta
        cutoff_dt = datetime.now() - timedelta(hours=max_age_hours)
        cutoff = cutoff_dt.isoformat()
        
        if session_id:
            cursor = self._db.execute(
                "DELETE FROM tool_results WHERE session_id = ? AND timestamp < ?",
                (session_id, cutoff)
            )
        else:
            cursor = self._db.execute(
                "DELETE FROM tool_results WHERE timestamp < ?",
                (cutoff,)
            )
        
        deleted = cursor.rowcount
        self._db.commit()
        
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old tool result entries")
        
        return deleted
    
    @_synchronized
    def close(self) -> None:
        """Close database connection."""
        if self._db:
            self._db.close()
            self._db = None


def parse_tool_reference(reference: str) -> dict[str, str] | None:
    """Parse a tool result reference string.
    
    Args:
        reference: Reference string like "[Tool:read_file ref:tc_abc12 hash:a1b2c3d4]"
        
    Returns:
        Dictionary with tool_name, ref, hash; or None if invalid
    """
    import re
    
    pattern = r'\[Tool:(\w+)\s+ref:(\w+)\s+hash:(\w+)\]'
    match = re.match(pattern, reference)
    
    if match:
        return {
            "tool_name": match.group(1),
            "ref": match.group(2),
            "hash": match.group(3)
        }
    
    return None
