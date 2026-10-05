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

DIRECT_GROUP = "Direct messages"


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

    @staticmethod
    def _migrate_group_id_column(conn: sqlite3.Connection) -> None:
        """Add group_id column to channels table if not present (migration)."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)").fetchall()}
        if "group_id" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL")
            conn.commit()
            logger.info("Migrated channels table: added 'group_id' column")

    @staticmethod
    def _migrate_direct_columns(conn: sqlite3.Connection) -> None:
        """Direct messages: a recipient and a delivery mark per message, a pair key per channel."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "to_session" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN to_session TEXT")
        if "delivered_at" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN delivered_at TEXT")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)").fetchall()}
        if "direct_key" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN direct_key TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_to_session "
                     "ON messages(to_session, delivered_at)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_direct_key "
                     "ON channels(direct_key)")
        conn.commit()

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
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
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
            CREATE INDEX IF NOT EXISTS idx_channels_group
                ON channels(group_id);
        """)
        conn.commit()
        # Migrations
        self._migrate_pinned_column(conn)
        self._migrate_group_id_column(conn)
        self._migrate_direct_columns(conn)

    # ── Channel CRUD ──────────────────────────────────────────

    # ── Group CRUD ────────────────────────────────────────────

    def create_group(
        self,
        name: str,
        description: str = "",
    ) -> dict[str, Any]:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO groups (name, description) VALUES (?, ?)",
            (name, description),
        )
        conn.commit()
        group_id = cur.lastrowid
        return {"group_id": group_id, "name": name, "status": "created"}

    def get_group(self, group_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    def list_groups(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT g.*, COUNT(c.id) AS channel_count "
            "FROM groups g LEFT JOIN channels c ON c.group_id = g.id "
            "GROUP BY g.id ORDER BY g.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_group(self, group_id: int) -> bool:
        """Delete a group; channels in it become ungrouped (NULL group_id)."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        conn.commit()
        return cur.rowcount > 0

    # ── Channel CRUD ──────────────────────────────────────────

    def delete_channel(self, channel_id: int) -> bool:
        """Permanently delete a channel and all its messages."""
        conn = self._get_conn()
        conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
        cur = conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        conn.commit()
        return cur.rowcount > 0

    def create_channel(
        self,
        name: str,
        topic: str,
        context: str = "",
        metadata: dict[str, Any] | None = None,
        group_id: int | None = None,
    ) -> dict[str, Any]:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO channels (group_id, name, topic, context, metadata_json) VALUES (?, ?, ?, ?, ?)",
            (group_id, name, topic, context, json.dumps(metadata) if metadata else None),
        )
        conn.commit()
        return {"channel_id": cur.lastrowid, "name": name, "status": "active", "group_id": group_id}

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
        group_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = self._channel_filter(status, search, group_id)
        rows = self._get_conn().execute(
            f"SELECT * FROM channels WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?", [*params, limit, offset]
        ).fetchall()
        return [self._row_to_channel(r) for r in rows]

    def count_channels(self, status: str | None = None, group_id: int | None = None, search: str | None = None) -> int:
        where, params = self._channel_filter(status, search, group_id)
        return self._get_conn().execute(f"SELECT COUNT(*) FROM channels WHERE {where}", params).fetchone()[0]

    @staticmethod
    def _channel_filter(status: str | None, search: str | None, group_id: int | None) -> tuple[str, list[Any]]:
        where, params = ["1=1"], []
        if status:
            where.append("status = ?")
            params.append(status)
        if search:
            where.append("(name LIKE ? OR topic LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if group_id is not None:
            where.append("group_id = ?")
            params.append(group_id)
        return " AND ".join(where), params

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

    def rename_channel(self, channel_id: int, new_name: str) -> bool:
        """Update the channel name in place. Channel-id, messages, pins,
        verdict and history stay intact — only the display name changes.
        Returns True if a row was updated.
        """
        if not new_name or not new_name.strip():
            return False
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE channels SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (new_name.strip(), channel_id),
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

    def append_message(self, message_id: int, content: str) -> dict[str, Any] | None:
        """Append a chunk to an existing message's content (server-side concat).

        Lets a large output be posted across several bounded calls yet stay ONE
        complete, machine-readable message row — avoids the coordinator LLM
        having to re-emit a huge blob in a single tool call (which truncates).

        Returns {message_id, channel_id, length} on success, or None if the
        message does not exist.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT channel_id FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if not row:
            return None
        channel_id = row[0]
        conn.execute(
            "UPDATE messages SET content = content || ? WHERE id = ?",
            (content, message_id),
        )
        conn.execute(
            "UPDATE channels SET updated_at = datetime('now') WHERE id = ?",
            (channel_id,),
        )
        conn.commit()
        length = conn.execute(
            "SELECT LENGTH(content) FROM messages WHERE id = ?", (message_id,)
        ).fetchone()[0]
        return {"message_id": message_id, "channel_id": channel_id, "length": length}

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return self._row_to_message(row) if row else None

    def get_messages(
        self,
        channel_id: int,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        sql = "SELECT * FROM messages WHERE channel_id = ? ORDER BY id"
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
            f"FORUM DEBATE: {channel['name']}",
            f"TOPIC: {channel['topic']}",
        ]
        if channel.get("context"):
            parts.append(f"\nCONTEXT:\n{channel['context']}")
        parts.append("\n" + "═" * 60 + "\n")

        for msg in messages:
            role_upper = msg["agent_role"].upper()
            parts.append(
                f'[{role_upper} "{msg["agent_name"]}" | Round {msg["round"]}]\n'
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

    # ── Direct messages between sessions ──────────────────────
    # A direct message is a post in the pair's channel, grouped under
    # DIRECT_GROUP so the web panel shows it, with its recipient in to_session;
    # agent_role carries the sender's session id.

    def post_direct(self, from_session: str, from_agent: str, to_session: str,
                    to_agent: str, content: str) -> dict[str, Any]:
        key = "|".join(sorted((from_session, to_session)))
        conn = self._get_conn()
        row = conn.execute("SELECT id FROM channels WHERE direct_key = ?", (key,)).fetchone()
        if row is None:
            group = conn.execute(
                "SELECT id FROM groups WHERE name = ? ORDER BY id LIMIT 1", (DIRECT_GROUP,)
            ).fetchone()
            group_id = group[0] if group else conn.execute(
                "INSERT INTO groups (name) VALUES (?)", (DIRECT_GROUP,)).lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO channels (group_id, name, topic, direct_key) "
                "VALUES (?, ?, 'Direct messages', ?)",
                (group_id, " ↔ ".join(f"{agent} {sid[:8]}".strip() for agent, sid in
                                      ((from_agent, from_session), (to_agent, to_session))), key),
            )
            row = conn.execute("SELECT id FROM channels WHERE direct_key = ?", (key,)).fetchone()
        cur = conn.execute(
            "INSERT INTO messages (channel_id, agent_name, agent_role, content, to_session) "
            "VALUES (?, ?, ?, ?, ?)",
            (row[0], from_agent, from_session, content, to_session),
        )
        conn.execute("UPDATE channels SET updated_at = datetime('now') WHERE id = ?", (row[0],))
        conn.commit()
        return {"message_id": cur.lastrowid, "channel_id": row[0]}

    def undelivered(self, session_id: str) -> list[dict[str, Any]]:
        """Direct messages the session has not been told about."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM messages WHERE to_session = ? AND delivered_at IS NULL ORDER BY id",
            (session_id,),
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def mark_delivered(self, message_ids: list[int]) -> None:
        """Delivered, once the request that carried them is over: marking them
        as they are read would lose them if that run never finished."""
        if not message_ids:
            return
        conn = self._get_conn()
        conn.executemany(
            "UPDATE messages SET delivered_at = datetime('now') WHERE id = ?",
            [(mid,) for mid in message_ids],
        )
        conn.commit()

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
