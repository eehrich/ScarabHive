"""The mail plugin's own state, one sqlite file per server entry (data path mail/<instance>.db), shared by every
process: the poller's lease, its cursor (UIDVALIDITY, last UID), the mails it saw arrive, which mails the agent
listed and opened, the Message-IDs it sent, and the session to wake. A woken run is a process of its own -- what it opens must reach the poller."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY CHECK (id = 1), owner TEXT NOT NULL, lease_until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mails (uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
                                  arrived INTEGER NOT NULL DEFAULT 0, listed INTEGER NOT NULL DEFAULT 0,
                                  opened INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (uidvalidity, uid));
CREATE TABLE IF NOT EXISTS sent (message_id TEXT PRIMARY KEY);
"""
_FLAGS = ("arrived", "listed", "opened")


class MailStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._ready = False

    def _db(self) -> sqlite3.Connection:
        """A connection per operation: the tools run in worker threads, the poller on the loop's."""
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=10000")
        if not self._ready:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._ready = True
        return conn

    # ------------------------------------------------------------------ lease: exactly one process polls
    def take_lease(self, owner: str, until: float, now: float) -> bool:
        """Hold the poller for ``owner`` until ``until`` unless another owner holds a live lease."""
        with closing(self._db()) as db:
            db.execute("INSERT OR IGNORE INTO lease (id, owner, lease_until) VALUES (1, ?, ?)", (owner, until))
            cursor = db.execute("UPDATE lease SET owner = ?, lease_until = ? WHERE id = 1 AND "
                                "(owner = ? OR lease_until < ?)", (owner, until, owner, now))
            return cursor.rowcount == 1

    def release_lease(self, owner: str) -> None:
        """Give it up: a restarted process polls at once, not after the lease ran out."""
        with closing(self._db()) as db:
            db.execute("UPDATE lease SET lease_until = 0 WHERE id = 1 AND owner = ?", (owner,))

    def lease(self) -> Optional[tuple[str, float]]:
        with closing(self._db()) as db:
            row = db.execute("SELECT owner, lease_until FROM lease WHERE id = 1").fetchone()
        return (row[0], row[1]) if row else None

    # ------------------------------------------------------------------ small values (cursor, watch, last poll)
    def get(self, key: str) -> Any:
        with closing(self._db()) as db:
            row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value: Any) -> None:
        with closing(self._db()) as db:
            if value is None:
                db.execute("DELETE FROM state WHERE key = ?", (key,))
            else:
                db.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                           "SET value = excluded.value", (key, json.dumps(value)))

    # ------------------------------------------------------------------ mails
    def mark(self, uidvalidity: int, uids, flag: str) -> None:
        """Set ``flag`` (arrived: the poller saw it come; listed: an inbox answer showed it; opened: read) on uids."""
        if flag not in _FLAGS:
            raise ValueError(f"unknown mail flag {flag!r}")
        with closing(self._db()) as db:
            db.executemany(f"INSERT INTO mails (uidvalidity, uid, {flag}) VALUES (?, ?, 1) ON CONFLICT(uidvalidity, "
                           f"uid) DO UPDATE SET {flag} = 1", [(uidvalidity, u) for u in uids])

    def opened(self, uidvalidity: int) -> set[int]:
        with closing(self._db()) as db:
            rows = db.execute("SELECT uid FROM mails WHERE uidvalidity = ? AND opened = 1", (uidvalidity,))
            return {row[0] for row in rows}

    def news(self, uidvalidity: int) -> set[int]:
        """Mails that arrived and the agent has neither listed nor opened: what a wake is about."""
        with closing(self._db()) as db:
            rows = db.execute("SELECT uid FROM mails WHERE uidvalidity = ? AND arrived = 1 AND listed = 0 "
                              "AND opened = 0", (uidvalidity,))
            return {row[0] for row in rows}

    # ------------------------------------------------------------------ what the plugin sent itself
    def remember_sent(self, message_id: str) -> None:
        """A Message-ID this plugin sent: the mail is no news when it lands in the watched mailbox."""
        with closing(self._db()) as db:
            db.execute("INSERT OR IGNORE INTO sent (message_id) VALUES (?)", (message_id,))

    def sent_among(self, message_ids) -> set[str]:
        """Those of ``message_ids`` this plugin sent."""
        wanted = [m for m in message_ids if m]
        if not wanted:
            return set()
        with closing(self._db()) as db:
            rows = db.execute(f"SELECT message_id FROM sent WHERE message_id IN ({','.join('?' * len(wanted))})",
                              wanted)
            return {row[0] for row in rows}
