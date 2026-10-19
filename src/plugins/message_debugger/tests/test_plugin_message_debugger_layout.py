"""The file's layout: payloads apart from their cost rows, compressed, and the move of a file from before.

A file from before llm_payloads held each payload in its cost row; the retention NULLed it there, and the row
kept its page (on the server: 590k rows in 389k pages, 1.3 GB empty inside them). The first start with this
code moves such a file over -- on the writer, so the plugin's start does not wait for it.
"""
import json
import sqlite3
import zlib

import pytest

from plugins.message_debugger.database import EVERYONE, MessageDebuggerDB, register_reader_functions

_OLD_SCHEMA = """
    CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ms REAL NOT NULL,
        snapshot_type TEXT NOT NULL, agent_name TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '', step INTEGER DEFAULT 0, message_count INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, context_window INTEGER, messages_json TEXT, llm_response_json TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')), user_id TEXT);
    CREATE TABLE llm_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ms REAL NOT NULL,
        direction TEXT NOT NULL, agent_name TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
        url TEXT DEFAULT '', is_streaming INTEGER DEFAULT 0, payload_json TEXT, response_json TEXT, error TEXT,
        duration_ms REAL, usage_json TEXT, finish_reason TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')), served_by TEXT, user_id TEXT);
    CREATE INDEX idx_llm_requests_request_id ON llm_requests(request_id);
    CREATE INDEX idx_llm_requests_stripable ON llm_requests(timestamp_ms)
        WHERE payload_json IS NOT NULL OR response_json IS NOT NULL;
"""
PAYLOAD = {"messages": [{"role": "user", "content": "Kapitel 3 im Zauberwald"}]}
RESPONSE = {"choices": [{"message": {"content": "gut"}}]}


@pytest.fixture
def old_file(tmp_path):
    path = tmp_path / "debugger.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute("INSERT INTO turns (timestamp_ms, snapshot_type, messages_json) VALUES (1, 'pre_llm', ?)",
                 (json.dumps([{"role": "user", "content": "alt"}]),))
    rows = [  # id, payload, response, user: a request with its payload, its response, a row stripped before
        (1, json.dumps(PAYLOAD), None, "alice"),
        (2, None, json.dumps(RESPONSE), "alice"),
        (3, None, None, None),
    ]
    for row_id, payload, response, user in rows:
        conn.execute("INSERT INTO llm_requests (id, timestamp_ms, direction, request_id, model, payload_json, "
                     "response_json, usage_json, user_id) VALUES (?, ?, 'request', 'r1', 'gpt-5', ?, ?, '{}', ?)",
                     (row_id, float(row_id), payload, response, user))
    conn.execute("UPDATE sqlite_sequence SET seq = 50 WHERE name = 'llm_requests'")  # rows 4-50 deleted since
    conn.commit()
    conn.close()
    return path


def _columns(db, table):
    return {row[1] for row in db._get_conn().execute(f"PRAGMA table_info({table})")}


def test_a_file_from_before_is_moved_over(old_file):
    db = MessageDebuggerDB(old_file, wal_mode=True, max_size_mb=0)
    try:
        assert db.flush(timeout=30)
        conn = db._get_conn()

        assert "payload_json" not in _columns(db, "llm_requests"), "llm_requests still holds the payloads"
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'llm_requests_new'").fetchone()
        assert db.count_llm_requests(owner=EVERYONE) == 3, "a cost row was lost"
        first, second, stripped = (db.get_llm_request(i, owner=EVERYONE) for i in (1, 2, 3))
        assert (first["payload_json"], first["response_json"]) == (PAYLOAD, None)
        assert (second["payload_json"], second["response_json"]) == (None, RESPONSE)
        assert (stripped["payload_json"], stripped["response_json"], stripped["usage_json"]) == (None, None, {})
        assert db.count_llm_requests(owner="alice") == 2, "the owners' index went with the old table"
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"idx_llm_requests_request_id", "idx_llm_requests_user"} <= indexes
        assert "idx_llm_requests_stripable" not in indexes
        assert db.get_turn(1, owner=EVERYONE)["messages_json"] == [{"role": "user", "content": "alt"}], \
            "a turn from before (plain text) is unreadable"
        assert "messages_json" not in _columns(db, "turns"), \
            "the compressed column kept its name: a LIKE on it finds only the old rows, silently"
        assert db.insert_llm_request(9, "request") > 50, "an id a deleted row had came back"

        db._migrate()  # another process, first meanwhile: finds it done
        assert db.count_llm_requests(owner=EVERYONE) == 4
    finally:
        db.close()

    again = MessageDebuggerDB(old_file, wal_mode=True, max_size_mb=0)
    try:
        assert again._needs_migration is False
    finally:
        again.close()


def test_a_move_that_failed_costs_no_cost_row(old_file, monkeypatch):
    """A move that fails (a full disk) leaves the old payloads in their cost rows. The retention strips them
    there, as before, and takes no cost row while any content is left; a request opened meanwhile still
    shows its payload."""
    import plugins.message_debugger.database as database

    with monkeypatch.context() as broken:
        broken.setattr(database, "_LLM_REQUEST_COLUMNS", "broken INTEGER")  # the copy fails inside the move
        db = MessageDebuggerDB(old_file, wal_mode=True, max_size_mb=1)
        assert db.flush(timeout=30)
    try:
        assert db._needs_migration and "payload_json" in _columns(db, "llm_requests"), "fixture: the move went"
        assert db.get_llm_request(1, owner=EVERYONE)["payload_json"] == PAYLOAD, "an old payload is hidden"
        for i in range(3):
            db.insert_llm_request(100 + i, "request", payload={"n": i})
            db.insert_turn(100 + i, "pre_llm", messages=[{"role": "user", "content": str(i)}])
        statements = []
        db._get_conn().set_trace_callback(statements.append)
        db._retention_batch = 1
        db._max_size_bytes = db._high_bytes = db._low_bytes = 1  # over the cap, whatever is left

        db.enforce_retention(force=True, budget=30)

        kinds = [("strip" if s.startswith("UPDATE llm_requests SET payload_json = NULL") else
                  "content" if s.startswith(("DELETE FROM llm_payloads", "DELETE FROM turns")) else
                  "cost" if s.startswith("DELETE FROM llm_requests") else None) for s in statements]
        kinds = [kind for kind in kinds if kind]
        assert "strip" in kinds, "the old payloads were never stripped: only their cost rows could free them"
        assert "cost" in kinds, "fixture: the cap never reached the cost rows"
        assert "strip" not in kinds[kinds.index("cost"):] and "content" not in kinds[kinds.index("cost"):], \
            f"a cost row went while content was left: {kinds}"
    finally:
        db.close()


def test_a_start_waits_for_another_process_moving_the_file_over(old_file, monkeypatch):
    """The API and the writer worker start together on one file. The first one's move holds the write lock for
    minutes; the second one's start must wait for it, not give up after the usual timeout and lose its
    debugger."""
    import threading

    import plugins.message_debugger.database as database

    monkeypatch.setattr(database, "BUSY_TIMEOUT_S", 0.2)
    holder = sqlite3.connect(old_file, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # the other process, moving it over
    release = threading.Timer(1.5, lambda: holder.execute("COMMIT"))
    release.start()
    try:
        db = MessageDebuggerDB(old_file, wal_mode=False, max_size_mb=0)
        db.close()
    finally:
        release.join()
        holder.close()


def test_a_process_whose_move_failed_sees_another_one_succeed(old_file):
    """Its own move failed (or gave up waiting); another process's went through after it. Thinking the file
    still old, it would ask for payloads in a column that is gone -- every retention of it failing."""
    import plugins.message_debugger.database as database

    real = database._LLM_REQUEST_COLUMNS
    database._LLM_REQUEST_COLUMNS = "broken INTEGER"  # the copy fails inside its move
    try:
        first = MessageDebuggerDB(old_file, wal_mode=True, max_size_mb=1)
        assert first.flush(timeout=30)
    finally:
        database._LLM_REQUEST_COLUMNS = real
    try:
        assert first._needs_migration, "fixture: the first move went"
        second = MessageDebuggerDB(old_file, wal_mode=True, max_size_mb=0)
        assert second.flush(timeout=30) and not second._needs_migration
        second.close()

        first._max_size_bytes = first._high_bytes = first._low_bytes = 1
        done = first.enforce_retention(force=True, budget=5)  # raised "no such column" before
        assert done["payloads_deleted"] == 2 and first._needs_migration is False

        first._needs_migration = True  # its reader, too, finds out by itself
        row_id = first.insert_llm_request(9, "request", payload=PAYLOAD)
        assert first.get_llm_request(row_id, owner=EVERYONE)["payload_json"] == PAYLOAD
        assert first._needs_migration is False
    finally:
        first.close()


def test_a_process_that_finds_the_move_done_knows_it(tmp_path):
    """Its start saw the old layout, another process moved the file over before its writer got to it."""
    db = MessageDebuggerDB(tmp_path / "new.db", wal_mode=False, max_size_mb=0)
    try:
        db._needs_migration = True  # as its start saw it
        db._migrate()
        assert db._needs_migration is False
    finally:
        db.close()


def test_payloads_and_messages_are_stored_compressed(tmp_path):
    db = MessageDebuggerDB(tmp_path / "new.db", wal_mode=True, max_size_mb=0)
    try:
        long_payload = {"messages": [{"role": "user", "content": "Der Wald rauscht. " * 400}]}
        row_id = db.insert_llm_request(1, "request", payload=long_payload)
        db.insert_turn(1, "pre_llm", messages=long_payload["messages"])
        conn = db._get_conn()

        (stored,) = conn.execute("SELECT payload_z FROM llm_payloads WHERE id = ?", (row_id,)).fetchone()
        assert isinstance(stored, bytes) and len(stored) < len(json.dumps(long_payload)) / 5
        assert json.loads(zlib.decompress(stored)) == long_payload
        (messages,) = conn.execute("SELECT messages_z FROM turns").fetchone()
        assert isinstance(messages, bytes), "turn messages not compressed"
        assert db.get_llm_request(row_id, owner=EVERYONE)["payload_json"] == long_payload
        assert db.get_turn(1, owner=EVERYONE)["messages_json"] == long_payload["messages"]
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] > 0, "the WAL keeps any size it grew to"
    finally:
        db.close()


def test_a_reader_outside_matches_compressed_and_plain_payloads(old_file):
    db = MessageDebuggerDB(old_file, wal_mode=True, max_size_mb=0)
    try:
        assert db.flush(timeout=30)
        db.insert_llm_request(5, "request", request_id="r2", payload={"text": "neu im Zauberwald"})
    finally:
        db.close()

    reader = sqlite3.connect(old_file)
    register_reader_functions(reader)
    found = reader.execute("SELECT r.id FROM llm_requests r JOIN llm_payloads p ON p.id = r.id "
                           "WHERE debugger_text(p.payload_z) LIKE '%Zauberwald%' ORDER BY r.id").fetchall()
    reader.close()
    assert [row[0] for row in found][0] == 1 and len(found) == 2, found
