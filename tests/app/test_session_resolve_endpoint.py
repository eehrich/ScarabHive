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


async def _patch(app, headers, url, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        return await client.patch(url, json=body, headers=headers, timeout=30.0)


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


def _running(monkeypatch, sessions):
    """What the job manager reports running: session id -> owner and agent."""
    from agent_system.services.background_job_manager import BackgroundJobManager

    async def active_sessions(self):
        return sessions

    monkeypatch.setattr(BackgroundJobManager, "active_sessions", active_sessions)


async def test_a_title_for_a_session_its_first_run_has_not_written_waits_for_that_run(
        api, account, headers, monkeypatch):
    """/title during the first run: no record to rename yet -- the run writes it
    with its first save, as agent-cli writes it once the turn is saved."""
    app, _ = api
    agent = app.state.agent
    _running(monkeypatch, {"s-new": {"user_id": account[1], "agent_name": agent.name}})

    response = await _patch(app, headers, "/api/sessions/s-new", {"title": "Blitter umbauen"})

    assert response.status_code == 200, response.text
    assert agent._session_tracker.title_to_write("s-new") == "Blitter umbauen"


async def test_another_users_running_session_is_not_named(api, account, headers, monkeypatch):
    app, _ = api
    agent = app.state.agent
    _running(monkeypatch, {"s-bob": {"user_id": "bob", "agent_name": agent.name}})

    response = await _patch(app, headers, "/api/sessions/s-bob", {"title": "meins"})

    assert response.status_code == 404, response.text
    assert agent._session_tracker.title_to_write("s-bob") is None


async def test_a_run_started_without_an_agent_name_is_found_too(api, account, headers, monkeypatch):
    """/events without an agent name puts "default" on its job: the default agent's run."""
    app, _ = api
    agent = app.state.agent
    _running(monkeypatch, {"s-new": {"user_id": account[1], "agent_name": "default", "request_id": "r-new"}})

    response = await _patch(app, headers, "/api/sessions/s-new", {"title": "Blitter umbauen"})

    assert response.status_code == 200, response.text
    assert agent._session_tracker.title_to_write("s-new") == "Blitter umbauen"


async def test_a_record_written_before_the_first_save_is_renamed_for_the_run_too(
        api, account, headers, monkeypatch):
    """A sub-agent's parent record can be there before the run saves: renamed,
    and the title the run carries goes with it -- or that save puts it back."""
    app, manager = api
    agent = app.state.agent
    await _titled(manager, account[1], "Coordinator Session", "s-new")
    agent._session_tracker.carry_title("s-new", "Blitter umbauen")
    _running(monkeypatch, {"s-new": {"user_id": account[1], "agent_name": agent.name}})

    response = await _patch(app, headers, "/api/sessions/s-new", {"title": "Copper-Liste"})

    assert response.status_code == 200, response.text
    assert (await manager.load_session(account[1], "s-new"))["title"] == "Copper-Liste"
    assert agent._session_tracker.title_to_write("s-new") == "Copper-Liste"


async def test_a_rename_hands_a_run_no_title_it_did_not_carry(api, account, headers, monkeypatch):
    """Renaming a session its run goes on in: the record has it, the run's save
    keeps it -- a title handed to the run would outlive a later rename."""
    app, manager = api
    agent = app.state.agent
    await _titled(manager, account[1], "alt", "s-old")
    _running(monkeypatch, {"s-old": {"user_id": account[1], "agent_name": agent.name}})

    response = await _patch(app, headers, "/api/sessions/s-old", {"title": "neu"})

    assert response.status_code == 200, response.text
    assert agent._session_tracker.title_to_write("s-old") is None


async def test_a_run_on_another_agent_is_found_on_that_agent(api, account, headers, monkeypatch):
    """The browser names its agent: the title goes to that agent's run, not the default one's."""
    from agent_system.servers.agent.server import Agent

    app, _ = api
    default = app.state.agent
    registry = app.state.tool_registry
    other = next((agent for agent in map(registry.get, registry.list())
                  if isinstance(agent, Agent) and agent is not default), None)
    assert other is not None, "fixture: no second agent in the registry"
    _running(monkeypatch, {"s-new": {"user_id": account[1], "agent_name": other.name, "request_id": "r-x"}})

    response = await _patch(app, headers, "/api/sessions/s-new", {"title": "Blitter umbauen"})

    assert response.status_code == 200, response.text
    assert other._session_tracker.title_to_write("s-new") == "Blitter umbauen"
    assert default._session_tracker.title_to_write("s-new") is None


async def test_a_save_finishing_right_after_the_rename_writes_the_new_title(
        api, account, headers, monkeypatch):
    """The run's save can finish between the PATCH's rename and anything after
    it: the title that run carries is replaced before the rename, or that save
    writes the old one over it and lets go of it."""
    from agent_system.llm.models import ChatMessage
    from agent_system.services.session_service import SessionService

    app, manager = api
    agent = app.state.agent
    await _titled(manager, account[1], "Coordinator Session", "s-new")
    agent._session_tracker.carry_title("s-new", "Blitter umbauen")
    agent._session_tracker.set_session_messages("s-new", [ChatMessage(role="user", content="hallo")])
    _running(monkeypatch, {"s-new": {"user_id": account[1], "agent_name": agent.name}})
    rename, service = manager.rename_session, SessionService(manager)

    async def rename_then_the_run_saves(user_id, session_id, title):
        await rename(user_id, session_id, title)
        assert await service.save_session(agent, account[1], "s-new", agent.name, "p", False), \
            "fixture: the run's save wrote nothing"

    monkeypatch.setattr(manager, "rename_session", rename_then_the_run_saves)
    response = await _patch(app, headers, "/api/sessions/s-new", {"title": "Copper-Liste"})

    assert response.status_code == 200, response.text
    assert (await manager.load_session(account[1], "s-new"))["title"] == "Copper-Liste"
    assert agent._session_tracker.title_to_write("s-new") is None


async def test_a_rename_drops_the_older_titles_agents_still_carry(api, account, headers, monkeypatch):
    """Carried by runs that never wrote them -- a failed first save, a run on
    another agent: the rename is the name now, or their next save puts one back."""
    from agent_system.servers.agent.server import Agent

    app, manager = api
    default = app.state.agent
    registry = app.state.tool_registry
    other = next((agent for agent in map(registry.get, registry.list())
                  if isinstance(agent, Agent) and agent is not default), None)
    assert other is not None, "fixture: no second agent in the registry"
    # the default agent is not registered when a server took its name
    names = [name for name in registry.list() if registry.get(name) is not default]
    monkeypatch.setattr(registry, "list", lambda: names)
    await _titled(manager, account[1], "zweiter Versuch", "s-old")
    default._session_tracker.carry_title("s-old", "T-first")
    other._session_tracker.carry_title("s-old", "T-other")
    _running(monkeypatch, {})

    response = await _patch(app, headers, "/api/sessions/s-old", {"title": "X-renamed"})

    assert response.status_code == 200, response.text
    assert default._session_tracker.title_to_write("s-old") is None
    assert other._session_tracker.title_to_write("s-old") is None


async def test_two_quick_renames_during_the_first_save_end_on_the_later(api, account, headers, monkeypatch):
    """PATCH B and PATCH C while the run's first save has created the record and
    not yet written it: that save writes its old title, then the newest one
    carried -- which B, finishing after C, must not have dropped as older."""
    import asyncio

    from agent_system.llm.models import ChatMessage
    from agent_system.services.session_service import SessionService

    app, manager = api
    agent, user = app.state.agent, account[1]
    tracker = agent._session_tracker
    tracker.set_session_metadata("s1", {"user_id": user, "agent_name": agent.name, "llm_profile": "p"})
    tracker.carry_title("s1", "T0")
    tracker.set_session_messages("s1", [ChatMessage(role="user", content="hallo")])
    _running(monkeypatch, {"s1": {"user_id": user, "agent_name": agent.name}})
    save, rename = manager.save_session, manager.rename_session
    run_inside, run_writes, b_inside, b_renames = (asyncio.Event() for _ in range(4))
    saves = []

    async def held_save(session_data):
        saves.append(session_data.get("title"))
        if len(saves) == 1:  # the run's own write
            run_inside.set()
            await run_writes.wait()
        await save(session_data)

    async def held_rename(user_id, session_id, title):
        if title == "B":
            b_inside.set()
            await b_renames.wait()
        await rename(user_id, session_id, title)

    monkeypatch.setattr(manager, "save_session", held_save)
    monkeypatch.setattr(manager, "rename_session", held_rename)
    run_save = asyncio.ensure_future(
        SessionService(manager).save_session(agent, user, "s1", agent.name, "p", True))
    await run_inside.wait()
    assert saves == ["T0"], f"fixture: the run's save is not writing its title: {saves}"
    patch_b = asyncio.ensure_future(_patch(app, headers, "/api/sessions/s1", {"title": "B"}))
    await b_inside.wait()
    assert (await _patch(app, headers, "/api/sessions/s1", {"title": "C"})).status_code == 200
    b_renames.set()
    assert (await patch_b).status_code == 200
    run_writes.set()
    assert await run_save is True

    assert (await manager.load_session(user, "s1"))["title"] == "C"
    assert tracker.title_to_write("s1") is None
