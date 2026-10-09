"""A chat's llm_params (the web chat's thinking level): checked, applied, kept with the session.

- /run (JSON and form) and POST /events take ``llm_params``; only CHAT_LLM_PARAMS pass, anything else
  is a 400 before a run starts -- provider_routing or plugins would get through AgentConfig's validator.
- Params without a profile apply to the agent's own primary: the web chat sends no profile any more
  unless a person picked one.
- The record keeps what the run set and loses it when a run sets nothing; a save that does not know
  (a sub-agent's, a plugin's) leaves it. It also keeps whether the profile was CHOSEN
  (llm_profile_override): llm_profile names the one that ran either way, and every web session before
  this carries the global default there.
- /agents names each agent's own profile, /llm/profiles the levels -- what the browser shows.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from agent_system import app_state
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api(tmp_path, monkeypatch):
    from agent_system import app as app_mod
    from agent_system.config.models import AuthConfig
    from agent_system.llm import factory as llm_factory

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
    monkeypatch.setattr(app_state, "session_service", service)
    built = []
    original = llm_factory.create_llm_from_profile

    def recording(*args, **kwargs):
        built.append((kwargs.get("llm_profile"), dict(kwargs.get("llm_params") or {})))
        return original(*args, **kwargs)

    monkeypatch.setattr(llm_factory, "create_llm_from_profile", recording)

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        # a turn to save: the record is written from the tracker's messages
        text = task if isinstance(task, str) else "go"  # the files path hands over a multimodal message
        self._session_tracker.set_session_messages(session_id, [
            ChatMessage(role="user", content=text), ChatMessage(role="assistant", content="done")])
        yield {"type": "start", "request_id": request_id, "session_id": session_id}
        yield {"type": "final", "summary": "done"}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    return SimpleNamespace(app=app, manager=manager, service=service, built=built,
                           own=app.state.agent.agent_config.default_llm_profile)


async def _post(app, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, timeout=60.0, **kwargs)


THINK_HIGH = {"thinking_level": "high"}


@pytest.mark.parametrize("how", ["json", "form", "events"])
async def test_params_without_a_profile_run_on_the_agents_own_primary(api, how):
    if how == "json":
        response = await _post(api.app, "/run", json={"task": "go", "llm_params": THINK_HIGH})
    elif how == "form":
        response = await _post(api.app, "/run", data={"task": "go", "llm_params": json.dumps(THINK_HIGH)},
                               files={"files": ("a.txt", b"hi", "text/plain")})
    else:
        response = await _post(api.app, "/events", json={"task": "go", "llm_params": THINK_HIGH})

    assert response.status_code == 200, response.text
    assert api.built, "fixture: no override was built"
    profile, params = api.built[0]
    assert profile == api.own
    assert params.get("thinking_level") == "high", params


async def test_a_run_without_params_or_profile_builds_no_override(api):
    response = await _post(api.app, "/run", json={"task": "go"})

    assert response.status_code == 200, response.text
    assert api.built == []


@pytest.mark.parametrize("params", [
    {"provider_routing": {"order": ["somewhere"]}},
    {"plugins": [{"id": "web"}]},
    {"base_url": "http://elsewhere"},
    {"thinking_level": "ultra"},
    ["thinking_level"],
])
@pytest.mark.parametrize("path", ["/run", "/events"])
async def test_what_a_chat_may_not_set_is_refused_before_the_run(api, params, path):
    response = await _post(api.app, path, json={"task": "go", "llm_params": params})

    assert response.status_code == 400, response.text
    assert api.built == []


async def test_a_form_field_that_is_no_json_is_refused(api):
    response = await _post(api.app, "/run", data={"task": "go", "llm_params": "{high"},
                           files={"files": ("a.txt", b"hi", "text/plain")})

    assert response.status_code == 400, response.text


async def _turn(app, how, sid, params=None):
    """One run into *sid*: /run with JSON, /run with a file (a form), or POST /events."""
    if how == "files":
        data = {"task": "go", "session_id": sid}
        if params is not None:
            data["llm_params"] = json.dumps(params)
        return await _post(app, "/run", data=data, files={"files": ("a.txt", b"hi", "text/plain")})
    body = {"task": "go", "session_id": sid}
    if params is not None:
        body["llm_params"] = params
    return await _post(app, "/run" if how == "json" else "/events", json=body)


@pytest.mark.parametrize("how", ["json", "files", "events"])
async def test_the_record_keeps_the_runs_params_and_loses_them_with_a_run_that_sets_none(api, how):
    sid = "keep-" + how
    response = await _turn(api.app, how, sid, THINK_HIGH)
    assert response.status_code == 200, response.text
    record = await api.manager.load_session("anonymous", sid)
    assert record.get("llm_params") == THINK_HIGH, record.get("llm_params")

    response = await _turn(api.app, how, sid)
    assert response.status_code == 200, response.text
    record = await api.manager.load_session("anonymous", sid)
    assert "llm_params" not in record, record.get("llm_params")


async def test_the_record_says_whether_the_profile_was_chosen(api):
    other = next(name for name in api.app.state.config.llm_system.profiles if name != api.own)
    response = await _post(api.app, "/run", json={"task": "go", "session_id": "chosen", "llm_profile": other})
    assert response.status_code == 200, response.text
    record = await api.manager.load_session("anonymous", "chosen")
    assert record.get("llm_profile_override") == other

    response = await _post(api.app, "/run", json={"task": "again", "session_id": "chosen"})
    assert response.status_code == 200, response.text
    record = await api.manager.load_session("anonymous", "chosen")
    assert record.get("llm_profile") == api.own, "fixture: the run did not go back to the agent's own"
    assert "llm_profile_override" in record and record["llm_profile_override"] is None, (
        "the agent's own profile is recorded as a choice, or the record does not say that none was made")


async def test_the_agents_own_save_writes_what_the_run_was_opened_with_and_a_stranger_leaves_it(api):
    agent = api.app.state.agent
    agent._session_service = api.service
    sid = "own-save"
    await api.service.open_for_run(agent, "anonymous", sid, api.own,
                                   llm_choice={"profile": None, "params": THINK_HIGH})
    agent._session_tracker.set_session_messages(sid, [ChatMessage(role="user", content="go")])
    assert await agent._save_session_to_disk(sid)
    assert (await api.manager.load_session("anonymous", sid)).get("llm_params") == THINK_HIGH

    # a writer that knows nothing of params (a sub-agent's session opening, a plugin) leaves the record's
    agent._session_tracker.set_session_metadata(sid, {"user_id": "anonymous", "agent_name": agent.name,
                                                      "llm_profile": api.own})
    assert await agent._save_session_to_disk(sid)
    assert (await api.manager.load_session("anonymous", sid)).get("llm_params") == THINK_HIGH


async def test_the_lists_name_the_agents_own_profile_and_the_levels(api):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
        agents = (await client.get("/agents")).json()
        profiles = (await client.get("/llm/profiles")).json()

    entry = next(d for d in agents["details"] if d["name"] == api.app.state.agent.name)
    assert entry["llm_profile"] == api.own
    assert "high" in profiles["thinking_levels"] and "ultra" not in profiles["thinking_levels"]


@pytest.mark.parametrize("how", ["json", "files", "events"])
async def test_the_runs_own_saves_write_its_params_before_any_save_after_it(api, how, monkeypatch):
    """A client that leaves mid-run gets no save after the run (it is skipped while the stream is gone,
    or while the run still holds the session): the run's own saves read the session's metadata, which
    the start event rewrites -- without the params in it they kept the record's old ones."""
    agent = api.app.state.agent
    agent._session_service = api.service
    seen = []

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        # the files path hands over a multimodal message: the turn's text is all a save needs
        self._session_tracker.set_session_messages(session_id, [ChatMessage(role="user", content="go")])
        # The start event rewrites the metadata where the stream reads it -- later than this run goes on,
        # so the save waits for the rewrite (the marker goes with the dict it replaces). /run with JSON
        # rewrites nothing: its metadata is what the opening wrote.
        self._session_tracker.get_session_metadata(session_id)["probe"] = True
        yield {"type": "start", "request_id": request_id, "session_id": session_id}
        for _ in range(500 if how != "json" else 0):
            if "probe" not in (self._session_tracker.get_session_metadata(session_id) or {}):
                break
            await asyncio.sleep(0.01)
        else:
            assert how == "json", "fixture: the start event never rewrote the metadata"
        await self._save_session_to_disk(session_id)
        seen.append((await api.manager.load_session("anonymous", session_id)).get("llm_params"))
        yield {"type": "final", "summary": "done"}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    response = await _turn(api.app, how, "own-" + how, THINK_HIGH)

    assert response.status_code == 200, response.text
    assert seen == [THINK_HIGH], seen


@pytest.mark.parametrize("path", ["/run", "/events"])
async def test_a_reconnecting_page_gets_the_runs_params_back(api, path):
    from agent_system.services.background_job_manager import get_background_job_manager

    rid = "rq" + path[1:] + "params"
    response = await _post(api.app, path, json={"task": "go", "request_id": rid, "llm_params": THINK_HIGH})
    assert response.status_code == 200, response.text
    job = await get_background_job_manager().get_job(rid)
    assert job is not None, "fixture: the run left no job"
    assert job.llm_params == THINK_HIGH

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
        reconnect = await client.get(f"/events?task=&request_id={rid}", timeout=30.0)
    events = [json.loads(line[len("data: "):]) for line in reconnect.text.splitlines() if line.startswith("data: ")]
    first = next(e for e in events if e.get("type") == "reconnect")
    assert first["llm_params"] == THINK_HIGH
