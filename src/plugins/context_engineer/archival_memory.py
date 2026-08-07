"""Archival Memory - Full conversation history storage with search.

This module provides archival storage for conversation messages that have
been paged out of the active context. It supports both simple text search
and optional semantic search via VectorStore (ChromaDB/sqlite-vec).

Key features:
- SQLite-based persistent storage
- Message summarization before archiving
- Semantic search via VectorStore (ChromaDB or sqlite-vec backend)
- Session-scoped archives
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_system.llm.token_utils import estimate_content_tokens

logger = logging.getLogger(__name__)


def _synchronized(method):
    """Serialize a store method on the instance's reentrant lock.

    ArchivalMemory's single sqlite3.Connection (check_same_thread=False) is hit
    both synchronously from the event loop (handler search/get_stats) and from
    worker threads via asyncio.to_thread (compaction store), so concurrent
    access must be serialized to avoid recursive-cursor errors / corruption.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


@dataclass
class ArchivedMessage:
    """An archived conversation message."""
    id: str
    role: str
    content: str
    summary: str
    timestamp: datetime
    session_id: str
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None
    tool_name: str | None = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "summary": self.summary,
            "timestamp": self.timestamp.isoformat(),
            "session_id": self.session_id,
            "token_count": self.token_count,
            "metadata": self.metadata,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name
        }
    
    @classmethod
    def from_row(cls, row: tuple) -> "ArchivedMessage":
        """Create from SQLite row."""
        return cls(
            id=row[0],
            role=row[1],
            content=row[2],
            summary=row[3],
            timestamp=datetime.fromisoformat(row[4]),
            session_id=row[5],
            token_count=row[6],
            metadata=json.loads(row[7]) if row[7] else {},
            tool_call_id=row[8],
            tool_name=row[9]
        )


class ArchivalMemory:
    """Archival storage for paged-out conversation messages.
    
    When messages are paged out of the active context, they are stored here
    with a summary. The LLM can search this archive to recall past conversations
    and decisions.
    
    Supports two search modes:
    - Text search: Simple substring/keyword matching via SQLite FTS5
    - Semantic search: Vector similarity search via VectorStore (optional)
    
    Usage:
        archive = ArchivalMemory(storage_path)
        
        # Store a message
        archive.store(message, summary="User asked about Python optimization")
        
        # Search for relevant messages
        results = archive.search("optimization", limit=5)
        
        # Get all messages for context reconstruction
        messages = archive.get_session_messages(session_id)
    """
    
    def __init__(
        self,
        storage_path: Path,
        session_id: str | None = None,
        enable_semantic_search: bool = False,
        vector_store_path: Path | None = None
    ):
        """Initialize archival memory.
        
        Args:
            storage_path: Path to SQLite database file
            session_id: Default session ID
            enable_semantic_search: Enable VectorStore semantic search
            vector_store_path: Path for VectorStore storage (if enabled)
        """
        self.storage_path = storage_path
        self.session_id = session_id
        self.enable_semantic_search = enable_semantic_search
        self._db: sqlite3.Connection | None = None
        self._vector_store = None
        self._vector_collection = "archival_memory"
        # Serializes connection access across event-loop + to_thread threads
        # (see _synchronized). Reentrant for nested synchronized calls.
        self._lock = threading.RLock()

        self._init_db()
        
        if enable_semantic_search:
            self._init_vector_store(vector_store_path or storage_path.parent / "vectors")
    
    def _init_db(self) -> None:
        """Initialize SQLite database."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._db = sqlite3.connect(str(self.storage_path), check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS archived_messages (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                session_id TEXT NOT NULL,
                token_count INTEGER NOT NULL,
                metadata TEXT,
                tool_call_id TEXT,
                tool_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_archived_session 
            ON archived_messages(session_id)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_archived_timestamp 
            ON archived_messages(timestamp)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_archived_role 
            ON archived_messages(role)
        """)
        # Full-text search index
        self._db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS archived_fts USING fts5(
                id, summary, content, session_id,
                content='archived_messages',
                content_rowid='rowid'
            )
        """)
        self._db.commit()
    
    def _init_vector_store(self, vector_path: Path) -> None:
        """Initialize VectorStore for semantic search."""
        try:
            from agent_system.utils.vector_store import VectorStore
            
            self._vector_store = VectorStore(persist_path=vector_path)
            # Ensure collection is created
            self._vector_store.get_or_create_collection(self._vector_collection)
            
            logger.info(
                f"VectorStore initialized at {vector_path} "
                f"(backend: {self._vector_store.backend})"
            )
            
        except Exception as e:
            logger.error(f"Failed to initialize VectorStore: {e}")
            self.enable_semantic_search = False
            self._vector_store = None
    
    @_synchronized
    def store(
        self,
        message: dict[str, Any],
        summary: str | None = None,
        session_id: str | None = None
    ) -> str:
        """Store a message in the archive.
        
        Args:
            message: Message dict with role, content, etc.
            summary: Optional summary (auto-generated if not provided)
            session_id: Session ID for isolation
            
        Returns:
            The archive entry ID
        """
        import uuid
        
        session_id = session_id or self.session_id or "default"
        
        # Generate ID
        entry_id = f"arch_{uuid.uuid4().hex[:12]}"
        
        # Extract message fields
        role = message.get("role", "unknown")
        content = message.get("content", "")
        if isinstance(content, list):
            # Handle multi-part content
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        elif content is None:
            # An assistant message that only makes tool calls carries
            # content=None. The column is NOT NULL, so this used to raise
            # IntegrityError and take Layer 2's whole archiving loop with it —
            # the same shape trap as tool_calls=None two lines below.
            content = ""
        
        # Generate summary if not provided
        if not summary:
            summary = self._generate_summary(message)
        
        # Estimate tokens
        token_count = estimate_content_tokens(content)
        
        # Extract tool info
        tool_call_id = message.get("tool_call_id")
        tool_name = message.get("name")
        
        # Store metadata. ``tool_calls`` is routinely present WITH VALUE None:
        # measured over the 40 largest sessions, 5838 messages carry None
        # against 709 carrying a list. An ``in`` test says yes for those and the
        # iteration below then raises TypeError, killing the whole Layer-2
        # archival. Truthiness is the only safe test on this field.
        metadata = {}
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            metadata["tool_calls"] = [
                {"name": tc.get("function", {}).get("name")}
                for tc in tool_calls
            ]
        
        # Insert into SQLite
        self._db.execute("""
            INSERT INTO archived_messages 
            (id, role, content, summary, timestamp, session_id, token_count, 
             metadata, tool_call_id, tool_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry_id,
            role,
            content,
            summary,
            datetime.now().isoformat(),
            session_id,
            token_count,
            json.dumps(metadata) if metadata else None,
            tool_call_id,
            tool_name
        ))
        
        # Update FTS index
        self._db.execute("""
            INSERT INTO archived_fts (id, summary, content, session_id)
            VALUES (?, ?, ?, ?)
        """, (entry_id, summary, content, session_id))
        
        self._db.commit()
        
        # Add to VectorStore if enabled
        if self.enable_semantic_search and self._vector_store:
            try:
                self._vector_store.add(
                    collection=self._vector_collection,
                    ids=[entry_id],
                    documents=[f"{summary}\n{content[:1000]}"],
                    metadatas=[{
                        "session_id": session_id,
                        "role": role,
                        "timestamp": datetime.now().isoformat()
                    }]
                )
            except Exception as e:
                logger.error(f"Failed to add to VectorStore: {e}")
        
        logger.debug(
            f"Archived message: id={entry_id}, role={role}, "
            f"tokens={token_count}, summary='{summary[:50]}...'"
        )

        return entry_id

    def store_many(
        self,
        messages: list[dict[str, Any]],
        session_id: str | None = None,
        index_semantic: bool | None = None
    ) -> list[str]:
        """Store many messages in ONE transaction. Returns ids, input order.

        ``store`` commits per message, which is the entire cost of archiving in
        bulk: measured on 4682 messages, 16.1 s of commits versus 0.08 s for the
        same rows written in a single transaction. Pre-Layer P archives whole
        blocks at once and cannot pay per-message commits.

        ``index_semantic=False`` skips the VectorStore. The rows and the FTS
        index are written either way, so the content stays listable and
        findable by keyword; only vector similarity would miss it. Callers use
        this when a batch is large enough that embedding it would stall the
        request (embedding dominates: 80 s of the same 4682-message batch).

        NOT synchronized itself: the connection work is, the embedding must not
        be (see ``_index_semantic``).
        """
        if not messages:
            return []

        ids, documents, metadatas = self._store_many_rows(messages, session_id)
        if index_semantic is not False and self.enable_semantic_search and self._vector_store:
            self._index_semantic(ids, documents, metadatas)
        return ids

    @_synchronized
    def _store_many_rows(
        self,
        messages: list[dict[str, Any]],
        session_id: str | None
    ) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        """The connection half of ``store_many``: rows + FTS in one transaction."""

        import uuid

        session_id = session_id or self.session_id or "default"

        # One timestamp per ROW, not one per batch. get_session_messages orders
        # by `timestamp ASC, id ASC` and the id is a random uuid — with a shared
        # timestamp the whole batch comes back in random order, and the prune
        # breadcrumb points the agent straight at that listing. The offsets keep
        # the conversation order of the input.
        base = datetime.now()
        stamps = [(base + timedelta(microseconds=i)).isoformat()
                  for i in range(len(messages))]

        rows: list[tuple] = []
        fts_rows: list[tuple] = []
        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for position, message in enumerate(messages):
            now = stamps[position]
            entry_id = f"arch_{uuid.uuid4().hex[:12]}"
            role = message.get("role", "unknown")
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            elif content is None:
                content = ""
            summary = self._generate_summary(message)

            metadata: dict[str, Any] = {}
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                metadata["tool_calls"] = [
                    {"name": tc.get("function", {}).get("name")}
                    for tc in tool_calls
                ]

            rows.append((
                entry_id, role, content, summary, now, session_id,
                estimate_content_tokens(content),
                json.dumps(metadata) if metadata else None,
                message.get("tool_call_id"), message.get("name"),
            ))
            fts_rows.append((entry_id, summary, content, session_id))
            ids.append(entry_id)
            documents.append(f"{summary}\n{content[:1000]}")
            metadatas.append({"session_id": session_id, "role": role,
                              "timestamp": now})

        self._db.executemany("""
            INSERT INTO archived_messages
            (id, role, content, summary, timestamp, session_id, token_count,
             metadata, tool_call_id, tool_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        self._db.executemany("""
            INSERT INTO archived_fts (id, summary, content, session_id)
            VALUES (?, ?, ?, ?)
        """, fts_rows)
        self._db.commit()

        logger.debug(f"Archived {len(ids)} messages in one transaction")
        return ids, documents, metadatas

    def _index_semantic(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]]
    ) -> None:
        """Feed a stored batch to the VectorStore. NOT synchronized, on purpose.

        Embedding a full batch costs ~17 ms per message — up to several seconds
        — and this instance's lock is taken synchronously on the event loop by
        recall, context_list, context_read and stats. Holding it across the
        embedding freezes the loop for every concurrent request. The rows are
        already committed, the VectorStore has its own lock, and it never calls
        back into this object, so nothing here needs the connection.
        """
        try:
            self._vector_store.add(
                collection=self._vector_collection,
                ids=ids,
                documents=documents,
                metadatas=metadatas
            )
        except Exception as e:
            # The rows are already committed — a failed index costs similarity
            # search on these entries, not the content.
            logger.error(f"Failed to add batch to VectorStore: {e}")

    @_synchronized
    def search(
        self,
        query: str,
        session_id: str | None = None,
        limit: int = 5,
        use_semantic: bool | None = None
    ) -> list[ArchivedMessage]:
        """Search archived messages.
        
        Args:
            query: Search query
            session_id: Optional session filter
            limit: Maximum results
            use_semantic: Use semantic search (default: auto-detect)
            
        Returns:
            List of matching archived messages
        """
        session_id = session_id or self.session_id
        
        # Determine search mode
        if use_semantic is None:
            use_semantic = self.enable_semantic_search
        
        if use_semantic and self._vector_store:
            return self._search_semantic(query, session_id, limit)
        else:
            return self._search_text(query, session_id, limit)
    
    def _escape_fts5_query(self, query: str) -> str:
        """Escape special characters for FTS5 MATCH syntax.
        
        FTS5 has special characters that need escaping:
        - Double quotes around phrases
        - Special operators: AND, OR, NOT, NEAR, *
        - Punctuation: . - : @ # etc.
        
        Strategy: Wrap each word in double quotes to treat as literal.
        """
        import re
        
        # Split on whitespace, wrap each token in quotes
        # This makes FTS5 treat each word as a literal phrase
        tokens = query.split()
        
        # Escape any double quotes within tokens
        escaped_tokens = []
        for token in tokens:
            # Remove/escape problematic chars
            # FTS5 doesn't like bare punctuation
            cleaned = re.sub(r'[^\w\s]', ' ', token).strip()
            if cleaned:
                escaped_tokens.append(f'"{cleaned}"')
        
        if not escaped_tokens:
            # Fallback: just return original with dangerous chars removed
            return re.sub(r'[^\w\s]', ' ', query).strip()
        
        # Join with OR for broader matching
        return ' OR '.join(escaped_tokens)
    
    def _search_text(
        self,
        query: str,
        session_id: str | None,
        limit: int
    ) -> list[ArchivedMessage]:
        """Full-text search using SQLite FTS5."""
        # Escape query for FTS5 syntax safety
        fts_query = self._escape_fts5_query(query)
        
        try:
            # Build query
            if session_id:
                cursor = self._db.execute("""
                    SELECT am.id, am.role, am.content, am.summary, am.timestamp,
                           am.session_id, am.token_count, am.metadata, 
                           am.tool_call_id, am.tool_name
                    FROM archived_messages am
                    JOIN archived_fts fts ON am.id = fts.id
                    WHERE archived_fts MATCH ? AND am.session_id = ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, session_id, limit))
            else:
                cursor = self._db.execute("""
                    SELECT am.id, am.role, am.content, am.summary, am.timestamp,
                           am.session_id, am.token_count, am.metadata,
                           am.tool_call_id, am.tool_name
                    FROM archived_messages am
                    JOIN archived_fts fts ON am.id = fts.id
                    WHERE archived_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, limit))
            
            results = [ArchivedMessage.from_row(row) for row in cursor.fetchall()]
            
        except Exception as e:
            # FTS5 query failed - fall through to LIKE search
            logger.debug(f"FTS5 search failed for query '{query}': {e}")
            results = []
        
        # Fallback to LIKE if FTS returns nothing
        if not results:
            like_query = f"%{query}%"
            if session_id:
                cursor = self._db.execute("""
                    SELECT id, role, content, summary, timestamp, session_id,
                           token_count, metadata, tool_call_id, tool_name
                    FROM archived_messages
                    WHERE session_id = ? AND (summary LIKE ? OR content LIKE ?)
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (session_id, like_query, like_query, limit))
            else:
                cursor = self._db.execute("""
                    SELECT id, role, content, summary, timestamp, session_id,
                           token_count, metadata, tool_call_id, tool_name
                    FROM archived_messages
                    WHERE summary LIKE ? OR content LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (like_query, like_query, limit))
            
            results = [ArchivedMessage.from_row(row) for row in cursor.fetchall()]
        
        return results
    
    def _search_semantic(
        self,
        query: str,
        session_id: str | None,
        limit: int
    ) -> list[ArchivedMessage]:
        """Semantic search using VectorStore."""
        if not self._vector_store:
            return self._search_text(query, session_id, limit)
        
        try:
            # Build where filter (only supported by ChromaDB backend)
            where = {"session_id": session_id} if session_id else None
            
            results = self._vector_store.query(
                collection=self._vector_collection,
                query_text=query,
                n_results=limit,
                where=where
            )
            
            if not results or not results.get("ids") or not results["ids"]:
                return []
            
            # Fetch full messages from SQLite
            ids = results["ids"]
            placeholders = ",".join("?" * len(ids))
            cursor = self._db.execute(f"""
                SELECT id, role, content, summary, timestamp, session_id,
                       token_count, metadata, tool_call_id, tool_name
                FROM archived_messages
                WHERE id IN ({placeholders})
            """, ids)
            
            # Maintain order from VectorStore results
            rows_by_id = {row[0]: row for row in cursor.fetchall()}
            return [
                ArchivedMessage.from_row(rows_by_id[id_])
                for id_ in ids if id_ in rows_by_id
            ]
            
        except Exception as e:
            logger.error(f"Semantic search failed, falling back to text: {e}")
            return self._search_text(query, session_id, limit)
    
    @_synchronized
    def get_session_messages(
        self,
        session_id: str | None = None,
        limit: int | None = None,
        role: str | None = None,
        offset: int = 0
    ) -> list[ArchivedMessage]:
        """Get archived messages for a session, oldest first.

        Args:
            session_id: Session ID
            limit: Optional limit
            role: Optional role filter
            offset: Skip this many entries — the other half of paging. Without
                it a caller can only ever see the first page, which is why the
                agent-facing tools could not walk the history at all.

        Returns:
            List of archived messages
        """
        session_id = session_id or self.session_id or "default"

        query = """
            SELECT id, role, content, summary, timestamp, session_id,
                   token_count, metadata, tool_call_id, tool_name
            FROM archived_messages
            WHERE session_id = ?
        """
        params: list[Any] = [session_id]

        if role:
            query += " AND role = ?"
            params.append(role)

        # id as tiebreaker: same-second timestamps are common (a tool call and
        # its result), and an unstable order would make paging skip or repeat.
        query += " ORDER BY timestamp ASC, id ASC"

        if limit:
            query += " LIMIT ?"
            params.append(limit)
        elif offset:
            query += " LIMIT -1"  # SQLite requires a LIMIT before OFFSET
        if offset:
            query += " OFFSET ?"
            params.append(offset)

        cursor = self._db.execute(query, params)
        return [ArchivedMessage.from_row(row) for row in cursor.fetchall()]

    @_synchronized
    def get(self, entry_id: str) -> ArchivedMessage | None:
        """One archived message by its id — a primary-key lookup.

        The context leaves ``{"type":"archived_ref","ref_id":"arch_…"}`` exactly
        where the message stood, so reading by that address is the shortest path
        back to the content. Going through the full-text index for it happens to
        work but is a coincidence of the id being indexed, not a contract.
        """
        row = self._db.execute(
            """
            SELECT id, role, content, summary, timestamp, session_id,
                   token_count, metadata, tool_call_id, tool_name
            FROM archived_messages WHERE id = ?
            """,
            (entry_id,),
        ).fetchone()
        return ArchivedMessage.from_row(row) if row else None

    @_synchronized
    def count_session_messages(self, session_id: str | None = None,
                               role: str | None = None) -> int:
        """How many archived messages a session has — the total a pager needs
        to tell the agent whether more pages exist."""
        session_id = session_id or self.session_id or "default"
        query = "SELECT COUNT(*) FROM archived_messages WHERE session_id = ?"
        params: list[Any] = [session_id]
        if role:
            query += " AND role = ?"
            params.append(role)
        row = self._db.execute(query, params).fetchone()
        return int(row[0]) if row else 0
    
    @_synchronized
    def get_stats(self, session_id: str | None = None) -> dict[str, Any]:
        """Get statistics about archived messages.
        
        Args:
            session_id: Optional session filter
            
        Returns:
            Dictionary with stats
        """
        if session_id:
            cursor = self._db.execute("""
                SELECT COUNT(*), SUM(token_count)
                FROM archived_messages WHERE session_id = ?
            """, (session_id,))
        else:
            cursor = self._db.execute("""
                SELECT COUNT(*), SUM(token_count) FROM archived_messages
            """)
        
        row = cursor.fetchone()
        count = row[0] or 0
        total_tokens = row[1] or 0
        
        # By role
        if session_id:
            cursor = self._db.execute("""
                SELECT role, COUNT(*) FROM archived_messages
                WHERE session_id = ? GROUP BY role
            """, (session_id,))
        else:
            cursor = self._db.execute("""
                SELECT role, COUNT(*) FROM archived_messages GROUP BY role
            """)
        
        by_role = {row[0]: row[1] for row in cursor.fetchall()}
        
        return {
            "total_messages": count,
            "total_tokens": total_tokens,
            "by_role": by_role,
            "semantic_search_enabled": self.enable_semantic_search
        }
    
    def _generate_summary(self, message: dict[str, Any]) -> str:
        """Generate a summary for a message.
        
        Args:
            message: The message dict
            
        Returns:
            Brief summary string
        """
        role = message.get("role", "unknown")
        content = message.get("content", "")
        
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        
        # Handle tool calls. Same shape trap as in store(): the key is present
        # with value None far more often than it holds a list, so an ``in`` test
        # both raises here AND used to produce the empty "called tools: ".
        # Falling through to the content summary is the better answer anyway.
        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            tools = [
                tc.get("function", {}).get("name", "unknown")
                for tc in tool_calls
            ]
            return f"Assistant called tools: {', '.join(tools)}"
        
        # Handle tool responses
        if role == "tool":
            tool_name = message.get("name", "unknown")
            return f"Tool response from {tool_name}: {content[:100]}..."
        
        # Regular messages - first sentence or line
        if content:
            first_part = content.split('\n')[0][:150]
            if len(first_part) < len(content):
                first_part += "..."
            return f"{role.title()}: {first_part}"
        
        return f"{role.title()} message (empty)"
    
    @_synchronized
    def cleanup_old(
        self,
        max_age_days: int = 7,
        session_id: str | None = None
    ) -> int:
        """Remove old archived messages.
        
        Args:
            max_age_days: Remove entries older than this
            session_id: Optional session filter
            
        Returns:
            Number of entries removed
        """
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        
        # Get IDs to delete (for ChromaDB cleanup)
        if session_id:
            cursor = self._db.execute(
                "SELECT id FROM archived_messages WHERE session_id = ? AND timestamp < ?",
                (session_id, cutoff)
            )
        else:
            cursor = self._db.execute(
                "SELECT id FROM archived_messages WHERE timestamp < ?",
                (cutoff,)
            )
        
        ids_to_delete = [row[0] for row in cursor.fetchall()]
        
        if not ids_to_delete:
            return 0
        
        # Delete from SQLite
        placeholders = ",".join("?" * len(ids_to_delete))
        self._db.execute(
            f"DELETE FROM archived_messages WHERE id IN ({placeholders})",
            ids_to_delete
        )
        self._db.execute(
            f"DELETE FROM archived_fts WHERE id IN ({placeholders})",
            ids_to_delete
        )
        self._db.commit()
        
        # Delete from VectorStore
        if self.enable_semantic_search and self._vector_store:
            try:
                self._vector_store.delete(
                    collection=self._vector_collection,
                    ids=ids_to_delete
                )
            except Exception as e:
                logger.error(f"Failed to delete from VectorStore: {e}")
        
        logger.info(f"Cleaned up {len(ids_to_delete)} old archived messages")
        return len(ids_to_delete)
    
    @_synchronized
    def close(self) -> None:
        """Close database connections."""
        if self._db:
            self._db.close()
            self._db = None
        if self._vector_store:
            self._vector_store.close()
            self._vector_store = None
