"""The HTTP entries refuse an agent behind a role gate the caller does not pass (metadata.min_role).

Each answers the refusal exactly as it answers an agent that does not exist -- the same
status, the same body with the name swapped -- so a caller cannot tell which agents are
there behind a gate. The reason is only logged. (POST /api/sessions stays a 403: it
accepts names nothing is registered under, so no answer there could look like theirs.)

The real build_app with auth on, signed in against a tmp user store (root: admin,
bob: user) -- never data/users.db. The gate is put on two agents in the test, not
in the shipped config: a probe agent registered by name, and the entry agent that
answers a run without an agent_name. Agent.run_events is replaced by a recorder,
so every assertion here is about the endpoint's own refusal: a gated run that got
past it shows up in ``started``.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from agent_system.auth.security import create_access_token
from agent_system.config.models import AgentConfig, AgentMetadata, ToolServerConfig
from agent_system.core.request_context import request_user_map
from agent_system.servers.agent.server import Agent
from live_accounts import signing_key

pytestmark = pytest.mark.anyio

PROBE = "probe_gated_agent"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_system import app as app_mod
    from agent_system.auth import database
    from agent_system.auth.models import UserCreate, UserRole

    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    monkeypatch.setattr(database, "setup_database", lambda db_path=None: users)
    for name, role in (("root", UserRole.ADMIN), ("bob", UserRole.USER)):
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role))
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))

    app = app_mod.build_app()
    assert app.state.auth_config.enabled, "fixture: these tests need auth on"
    # the lifespan sets it; these tests run without one
    from agent_system.services.session_manager import SessionManager
    monkeypatch.setattr(app.state, "session_manager", SessionManager(storage_path=str(tmp_path / "sessions")),
                        raising=False)

    started = []

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        started.append(self.name)
        yield {"type": "final", "summary": "done"}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)

    registry = app.state.tool_registry
    probe = Agent(PROBE, app.state.config,
                  ToolServerConfig(type="agent", enabled=True, agent_config=AgentConfig(llm_profile="normal"),
                                   metadata=AgentMetadata(visibility="ui", min_role="admin")),
                  registry)
    probe._tool_public = True  # what apply_to gives a visibility "ui" plugin agent
    monkeypatch.setitem(registry._servers, PROBE, probe)
    assert probe.min_role == "admin", "fixture: the probe carries no gate"

    def headers(name):
        account = users.get_user_by_username(name)
        token = create_access_token({"sub": name, "user_id": account.id, "role": account.role.value},
                                    secret_key=signing_key(), algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    return SimpleNamespace(app=app, entry=app.state.agent, started=started, headers=headers)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


#: A name nothing is registered under.
NOBODY = "probe_no_such_agent"


def _swapped(response, name=PROBE):
    """(status, body) with the agent's name taken out, for comparing a refusal with a missing agent."""
    return response.status_code, response.text.replace(name, "<agent>")


async def _run(api, who, endpoint, agent_name=None, request_id=None):
    body = {"task": "go", "session_id": f"s-{uuid.uuid4().hex[:8]}"}
    if agent_name:
        body["agent_name"] = agent_name
    if request_id:
        body["request_id"] = request_id
    async with _client(api.app) as client:
        if endpoint == "/run":
            return await client.post("/run", json=body, headers=api.headers(who), timeout=60.0)
        return await client.get("/events", params=body, headers=api.headers(who), timeout=60.0)


@pytest.mark.parametrize("endpoint", ["/run", "/events"])
async def test_a_user_is_refused_a_gated_agent_by_name_and_an_admin_runs_it(api, endpoint):
    rid = f"gate{uuid.uuid4().hex[:10]}"
    refused = await _run(api, "bob", endpoint, PROBE, request_id=rid)
    unknown = await _run(api, "bob", endpoint, NOBODY)

    assert unknown.status_code == 404, f"fixture: an unknown agent is no 404 here: {unknown.text}"
    assert _swapped(refused) == _swapped(unknown, NOBODY), "the refusal tells the gated agent from a missing one"
    assert api.started == [], "the refused run started anyway"
    assert rid not in request_user_map, "the refused request stayed registered"

    allowed = await _run(api, "root", endpoint, PROBE)
    assert allowed.status_code == 200, allowed.text
    assert api.started == [PROBE]


@pytest.mark.parametrize("endpoint", ["/run", "/events"])
async def test_the_default_agent_is_gated_too(api, endpoint, monkeypatch):
    monkeypatch.setattr(api.entry, "min_role", "admin")

    refused = await _run(api, "bob", endpoint)
    allowed = await _run(api, "root", endpoint)

    # the entry agent has a name only the server knows: nothing to hide behind a 404
    assert refused.status_code == 403 and api.entry.name not in refused.text, refused.text
    assert allowed.status_code == 200, allowed.text
    assert api.started == [api.entry.name], "the default agent ran for a caller its gate refuses"


async def test_the_agent_list_shows_a_gated_agent_only_to_whom_may_run_it(api, monkeypatch):
    monkeypatch.setattr(api.entry, "min_role", "admin")
    async with _client(api.app) as client:
        as_user = (await client.get("/agents", headers=api.headers("bob"))).json()
        as_admin = (await client.get("/agents", headers=api.headers("root"))).json()

    assert PROBE in as_admin["agents"], as_admin
    assert as_admin["default"] == api.entry.name
    assert PROBE not in as_user["agents"], as_user
    assert PROBE not in [detail["name"] for detail in as_user["details"]], as_user
    assert as_user["default"] is None, "a default the caller may not run was offered"
    assert set(as_user["agents"]) < set(as_admin["agents"]), "the user's list lost more than the gated agents"


@pytest.mark.parametrize("path", ["/agents/{}/tools", "/agents/{}/allowed-tools"])
async def test_what_a_gated_agent_can_reach_is_refused_to_a_user(api, path):
    async with _client(api.app) as client:
        as_user = await client.get(path.format(PROBE), headers=api.headers("bob"))
        unknown = await client.get(path.format(NOBODY), headers=api.headers("bob"))
        as_admin = await client.get(path.format(PROBE), headers=api.headers("root"))

    assert _swapped(as_user) == _swapped(unknown, NOBODY), (as_user.text, unknown.text)
    assert as_admin.status_code == 200 and "not found" not in as_admin.text, as_admin.text


async def test_a_session_for_a_gated_agent_is_refused_to_a_user(api):
    """The record names the agent a wake later runs as the session's user."""
    async with _client(api.app) as client:
        as_user = await client.post("/api/sessions", json={"agent_name": PROBE, "session_id": "s-user"},
                                    headers=api.headers("bob"))
        as_admin = await client.post("/api/sessions", json={"agent_name": PROBE, "session_id": "s-admin"},
                                     headers=api.headers("root"))
        ungated = await client.post("/api/sessions", json={"agent_name": api.entry.name, "session_id": "s-open"},
                                    headers=api.headers("bob"))

    assert as_user.status_code == 403, as_user.text
    assert as_admin.status_code == 201, as_admin.text
    assert ungated.status_code == 201, ungated.text


async def test_a_chat_command_on_a_gated_agent_is_refused_to_a_user(api):
    """Its tools would run with the gated agent's authorization."""
    body = {"name": "/no_such_command", "payload": "", "agent_name": PROBE}
    async with _client(api.app) as client:
        as_user = await client.post("/chat/command", json=body, headers=api.headers("bob"))
        unknown = await client.post("/chat/command", json={**body, "agent_name": NOBODY},
                                    headers=api.headers("bob"))
        as_admin = await client.post("/chat/command", json=body, headers=api.headers("root"))

    assert _swapped(as_user) == _swapped(unknown, NOBODY), (as_user.text, unknown.text)
    # past the gate, the admin meets the next check: the command does not exist
    assert as_admin.status_code == 404 and as_admin.json() != unknown.json(), as_admin.text


@pytest.mark.parametrize("bound", [True, False])
async def test_the_list_filters_by_the_gate_on_both_of_its_paths(api, monkeypatch, bound):
    """The view (a registry bound to its runtime) and the instance (one that is not)."""
    if not bound:
        monkeypatch.setattr(api.app.state.tool_registry, "_runtime", None)
    async with _client(api.app) as client:
        as_user = (await client.get("/agents", headers=api.headers("bob"))).json()
        as_admin = (await client.get("/agents", headers=api.headers("root"))).json()

    assert PROBE in as_admin["agents"], as_admin
    assert PROBE not in as_user["agents"], as_user


# ---------------------------------------------------------------- auth off: the gates cannot hold

class _Records(__import__("logging").Handler):
    """On the app's own logger: build_app reconfigures the root logger (force=True),
    which takes pytest's capture handler with it."""

    def __init__(self):
        super().__init__(level=0)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def app_log():
    import logging

    records = _Records()
    logger = logging.getLogger("agent_system.app")
    logger.addHandler(records)
    yield records
    logger.removeHandler(records)


def test_gated_agent_names_follow_the_type_chain():
    from agent_system.app import _gated_agent_names
    from agent_system.config.models import AgentSystemConfig, PluginsConfig

    def server(type_, enabled, min_role=None):
        return ToolServerConfig(type=type_, enabled=enabled, agent_config=AgentConfig(llm_profile="normal"),
                                metadata=AgentMetadata(min_role=min_role) if min_role else None)

    config = AgentSystemConfig(plugins=PluginsConfig(servers={
        "gated_base": server("agent", False, "admin"),
        "child": server("gated_base", True),            # inherits the gate
        "open": server("agent", True),
        "gated_but_off": server("agent", False, "user"),
        "gated_on": server("agent", True, "user"),
    }))

    assert _gated_agent_names(config) == ["child", "gated_on"]


def test_a_start_without_auth_names_the_gates_it_cannot_enforce(monkeypatch, tmp_path, app_log):
    import logging

    from agent_system import app as app_mod
    from tests.app.test_run_llm_override_uses_live_config import _disable_auth

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(app_mod, "_gated_agent_names", lambda config: ["probe_one", "probe_two"])

    app_mod.build_app()

    named = [record for record in app_log.records
             if record.levelno == logging.WARNING and "probe_one" in record.getMessage()]
    assert len(named) == 1, [record.getMessage() for record in named]
    assert "probe_two" in named[0].getMessage()


def test_a_start_with_auth_says_nothing_about_the_gates(api, app_log, monkeypatch):
    """The fixture built its app with auth on; build one more and listen."""
    import logging

    from agent_system import app as app_mod

    monkeypatch.setattr(app_mod, "_gated_agent_names", lambda config: ["probe_one"])
    app_mod.build_app()

    assert not [record for record in app_log.records
                if record.levelno == logging.WARNING and "probe_one" in record.getMessage()]


async def test_a_gated_agents_plugin_commands_are_not_offered_to_a_user(api, monkeypatch):
    """They are derived from its tool allowlist -- what the gate keeps from the caller, as /tools does."""
    from agent_system.plugin_commands import collect_plugin_commands, spellings

    registry = api.app.state.tool_registry
    agents = [registry.get(name) for name in registry.list() if isinstance(registry.get(name), Agent)]
    with_commands = [(agent, collect_plugin_commands(agent)) for agent in agents]
    target, commands = next(((a, c) for a, c in with_commands if c), (None, None))
    assert target is not None, "fixture: no registered agent offers a plugin command"
    monkeypatch.setattr(target, "min_role", "admin")
    line = "/" + spellings(commands)[0]

    async with _client(api.app) as client:
        listed = {who: (await client.get("/chat/commands", params={"agent": target.name},
                                          headers=api.headers(who))).json()["plugin_commands"]
                  for who in ("bob", "root")}
        resolved = {who: (await client.post("/chat/resolve", json={"line": line, "agent_name": target.name},
                                            headers=api.headers(who))).json()
                    for who in ("bob", "root")}

    assert listed["root"], "the admin lost the gated agent's commands"
    assert listed["bob"] == [], listed["bob"]
    assert resolved["root"]["kind"] != resolved["bob"]["kind"], resolved


async def test_the_agent_list_reads_the_registry_and_the_configs_off_the_event_loop(api, monkeypatch):
    """GET /agents was a plain ``def``, so FastAPI ran it in the thread pool; as ``async def`` (it resolves the
    caller for the gate) its walk and the per-agent config reads would stall every stream on the loop."""
    import asyncio

    from agent_system import app as app_mod
    from agent_system.config import settings

    on_loop = []
    registry = app_mod._app_registry
    real_list, real_config = registry.list, settings.get_tool_server_config

    def listing():
        on_loop.append(("walk", asyncio._get_running_loop() is not None))
        return real_list()

    def config_of(name, config=None):
        on_loop.append(("details", asyncio._get_running_loop() is not None))
        return real_config(name, config)

    monkeypatch.setattr(registry, "list", listing)
    monkeypatch.setattr(settings, "get_tool_server_config", config_of)
    async with _client(api.app) as client:
        answer = await client.get("/agents", headers=api.headers("root"))

    assert answer.status_code == 200 and answer.json()["agents"], answer.text
    assert {kind for kind, _ in on_loop} == {"walk", "details"}, "fixture: the endpoint read nothing"
    assert not any(loop for _, loop in on_loop), on_loop[:5]


async def test_an_admin_still_cannot_run_another_users_session(api):
    """POST /run answers another user's session with a 403 before any agent sees it -- an admin's included.
    The run path does the same (Agent._foreign_session refuses any registered owner who is not the session's
    stored user); this pins the HTTP side it was made to match."""
    from agent_system import app as app_mod

    manager = app_mod._session_service.session_manager
    await manager.create_session(user_id="bob", session_id="s-bobs", agent_name=api.entry.name,
                                 llm_profile="default")
    async with _client(api.app) as client:
        answer = await client.post("/run", json={"task": "go", "session_id": "s-bobs"},
                                   headers=api.headers("root"), timeout=60.0)

    assert answer.status_code == 403, answer.text
    assert api.started == []
