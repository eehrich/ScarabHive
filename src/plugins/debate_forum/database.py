"""Debate Forum Plugin - SQLite database storage.

Persistent storage for debate channels and messages.
Uses SQLite with WAL mode for concurrent read/write access.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DebateForumDB:
    """SQLite-backed storage for debate forum data.

    Tables:
    - channels: Debate channels (one per debate topic)
    - messages: Individual agent messages within channels
    """

    def __init__(self, db_path: str | Path, wal_mode: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._wal_mode = wal_mode
        self._local = threading.local()
        self._init_schema()
        logger.info("DebateForumDB initialized: %s", self.db_path)

    @staticmethod
    def _migrate_pinned_column(conn: sqlite3.Connection) -> None:
        """Add pinned column to messages table if not present (migration)."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "pinned" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            conn.commit()
            logger.info("Migrated messages table: added 'pinned' column")

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False, timeout=10.0
            )
            self._local.conn.row_factory = sqlite3.Row
            if self._wal_mode:
                self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT '',
                context TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                verdict_json TEXT,
                verdict_summary TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                agent_name TEXT NOT NULL,
                agent_role TEXT NOT NULL DEFAULT '',
                round INTEGER NOT NULL DEFAULT 1,
                content TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_channel
                ON messages(channel_id, round);
            CREATE INDEX IF NOT EXISTS idx_channels_status
                ON channels(status);
        """)
        conn.commit()
        # Migrate: add pinned column if missing
        self._migrate_pinned_column(conn)

    # ── Channel CRUD ──────────────────────────────────────────

    def create_channel(
        self,
        name: str,
        topic: str,
        context: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO channels (name, topic, context, metadata_json) VALUES (?, ?, ?, ?)",
            (name, topic, context, json.dumps(metadata) if metadata else None),
        )
        conn.commit()
        return {"channel_id": cur.lastrowid, "name": name, "status": "active"}

    def get_channel(self, channel_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not row:
            return None
        return self._row_to_channel(row)

    def list_channels(
        self,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        sql = "SELECT * FROM channels WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if search:
            sql += " AND (name LIKE ? OR topic LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_channel(r) for r in rows]

    def count_channels(self, status: str | None = None) -> int:
        conn = self._get_conn()
        if status:
            row = conn.execute("SELECT COUNT(*) FROM channels WHERE status = ?", (status,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM channels").fetchone()
        return row[0]

    def conclude_channel(
        self, channel_id: int, verdict: dict[str, Any], summary: str = ""
    ) -> bool:
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE channels SET status = 'concluded', verdict_json = ?, "
            "verdict_summary = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(verdict), summary, channel_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def reopen_channel(self, channel_id: int) -> bool:
        """Reopen a concluded or archived channel back to active status."""
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE channels SET status = 'active', updated_at = datetime('now') "
            "WHERE id = ? AND status IN ('concluded', 'archived')",
            (channel_id,),
        )
        conn.commit()
        return cur.rowcount > 0

    def archive_channel(self, channel_id: int) -> bool:
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE channels SET status = 'archived', updated_at = datetime('now') WHERE id = ?",
            (channel_id,),
        )
        conn.commit()
        return cur.rowcount > 0

    # ── Messages ──────────────────────────────────────────────

    def post_message(
        self,
        channel_id: int,
        agent_name: str,
        agent_role: str,
        round_num: int,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO messages (channel_id, agent_name, agent_role, round, content, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, agent_name, agent_role, round_num, content,
             json.dumps(metadata) if metadata else None),
        )
        # Touch channel updated_at
        conn.execute(
            "UPDATE channels SET updated_at = datetime('now') WHERE id = ?",
            (channel_id,),
        )
        conn.commit()
        return {"message_id": cur.lastrowid, "channel_id": channel_id}

    def get_messages(
        self,
        channel_id: int,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        sql = "SELECT * FROM messages WHERE channel_id = ? ORDER BY round, id"
        params: list[Any] = [channel_id]
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_message(r) for r in rows]

    def get_message_count(self, channel_id: int) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row[0]

    def format_thread(self, channel_id: int, max_messages: int = 0) -> str:
        """Format channel thread as human-readable text for LLM context."""
        channel = self.get_channel(channel_id)
        if not channel:
            return ""

        messages = self.get_messages(channel_id, limit=max_messages)

        parts = [
            f"FORUM-DEBATTE: {channel['name']}",
            f"TOPIC: {channel['topic']}",
        ]
        if channel.get("context"):
            parts.append(f"\nKONTEXT:\n{channel['context']}")
        parts.append("\n" + "═" * 60 + "\n")

        for msg in messages:
            role_upper = msg["agent_role"].upper()
            parts.append(
                f'[{role_upper} "{msg["agent_name"]}" | Runde {msg["round"]}]\n'
                f'{msg["content"]}\n'
            )

        if channel.get("verdict_summary"):
            parts.append("─" * 40)
            parts.append(f"VERDICT: {channel['verdict_summary']}")

        return "\n".join(parts)

    # ── Pinning ────────────────────────────────────────────────

    def pin_message(self, message_id: int) -> bool:
        """Pin a message so it's always included in context injection."""
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE messages SET pinned = 1 WHERE id = ?", (message_id,)
        )
        conn.commit()
        return cur.rowcount > 0

    def unpin_message(self, message_id: int) -> bool:
        """Unpin a previously pinned message."""
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE messages SET pinned = 0 WHERE id = ?", (message_id,)
        )
        conn.commit()
        return cur.rowcount > 0

    def get_pinned_messages(self, channel_id: int) -> list[dict[str, Any]]:
        """Get all pinned messages for a channel, ordered by round and id."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND pinned = 1 ORDER BY round, id",
            (channel_id,),
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    # ── Stats ─────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        conn = self._get_conn()
        channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM channels WHERE status='active'").fetchone()[0]
        concluded = conn.execute("SELECT COUNT(*) FROM channels WHERE status='concluded'").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM channels WHERE status='archived'").fetchone()[0]
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return {
            "total_channels": channels,
            "active": active,
            "concluded": concluded,
            "archived": archived,
            "total_messages": messages,
        }

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _row_to_channel(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for field in ("verdict_json", "metadata_json"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        if d.get("metadata_json"):
            try:
                d["metadata_json"] = json.loads(d["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d
