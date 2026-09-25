"""`/api/sessions/resolve` — the browser finds a session by the name its person gave it.

And `/api/sessions/listing`, the browser's `/sessions [count|all]`: the terminal's
listing functions behind a route, so both chats list the same sessions.

A session id is machine-made (`2332j2kj22k`) and cannot be renamed: it is the
key the usage tracker, the message debugger, the context stores, the
sub-session indexes and the presence locks all file their rows under. So the
title is the name, and both chat surfaces take it — the terminal calls
SessionManager.resolve_session_ref directly, the browser through this route.

The route has to be declared BEFORE `/{session_id}`, or "resolve" is read as
an id and the answer is a 404 about a session nobody asked for.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace as NS

import httpx
import pytest

from agent_system.auth.security import create_access_token
from agent_system.services.session_manager import SessionManager

pytestmark = pytest.mark.anyio

#: The session routes ride on the auth router (app.py:1110), so the test signs
#: itself a token for a REAL account, the way tests/app/test_app_chat_commands.py
#: does -- a name the user store does not know is rejected before any route runs.
#: Read-only: the sessions themselves live in tmp_path.
DEV_SECRET = "published-signing-key-replace-with-your-own-0000000000"


@pytest.fixture(scope="module")
def account():
    """The admin row, read-only -- a plain connect CREATES the file when it is
    missing, and a collection-time error there takes the whole run down."""
    try:
        with sqlite3.connect("file:data/users.db?mode=ro", uri=True) as db:
            row = db.execute("select id, username, role from users "
                             "where username='admin'").fetchone()
    except sqlite3.Error as e:
        pytest.skip(f"no user store to sign a token against: {e}")
    if not row:
        pytest.skip("no admin account to sign a token against")
    return row


@pytest.fixture
def headers(account):
    return {"Authorization": "Bearer " + create_access_token(
        {"sub": account[1], "user_id": account[0], "role": account[2]},
        secret_key=DEV_SECRET, algorithm="HS256")}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api(tmp_path, monkeypatch):
    from agent_system import app as app_mod

    # Auth stays ON: the session router is only mounted when it is (app.py:1110).
    # Anonymous is allowed through -- these routes take get_optional_user.
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    manager = SessionManager(storage_path=str(tmp_path))
    app.state.session_manager = manager
    return app, manager


async def _titled(manager, user, title, session_id):
    session = await manager.create_session(user_id=user, session_id=session_id,
                                           title=title, agent_name="a", llm_profile="p")
    await manager.save_session(session)


async def _record(manager, user, session_id, updated_at, *, agent="a", title=None):
    """A record with the stamp it is given -- save_session stamps "now", and
    "newest" would then be decided by the wall clock."""
    await manager.reinstate_session({
        "session_id": session_id, "user_id": user, "title": title or session_id,
        "created_at": updated_at, "updated_at": updated_at,
        "agent_name": agent, "llm_profile": "p", "messages": [], "metadata": {}})


def _runtime(**visibility):
    """What app.state.runtime answers: each agent's declared visibility."""
    return NS(describe=lambda name: NS(visibility=visibility[name]) if name in visibility else None)


async def _get(app, headers, url):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        return await client.get(url, headers=headers, timeout=30.0)


async def test_a_title_answers_with_the_id_it_belongs_to(api, account, headers):
    app, manager = api
    await _titled(manager, account[1], "FPGA Quartus", "2332j2kj22k")

    response = await _get(app, headers, "/api/sessions/resolve?ref=fpga%20quartus")

    assert response.status_code == 200, response.text
    assert response.json()["session_id"] == "2332j2kj22k"


async def test_an_id_is_handed_back_as_it_came(api, account, headers):
    app, manager = api
    await _titled(manager, account[1], "FPGA Quartus", "2332j2kj22k")

    response = await _get(app, headers, "/api/sessions/resolve?ref=2332j2kj22k")

    assert response.json()["session_id"] == "2332j2kj22k"


async def test_a_name_nobody_gave_is_a_404_naming_it(api, headers):
    app, _ = api

    response = await _get(app, headers, "/api/sessions/resolve?ref=Amiga")

    assert response.status_code == 404, response.text
    assert "Amiga" in response.json()["detail"]


async def test_a_title_several_sessions_share_names_the_others(api, account, headers):
    app, manager = api
    # the newer one written FIRST: the stamp decides, not the order of writing
    await _record(manager, account[1], "new1", "2026-09-24T10:00:00+00:00", title="FPGA Quartus")
    await _record(manager, account[1], "old1", "2026-09-01T10:00:00+00:00", title="FPGA Quartus")

    response = await _get(app, headers, "/api/sessions/resolve?ref=FPGA%20Quartus")

    assert response.json() == {"session_id": "new1", "others": ["old1"]}


async def _chats_and_runs(manager, user):
    for day, (sid, agent) in enumerate([("chat1", "coder"), ("run1", "v4_scorer"),
                                        ("tool1", "sam_tool"), ("run2", "v4_scorer"),
                                        ("chat2", "coder")], start=10):
        await _record(manager, user, sid, f"2026-09-{day}T10:00:00+00:00", agent=agent)


async def test_the_listing_leaves_the_pipeline_runs_out_and_counts_them(api, account, headers):
    app, manager = api
    app.state.runtime = _runtime(coder="ui", v4_scorer="private", sam_tool="tool")
    await _chats_and_runs(manager, account[1])

    body = (await _get(app, headers, "/api/sessions/listing")).json()

    assert [s["session_id"] for s in body["sessions"]] == ["chat2", "chat1"], body
    assert body["total"] == 2 and body["left_out"] == 3
    assert body["most_left_out"] == {"agent": "v4_scorer", "count": 2}


async def test_this_chats_agent_and_session_stay_whatever_their_agent(api, account, headers):
    app, manager = api
    app.state.runtime = _runtime(coder="ui", v4_scorer="private", sam_tool="tool")
    await _chats_and_runs(manager, account[1])

    body = (await _get(app, headers,
                       "/api/sessions/listing?agent=sam_tool&current=run1")).json()

    assert [s["session_id"] for s in body["sessions"]] == ["chat2", "tool1", "run1", "chat1"]
    assert body["left_out"] == 1


async def test_all_lists_every_one_and_a_count_cuts(api, account, headers):
    app, manager = api
    app.state.runtime = _runtime(coder="ui", v4_scorer="private", sam_tool="tool")
    await _chats_and_runs(manager, account[1])

    everything = (await _get(app, headers, "/api/sessions/listing?count=ALL")).json()
    one = (await _get(app, headers, "/api/sessions/listing?count=1")).json()

    assert len(everything["sessions"]) == 5 and everything["left_out"] == 0
    assert everything["most_left_out"] is None
    assert [s["session_id"] for s in one["sessions"]] == ["chat2"] and one["total"] == 2


async def test_a_count_that_is_not_one_is_refused(api, headers):
    app, _ = api
    # "-1" for "the last one" is a common reflex; read as 0 it would list them all
    for typed in ("2o", "-1"):
        response = await _get(app, headers, f"/api/sessions/listing?count={typed}")
        assert response.status_code == 400, (typed, response.text)
        assert typed in response.json()["detail"]
