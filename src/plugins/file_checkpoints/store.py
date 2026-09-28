"""The record of one session: which turn changed which path, and how it looked before.

Laid out per user and root session, so the owner is part of the address:

    <root>/<user>/<session>/journal.db      turns, changes, untracked calls
    <root>/<user>/<session>/blobs/<sha256>  the bytes a change replaced

SQLite, because a write-heavy turn upserts one row per call and a JSON
document would be rewritten whole each time, and because the API and an
agent-cli process may hold the same session one after the other. Blobs are
named by their digest, so a file written back and forth is kept once, and are
written before the row that names them: a row never points at nothing.

Everything here is synchronous; the hooks run it in a worker thread.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from .fs import State
from .turns import TurnRef

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turns (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_key TEXT NOT NULL UNIQUE,
    ancestors TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_seq INTEGER NOT NULL REFERENCES turns(seq) ON DELETE CASCADE,
    path TEXT NOT NULL,
    server TEXT NOT NULL,
    tool TEXT NOT NULL,
    before_kind TEXT NOT NULL,
    before_digest TEXT,
    before_mode INTEGER,
    before_size INTEGER,
    before_kept INTEGER NOT NULL DEFAULT 0,
    before_note TEXT NOT NULL DEFAULT '',
    after_kind TEXT,
    after_digest TEXT,
    -- when "after" was last set, in the record's own order: of two spellings of
    -- one file recorded in one step, the one set last says what the file is
    after_seq INTEGER NOT NULL DEFAULT 0,
    -- set when a later call of the same turn found the path changed since this one's last call
    outside_note TEXT NOT NULL DEFAULT '',
    recorded_at REAL NOT NULL,
    UNIQUE (turn_seq, path)
);
CREATE TABLE IF NOT EXISTS untracked (
    turn_seq INTEGER NOT NULL REFERENCES turns(seq) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (turn_seq, tool)
);
"""


class ForeignJournal(PermissionError):
    """The record at this address was written for another user."""


def is_session_id(session_id: str) -> bool:
    """Whether the store can address ``session_id`` (SessionManager's rule)."""
    return bool(_SESSION_ID.match(session_id or ""))


def safe_user(user_id: str) -> str:
    """The user's directory name -- the rule SessionManager uses for its own."""
    return str(user_id).replace("..", "_").replace("/", "_").replace("\\", "_")


@dataclass(frozen=True)
class Turn:
    seq: int
    key: str
    ancestors: Tuple[str, ...]
    question: str


@dataclass(frozen=True)
class Change:
    id: int
    turn_seq: int
    path: str
    server: str
    tool: str
    before: State
    after: Optional[State]
    #: Why the path changed outside the agent within the turn, or "".
    outside: str = ""
    #: When ``after`` was last set (the record's own order).
    after_seq: int = 0


class Journal:
    """The record of one (user, root session)."""

    def __init__(self, root: Path, user_id: str, session_id: str):
        if not _SESSION_ID.match(session_id or ""):
            raise ValueError(f"Invalid session id: {session_id!r}")
        self.user_id = user_id
        self.session_id = session_id
        self.directory = Path(root) / safe_user(user_id) / session_id
        self.db_path = self.directory / "journal.db"
        self.blob_dir = self.directory / "blobs"

    @property
    def ident(self) -> Tuple[str, str]:
        return (self.user_id, self.session_id)

    def exists(self) -> bool:
        return self.db_path.is_file()

    @contextmanager
    def connect(self, create: bool = False) -> Iterator[sqlite3.Connection]:
        """A connection in one transaction: committed when the block ends, rolled
        back when it raises."""
        if create:
            self.blob_dir.mkdir(parents=True, exist_ok=True)
        elif not self.exists():
            raise FileNotFoundError(self.db_path)
        with closing(sqlite3.connect(self.db_path, timeout=30.0)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if create:
                conn.executescript(_SCHEMA)
                conn.execute("INSERT OR IGNORE INTO meta VALUES ('version', ?)", (SCHEMA_VERSION,))
                conn.execute("INSERT OR IGNORE INTO meta VALUES ('user_id', ?)", (self.user_id,))
                conn.execute("INSERT OR IGNORE INTO meta VALUES ('session_id', ?)", (self.session_id,))
            owner = conn.execute("SELECT value FROM meta WHERE key = 'user_id'").fetchone()
            if owner is None or owner[0] != self.user_id:
                raise ForeignJournal(f"the record of {self.session_id} is not {self.user_id}'s")
            with conn:
                yield conn

    # --- recording -----------------------------------------------------------

    @staticmethod
    def turn_seq(conn: sqlite3.Connection, turn: TurnRef, create: bool) -> Optional[int]:
        row = conn.execute("SELECT seq FROM turns WHERE turn_key = ?", (turn.key,)).fetchone()
        if row is not None:
            return int(row[0])
        if not create:
            return None
        cursor = conn.execute(
            "INSERT INTO turns (turn_key, ancestors, question, created_at) VALUES (?, ?, ?, ?)",
            (turn.key, json.dumps(list(turn.ancestors)), turn.question, time.time()))
        return int(cursor.lastrowid)

    @staticmethod
    def change_id(conn: sqlite3.Connection, turn_seq: int, path: str) -> Optional[int]:
        row = conn.execute("SELECT id FROM changes WHERE turn_seq = ? AND path = ?",
                           (turn_seq, path)).fetchone()
        return int(row[0]) if row is not None else None

    def keep_blob(self, digest: str, content: bytes) -> bool:
        """Store ``content`` under its digest; False when it was there already."""
        target = self.blob_dir / digest
        if target.exists():
            return False
        handle, temp = tempfile.mkstemp(dir=self.blob_dir, prefix=".", suffix=".blob")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(content)
            os.replace(temp, target)
        except BaseException:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise
        return True

    def read_blob(self, digest: str) -> bytes:
        return (self.blob_dir / digest).read_bytes()

    @staticmethod
    def add_change(conn: sqlite3.Connection, turn_seq: int, path: str, server: str, tool: str,
                   before: State) -> int:
        cursor = conn.execute(
            "INSERT INTO changes (turn_seq, path, server, tool, before_kind, before_digest, before_mode,"
            " before_size, before_kept, before_note, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (turn_seq, path, server, tool, before.kind, before.digest, before.mode, before.size,
             1 if before.kept else 0, before.note, time.time()))
        return int(cursor.lastrowid)

    @staticmethod
    def set_after(conn: sqlite3.Connection, change_id: int, after: State) -> None:
        conn.execute("UPDATE changes SET after_kind = ?, after_digest = ?,"
                     " after_seq = (SELECT COALESCE(MAX(after_seq), 0) + 1 FROM changes) WHERE id = ?",
                     (after.kind, after.digest, change_id))

    @staticmethod
    def after_of(conn: sqlite3.Connection, change_id: int) -> Optional[State]:
        row = conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        return _after(row) if row is not None else None

    @staticmethod
    def mark_outside(conn: sqlite3.Connection, change_id: int, note: str) -> None:
        conn.execute("UPDATE changes SET outside_note = ? WHERE id = ? AND outside_note = ''", (note, change_id))

    @staticmethod
    def before_of(conn: sqlite3.Connection, change_id: int) -> Optional[State]:
        row = conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        return _before(row) if row is not None else None

    @staticmethod
    def drop_change(conn: sqlite3.Connection, change_id: int) -> None:
        conn.execute("DELETE FROM changes WHERE id = ?", (change_id,))

    @staticmethod
    def note_untracked(conn: sqlite3.Connection, turn_seq: int, tool: str) -> None:
        conn.execute("INSERT INTO untracked (turn_seq, tool, count) VALUES (?, ?, 1)"
                     " ON CONFLICT (turn_seq, tool) DO UPDATE SET count = count + 1", (turn_seq, tool))

    # --- reading -------------------------------------------------------------

    @staticmethod
    def turns(conn: sqlite3.Connection) -> List[Turn]:
        return [Turn(seq=int(row["seq"]), key=row["turn_key"],
                     ancestors=tuple(json.loads(row["ancestors"] or "[]")), question=row["question"])
                for row in conn.execute("SELECT * FROM turns ORDER BY seq")]

    @staticmethod
    def changes(conn: sqlite3.Connection, turn_seqs: Sequence[int]) -> List[Change]:
        if not turn_seqs:
            return []
        marks = ",".join("?" * len(turn_seqs))
        rows = conn.execute(f"SELECT * FROM changes WHERE turn_seq IN ({marks}) ORDER BY id",
                            tuple(turn_seqs)).fetchall()
        return [Change(id=int(row["id"]), turn_seq=int(row["turn_seq"]), path=row["path"],
                       server=row["server"], tool=row["tool"], before=_before(row), after=_after(row),
                       outside=row["outside_note"] or "", after_seq=int(row["after_seq"] or 0))
                for row in rows]

    @staticmethod
    def untracked(conn: sqlite3.Connection, turn_seqs: Sequence[int]) -> Dict[int, Dict[str, int]]:
        if not turn_seqs:
            return {}
        marks = ",".join("?" * len(turn_seqs))
        found: Dict[int, Dict[str, int]] = {}
        for row in conn.execute(f"SELECT * FROM untracked WHERE turn_seq IN ({marks})", tuple(turn_seqs)):
            found.setdefault(int(row["turn_seq"]), {})[row["tool"]] = int(row["count"])
        return found

    # --- forgetting ----------------------------------------------------------

    @staticmethod
    def forget_turns(conn: sqlite3.Connection, turn_seqs: Sequence[int]) -> None:
        for seq in turn_seqs:
            conn.execute("DELETE FROM changes WHERE turn_seq = ?", (seq,))
            conn.execute("DELETE FROM untracked WHERE turn_seq = ?", (seq,))
            conn.execute("DELETE FROM turns WHERE seq = ?", (seq,))

    @staticmethod
    def drop_empty_turns(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM turns WHERE seq NOT IN (SELECT turn_seq FROM changes)"
                     " AND seq NOT IN (SELECT turn_seq FROM untracked)")

    def collect_blobs(self) -> int:
        """Delete the blobs no change names any more; the bytes freed."""
        with self.connect() as conn:
            named = {row[0] for row in conn.execute(
                "SELECT DISTINCT before_digest FROM changes WHERE before_kept = 1"
                " AND before_digest IS NOT NULL")}
        freed = 0
        try:
            entries = list(os.scandir(self.blob_dir))
        except FileNotFoundError:
            return 0
        for entry in entries:
            if entry.name.startswith(".") or entry.name in named or not entry.is_file():
                continue
            try:
                size = entry.stat().st_size
                os.unlink(entry.path)
                freed += size
            except FileNotFoundError:
                continue
        return freed

    @staticmethod
    def kept_bytes(conn: sqlite3.Connection) -> int:
        """The bytes the record needs: every blob a change still names, once."""
        row = conn.execute("SELECT COALESCE(SUM(before_size), 0) FROM (SELECT DISTINCT before_digest,"
                           " before_size FROM changes WHERE before_kept = 1)").fetchone()
        return int(row[0] or 0)

    @classmethod
    def prune(cls, conn: sqlite3.Connection, max_turns: int, max_bytes: int, keep_seq: Optional[int],
              needed: int = 0) -> List[int]:
        """Forget the oldest turns until at most ``max_turns`` are left and what
        the record keeps fits ``max_bytes`` with ``needed`` more on top (what the
        call about to be recorded keeps). ``keep_seq`` (the turn being recorded)
        is never forgotten. Returns the forgotten turns; their blobs go with the
        next ``collect_blobs``."""
        seqs = [int(row[0]) for row in conn.execute("SELECT seq FROM turns ORDER BY seq")]
        forgotten = [seq for seq in seqs if seq != keep_seq][:max(0, len(seqs) - max_turns)]
        cls.forget_turns(conn, forgotten)
        while cls.kept_bytes(conn) + needed > max_bytes:
            oldest = conn.execute("SELECT seq FROM turns WHERE seq != ? ORDER BY seq LIMIT 1",
                                  (keep_seq if keep_seq is not None else -1,)).fetchone()
            if oldest is None:
                break
            cls.forget_turns(conn, [int(oldest[0])])
            forgotten.append(int(oldest[0]))
        return forgotten

    def delete(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def _before(row: sqlite3.Row) -> State:
    return State(kind=row["before_kind"], digest=row["before_digest"], mode=row["before_mode"],
                 size=row["before_size"], kept=bool(row["before_kept"]), note=row["before_note"] or "")


def _after(row: sqlite3.Row) -> Optional[State]:
    if row["after_kind"] is None:
        return None
    return State(kind=row["after_kind"], digest=row["after_digest"])


def journals_older_than(root: Path, seconds: float) -> List[Path]:
    """Session directories under ``root`` whose record was last written longer
    than ``seconds`` ago."""
    cutoff = time.time() - seconds
    found: List[Path] = []
    try:
        users = list(os.scandir(root))
    except FileNotFoundError:
        return found
    for user in users:
        if not user.is_dir(follow_symlinks=False):
            continue
        for session in os.scandir(user.path):
            db = Path(session.path) / "journal.db"
            try:
                if session.is_dir(follow_symlinks=False) and db.stat().st_mtime < cutoff:
                    found.append(Path(session.path))
            except FileNotFoundError:
                continue
    return found


__all__ = ["Journal", "Turn", "Change", "ForeignJournal", "is_session_id", "safe_user",
           "journals_older_than"]
