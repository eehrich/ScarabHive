"""A user reads the captures of their own calls, an admin everyone's.

The captures are the raw LLM traffic -- system prompts, tool calls, answers.
Each row carries the user whose call it was; the database refuses a read that
does not say whose rows it wants. A row nobody owns (a call no run named a user
for, everything from before the column) is an admin's, and so is clearing and
pruning, which act on everyone's rows.
"""
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.message_debugger.database import EVERYONE, Account, MessageDebuggerDB
from tests.session_owners import admin, user, viewed_by

BASE = "/plugins/message_debugger"
READS = ["/turns", "/llm-requests", "/stats"]
OWNERS = ["alice", "bob", None, "anonymous"]  # anonymous: runs whose owner nobody knew were recorded under it


@pytest.fixture
def served(tmp_path):
    from plugins.message_debugger.plugin import MessageDebuggerHybridPlugin
    from plugins.message_debugger.web_endpoints import MessageDebuggerWebFactory

    server_config = ToolServerConfig()
    server_config.config = {"db_path": str(tmp_path / "plugin.db")}
    plugin = MessageDebuggerHybridPlugin("message_debugger", AgentSystemConfig(), server_config)
    db = MessageDebuggerDB(db_path=str(tmp_path / "debug.db"))
    ids = {}
    for owner in OWNERS:
        name = owner or "nobody"
        ids[name] = {
            "turns": db.insert_turn(time.time() * 1000, "pre_llm", agent_name="chat", request_id=f"r-{name}",
                                    session_id=f"s-{name}", message_count=1, user_id=owner),
            "llm-requests": db.insert_llm_request(time.time() * 1000, "request", agent_name="chat",
                                                  request_id=f"r-{name}", session_id=f"s-{name}",
                                                  provider="openai", model="m", payload={"messages": []},
                                                  user_id=owner),
        }
    app = FastAPI()
    app.include_router(MessageDebuggerWebFactory(db=db, name="message_debugger", server=plugin).get_web_router())
    return TestClient(app), viewed_by(app), db, ids


def member(name):
    """A user whose account is older than every row here."""
    return user(name).model_copy(update={"created_at": datetime(2020, 1, 1)})


def _rows(answer):
    return answer.get("turns", answer.get("requests"))


def test_a_user_reads_only_their_own(served):
    client, viewer, _, _ = served
    viewer["user"] = member("alice")

    for path in ("/turns", "/llm-requests"):
        answer = client.get(f"{BASE}{path}").json()
        assert [row["request_id"] for row in _rows(answer)] == ["r-alice"], path
        assert answer["total"] == 1 and {row["user_id"] for row in _rows(answer)} == {"alice"}
    stats = client.get(f"{BASE}/stats").json()
    assert (stats["total_turns"], stats["total_llm_requests"], stats["unique_session_count"]) == (1, 1, 1)
    assert stats["sees_everything"] is False
    assert "db_size_mb" not in stats, "the file's size is everyone's rows"


@pytest.mark.parametrize("filters", [{"session_id": "s-bob"}, {"request_id": "r-bob"}, {"request_id": "r"},
                                     {"agent_name": "chat", "session_id": "s-nobody"}])
def test_no_filter_reaches_past_the_owner(served, filters):
    client, viewer, _, _ = served
    viewer["user"] = member("alice")

    for path in ("/turns", "/llm-requests"):
        answer = client.get(f"{BASE}{path}", params=filters).json()
        assert answer["total"] == 0 and _rows(answer) == [], (path, filters)


@pytest.mark.parametrize("owner", ["bob", "nobody", "anonymous"])
def test_another_ones_entry_answers_as_one_that_does_not_exist(served, owner):
    client, viewer, _, ids = served
    viewer["user"] = member("alice")

    for path in ("/turns", "/llm-requests"):
        foreign_id = ids[owner][path.strip('/')]
        foreign, missing = client.get(f"{BASE}{path}/{foreign_id}"), client.get(f"{BASE}{path}/999999")
        assert (foreign.status_code, foreign.json()["detail"]) == \
            (404, missing.json()["detail"].replace("999999", str(foreign_id))), path
        assert client.get(f"{BASE}{path}/{ids['alice'][path.strip('/')]}").status_code == 200


@pytest.mark.parametrize("path", READS)
def test_naming_another_user_is_an_admins(served, path):
    client, viewer, _, _ = served
    viewer["user"] = member("alice")

    assert client.get(f"{BASE}{path}", params={"user_id": "bob"}).status_code == 403
    assert client.get(f"{BASE}{path}", params={"user_id": "alice"}).status_code == 200


@pytest.mark.parametrize("method, path", [("delete", "/clear"), ("post", "/prune?vacuum=false")])
def test_clearing_and_pruning_stay_an_admins(served, method, path):
    client, viewer, db, _ = served
    viewer["user"] = member("alice")

    assert getattr(client, method)(f"{BASE}{path}").status_code == 403
    assert db.count_turns(owner=EVERYONE) == len(OWNERS), "a user's request changed the captured data"


@pytest.mark.parametrize("who", [None, "anonymous", "cli_user"])
@pytest.mark.parametrize("path", READS)
def test_an_identity_without_an_account_reads_nothing(served, who, path):
    """Nobody signed in reads as "anonymous" -- the name runs whose owner
    nobody knew were recorded under. No account can carry these names."""
    client, viewer, _, _ = served
    viewer["user"] = user(who) if who else None

    assert client.get(f"{BASE}{path}").status_code == 403


def test_an_account_under_a_deleted_users_name_reads_nothing_of_theirs(served):
    """Names come free again when an account is deleted, its captures stay. A new
    account under the name reads what it captured itself, from its making on."""
    client, viewer, db, ids = served
    viewer["user"] = user("alice")  # made just now, after the rows of the alice before

    assert client.get(f"{BASE}/turns").json()["total"] == 0
    assert client.get(f"{BASE}/stats").json()["total_llm_requests"] == 0
    assert client.get(f"{BASE}/turns/{ids['alice']['turns']}").status_code == 404
    db.insert_turn(time.time() * 1000 + 1000, "pre_llm", agent_name="chat", request_id="r-new", user_id="alice")
    assert [row["request_id"] for row in client.get(f"{BASE}/turns").json()["turns"]] == ["r-new"]


def test_an_admin_reads_everyones_and_may_name_one(served):
    client, viewer, _, ids = served
    viewer["user"] = admin()

    assert client.get(f"{BASE}/turns").json()["total"] == len(OWNERS)
    assert [row["request_id"] for row in _rows(client.get(f"{BASE}/llm-requests",
                                                          params={"user_id": "bob"}).json())] == ["r-bob"]
    assert client.get(f"{BASE}/turns/{ids['nobody']['turns']}").status_code == 200
    stats = client.get(f"{BASE}/stats").json()
    assert stats["sees_everything"] is True and stats["total_turns"] == len(OWNERS) and "db_size_mb" in stats
    assert client.get(f"{BASE}/stats", params={"user_id": "bob"}).json()["total_llm_requests"] == 1
    assert client.post(f"{BASE}/prune?vacuum=false").status_code == 200
    assert client.delete(f"{BASE}/clear").status_code == 200


# ---- the database: a read says whose rows it wants ------------------------

@pytest.fixture
def db(tmp_path):
    return MessageDebuggerDB(db_path=str(tmp_path / "owners.db"))


READERS = {
    "get_turns": lambda db, **owner: db.get_turns(**owner),
    "count_turns": lambda db, **owner: db.count_turns(**owner),
    "get_turn": lambda db, **owner: db.get_turn(1, **owner),
    "get_llm_requests": lambda db, **owner: db.get_llm_requests(**owner),
    "count_llm_requests": lambda db, **owner: db.count_llm_requests(**owner),
    "get_llm_request": lambda db, **owner: db.get_llm_request(1, **owner),
    "newest_id": lambda db, **owner: db.newest_id("turns", **owner),
    "get_stats": lambda db, **owner: db.get_stats(**owner),
}


@pytest.mark.parametrize("read", READERS)
def test_a_read_that_does_not_say_whose_rows_it_wants_is_refused(db, read):
    with pytest.raises(TypeError):
        READERS[read](db)
    for nobody in (None, ""):
        with pytest.raises(ValueError):
            READERS[read](db, owner=nobody)
    READERS[read](db, owner="alice")
    READERS[read](db, owner=EVERYONE)


def test_a_users_reads_go_through_their_index_only(db):
    """user_id sits behind the payloads: a plan that reads it from each row a
    different index found walks every payload on the way (184 KB on average
    in the live file). The user's index must be the one that serves.

    Measured on a file with statistics where the planner has a choice: one
    user runs everything (a single-user instance), each session, request and
    agent is rare, errors are few. Without the +column marks it takes the
    session, agent, request or error index, a DISTINCT walks the agent or
    provider index, and the session count scans the table."""
    now = time.time() * 1000
    for i in range(400):
        db.insert_turn(now + i, "pre_llm", agent_name=f"agent{i % 40}", request_id=f"r{i}", session_id=f"s{i}",
                       user_id="alice")
        db.insert_llm_request(now + i, "request", agent_name=f"agent{i % 40}", request_id=f"r{i}",
                              session_id=f"s{i}", provider=f"p{i % 20}", error="boom" if i % 80 == 0 else None,
                              user_id="alice")
    conn = db._get_conn()
    conn.execute("ANALYZE")
    account = Account("alice", now - 1)  # what a signed-in user reads with
    statements = []
    conn.set_trace_callback(statements.append)
    filters = dict(agent_name="agent3", session_id="s10", request_id="r10", max_id=10**6)
    db.get_turns(owner=account, snapshot_type="pre_llm", **filters)
    db.count_turns(owner=account, snapshot_type="pre_llm", **filters)
    db.get_llm_requests(owner=account, direction="request", provider="openai", **filters)
    db.count_llm_requests(owner=account, direction="request", provider="openai", **filters)
    db.newest_id("turns", owner=account)
    db.newest_id("llm_requests", owner=account)
    db.get_stats(owner=account)
    conn.set_trace_callback(None)

    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 14, selects  # 4 lists and counts, 2 newest ids, 8 statistics
    for sql in selects:
        table = "llm_requests" if " FROM llm_requests" in sql else "turns"
        plan = [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()]
        served = [line for line in plan if f"INDEX idx_{table}_user " in line]
        assert served, (sql, plan)
        assert not any("INDEX idx_" in line and f"idx_{table}_user " not in line for line in plan), (sql, plan)
        # A page reads its fifty rows; a count or a statistic reads no row at all.
        if not sql.startswith("SELECT id,"):
            assert all("COVERING INDEX" in line for line in served), (sql, plan)


def test_the_hooks_record_whose_call_it_was(db):
    import plugins.message_debugger as package
    from plugins.message_debugger.hooks import MessageDebuggerPlugin

    plugin = MessageDebuggerPlugin(Path(package.__file__).parent, db=db)

    async def capture(user_id):
        messages = [ChatMessage(role="user", content="hi")]
        for hook_type, method in ((HookType.PRE_LLM_CALL, plugin.debugger_capture_pre_llm),
                                  (HookType.PRE_LLM_REQUEST, plugin.debugger_capture_pre_request),
                                  (HookType.POST_LLM_RESPONSE, plugin.debugger_capture_post_response)):
            await method(HookContext(hook_type=hook_type, request_id="r", session_id="s", messages=messages,
                                     llm_provider="p", llm_model="m", user_id=user_id))

    import asyncio
    asyncio.run(capture("alice"))
    asyncio.run(capture(None))
    assert db.flush()

    assert [row["user_id"] for row in db.get_turns(owner=EVERYONE)] == [None, "alice"]
    assert sorted(str(row["user_id"]) for row in db.get_llm_requests(owner=EVERYONE)) == \
        ["None", "None", "alice", "alice"]
    assert db.count_turns(owner="alice") == 1 and db.count_llm_requests(owner="alice") == 2


def test_an_older_database_gets_the_column_and_its_rows_stay_an_admins(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ms REAL NOT NULL,
            snapshot_type TEXT NOT NULL, agent_name TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '', step INTEGER DEFAULT 0, message_count INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0, context_window INTEGER, messages_json TEXT, llm_response_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE llm_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ms REAL NOT NULL,
            direction TEXT NOT NULL, agent_name TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            url TEXT DEFAULT '', is_streaming INTEGER DEFAULT 0, payload_json TEXT, response_json TEXT, error TEXT,
            duration_ms REAL, usage_json TEXT, finish_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')), served_by TEXT);
        INSERT INTO turns (timestamp_ms, snapshot_type) VALUES (1, 'pre_llm');
        INSERT INTO llm_requests (timestamp_ms, direction) VALUES (1, 'request');
    """)
    conn.commit()
    conn.close()

    db = MessageDebuggerDB(db_path=str(path))

    conn = db._get_conn()
    for table in ("turns", "llm_requests"):
        assert "user_id" in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (f"idx_{table}_user",)).fetchone()
    assert (db.count_turns(owner=EVERYONE), db.count_llm_requests(owner=EVERYONE)) == (1, 1)
    assert (db.count_turns(owner="alice"), db.count_llm_requests(owner="alice")) == (0, 0)


def test_a_column_another_process_added_meanwhile_costs_nothing(tmp_path):
    """Two processes start on a file from before the column: both look, both add it.
    The second one's "duplicate column" must not cost it the debugger."""
    db = MessageDebuggerDB(db_path=str(tmp_path / "race.db"))
    conn = db._get_conn()

    class Stale:
        """A connection that still sees the table as it was when it looked."""

        def execute(self, sql, *args):
            return iter([]) if sql.startswith("PRAGMA table_info") else conn.execute(sql, *args)

    MessageDebuggerDB._add_column(Stale(), "turns", "user_id")  # there already: its ALTER fails
    with pytest.raises(sqlite3.OperationalError):  # any other failure still is one
        MessageDebuggerDB._add_column(Stale(), "no_such_table", "user_id")
