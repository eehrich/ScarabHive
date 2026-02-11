"""
Lessons Learned Server - Core SQLite CRUD + VectorStore integration.

Provides MCP tool interface for storing, searching, and managing
persistent lessons that agents learn across sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agent_system.hooks.plugin_hook import HookContext, HookResult, PluginHook
from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.utils.vector_store import VectorStore

from .models import (
    DEFAULT_CATEGORIES,
    DeduplicationResult,
)
from .prompt_builder import build_lesson_prompt

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPServerConfig

logger = logging.getLogger(__name__)

# ==============================================================================
# Constants
# ==============================================================================

CONFIDENCE_BASE = {
    "manual": 0.8,
    "cross_agent": 0.6,
    "reflection": 0.5,
    "auto": 0.4,
}

AUTO_DEACTIVATE_THRESHOLD = 0.2
AUTO_REACTIVATE_THRESHOLD = 0.4

# ==============================================================================
# SQL Schema
# ==============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id       TEXT NOT NULL UNIQUE,
    agent_name      TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'general',
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 5,
    confidence      REAL NOT NULL DEFAULT 0.5,
    status          TEXT NOT NULL DEFAULT 'draft',
    source_type     TEXT NOT NULL DEFAULT 'auto',
    source_agent    TEXT,
    source_session  TEXT,
    tags            TEXT DEFAULT '[]',
    context_filter  TEXT DEFAULT '{}',
    evidence_count  INTEGER NOT NULL DEFAULT 1,
    application_count INTEGER NOT NULL DEFAULT 0,
    effectiveness   REAL,
    last_applied_at TEXT,
    last_confirmed_at TEXT,
    expires_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (priority BETWEEN 1 AND 10),
    CHECK (confidence BETWEEN 0.0 AND 1.0),
    CHECK (status IN ('draft', 'active', 'inactive', 'archived')),
    CHECK (source_type IN ('auto', 'cross_agent', 'manual', 'reflection'))
);

CREATE INDEX IF NOT EXISTS idx_lessons_agent ON lessons(agent_name);
CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status);
CREATE INDEX IF NOT EXISTS idx_lessons_agent_status ON lessons(agent_name, status);
CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(category);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    agent_name  TEXT,
    icon        TEXT DEFAULT '📝',
    sort_order  INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lesson_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id   TEXT NOT NULL REFERENCES lessons(lesson_id) ON DELETE CASCADE,
    session_id  TEXT NOT NULL,
    agent_name  TEXT NOT NULL,
    evidence_type TEXT NOT NULL DEFAULT 'confirm',
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (evidence_type IN ('confirm', 'contradict', 'neutral'))
);

CREATE INDEX IF NOT EXISTS idx_evidence_lesson ON lesson_evidence(lesson_id);

CREATE TABLE IF NOT EXISTS lesson_applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id       TEXT NOT NULL REFERENCES lessons(lesson_id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
    turn_count      INTEGER,
    outcome         TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_applications_lesson ON lesson_applications(lesson_id);

CREATE TABLE IF NOT EXISTS extraction_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    extracted_count INTEGER NOT NULL DEFAULT 0,
    skipped_reason  TEXT,
    llm_profile     TEXT,
    token_usage     INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class LessonsLearnedServer(SchemaBasedMCPServer, PluginHook):
    """
    Lessons Learned MCP Server with semantic search.

    Implements:
    - MCP tool: lessons_learned with operations (store/search/list/update/confirm/teach)
    - Hooks: inject_lessons (pre_llm_call), extract_lessons (session_end)
    - Storage: SQLite (structured) + VectorStore (semantic search/dedup)
    """

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPServerConfig",
    ) -> None:
        super().__init__(name, system_config, mcp_config)
        self._system_config = system_config

        # Resolve paths from mcp_config (runtime overrides) with sensible defaults
        # Follows pattern from todo/memory plugins: getattr(mcp_config, key, default)
        db_path_str = str(getattr(mcp_config, "database_path", "data/lessons_learned/lessons.db"))
        # Also support dict-style config (used in tests via MagicMock)
        if hasattr(mcp_config, "config") and isinstance(mcp_config.config, dict):
            db_path_str = mcp_config.config.get("database_path", db_path_str)
        self.db_path = Path(db_path_str)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Vector store for semantic search/dedup
        self.vector_store_path = self.db_path.parent / "vectors"
        self.vector_store_path.mkdir(parents=True, exist_ok=True)
        self.vector_store = VectorStore(persist_path=str(self.vector_store_path))

        # Plugin config - read from mcp_config attributes (set from plugins.yaml)
        self.max_lessons_per_agent = int(getattr(mcp_config, "max_lessons_per_agent", 200))
        self.dedup_similarity_threshold = float(getattr(mcp_config, "dedup_similarity_threshold", 0.82))
        self.exact_duplicate_threshold = float(getattr(mcp_config, "exact_duplicate_threshold", 0.95))
        self.consolidation_llm_profile = str(getattr(mcp_config, "consolidation_llm_profile", "turbo"))

        # Lesson ID counters (agent_name -> int)
        self._lesson_counters: Dict[str, int] = {}

        # Initialize database
        self._ensure_database()

        logger.info(f"LessonsLearnedServer initialized: db={self.db_path}")

    # ==========================================================================
    # Database Management
    # ==========================================================================

    def _get_connection(self) -> sqlite3.Connection:
        """Get a SQLite connection with WAL mode."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn
    
    def _ensure_database(self) -> None:
        """Create tables and seed default categories."""
        conn = self._get_connection()
        try:
            conn.executescript(SCHEMA_SQL)
            # Seed default categories if empty
            count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            if count == 0:
                for cat in DEFAULT_CATEGORIES:
                    conn.execute(
                        "INSERT OR IGNORE INTO categories (name, description, icon, sort_order) VALUES (?, ?, ?, ?)",
                        (cat["name"], cat["description"], cat["icon"], cat["sort_order"]),
                    )
            conn.commit()
        finally:
            conn.close()

    def _generate_lesson_id(self, agent_name: str) -> str:
        """Generate sequential lesson ID for an agent: {agent}_les_001, {agent}_les_002, ..."""
        prefix = f"{agent_name}_les_"
        if agent_name not in self._lesson_counters:
            conn = self._get_connection()
            try:
                # Find the max numeric suffix for this agent's lessons
                row = conn.execute(
                    "SELECT MAX(CAST(SUBSTR(lesson_id, ?) AS INTEGER)) "
                    "FROM lessons WHERE agent_name = ? AND lesson_id LIKE ?",
                    (len(prefix) + 1, agent_name, f"{prefix}%"),
                ).fetchone()
                self._lesson_counters[agent_name] = (row[0] or 0)
            finally:
                conn.close()

        self._lesson_counters[agent_name] += 1
        return f"{prefix}{self._lesson_counters[agent_name]:03d}"

    def _collection_name(self, agent_name: str) -> str:
        """VectorStore collection name for an agent."""
        return f"lessons_{agent_name.replace('-', '_')}"

    # ==========================================================================
    # CRUD Operations
    # ==========================================================================

    async def store_lesson(
        self,
        agent_name: str,
        title: str,
        content: str,
        category: str = "general",
        priority: int = 5,
        tags: Optional[List[str]] = None,
        source_type: str = "manual",
        source_agent: Optional[str] = None,
        source_session: Optional[str] = None,
        status: str = "draft",
        confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Store a new lesson in SQLite + VectorStore."""
        lesson_id = self._generate_lesson_id(agent_name)
        if confidence is None:
            confidence = CONFIDENCE_BASE.get(source_type, 0.5)
        tags_json = json.dumps(tags or [])
        now = datetime.now(UTC).isoformat()

        conn = self._get_connection()
        try:
            # Check limit
            count = conn.execute(
                "SELECT COUNT(*) FROM lessons WHERE agent_name = ? AND status != 'archived'",
                (agent_name,),
            ).fetchone()[0]
            if count >= self.max_lessons_per_agent:
                return {"error": f"Lesson limit ({self.max_lessons_per_agent}) reached for agent '{agent_name}'. Archive or delete older lessons."}

            conn.execute(
                """INSERT INTO lessons
                   (lesson_id, agent_name, category, title, content, priority, confidence,
                    status, source_type, source_agent, source_session, tags,
                    last_confirmed_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (lesson_id, agent_name, category, title, content, priority, confidence,
                 status, source_type, source_agent, source_session, tags_json,
                 now, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        # Store in VectorStore for semantic search
        try:
            vector_text = f"{title}. {content}"
            await asyncio.to_thread(
                self.vector_store.add,
                collection=self._collection_name(agent_name),
                ids=[lesson_id],
                documents=[vector_text],
                metadatas=[{"lesson_id": lesson_id, "agent_name": agent_name, "category": category}],
            )
        except Exception as e:
            logger.warning(f"VectorStore add failed (non-critical): {e}")

        logger.info(f"Stored lesson {lesson_id} for agent '{agent_name}': {title}")
        return {
            "status": "stored",
            "lesson_id": lesson_id,
            "agent_name": agent_name,
            "title": title,
            "lesson_status": status,
        }

    async def search_lessons(
        self,
        query: str,
        agent_name: Optional[str] = None,
        category: Optional[str] = None,
        status: Optional[str] = "active",
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Semantic search for lessons using VectorStore."""
        # Determine which agents to search
        if agent_name:
            agents_to_search = [agent_name]
        else:
            conn = self._get_connection()
            try:
                rows = conn.execute("SELECT DISTINCT agent_name FROM lessons").fetchall()
                agents_to_search = [r[0] for r in rows]
            finally:
                conn.close()

        all_results: List[Dict[str, Any]] = []
        for agent in agents_to_search:
            try:
                results = await asyncio.to_thread(
                    self.vector_store.query,
                    collection=self._collection_name(agent),
                    query_text=query,
                    n_results=limit,
                    include=["documents", "metadatas", "distances"],
                )
                if results["ids"] and len(results["ids"][0]) > 0:
                    for i in range(len(results["ids"][0])):
                        lid = results["ids"][0][i]
                        distance = results["distances"][0][i]
                        similarity = max(0.0, min(1.0, 1.0 - (distance / 2.0)))
                        all_results.append({
                            "lesson_id": lid,
                            "agent_name": agent,
                            "similarity": round(similarity, 3),
                        })
            except Exception as e:
                logger.debug(f"VectorStore query for {agent} failed: {e}")

        # Sort by similarity, take top N
        all_results.sort(key=lambda r: r["similarity"], reverse=True)
        top_ids = [r["lesson_id"] for r in all_results[:limit]]

        if not top_ids:
            return {"query": query, "results": [], "count": 0}

        # Fetch full lesson data from SQLite
        conn = self._get_connection()
        try:
            placeholders = ",".join("?" * len(top_ids))
            sql = f"SELECT * FROM lessons WHERE lesson_id IN ({placeholders})"
            params: list[Any] = list(top_ids)
            if status:
                sql += " AND status = ?"
                params.append(status)
            if category:
                sql += " AND category = ?"
                params.append(category)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        # Build results with similarity scores
        lessons_by_id = {dict(r)["lesson_id"]: dict(r) for r in rows}
        results = []
        for r in all_results[:limit]:
            lesson = lessons_by_id.get(r["lesson_id"])
            if lesson:
                lesson["similarity"] = r["similarity"]
                lesson["tags"] = json.loads(lesson.get("tags", "[]"))
                results.append(lesson)

        return {"query": query, "results": results, "count": len(results)}

    async def list_lessons(
        self,
        agent_name: Optional[str] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        sort_by: str = "priority",
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List lessons with filtering and pagination."""
        conn = self._get_connection()
        try:
            sql = "SELECT * FROM lessons WHERE 1=1"
            params: list[Any] = []
            if agent_name:
                sql += " AND agent_name = ?"
                params.append(agent_name)
            if status:
                sql += " AND status = ?"
                params.append(status)
            if category:
                sql += " AND category = ?"
                params.append(category)

            # Count total
            count_sql = sql.replace("SELECT *", "SELECT COUNT(*)")
            total = conn.execute(count_sql, params).fetchone()[0]

            # Sort
            sort_map = {
                "priority": "priority DESC, confidence DESC",
                "confidence": "confidence DESC",
                "evidence": "evidence_count DESC",
                "created": "created_at DESC",
                "updated": "updated_at DESC",
                "applied": "last_applied_at DESC NULLS LAST",
            }
            order = sort_map.get(sort_by, "priority DESC")
            sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(sql, params).fetchall()
            lessons = []
            for r in rows:
                lesson = dict(r)
                lesson["tags"] = json.loads(lesson.get("tags", "[]"))
                lessons.append(lesson)

            return {"lessons": lessons, "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()

    async def get_lesson(self, lesson_id: str) -> Optional[Dict[str, Any]]:
        """Get a single lesson by ID with evidence summary."""
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT * FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone()
            if not row:
                return None
            lesson = dict(row)
            lesson["tags"] = json.loads(lesson.get("tags", "[]"))
            lesson["context_filter"] = json.loads(lesson.get("context_filter", "{}"))

            # Get evidence summary
            evidence = conn.execute(
                """SELECT evidence_type, COUNT(*) as cnt
                   FROM lesson_evidence WHERE lesson_id = ?
                   GROUP BY evidence_type""",
                (lesson_id,),
            ).fetchall()
            lesson["evidence_summary"] = {r["evidence_type"]: r["cnt"] for r in evidence}

            return lesson
        finally:
            conn.close()

    async def update_lesson(self, lesson_id: str, **updates: Any) -> Dict[str, Any]:
        """Update a lesson's fields."""
        allowed = {"title", "content", "category", "priority", "status", "confidence",
                    "tags", "context_filter", "expires_at", "agent_name", "source_type"}
        to_update = {k: v for k, v in updates.items() if k in allowed and v is not None}
        if not to_update:
            return {"error": "No valid fields to update."}

        # Get old lesson data before update (needed for agent_name migration)
        conn = self._get_connection()
        try:
            old_lesson = conn.execute(
                "SELECT agent_name FROM lessons WHERE lesson_id = ?", (lesson_id,)
            ).fetchone()
            if not old_lesson:
                return {"error": f"Lesson '{lesson_id}' not found."}
            old_agent_name = old_lesson["agent_name"]
        finally:
            conn.close()

        if "tags" in to_update and isinstance(to_update["tags"], list):
            to_update["tags"] = json.dumps(to_update["tags"])
        if "context_filter" in to_update and isinstance(to_update["context_filter"], dict):
            to_update["context_filter"] = json.dumps(to_update["context_filter"])
        to_update["updated_at"] = datetime.now(UTC).isoformat()

        set_clause = ", ".join(f"{k} = ?" for k in to_update)
        values = list(to_update.values()) + [lesson_id]

        conn = self._get_connection()
        try:
            conn.execute(f"UPDATE lessons SET {set_clause} WHERE lesson_id = ?", values)
            conn.commit()
        finally:
            conn.close()

        # Re-index in VectorStore if content/title/agent changed
        agent_name_changed = "agent_name" in updates and updates["agent_name"] != old_agent_name
        if "title" in updates or "content" in updates or agent_name_changed:
            lesson = await self.get_lesson(lesson_id)
            if lesson:
                new_agent_name = lesson["agent_name"]
                vector_text = f"{lesson['title']}. {lesson['content']}"
                metadata = {
                    "lesson_id": lesson_id,
                    "agent_name": new_agent_name,
                    "category": lesson["category"]
                }

                try:
                    # If agent changed, delete from old collection
                    if agent_name_changed:
                        try:
                            await asyncio.to_thread(
                                self.vector_store.delete,
                                collection=self._collection_name(old_agent_name),
                                ids=[lesson_id],
                            )
                        except Exception as e:
                            logger.debug(f"VectorStore delete from old collection failed: {e}")

                    # Add/update in (new) collection
                    await asyncio.to_thread(
                        self.vector_store.add,
                        collection=self._collection_name(new_agent_name),
                        ids=[lesson_id],
                        documents=[vector_text],
                        metadatas=[metadata],
                    )
                except Exception as e:
                    logger.warning(f"VectorStore re-index failed: {e}")

        return {"status": "updated", "lesson_id": lesson_id, "updated_fields": list(updates.keys())}

    async def delete_lesson(self, lesson_id: str) -> Dict[str, Any]:
        """Delete a lesson from SQLite + VectorStore."""
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT agent_name FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone()
            if not row:
                return {"error": f"Lesson '{lesson_id}' not found."}
            agent_name = row["agent_name"]
            conn.execute("DELETE FROM lessons WHERE lesson_id = ?", (lesson_id,))
            conn.commit()
        finally:
            conn.close()

        try:
            await asyncio.to_thread(
                self.vector_store.delete,
                collection=self._collection_name(agent_name),
                ids=[lesson_id],
            )
        except Exception as e:
            logger.debug(f"VectorStore delete failed (non-critical): {e}")

        return {"status": "deleted", "lesson_id": lesson_id}

    async def add_evidence(
        self,
        lesson_id: str,
        session_id: str,
        agent_name: str,
        evidence_type: str = "confirm",
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add evidence for/against a lesson and recalculate confidence."""
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT * FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone()
            if not row:
                return {"error": f"Lesson '{lesson_id}' not found."}

            now = datetime.now(UTC).isoformat()
            conn.execute(
                """INSERT INTO lesson_evidence (lesson_id, session_id, agent_name, evidence_type, description)
                   VALUES (?, ?, ?, ?, ?)""",
                (lesson_id, session_id, agent_name, evidence_type, description),
            )
            # Update counts and recalculate confidence
            evidence_count = conn.execute(
                "SELECT COUNT(*) FROM lesson_evidence WHERE lesson_id = ?", (lesson_id,)
            ).fetchone()[0]
            new_confidence = self._calculate_confidence(conn, lesson_id, dict(row))

            update_fields: Dict[str, Any] = {
                "evidence_count": evidence_count,
                "confidence": new_confidence,
                "updated_at": now,
            }
            if evidence_type == "confirm":
                update_fields["last_confirmed_at"] = now

            # Auto-activate/deactivate based on confidence
            current_status = row["status"]
            if new_confidence < AUTO_DEACTIVATE_THRESHOLD and current_status == "active":
                update_fields["status"] = "inactive"
            elif new_confidence >= AUTO_REACTIVATE_THRESHOLD and current_status == "inactive":
                update_fields["status"] = "active"

            set_clause = ", ".join(f"{k} = ?" for k in update_fields)
            conn.execute(
                f"UPDATE lessons SET {set_clause} WHERE lesson_id = ?",
                list(update_fields.values()) + [lesson_id],
            )
            conn.commit()
            return {
                "status": "evidence_added",
                "lesson_id": lesson_id,
                "evidence_type": evidence_type,
                "new_confidence": round(new_confidence, 3),
                "evidence_count": evidence_count,
            }
        finally:
            conn.close()

    def _calculate_confidence(self, conn: sqlite3.Connection, lesson_id: str, lesson: dict) -> float:
        """Calculate confidence score based on evidence and effectiveness."""
        source_type = lesson.get("source_type", "auto")
        base = CONFIDENCE_BASE.get(source_type, 0.5)

        confirms = conn.execute(
            "SELECT COUNT(*) FROM lesson_evidence WHERE lesson_id = ? AND evidence_type = 'confirm'",
            (lesson_id,),
        ).fetchone()[0]
        contradicts = conn.execute(
            "SELECT COUNT(*) FROM lesson_evidence WHERE lesson_id = ? AND evidence_type = 'contradict'",
            (lesson_id,),
        ).fetchone()[0]
        
        # Evidence factor: if no evidence yet, use 1.0 (trust base confidence)
        # With evidence: weight by ratio of confirms to total evidence
        total_evidence = confirms + contradicts
        if total_evidence == 0:
            evidence_factor = 1.0
        else:
            # Scale from 0.5 (all contradict) to 1.5 (all confirm)
            evidence_factor = 0.5 + (confirms / total_evidence)

        eff = lesson.get("effectiveness")
        effectiveness_factor = (0.5 + 0.5 * eff) if eff is not None else 1.0

        return min(1.0, base * evidence_factor * effectiveness_factor)

    async def record_application(
        self,
        lesson_id: str,
        session_id: str,
        agent_name: str,
    ) -> None:
        """Record that a lesson was applied (injected into prompt)."""
        conn = self._get_connection()
        try:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "INSERT INTO lesson_applications (lesson_id, session_id, agent_name) VALUES (?, ?, ?)",
                (lesson_id, session_id, agent_name),
            )
            conn.execute(
                "UPDATE lessons SET application_count = application_count + 1, last_applied_at = ?, updated_at = ? WHERE lesson_id = ?",
                (now, now, lesson_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def get_active_lessons_for_agent(
        self,
        agent_name: str,
        min_confidence: float = 0.3,
        max_lessons: int = 15,
        categories: Optional[List[str]] = None,
        exclude_categories: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get active lessons for injection into system prompt."""
        conn = self._get_connection()
        try:
            sql = "SELECT * FROM lessons WHERE agent_name = ? AND status = 'active' AND confidence >= ?"
            params: list[Any] = [agent_name, min_confidence]
            if categories:
                placeholders = ",".join("?" * len(categories))
                sql += f" AND category IN ({placeholders})"
                params.extend(categories)
            if exclude_categories:
                placeholders = ",".join("?" * len(exclude_categories))
                sql += f" AND category NOT IN ({placeholders})"
                params.extend(exclude_categories)
            sql += " ORDER BY priority DESC, confidence DESC LIMIT ?"
            params.append(max_lessons)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def get_stats(self, agent_name: Optional[str] = None) -> Dict[str, Any]:
        """Get dashboard statistics."""
        conn = self._get_connection()
        try:
            where = "WHERE agent_name = ?" if agent_name else ""
            params: list[Any] = [agent_name] if agent_name else []
            total = conn.execute(f"SELECT COUNT(*) FROM lessons {where}", params).fetchone()[0]
            by_status = conn.execute(
                f"SELECT status, COUNT(*) as cnt FROM lessons {where} GROUP BY status", params
            ).fetchall()
            by_category = conn.execute(
                f"SELECT category, COUNT(*) as cnt FROM lessons {where} GROUP BY category", params
            ).fetchall()
            agents = conn.execute("SELECT DISTINCT agent_name FROM lessons").fetchall()

            return {
                "total": total,
                "by_status": {r["status"]: r["cnt"] for r in by_status},
                "by_category": {r["category"]: r["cnt"] for r in by_category},
                "agents": [r["agent_name"] for r in agents],
                "agent_count": len(agents),
            }
        finally:
            conn.close()

    async def list_categories(self) -> List[Dict[str, Any]]:
        """List all categories."""
        conn = self._get_connection()
        try:
            rows = conn.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ==========================================================================
    # Consolidation (LLM-powered merge of similar lessons)
    # ==========================================================================

    async def consolidate_lessons(
        self,
        agent_name: Optional[str] = None,
        similarity_threshold: Optional[float] = None,
        dry_run: bool = False,
        progress_callback: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Find and merge similar lessons using LLM evaluation.

        1. Fetch all non-archived lessons (per agent or globally)
        2. Find clusters of similar lessons via VectorStore
        3. LLM evaluates each cluster: best title/content, merge recommendation
        4. Merge: keep primary, archive duplicates, bump evidence

        Returns summary of actions taken.
        """
        threshold = similarity_threshold or self.dedup_similarity_threshold

        # Determine agents to process
        if agent_name:
            agents_to_process = [agent_name]
        else:
            conn = self._get_connection()
            try:
                rows = conn.execute("SELECT DISTINCT agent_name FROM lessons WHERE status != 'archived'").fetchall()
                agents_to_process = [r[0] for r in rows]
            finally:
                conn.close()

        total_clusters = 0
        total_merged = 0
        total_skipped = 0
        merge_details: List[Dict[str, Any]] = []

        async def _progress(phase: str, message: str, percent: int) -> None:
            if progress_callback:
                await progress_callback({"type": "progress", "phase": phase, "message": message, "percent": min(100, max(0, percent))})

        await _progress("init", f"Starting consolidation for {len(agents_to_process)} agent(s)...", 5)

        agent_idx = 0
        for agent in agents_to_process:
            agent_idx += 1
            agent_base_pct = 5 + int(90 * (agent_idx - 1) / max(1, len(agents_to_process)))
            agent_range_pct = int(90 / max(1, len(agents_to_process)))

            await _progress("scan", f"Scanning agent '{agent}' for similar lessons...", agent_base_pct)
            clusters = await self._find_similar_clusters(agent, threshold)
            if not clusters:
                continue

            total_clusters += len(clusters)
            await _progress("scan", f"Agent '{agent}': found {len(clusters)} cluster(s)", agent_base_pct + 5)

            cluster_idx = 0
            for cluster in clusters:
                cluster_idx += 1
                cluster_pct = agent_base_pct + int(agent_range_pct * cluster_idx / max(1, len(clusters)))

                if len(cluster) < 2:
                    continue

                await _progress("evaluate", f"Agent '{agent}': evaluating cluster {cluster_idx}/{len(clusters)} ({len(cluster)} lessons)...", cluster_pct)

                # Fetch full lesson data for cluster
                conn = self._get_connection()
                try:
                    placeholders = ",".join("?" * len(cluster))
                    rows = conn.execute(
                        f"SELECT * FROM lessons WHERE lesson_id IN ({placeholders})",
                        cluster,
                    ).fetchall()
                    lessons = [dict(r) for r in rows]
                finally:
                    conn.close()

                if len(lessons) < 2:
                    continue

                # Ask LLM to evaluate the cluster
                evaluation = await self._llm_evaluate_cluster(lessons)

                if not evaluation:
                    total_skipped += 1
                    merge_details.append({
                        "action": "skipped",
                        "reason": "LLM evaluation failed",
                        "lessons": [l["lesson_id"] for l in lessons],
                    })
                    continue

                groups = evaluation.get("groups", [])
                keep_separate = evaluation.get("keep_separate", [])

                if not groups:
                    total_skipped += 1
                    merge_details.append({
                        "action": "skipped",
                        "reason": evaluation.get("reason", "LLM decided not to merge"),
                        "lessons": [l["lesson_id"] for l in lessons],
                    })
                    continue

                lessons_by_id = {l["lesson_id"]: l for l in lessons}

                for group in groups:
                    merge_ids = group.get("merge_ids", [])
                    if len(merge_ids) < 2:
                        continue

                    group_lessons = [lessons_by_id[mid] for mid in merge_ids if mid in lessons_by_id]
                    if len(group_lessons) < 2:
                        continue

                    if dry_run:
                        merge_details.append({
                            "action": "would_merge",
                            "primary": group.get("primary_id"),
                            "merged_title": group.get("title"),
                            "deleted": [mid for mid in merge_ids if mid != group.get("primary_id")],
                            "reason": group.get("reason", ""),
                        })
                        total_merged += len(group_lessons) - 1
                    else:
                        result = await self._execute_merge(
                            agent=agent,
                            lessons=group_lessons,
                            merge_decision=group,
                        )
                        merge_details.append(result)
                        total_merged += result.get("deleted_count", 0)

                if keep_separate:
                    merge_details.append({
                        "action": "kept_separate",
                        "lessons": keep_separate,
                        "reason": evaluation.get("reason", ""),
                    })

        await _progress("done", f"Consolidation complete: {total_merged} lessons merged, {total_skipped} clusters skipped", 100)

        return {
            "status": "completed",
            "agents_processed": len(agents_to_process),
            "clusters_found": total_clusters,
            "lessons_merged": total_merged,
            "clusters_skipped": total_skipped,
            "dry_run": dry_run,
            "details": merge_details,
        }

    async def _find_similar_clusters(
        self, agent_name: str, threshold: float
    ) -> List[List[str]]:
        """Find clusters of similar lessons for an agent using VectorStore."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT lesson_id, title, content FROM lessons "
                "WHERE agent_name = ? AND status != 'archived'",
                (agent_name,),
            ).fetchall()
            lessons = [dict(r) for r in rows]
        finally:
            conn.close()

        if len(lessons) < 2:
            return []

        # Build adjacency: for each lesson, find similar ones
        adjacency: Dict[str, set] = {l["lesson_id"]: set() for l in lessons}

        for lesson in lessons:
            try:
                query_text = f"{lesson['title']}. {lesson['content']}"
                results = await asyncio.to_thread(
                    self.vector_store.query,
                    collection=self._collection_name(agent_name),
                    query_text=query_text,
                    n_results=min(10, len(lessons)),
                    include=["metadatas", "distances"],
                )
                if results["ids"] and results["ids"][0]:
                    for i, rid in enumerate(results["ids"][0]):
                        if rid == lesson["lesson_id"]:
                            continue
                        if rid not in adjacency:
                            continue  # archived or unknown lesson in VectorStore
                        distance = results["distances"][0][i]
                        similarity = max(0.0, min(1.0, 1.0 - (distance / 2.0)))
                        if similarity >= threshold:
                            adjacency[lesson["lesson_id"]].add(rid)
                            adjacency[rid].add(lesson["lesson_id"])
            except Exception as e:
                logger.debug(f"Similarity query failed for {lesson['lesson_id']}: {e}")

        # Union-Find to build clusters
        parent: Dict[str, str] = {lid: lid for lid in adjacency}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for lid, neighbors in adjacency.items():
            for nid in neighbors:
                union(lid, nid)

        # Group by root
        groups: Dict[str, List[str]] = {}
        for lid in adjacency:
            root = find(lid)
            groups.setdefault(root, []).append(lid)

        # Only return clusters with 2+ lessons
        return [members for members in groups.values() if len(members) >= 2]

    async def _llm_evaluate_cluster(
        self, lessons: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Use LLM to evaluate a cluster of similar lessons and decide on merging."""
        try:
            from agent_system.llm.factory import create_llm_from_profile
            from agent_system.llm.models import ChatMessage as CM

            system_config = getattr(self, "_system_config", None)
            if not system_config:
                logger.warning("No system_config for LLM consolidation")
                return None

            llm = create_llm_from_profile(config=system_config, llm_profile=self.consolidation_llm_profile)

            # Build prompt with lesson details
            lessons_text = ""
            for l in lessons:
                lessons_text += (
                    f"\n---\n"
                    f"ID: {l['lesson_id']}\n"
                    f"Title: {l['title']}\n"
                    f"Content: {l['content']}\n"
                    f"Status: {l['status']} | Priority: {l['priority']} | "
                    f"Confidence: {l['confidence']:.2f} | Evidence: {l['evidence_count']} | "
                    f"Applications: {l['application_count']}\n"
                    f"Category: {l['category']} | Source: {l['source_type']}\n"
                )

            system_prompt = (
                "You are a lesson consolidation assistant. You evaluate groups of similar lessons "
                "and decide which ones should be merged.\n\n"
                "Rules:\n"
                "- Only merge if lessons truly cover the same insight or topic\n"
                "- Keep the best version: clearest title, most comprehensive content\n"
                "- Preserve important nuances from all lessons in the merged content\n"
                "- Pick the lesson with the highest evidence/confidence as the primary\n"
                "- If lessons are similar but cover DIFFERENT aspects, put them in separate groups or leave them alone\n"
                "- You can create multiple merge groups from one set of lessons\n\n"
                "- remove IDs or concreate details from the prompt, and focus on the core insight\n"
                "Output valid JSON with a list of merge groups:\n"
                "```json\n"
                '{\n'
                '  "groups": [\n'
                '    {\n'
                '      "merge_ids": ["id1", "id2"],\n'
                '      "primary_id": "id1",\n'
                '      "title": "Best merged title (max 200 chars)",\n'
                '      "content": "Best merged content (max 2000 chars)",\n'
                '      "category": "best category",\n'
                '      "priority": 5,\n'
                '      "reason": "Why these belong together"\n'
                '    }\n'
                '  ],\n'
                '  "keep_separate": ["id3"],\n'
                '  "reason": "Overall explanation"\n'
                '}\n'
                "```\n"
                "Rules for groups:\n"
                "- Each group must have at least 2 lesson IDs in merge_ids\n"
                "- A lesson ID must appear in exactly one group or in keep_separate\n"
                "- If nothing should be merged, return {\"groups\": [], \"keep_separate\": [...all ids...], \"reason\": \"...\"}\n"
                "Return ONLY the JSON."
            )

            user_prompt = f"Evaluate these {len(lessons)} similar lessons and decide if they should be merged:\n{lessons_text}"

            messages = [
                CM(role="system", content=system_prompt, timestamp=datetime.now(UTC)),
                CM(role="user", content=user_prompt, timestamp=datetime.now(UTC)),
            ]

            response = await llm.chat(messages=messages)

            # Parse response
            text = response.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:])
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            return json.loads(text)

        except Exception as e:
            logger.error(f"LLM cluster evaluation failed: {e}", exc_info=True)
            return None

    async def _execute_merge(
        self,
        agent: str,
        lessons: List[Dict[str, Any]],
        merge_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a merge: update primary lesson, archive duplicates, add evidence."""
        primary_id = merge_decision.get("primary_id", "")
        new_title = merge_decision.get("title", "")
        new_content = merge_decision.get("content", "")
        new_category = merge_decision.get("category")
        new_priority = merge_decision.get("priority")

        # Validate primary_id exists in cluster
        lesson_ids = [l["lesson_id"] for l in lessons]
        if primary_id not in lesson_ids:
            # Fallback: pick lesson with highest evidence_count
            primary_id = max(lessons, key=lambda l: (l["evidence_count"], l["confidence"]))["lesson_id"]

        # Calculate merged evidence count
        total_evidence = sum(l["evidence_count"] for l in lessons)
        total_applications = sum(l["application_count"] for l in lessons)

        # Best confidence from the group (keeps strongest signal)
        best_confidence = max(l["confidence"] for l in lessons)

        # Update primary lesson
        update_fields: Dict[str, Any] = {}
        if new_title:
            update_fields["title"] = new_title[:200]
        if new_content:
            update_fields["content"] = new_content[:2000]
        if new_category:
            update_fields["category"] = new_category
        if new_priority:
            update_fields["priority"] = min(10, max(1, int(new_priority)))

        if update_fields:
            await self.update_lesson(primary_id, **update_fields)

        # Update evidence count and confidence on primary
        conn = self._get_connection()
        try:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE lessons SET evidence_count = ?, application_count = ?, "
                "confidence = ?, updated_at = ? WHERE lesson_id = ?",
                (total_evidence, total_applications, best_confidence, now, primary_id),
            )

            # Delete duplicates and add merge evidence
            deleted_ids = []
            for l in lessons:
                if l["lesson_id"] == primary_id:
                    continue
                # Delete evidence/applications for the duplicate
                conn.execute("DELETE FROM lesson_evidence WHERE lesson_id = ?", (l["lesson_id"],))
                conn.execute("DELETE FROM lesson_applications WHERE lesson_id = ?", (l["lesson_id"],))
                # Delete the duplicate lesson
                conn.execute("DELETE FROM lessons WHERE lesson_id = ?", (l["lesson_id"],))
                # Add evidence record noting the merge on primary
                conn.execute(
                    """INSERT INTO lesson_evidence
                       (lesson_id, session_id, agent_name, evidence_type, description)
                       VALUES (?, ?, ?, ?, ?)""",
                    (primary_id, "consolidation", agent, "confirm",
                     f"Merged from {l['lesson_id']}: {l['title']}"),
                )
                deleted_ids.append(l["lesson_id"])

            conn.commit()
        finally:
            conn.close()

        # Remove deleted lessons from VectorStore
        if deleted_ids:
            try:
                await asyncio.to_thread(
                    self.vector_store.delete,
                    collection=self._collection_name(agent),
                    ids=deleted_ids,
                )
            except Exception as e:
                logger.debug(f"VectorStore delete after merge failed: {e}")

        # Re-index primary in VectorStore
        try:
            vector_text = f"{new_title or lessons[0]['title']}. {new_content or lessons[0]['content']}"
            await asyncio.to_thread(
                self.vector_store.add,
                collection=self._collection_name(agent),
                ids=[primary_id],
                documents=[vector_text],
                metadatas=[{"lesson_id": primary_id, "agent_name": agent}],
            )
        except Exception as e:
            logger.debug(f"VectorStore re-index after merge failed: {e}")

        return {
            "action": "merged",
            "primary_id": primary_id,
            "merged_title": new_title,
            "deleted": deleted_ids,
            "deleted_count": len(deleted_ids),
            "total_evidence": total_evidence,
        }

    async def check_duplicate(
        self, agent_name: str, title: str, content: str
    ) -> DeduplicationResult:
        """Check if a similar lesson already exists using VectorStore."""
        try:
            candidate_text = f"{title}. {content}"
            results = await asyncio.to_thread(
                self.vector_store.query,
                collection=self._collection_name(agent_name),
                query_text=candidate_text,
                n_results=3,
                include=["metadatas", "distances"],
            )
            if results["ids"] and len(results["ids"][0]) > 0:
                distance = results["distances"][0][0]
                similarity = max(0.0, min(1.0, 1.0 - (distance / 2.0)))
                if similarity >= self.dedup_similarity_threshold:
                    existing_id = results["metadatas"][0][0].get("lesson_id", results["ids"][0][0])
                    action = "confirm" if similarity >= self.exact_duplicate_threshold else "merge"
                    return DeduplicationResult(
                        is_duplicate=True,
                        existing_lesson_id=existing_id,
                        similarity=similarity,
                        action=action,
                    )
        except Exception as e:
            logger.debug(f"Dedup check failed (treating as new): {e}")

        return DeduplicationResult(is_duplicate=False, action="create")

    # ==========================================================================
    # MCP Tool Implementation (execute method dispatched by schema mixin)
    # ==========================================================================

    async def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Main tool dispatcher for 'lessons_learned' tool."""
        operation = params.get("operation", "list")
        status_reporter = params.get("_status")
        session_id = params.get("_session_id", "default")
        agent_name = params.get("_agent_name", "")

        try:
            if operation == "store":
                if status_reporter:
                    await status_reporter.progress("Storing lesson...")
                result = await self._op_store(params, agent_name, session_id)
            elif operation == "search":
                if status_reporter:
                    await status_reporter.progress("Searching lessons...")
                result = await self._op_search(params, agent_name)
            elif operation == "list":
                result = await self._op_list(params, agent_name)
            elif operation == "update":
                result = await self._op_update(params)
            elif operation == "confirm":
                result = await self._op_confirm(params, agent_name, session_id)
            elif operation == "teach":
                if status_reporter:
                    await status_reporter.progress("Teaching lesson to target agent...")
                result = await self._op_teach(params, agent_name, session_id)
            else:
                result = {"error": f"Unknown operation: {operation}"}

            if status_reporter:
                await status_reporter.end(result.get("status", "done"))
            return result
        except Exception as e:
            logger.error(f"Lesson operation '{operation}' failed: {e}", exc_info=True)
            if status_reporter:
                await status_reporter.error(str(e))
            return {"error": str(e)}

    async def _op_store(self, params: Dict[str, Any], agent_name: str, session_id: str) -> Dict[str, Any]:
        title = params.get("title", "")
        content = params.get("content", "")
        if not title or not content:
            return {"error": "Both 'title' and 'content' are required."}

        # Check for duplicates
        dedup = await self.check_duplicate(agent_name, title, content)
        if dedup.is_duplicate and dedup.action == "confirm":
            return await self.add_evidence(
                dedup.existing_lesson_id or "", session_id, agent_name,
                "confirm", f"Duplicate lesson submitted: {title}"
            )

        return await self.store_lesson(
            agent_name=agent_name,
            title=title,
            content=content,
            category=params.get("category", "general"),
            priority=params.get("priority", 5),
            tags=params.get("tags"),
            source_type="reflection",
            source_session=session_id,
            status="draft",
        )

    async def _op_search(self, params: Dict[str, Any], agent_name: str) -> Dict[str, Any]:
        query = params.get("query", "")
        if not query:
            return {"error": "'query' is required for search."}
        target = params.get("agent_name") or agent_name
        return await self.search_lessons(
            query=query,
            agent_name=target if target else None,
            category=params.get("category"),
            limit=params.get("limit", 10),
        )

    async def _op_list(self, params: Dict[str, Any], agent_name: str) -> Dict[str, Any]:
        target = params.get("agent_name") or agent_name
        return await self.list_lessons(
            agent_name=target if target else None,
            status=params.get("status"),
            category=params.get("category"),
            sort_by=params.get("sort_by", "priority"),
            limit=params.get("limit", 20),
        )

    async def _op_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        lesson_id = params.get("lesson_id", "")
        if not lesson_id:
            return {"error": "'lesson_id' is required."}
        updates = {k: v for k, v in params.items()
                   if k in ("title", "content", "category", "priority", "status", "tags", "confidence", "source_type")}
        return await self.update_lesson(lesson_id, **updates)

    async def _op_confirm(self, params: Dict[str, Any], agent_name: str, session_id: str) -> Dict[str, Any]:
        lesson_id = params.get("lesson_id", "")
        if not lesson_id:
            return {"error": "'lesson_id' is required."}
        return await self.add_evidence(
            lesson_id=lesson_id,
            session_id=session_id,
            agent_name=agent_name,
            evidence_type=params.get("evidence_type", "confirm"),
            description=params.get("description"),
        )

    async def _op_teach(self, params: Dict[str, Any], source_agent: str, session_id: str) -> Dict[str, Any]:
        target = params.get("target_agent", "")
        title = params.get("title", "")
        content = params.get("content", "")
        if not target or not title or not content:
            return {"error": "'target_agent', 'title', and 'content' are required."}

        # Check for duplicates in target agent
        dedup = await self.check_duplicate(target, title, content)
        if dedup.is_duplicate and dedup.action == "confirm":
            return await self.add_evidence(
                dedup.existing_lesson_id or "", session_id, source_agent,
                "confirm", f"Cross-agent confirmation from {source_agent}: {title}",
            )

        return await self.store_lesson(
            agent_name=target,
            title=title,
            content=content,
            category=params.get("category", "general"),
            priority=params.get("priority", 5),
            tags=params.get("tags"),
            source_type="cross_agent",
            source_agent=source_agent,
            source_session=session_id,
            status="draft",
        )

    # ==========================================================================
    # Hook Implementations
    # ==========================================================================

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Inject active lessons into system prompt."""
        try:
            agent_name = context.agent_name
            if not agent_name:
                return HookResult(success=True, modified=False)

            # Get hook config from agent overrides
            config = context.hook_config or {}
            max_lessons = config.get("max_lessons", 15)
            max_tokens = config.get("max_tokens", 1500)
            min_confidence = config.get("min_confidence", 0.3)
            include_categories = config.get("include_categories")
            exclude_categories = config.get("exclude_categories", [])

            lessons = await self.get_active_lessons_for_agent(
                agent_name=agent_name,
                min_confidence=min_confidence,
                max_lessons=max_lessons,
                categories=include_categories,
                exclude_categories=exclude_categories,
            )

            if not lessons:
                return HookResult(success=True, modified=False)

            # Build injection text
            injection = build_lesson_prompt(lessons, max_tokens=max_tokens)
            if not injection:
                return HookResult(success=True, modified=False)

            from agent_system.llm.models import ChatMessage
            if context.messages:
                # Remove old injection (identified by injected_by attribute)
                for i in range(len(context.messages) - 1, -1, -1):
                    if getattr(context.messages[i], 'injected_by', None) == "lessons_learned":
                        context.messages.pop(i)

                # Insert after system messages
                insert_pos = self._find_system_message_position(context.messages)
                context.messages.insert(insert_pos, ChatMessage(
                    role="system",
                    content=injection,
                    injected_by="lessons_learned",
                ))

            # Record applications
            session_id = context.session_id
            for lesson in lessons:
                try:
                    await self.record_application(lesson["lesson_id"], session_id, agent_name)
                except Exception:
                    pass

            logger.info(f"Injected {len(lessons)} lessons for agent '{agent_name}'")
            return HookResult(
                success=True, modified=True, context=context,
                metadata={"injected_lessons": len(lessons)},
            )
        except Exception as e:
            logger.error(f"inject_lessons hook failed: {e}", exc_info=True)
            return HookResult(success=False, modified=False, metadata={"error": str(e)})

    async def on_session_end(self, context: HookContext) -> HookResult:
        """Extract lessons from conversation at session end."""
        try:
            agent_name = context.agent_name
            session_id = context.session_id
            if not agent_name or not context.messages:
                return HookResult(success=True, modified=False)

            config = context.hook_config or {}
            min_turns = config.get("min_turns", 5)
            max_lessons = config.get("max_lessons_per_session", 5)
            auto_approve = config.get("auto_approve", False)
            extraction_enabled = config.get("enabled", True)

            if not extraction_enabled:
                return HookResult(success=True, modified=False)

            # Count turns (user + assistant messages)
            turn_count = sum(
                1 for m in context.messages
                if hasattr(m, 'role') and m.role in ('user', 'assistant')
            )
            if turn_count < min_turns:
                logger.debug(f"Skipping extraction: {turn_count} turns < {min_turns} min")
                return HookResult(success=True, modified=False)

            # Import and run extraction
            from .extraction import extract_lessons_from_conversation
            result = await extract_lessons_from_conversation(
                messages=context.messages,
                agent_name=agent_name,
                session_id=session_id,
                server=self,
                max_lessons=max_lessons,
                auto_approve=auto_approve,
                llm_profile=config.get("extraction_llm_profile", "turbo"),
                agent=context.agent,
            )

            logger.info(
                f"Extraction for '{agent_name}': created={result.created_count}, "
                f"merged={result.merged_count}, confirmed={result.confirmed_count}"
            )
            return HookResult(
                success=True, modified=False,
                metadata={
                    "extracted": result.created_count,
                    "merged": result.merged_count,
                    "confirmed": result.confirmed_count,
                },
            )
        except Exception as e:
            logger.error(f"extract_lessons hook failed: {e}", exc_info=True)
            return HookResult(success=False, modified=False, metadata={"error": str(e)})

    @staticmethod
    def _find_system_message_position(messages: list) -> int:
        """Find position after consecutive system messages at start."""
        position = 0
        for i, msg in enumerate(messages):
            role = msg.role if hasattr(msg, 'role') else msg.get('role')
            if role == 'system':
                position = i + 1
            else:
                break
        return position
