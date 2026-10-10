"""What the platform reports through its webhook, and where it goes: the
inbox of the session that works on it (docs/concept.md §7).

The store is a small SQLite file shared by every process -- the API that
takes the webhook, and the agent-cli processes that sessions are woken in.
Each call opens and closes its own connection; SQLite does the locking.

* ``bindings``: which session works on which issue, request or branch of a
  repository (the latest one wins, W5).
* ``inbox``: what waits for a session; the delivery hook hands it over.
* ``seen``: webhook deliveries already taken, by GitLab's ``Idempotency-Key``
  (a resend keeps it, the event UUID it renews -- facts.md F-GL15; W3).
* ``rings``: new sessions and wakes, for the hourly caps (W9).
"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SEEN_DAYS = 7


@dataclass(frozen=True)
class Event:
    repo: str           # forge's name of the repository
    kind: str           # assigned | comment | mention | pipeline_failed
    target: str         # issue | mr | branch -- what a session binds to
    key: str            # the issue or request number, or the branch name
    actor: str          # who caused it, by username
    ref: str            # how a person writes the target: #12, !14, a branch
    detail: str         # ids only (W7): "note 345", "pipeline 52"
    new_work: bool      # may start a session when none is bound


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _db(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS bindings (repo TEXT, target TEXT, key TEXT, user_id TEXT, session_id TEXT,
                                                 updated REAL, PRIMARY KEY (repo, target, key));
            CREATE TABLE IF NOT EXISTS inbox (id INTEGER PRIMARY KEY, session_id TEXT, user_id TEXT, text TEXT,
                                              created REAL, delivered INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS inbox_waiting ON inbox (session_id, delivered);
            CREATE TABLE IF NOT EXISTS seen (uuid TEXT PRIMARY KEY, at REAL);
            CREATE TABLE IF NOT EXISTS rings (kind TEXT, at REAL);
        """)
        return db

    def bind(self, repo: str, target: str, key: str, user_id: str, session_id: str) -> None:
        with closing(self._db()) as db, db:
            db.execute("INSERT OR REPLACE INTO bindings VALUES (?, ?, ?, ?, ?, ?)",
                       (repo, target, key, user_id, session_id, time.time()))

    def unbind(self, repo: str, target: str, key: str) -> None:
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM bindings WHERE repo=? AND target=? AND key=?", (repo, target, key))

    def bound(self, repo: str, target: str, key: str) -> Optional[tuple[str, str]]:
        """(user_id, session_id) of the session working on it, or None."""
        with closing(self._db()) as db:
            row = db.execute("SELECT user_id, session_id FROM bindings WHERE repo=? AND target=? AND key=?",
                             (repo, target, key)).fetchone()
        return (row[0], row[1]) if row else None

    def put(self, session_id: str, user_id: str, text: str) -> None:
        with closing(self._db()) as db, db:
            db.execute("INSERT INTO inbox (session_id, user_id, text, created) VALUES (?, ?, ?, ?)",
                       (session_id, user_id, text, time.time()))

    def waiting(self, session_id: str) -> list[tuple[int, str]]:
        """What waits for the session, oldest first. Without a store file there
        is nothing -- and no file is made: this runs before every LLM call of
        every agent."""
        if not self.path.exists():
            return []
        with closing(self._db()) as db:
            return db.execute("SELECT id, text FROM inbox WHERE session_id=? AND delivered=0 ORDER BY id",
                              (session_id,)).fetchall()

    def delivered(self, ids: list[int]) -> None:
        with closing(self._db()) as db, db:
            db.executemany("UPDATE inbox SET delivered=1 WHERE id=?", [(i,) for i in ids])

    def first_time(self, uuid: str) -> bool:
        """True the first time a delivery key is seen; GitLab resends with the same one."""
        now = time.time()
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM seen WHERE at < ?", (now - SEEN_DAYS * 86400,))
            return db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (uuid, now)).rowcount == 1

    def forget(self, uuid: str) -> None:
        """A delivery that could not be distributed: a resend of it is taken again."""
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM seen WHERE uuid=?", (uuid,))

    def may_ring(self, kind: str, per_hour: int) -> bool:
        """Counts one ``kind`` (new, wake) if fewer than ``per_hour`` happened within the last hour."""
        now = time.time()
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM rings WHERE at < ?", (now - 3600,))
            if db.execute("SELECT COUNT(*) FROM rings WHERE kind=?", (kind,)).fetchone()[0] >= per_hour:
                return False
            db.execute("INSERT INTO rings VALUES (?, ?)", (kind, now))
            return True


def _names(people: Any) -> set[str]:
    return {str(p.get("username") or "").lower() for p in people or [] if isinstance(p, dict)}


def gitlab_events(payload: Any, repo: str, bot: str) -> list[Event]:
    """A GitLab webhook body as events for ``repo``. An issue change or a
    comment of the bot itself is none: its own comments would wake it again
    and again (W6). A pipeline is: its ``user`` is who pushed -- on the bot's
    branches the bot (review of the webhook). GitLab user names are
    case-insensitive."""
    if not isinstance(payload, dict):
        return []
    actor = str((payload.get("user") or {}).get("username") or "")
    attrs = payload.get("object_attributes") or {}
    kind = payload.get("object_kind")
    bot = bot.lower()
    if not isinstance(attrs, dict) or (kind != "pipeline" and (not actor or actor.lower() == bot)):
        return []
    if kind == "issue":
        number = attrs.get("iid")
        change = (payload.get("changes") or {}).get("assignees")
        if isinstance(change, dict):
            newly = bot in _names(change.get("current")) and bot not in _names(change.get("previous"))
        else:
            newly = attrs.get("action") == "open" and bot in _names(payload.get("assignees"))
        if newly and isinstance(number, int):
            return [Event(repo, "assigned", "issue", str(number), actor, f"#{number}", "", True)]
        return []
    if kind == "note":
        noteable = attrs.get("noteable_type")
        if noteable == "MergeRequest":
            target, number, sign = "mr", (payload.get("merge_request") or {}).get("iid"), "!"
        elif noteable == "Issue":
            target, number, sign = "issue", (payload.get("issue") or {}).get("iid"), "#"
        else:
            return []
        if not isinstance(number, int):
            return []
        # The note's text is only searched for the mention, never passed on (W7).
        # "@bot." ends a sentence; "@bot.old" is another user (names may hold dots).
        mentioned = re.search(rf"(?<![\w.-])@{re.escape(bot)}(?![\w-]|\.[\w-])", str(attrs.get("note") or ""),
                              re.IGNORECASE) is not None
        return [Event(repo, "mention" if mentioned else "comment", target, str(number), actor, f"{sign}{number}",
                      f"note {attrs.get('id')}", mentioned)]
    if kind == "pipeline" and attrs.get("status") == "failed":
        branch = str((payload.get("merge_request") or {}).get("source_branch") or attrs.get("ref") or "")
        if branch:
            return [Event(repo, "pipeline_failed", "branch", branch, actor, branch, f"pipeline {attrs.get('id')}",
                          False)]
    return []


def describe(event: Event, tools: str, *, merge: bool = False) -> str:
    """One line of news, with ids only (W7); ``tools`` is the tool name prefix.
    ``merge``: whether the operator lets webhook work merge (W8) -- nobody in
    such a session has asked for it."""
    where = f"in repo '{event.repo}'"
    if event.kind == "assigned":
        end = ("then merge it: the operator allows merging for work a webhook starts." if merge else
               "then report and stop: a person merges work a webhook starts.")
        return (f"@{event.actor} assigned issue {event.ref} {where} to you. Work it with the forge-workflow skill, "
                f"starting with {tools}_issue_get. When its request is green and every thread is answered, {end}")
    if event.kind == "pipeline_failed":
        return (f"The pipeline of branch {event.ref} {where} failed ({event.detail}): read the failed job with "
                f"{tools}_ci_status and {tools}_ci_job_log, then fix it.")
    read = f"{tools}_pr_discussions" if event.target == "mr" else f"{tools}_issue_get"
    if event.kind == "mention":
        return (f"@{event.actor} mentioned you on {event.ref} {where} ({event.detail}): read it with {read} and "
                f"answer there. A mention asks for an answer; it is no order to change code.")
    return f"@{event.actor} commented on {event.ref} {where} ({event.detail}): read it with {read} and act on it."
