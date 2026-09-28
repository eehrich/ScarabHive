"""Which session a stored response belongs to -- what ``previous_response_id`` continues.

One row per response the API stored, and per session the latest one: a
conversation continues from its latest response only. A session holds one line
of turns, so continuing from an older response (a branch) would silently build
on turns the client did not mean -- that is refused instead.

And per conversation the ``instructions`` it was last given: they are part of the
conversation once a turn with them is stored, so the same ones sent again are not
added a second time.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


class ResponseStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS responses (id TEXT PRIMARY KEY, user TEXT NOT NULL, "
                             "session_id TEXT NOT NULL, agent TEXT NOT NULL, created REAL NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS conversations (session_id TEXT PRIMARY KEY, "
                             "latest TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS instructions (session_id TEXT PRIMARY KEY, "
                             "text TEXT NOT NULL)")

    def find(self, response_id: str, user: str) -> Optional[dict[str, str]]:
        """The response's session and agent -- None for an unknown id or another user's response."""
        with self._lock:
            row = self._db.execute("SELECT session_id, agent, user FROM responses WHERE id = ?",
                                   (response_id,)).fetchone()
        if row is None or row[2] != user:
            return None
        return {"session_id": row[0], "agent": row[1]}

    def latest(self, session_id: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT latest FROM conversations WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] if row else None

    def instructions(self, session_id: str) -> Optional[str]:
        """The instructions the conversation was last given, or None."""
        with self._lock:
            row = self._db.execute("SELECT text FROM instructions WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] if row else None

    def add(self, response_id: str, user: str, session_id: str, agent: str,
            instructions: Optional[str] = None) -> None:
        """A response stored with its conversation -- now the latest; ``instructions`` if the turn had them."""
        with self._lock, self._db:
            self._db.execute("INSERT INTO responses (id, user, session_id, agent, created) VALUES (?, ?, ?, ?, ?)",
                             (response_id, user, session_id, agent, time.time()))
            self._db.execute("INSERT INTO conversations (session_id, latest) VALUES (?, ?) "
                             "ON CONFLICT(session_id) DO UPDATE SET latest = excluded.latest",
                             (session_id, response_id))
            if instructions is not None:
                self._db.execute("INSERT INTO instructions (session_id, text) VALUES (?, ?) "
                                 "ON CONFLICT(session_id) DO UPDATE SET text = excluded.text",
                                 (session_id, instructions))

    def close(self) -> None:
        with self._lock:
            self._db.close()
