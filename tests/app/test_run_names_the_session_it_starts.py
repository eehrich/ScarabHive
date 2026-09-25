"""A title for the session a run starts: /run and /events take ``session_title``.

The browser's `/title` before the first message has no session to rename --
the server makes the id with the first run. As in agent-cli, the title goes out
with that message and the run's first save writes it: handed to the run at its
start event, where the run waits until the event is passed on, so no save of
the run can come first (SessionService writes it with the first save, then
drops it).

The run here is a stand-in that does what a real one does around the title:
metadata before the start event, then the conversation to disk through the
agent's own save. Everything from the endpoint to the file is the real code.
"""
from __future__ import annotations

import httpx
import pytest

from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

pytestmark = pytest.mark.anyio
TITLE = "Blitter umbauen"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_system import app as app_mod
    from agent_system.config.models import AuthConfig

    class _Off:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _Off(), raising=False)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    manager = SessionManager(storage_path=str(tmp_path))
    service = SessionService(manager)
    monkeypatch.setattr(app_mod, "_session_service", service)
    monkeypatch.setattr(app.state.agent, "_session_service", service)

    started = []

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        sid = session_id or "fresh1"
        started.append(sid)
        tracker = self._session_tracker
        if not tracker.get_session_metadata(sid):
            tracker.set_session_metadata(sid, {"user_id": "anonymous", "agent_name": self.name,
                                               "llm_profile": "default"})
        yield {"type": "start", "request_id": request_id, "session_id": sid}
        # what a run does after its first answer: the conversation to disk
        tracker.set_session_messages(sid, [ChatMessage(role="user", content="hallo"),
                                           ChatMessage(role="assistant", content="ok")])
        await self._save_session_to_disk(sid)
        yield {"type": "final", "summary": "done"}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    return SimpleNamespace(app=app, manager=manager, started=started)


async def _start(app, how):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        if how == "run-json":
            return await client.post("/run", json={"task": "hallo", "session_title": TITLE}, timeout=60.0)
        if how == "run-files":
            return await client.post("/run", data={"task": "hallo", "session_title": TITLE},
                                     files={"files": ("notes.md", b"the notes", "text/markdown")},
                                     timeout=60.0)
        if how == "events-post":
            return await client.post("/events", json={"task": "hallo", "session_title": TITLE},
                                     timeout=60.0)
        return await client.get("/events", params={"task": "hallo", "session_title": TITLE},
                                timeout=60.0)


@pytest.mark.parametrize("how", ["run-json", "run-files", "events-post", "events-get"])
async def test_the_first_save_writes_the_title_the_run_was_given(api, how):
    response = await _start(api.app, how)

    assert response.status_code == 200, response.text
    # the id the run used -- /run without one makes its own
    sid, = api.started
    stored = await api.manager.load_session("anonymous", sid)
    assert stored["title"] == TITLE, f"{how}: the session was named {stored['title']!r}"
    tracker = api.app.state.agent._session_tracker
    assert tracker.title_to_write(sid) is None, "written, and still waiting to be written"


async def test_without_a_title_the_first_message_names_it(api):
    """What the endpoint did before: the title comes from the first request."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.post("/events", json={"task": "hallo"}, timeout=60.0)

    assert response.status_code == 200, response.text
    sid, = api.started
    assert (await api.manager.load_session("anonymous", sid))["title"] == "hallo"


@pytest.mark.parametrize("route", ["/run", "/events"])
async def test_a_title_does_not_rename_a_session_the_run_continues(api, route):
    """session_title names the session a run creates; one it continues keeps its
    name -- and a run of it that saves nothing leaves no title waiting."""
    session = await api.manager.create_session(user_id="anonymous", session_id="s-old", title="alt",
                                               agent_name="a", llm_profile="p")
    await api.manager.save_session(session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.post(route, json={"task": "hallo", "session_id": "s-old",
                                                  "session_title": TITLE}, timeout=60.0)

    assert response.status_code == 200, response.text
    assert api.started == ["s-old"], "fixture: the run did not continue s-old"
    assert (await api.manager.load_session("anonymous", "s-old"))["title"] == "alt"
    assert api.app.state.agent._session_tracker.title_to_write("s-old") is None


async def test_a_title_that_is_not_text_is_not_one(api):
    """A JSON number is no title: the run goes on under the first message's."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.post("/events", json={"task": "hallo", "session_title": 5}, timeout=60.0)

    assert response.status_code == 200, response.text
    sid, = api.started
    assert (await api.manager.load_session("anonymous", sid))["title"] == "hallo"


class TestTheTitleIsWrittenOnce:
    """SessionService writes a carried title with the next save that writes
    anything -- and then lets go of it, as agent-cli drops its /title."""

    def _agent(self, tmp_path):
        from types import SimpleNamespace as NS

        from agent_system.servers.agent.components.session_tracking import SessionTracker

        manager = SessionManager(storage_path=str(tmp_path))
        tracker = SessionTracker()
        tracker.set_session_metadata("s1", {"user_id": "u", "agent_name": "coder", "llm_profile": "p"})
        agent = NS(_session_service=SessionService(manager), _session_tracker=tracker,
                   name="coder", agent_config=NS(default_llm_profile="p"))
        return agent, tracker, manager

    async def test_a_save_that_wrote_nothing_keeps_it_for_the_next(self, tmp_path):
        agent, tracker, manager = self._agent(tmp_path)
        tracker.carry_title("s1", TITLE)

        assert await Agent._save_session_to_disk(agent, "s1") is False, "fixture: nothing to write"
        assert tracker.title_to_write("s1") == TITLE, "dropped by a save that wrote nothing"

        tracker.set_session_messages("s1", [ChatMessage(role="user", content="hallo")])
        assert await Agent._save_session_to_disk(agent, "s1") is True
        assert (await manager.load_session("u", "s1"))["title"] == TITLE

    async def test_a_checkpoint_that_writes_first_writes_it(self, tmp_path):
        """A long tool keeps the run's own save away; the checkpoint may create
        the record -- under the run's title, and a rename after it holds."""
        agent, tracker, manager = self._agent(tmp_path)
        tracker.carry_title("s1", TITLE)
        tracker.set_session_messages("s1", [ChatMessage(role="user", content="hallo"),
                                            ChatMessage(role="assistant", content="ok")])

        assert await agent._session_service.checkpoint_session(agent, "u", "s1") is True, \
            "fixture: the checkpoint wrote nothing"
        assert (await manager.load_session("u", "s1"))["title"] == TITLE

        await manager.rename_session("u", "s1", "Neuer Name")
        await Agent._save_session_to_disk(agent, "s1")
        assert (await manager.load_session("u", "s1"))["title"] == "Neuer Name"

    async def test_a_title_carried_while_the_first_save_runs_is_written_too(self, tmp_path):
        """A /title as the record is being created finds none and is carried:
        newer than the one that save writes -- written right after, not
        dropped with it."""
        agent, tracker, manager = self._agent(tmp_path)
        tracker.carry_title("s1", TITLE)
        tracker.set_session_messages("s1", [ChatMessage(role="user", content="hallo")])
        save = manager.save_session

        async def save_while_a_title_comes(session_data):
            manager.save_session = save
            tracker.carry_title("s1", "Copper-Liste")  # the PATCH that found no record
            await save(session_data)

        manager.save_session = save_while_a_title_comes
        assert await Agent._save_session_to_disk(agent, "s1") is True

        assert (await manager.load_session("u", "s1"))["title"] == "Copper-Liste"
        assert tracker.title_to_write("s1") is None, "written, and still waiting to be written"

    async def test_a_first_run_that_saved_nothing_leaves_its_title_to_the_next(self, tmp_path):
        """The run started (the browser has the id) and ended without a save: the
        next message's run opens the session afresh -- and still writes it."""
        agent, tracker, manager = self._agent(tmp_path)
        tracker.carry_title("s1", TITLE)

        assert await agent._session_service.open_for_run(agent, "u", "s1", "p") is False, \
            "fixture: s1 is on disk"
        tracker.set_session_messages("s1", [ChatMessage(role="user", content="zweiter Versuch")])
        assert await Agent._save_session_to_disk(agent, "s1") is True

        assert (await manager.load_session("u", "s1"))["title"] == TITLE

    async def test_another_users_run_of_that_id_does_not_get_it(self, tmp_path):
        agent, tracker, manager = self._agent(tmp_path)
        tracker.carry_title("s1", TITLE)

        assert await agent._session_service.open_for_run(agent, "bob", "s1", "p") is False
        tracker.set_session_messages("s1", [ChatMessage(role="user", content="bobs Frage")])
        assert await Agent._save_session_to_disk(agent, "s1") is True

        assert (await manager.load_session("bob", "s1"))["title"] == "bobs Frage"

    async def test_a_title_that_cannot_be_written_after_the_save_waits_for_the_next(self, tmp_path):
        """The save went through: it says so -- the session-end hooks go by it --
        and the title it could not write stays for the next save."""
        agent, tracker, manager = self._agent(tmp_path)
        tracker.carry_title("s1", TITLE)
        tracker.set_session_messages("s1", [ChatMessage(role="user", content="hallo")])
        save = manager.save_session

        async def save_while_a_title_comes(session_data):
            manager.save_session = save
            tracker.carry_title("s1", "Copper-Liste")
            await save(session_data)

        async def rename_fails(*args):
            raise OSError("disk full")

        manager.save_session = save_while_a_title_comes
        manager.rename_session = rename_fails
        assert await Agent._save_session_to_disk(agent, "s1") is True, "a written save reported as failed"
        assert tracker.title_to_write("s1") == "Copper-Liste"

    async def test_a_checkpoint_waits_for_the_save_it_would_overlap(self, tmp_path):
        """The checkpoint loop runs through the whole run, the run's own save
        within it: each reads the title before it writes, so one at a time."""
        import asyncio

        agent, tracker, manager = self._agent(tmp_path)
        record = await manager.create_session(user_id="u", session_id="s1", title="alt",
                                              agent_name="coder", llm_profile="p")
        await manager.save_session(record)
        tracker.set_session_messages("s1", [ChatMessage(role="user", content="hallo"),
                                            ChatMessage(role="assistant", content="ok")])
        save, gate, writes = manager.save_session, asyncio.Event(), []

        async def slow_save(session_data):
            writes.append("in")
            if len(writes) == 1:
                await gate.wait()
            await save(session_data)
            writes.append("out")

        manager.save_session = slow_save
        run_save = asyncio.ensure_future(Agent._save_session_to_disk(agent, "s1"))
        while not writes:
            await asyncio.sleep(0)
        checkpoint = asyncio.ensure_future(agent._session_service.checkpoint_session(agent, "u", "s1"))
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        assert await run_save is True and await checkpoint is True, "fixture: a save wrote nothing"

        assert writes == ["in", "out", "in", "out"], writes

    async def test_a_rename_after_the_first_save_is_not_put_back(self, tmp_path):
        agent, tracker, manager = self._agent(tmp_path)
        tracker.carry_title("s1", TITLE)
        tracker.set_session_messages("s1", [ChatMessage(role="user", content="hallo")])
        await Agent._save_session_to_disk(agent, "s1")

        await manager.rename_session("u", "s1", "Neuer Name")
        await Agent._save_session_to_disk(agent, "s1")

        assert (await manager.load_session("u", "s1"))["title"] == "Neuer Name"
