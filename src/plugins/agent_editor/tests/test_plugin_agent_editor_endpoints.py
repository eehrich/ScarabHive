"""The Agent Editor API through its real router, on the config tree of the store tests: access, rows and details,
the live state against a running app, effective tools, the prompt reader and the writes over HTTP.

The app state is a stand-in for the real app: a Runtime whose declarations are built from the tree as it was at
"start" (the runtime's own ServerDecl/view code), a ToolServerRegistry with small tool servers, and Agent instances made
without their constructor (only `agent_config` is read from them).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.auth import database
from agent_system.auth.models import UserCreate, UserRole, UserUpdate
from agent_system.auth.security import create_access_token
from agent_system.config.models import AgentSystemConfig, AuthConfig, ToolServerConfig
from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.hooks.plugin_hook import HookType
from agent_system.hooks.registry import HookRegistry
from agent_system.tools.base import ToolServerRegistry
from agent_system.tools.base import ToolDef
from agent_system.runtime import Runtime, ServerDecl
from agent_system.servers.agent.server import Agent
from plugins.agent_editor import sources
from plugins.agent_editor.plugin import PLUGIN_FACTORY
from plugins.agent_editor.store import version_of
from plugins.agent_editor.tests.test_plugin_agent_editor_store import build_tree, plugins_file, team

BASE = "/plugins/agent_editor"
ROW_KEYS = {"name", "type", "base", "enabled", "description", "visibility", "category", "tags", "group", "file",
            "files", "editable", "readonly_reason", "state", "changed", "restart", "problems"}
DETAIL_KEYS = {"name", "file", "files", "version", "editable", "readonly_reason", "form_reason", "own", "parent", "inherited",
               "effective", "state", "changed", "reload_fields", "restart", "children", "spawnable", "prompt"}


class ToolServer:
    def __init__(self, *tools: str):
        self.tools = tools
        self.tools_description = None

    async def list_tools(self):
        return [ToolDef(name=name, description=self.tools_description or f"does {name}", input_schema={"type": "object"})
                for name in self.tools]


class LegacyToolServer:
    def get_tools(self):
        return [{"name": "search_web", "description": "Searches", "inputSchema": {"type": "object"}}]


class SimpleState:
    def __init__(self, **values):
        self.__dict__.update(values)


def built_agent(config: AgentSystemConfig, name: str) -> Agent:
    """An Agent as the registry holds it, without running its constructor (no LLM client)."""
    agent = Agent.__new__(Agent)
    agent.agent_config = get_tool_server_config(name, config).agent_config
    agent._tool_visible = False
    return agent


def started_app(config_path: Path) -> SimpleState:
    """A runtime as if the app had started on the tree as it is now: every enabled server declared."""
    config = load_settings(str(config_path))
    catalog = sources.catalog_for(config)
    registry = ToolServerRegistry()
    runtime = Runtime.__new__(Runtime)
    runtime.config, runtime.registry = config, registry
    runtime._decls = {}
    for name, server in config.plugins.servers.items():
        if server.enabled:
            merged = get_tool_server_config(name, config)
            runtime._decls[name] = ServerDecl(name=name, type=merged.type, server_config=merged, factory=object,
                                              plugin_metadata=catalog.manifest(merged.type))
    registry.bind(runtime)
    registry.register("files", ToolServer("files_read_file", "files_delete_file"))
    registry.register("search", LegacyToolServer())
    registry.register("web", ToolServer("web_search", "web_fetch"))
    return SimpleState(config=config, runtime=runtime, tool_registry=registry, config_path=str(config_path))


SECRET = "sekrit-value-123"


@pytest.fixture(autouse=True)
def secret_env(monkeypatch):
    """The writer's `api_hint` references this variable; its value must never leave the API."""
    monkeypatch.setenv("AGENT_EDITOR_TEST_KEY", SECRET)


@pytest.fixture(autouse=True)
def plugin_registry(monkeypatch):
    """An empty process-wide plugin registry per test; the returned function registers a server in it, as the
    runtime does for every plugin it builds (tool discovery and the config reload both walk it)."""
    from agent_system.plugins.tool_adapter import plugin_tool_registry
    servers = {}
    monkeypatch.setattr(plugin_tool_registry, "plugin_servers", servers)
    return lambda name, instance: servers.__setitem__(name, SimpleState(plugin_server=instance))


@pytest.fixture
def reload_reaches(plugin_registry):
    return plugin_registry


@pytest.fixture
def db(tmp_path, monkeypatch):
    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    for name, role in [("root", UserRole.ADMIN), ("bob", UserRole.USER)]:
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role))
    return users


@pytest.fixture
def tree(tmp_path):
    return build_tree(tmp_path)


def make_client(tree: Path, state: SimpleState | None = None, auth_enabled: bool = True) -> TestClient:
    plugin = PLUGIN_FACTORY("agent_editor", AgentSystemConfig(auth=AuthConfig(enabled=auth_enabled)),
                            ToolServerConfig(config_path=str(tree), root=str(tree.parent.parent)))
    app = FastAPI()
    app.include_router(plugin.get_web_router())
    for key, value in (state.__dict__ if state else {}).items():
        setattr(app.state, key, value)
    return TestClient(app)


def started_app_or_none(tree: Path) -> SimpleState | None:
    """The running app for a tree that may not load (then there is none)."""
    try:
        return started_app(tree)
    except Exception:
        return None


def forget_snapshots(web: TestClient) -> None:
    """An edit made outside the API shows up within a second; the tests do not wait for it."""
    for route in cast(FastAPI, web.app).routes:
        for store in getattr(getattr(getattr(route, "endpoint", None), "__self__", None), "_stores", {}).values():
            store._key = None


def as_user(name: str) -> dict:
    account = database.get_db().get_user_by_username(name)
    token = create_access_token({"sub": name, "user_id": account.id, "role": "admin"}, expires_delta=timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app_state(tree):
    return started_app(tree)


@pytest.fixture
def web(db, tree, app_state):
    return make_client(tree, app_state)


def rows(web) -> dict:
    answer = web.get(f"{BASE}/agents", headers=as_user("root"))
    assert answer.status_code == 200, answer.text
    return {row["name"]: row for row in answer.json()["agents"]}


def detail(web, name: str) -> dict:
    answer = web.get(f"{BASE}/agents/{name}", headers=as_user("root"))
    assert answer.status_code == 200, answer.text
    return answer.json()


def put(web, name: str, entry: dict, version: str, dry_run: bool = False):
    return web.put(f"{BASE}/agents/{name}", headers=as_user("root"),
                   json={"entry": entry, "version": version, "dry_run": dry_run})


# ---------------------------------------------------------------------- access


@pytest.mark.parametrize("who, status", [("nobody", 401), ("a user", 403), ("a deactivated admin", 403)])
def test_everyone_but_an_admin_is_refused(db, tree, app_state, tmp_path, who, status):
    web = make_client(tree, app_state)
    if who == "a deactivated admin":
        db.create_user(UserCreate(username="gone", email="gone@example.com", password="correct-horse", role=UserRole.ADMIN))
        headers = as_user("gone")  # signed while still active
        db.update_user(db.get_user_by_username("gone").id, UserUpdate(is_active=False))
    else:
        headers = {"nobody": {}, "a user": as_user("bob")}[who]
    before = team(tmp_path).read_bytes()
    version = version_of(before)
    answers = [
        web.get(f"{BASE}/agents", headers=headers),
        web.get(f"{BASE}/agents/writer", headers=headers),
        web.get(f"{BASE}/meta", headers=headers),
        web.get(f"{BASE}/tools", headers=headers),
        web.get(f"{BASE}/inherited", headers=headers, params={"type": "writer"}),
        web.get(f"{BASE}/prompt", headers=headers, params={"path": "config/prompts/system_prompt.md"}),
        web.put(f"{BASE}/agents/writer", headers=headers, json={"entry": {"type": "agent"}, "version": version}),
        web.request("DELETE", f"{BASE}/agents/helper", headers=headers, json={"version": version}),
        web.post(f"{BASE}/agents", headers=headers, json={"name": "mallory", "entry": {}}),
        web.put(f"{BASE}/agents/critic/spawnable", headers=headers,
                json={"sam": "team_sam", "allowed": True, "version": version_of(plugins_file(tmp_path).read_bytes())}),
        web.post(f"{BASE}/tools/effective", headers=headers, json={"allowed": ["*"]}),
        web.get(f"{BASE}/managers", headers=headers),
        web.post(f"{BASE}/managers", headers=headers, json={"name": "mallory_sam", "allowed_agents": []}),
        web.put(f"{BASE}/managers/team_sam", headers=headers,
                json={"entry": {"type": "sub_agent_manager"}, "version": version_of(plugins_file(tmp_path).read_bytes())}),
        web.post(f"{BASE}/yaml", headers=headers, json={"entry": {}}),
        web.post(f"{BASE}/yaml/parse", headers=headers, json={"yaml": "a: 1"}),
    ]
    assert [answer.status_code for answer in answers] == [status] * len(answers)
    assert team(tmp_path).read_bytes() == before
    assert not (tmp_path / "config/agents/mallory.yaml").exists()
    assert not (tmp_path / "config/agents/mallory_sam.yaml").exists()


def test_with_authentication_off_everything_is_refused_and_the_page_says_so(db, tree, app_state, tmp_path):
    web = make_client(tree, app_state, auth_enabled=False)
    before = team(tmp_path).read_bytes()
    listed = web.get(f"{BASE}/agents", headers=as_user("root"))
    saved = put(web, "writer", {"type": "agent"}, version_of(before))
    assert (listed.status_code, saved.status_code) == (403, 403)
    assert listed.json()["detail"] == "The agent editor needs authentication to be enabled"
    assert team(tmp_path).read_bytes() == before
    page = web.get(f"{BASE}/")
    assert page.status_code == 200 and "static/panel.js" not in page.text
    enabled = make_client(tree, app_state).get(f"{BASE}/")
    assert f"{BASE}/static/panel.js" in enabled.text


def test_writes_take_json_only(web, tmp_path):
    before = team(tmp_path).read_bytes()
    body = f'{{"entry": {{"type": "agent"}}, "version": "{version_of(before)}"}}'
    plain = web.put(f"{BASE}/agents/writer", headers={**as_user("root"), "Content-Type": "text/plain"}, content=body)
    untyped = web.put(f"{BASE}/agents/writer", headers=as_user("root"), content=body)
    form = web.post(f"{BASE}/agents", headers=as_user("root"), data={"name": "mallory"})
    broken = web.put(f"{BASE}/agents/writer", headers={**as_user("root"), "Content-Type": "application/json"},
                     content="{not json")
    assert [plain.status_code, untyped.status_code, form.status_code, broken.status_code] == [415, 415, 415, 422]
    assert team(tmp_path).read_bytes() == before


@pytest.mark.parametrize("body, field", [
    ({"version": "x"}, "entry"),
    ({"entry": [], "version": "x"}, "entry"),
    ({"entry": {}}, "version"),
])
def test_a_malformed_request_names_the_field(web, body, field):
    answer = web.put(f"{BASE}/agents/writer", headers=as_user("root"), json=body)
    assert answer.status_code == 422 and answer.json()["detail"].startswith(f"{field}:")


# ---------------------------------------------------------------------- rows and details


def test_rows_list_the_agents_with_file_group_and_editability(web):
    listed = web.get(f"{BASE}/agents", headers=as_user("root")).json()
    assert listed["errors"] == []
    by_name = {row["name"]: row for row in listed["agents"]}
    assert sorted(by_name) == ["critic", "demo_agent", "helper", "twin", "writer"]  # not the managers, not files
    assert all(set(row) == ROW_KEYS for row in by_name.values())
    assert by_name["writer"] == {
        "name": "writer", "type": "basic_agent", "base": "basic_agent", "enabled": True,
        "description": "Writes things", "visibility": "ui", "category": None, "tags": ["prose", "draft"],
        "group": "config", "file": "config/agents/team.yaml", "files": ["config/agents/team.yaml"],
        "editable": True, "readonly_reason": None, "state": "in_sync", "changed": [], "restart": False,
        "problems": ["Matches no tool: notes"],  # no server of that name runs
    }
    assert (by_name["critic"]["type"], by_name["critic"]["base"], by_name["critic"]["visibility"]) == ("writer", "basic_agent", "ui")
    assert (by_name["demo_agent"]["group"], by_name["demo_agent"]["state"]) == ("demo", "off")
    twin = by_name["twin"]
    assert (twin["editable"], twin["files"]) == (False, ["config/agents/twin_a.yaml", "config/agents/twin_b.yaml"])
    assert twin["readonly_reason"] == "defined in 2 files: config/agents/twin_a.yaml, config/agents/twin_b.yaml"


def test_agent_ness_is_decided_per_entry_and_a_class_needs_only_agents(db, tree):
    state = started_app(tree)
    web = make_client(tree, state)
    # a type the app built a tool server and an agent from: only that entry is an agent, the type is no class
    state.tool_registry.register("files_agent", built_agent(state.config, "writer"))
    listed = rows(web)
    assert "files_agent" in listed and listed["files_agent"]["base"] == "file_ops"
    assert "files" not in listed and "files_off" not in listed
    assert "file_ops" not in [row["name"] for row in web.get(f"{BASE}/meta", headers=as_user("root")).json()["classes"]]
    assert web.get(f"{BASE}/agents/files", headers=as_user("root")).status_code == 404
    assert detail(web, "files_agent")["own"] == {"type": "file_ops", "enabled": True}
    # every built instance of the type is an agent: the type is a class, its undeclared entries are agents too
    state.tool_registry.register("files", built_agent(state.config, "writer"))
    listed = rows(web)
    assert {"files", "files_agent", "files_off"} <= set(listed)
    assert "file_ops" in [row["name"] for row in web.get(f"{BASE}/meta", headers=as_user("root")).json()["classes"]]


def test_a_delete_between_two_rows_does_not_break_the_list(db, tree, tmp_path, monkeypatch):
    from plugins.agent_editor.store import Store
    web = make_client(tree, started_app(tree))
    rows(web)
    real = Store.snapshot
    calls = []

    def snapshot_then_delete(store):
        calls.append(1)
        if len(calls) == 2:  # someone removes the helper while the list is being built
            path = team(tmp_path)
            path.write_bytes(path.read_bytes().split(b"    # --- a helper")[0])
            store._key = None
        return real(store)

    monkeypatch.setattr(Store, "snapshot", snapshot_then_delete)
    assert "helper" in rows(web)
    assert "helper" not in rows(web)


SHAKY = """\
external_servers:
  remote_servers:
    ext_on: {enabled: true, transport: stdio, command: x}
    ext_off: {enabled: false}
plugins:
  servers:
    shaky:
      type: basic_agent
      enabled: true
      agent_config:
        llm_profile: [fast, nope, "${AGENT_EDITOR_TEST_KEY}"]
        llm_profile_advanced: [slow, gone]
        system_template: config/prompts/missing.md
        skills: {always: [known, ghost], on_demand: [spook, ghost]}
        tools: {allowed: ["files/*", "web/nothing", "ext_on.*", "ext_off.*", "ext_on", "ext_o*", "ext_off*", "ext_on.ech?", "zzz*", "ext_on.", "[a]*"],
                blocked: ["files/none", "web/web_fetch", "ext_off.x"]}
    steady:
      type: basic_agent
      agent_config:
        tools: {allowed: ["files/*", "web/nothing", "ext_on.*", "ext_off.*", "ext_on", "ext_o*", "ext_off*", "ext_on.ech?", "zzz*", "ext_on.", "[a]*"]}
    broken:
      type: basic_agent
      agent_config:
        tools: {allowed: ["+a", "b"]}
"""


def test_problems_name_what_the_editor_warns_about(db, tree, tmp_path, monkeypatch):
    (tmp_path / "config/agents/shaky.yaml").write_text(SHAKY, encoding="utf-8")
    monkeypatch.setattr(sources, "skills", lambda config: [{"name": "known", "description": ""}])
    answer = make_client(tree, started_app_or_none(tree)).get(f"{BASE}/agents", headers=as_user("root"))
    listed = {row["name"]: row for row in answer.json()["agents"]}
    assert listed["shaky"]["problems"] == [
        "Unknown model profile: nope", "Unknown model profile: ${AGENT_EDITOR_TEST_KEY}", "Unknown model profile: gone",
        "Prompt template not found: config/prompts/missing.md", "Unknown skill: ghost", "Unknown skill: spook",
        "Matches no tool: web/nothing", "Matches no tool: ext_on", "Matches no tool: ext_on.ech?", "Matches no tool: zzz*",
        "Matches no tool: ext_on.", "Matches no tool: [a]*", "Matches no tool: files/none",
        "External server is off: ext_off.*", "External server is off: ext_off*",  # an external server's tools are not listed: ext_on.* is not judged
    ]
    assert listed["steady"]["problems"] == [  # the same allowed list, other blocked
        "Matches no tool: web/nothing", "Matches no tool: ext_on", "Matches no tool: ext_on.ech?", "Matches no tool: zzz*",
        "Matches no tool: ext_on.", "Matches no tool: [a]*",
        "External server is off: ext_off.*", "External server is off: ext_off*",
    ]
    assert listed["broken"]["problems"] == [f"Does not resolve: {listed_error(answer, 'broken')}"]
    assert "mixes list merge syntax" in listed["broken"]["problems"][0]
    assert listed["helper"]["problems"] == [] and SECRET not in answer.text


def listed_error(answer, name: str) -> str:
    return next(error for error in answer.json()["errors"] if error.startswith(f"{name}: "))[len(name) + 2:]


@pytest.mark.parametrize("llm", ["llm_system:\n  default_profile: fast\n", None], ids=["no profiles", "no llm_system"])
def test_without_any_profile_every_chain_member_is_a_problem(db, tree, tmp_path, llm):
    path = tmp_path / "config/llm.yaml"
    if llm is None:
        path.unlink()  # no llm_system at all (an empty file would count as an empty one)
    else:
        path.write_text(llm, encoding="utf-8")
    assert (load_settings(str(tree)).llm_system is None) is (llm is None)
    web = make_client(tree, started_app_or_none(tree))
    assert [text for text in rows(web)["helper"]["problems"] if "profile" in text] == ["Unknown model profile: fast"]
    assert web.get(f"{BASE}/meta", headers=as_user("root")).json()["profiles"] == []


def test_patterns_for_external_servers_are_not_judged_against_the_catalogue(db, tree, tmp_path):
    (tmp_path / "config/agents/shaky.yaml").write_text(SHAKY, encoding="utf-8")
    allowed = ["files/*", "web/nothing", "ext_on.*", "ext_off.*", "ext_on", "ext_on/*", "ext_on.echo", "ext_o*", "ext_off*",
               "ext_on.ech?", "*.x", "zzz*", "ext*/*", "ext_on.", "[a]*", "ext_o*.echo*", "ext_on.x/*"]
    answer = effective(make_client(tree, started_app_or_none(tree)), name="shaky", allowed=allowed, blocked=["files/none"])
    # external tools are reached only by the exact dotted name or a * glob
    assert answer["unmatched"] == ["web/nothing", "ext_on", "ext_on/*", "ext_on.ech?", "*.x", "zzz*", "ext*/*",
                                   "ext_on.", "[a]*", "ext_on.x/*", "files/none"]
    assert answer["external"] == {"ext_on.*": True, "ext_off.*": False, "ext_on.echo": True, "ext_o*": True,
                                  "ext_off*": False, "ext_o*.echo*": True}
    assert answer["counts"]["ext_on.*"] == 0


def test_a_failing_tool_catalogue_leaves_the_list_and_says_the_patterns_went_unchecked(web, monkeypatch):
    async def failing(state):
        raise RuntimeError("registry gone")

    monkeypatch.setattr(sources, "tool_catalog", failing)
    answer = web.get(f"{BASE}/agents", headers=as_user("root")).json()
    by_name = {row["name"]: row for row in answer["agents"]}
    assert by_name["writer"]["problems"] == []
    assert answer["errors"] == ["Tool patterns were not checked, the tool catalogue failed: registry gone"]


def test_the_detail_separates_own_inherited_and_effective(web, tmp_path):
    critic = detail(web, "critic")
    assert set(critic) == DETAIL_KEYS
    assert critic["own"] == {"type": "writer", "enabled": True,
                             "agent_config": {"llm_params": {"temperature": 0.5}, "tools": {"allowed": ["+search/*"]}}}
    assert critic["parent"] == "writer" and critic["children"] == []
    assert critic["inherited"]["enabled"] is False  # the runtime reads `enabled` from the entry itself
    assert critic["effective"]["enabled"] is True
    assert critic["inherited"]["agent_config"]["tools"]["allowed"] == ["files/*", "notes"]
    assert critic["inherited"]["agent_config"]["max_steps"] == 30
    assert critic["inherited"]["agent_config"]["llm_params"] is None
    assert critic["effective"]["agent_config"]["tools"]["allowed"] == ["files/*", "notes", "search/*"]
    assert critic["effective"]["agent_config"]["llm_params"] == {"temperature": 0.5}
    assert (critic["file"], critic["version"], critic["editable"]) == (
        "config/agents/team.yaml", version_of(team(tmp_path).read_bytes()), True)
    assert critic["prompt"] == {"path": "config/agents/prompts/writer.md", "exists": True}

    writer = detail(web, "writer")
    assert (writer["parent"], writer["children"]) == (None, ["critic"])
    assert writer["inherited"]["agent_config"]["tools"]["allowed"] == ["files/*"]  # default_config
    assert writer["own"]["agent_config"]["system_template"] == "./prompts/writer.md"
    assert {row["sam"]: row["allowed"] for row in writer["spawnable"]} == {
        "blocking_sam": False, "child_sam": True, "open_sam": True, "plain_sam": True, "team_sam": True}
    assert detail(web, "helper")["prompt"] == {"path": "config/prompts/system_prompt.md", "exists": True}


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])  # utf-16: PowerShell 5.1's `>`, which the loader reads
def test_secrets_show_as_placeholders(db, tree, tmp_path, encoding):
    state = started_app(tree)
    assert state.config.plugins.servers["writer"].api_hint == f"Bearer {SECRET}", "the fixture must expand the variable"
    web = make_client(tree, state)
    critic = detail(web, "critic")
    assert critic["effective"]["api_hint"] == critic["inherited"]["api_hint"] == "Bearer ${AGENT_EDITOR_TEST_KEY}"
    writer = web.get(f"{BASE}/agents/writer", headers=as_user("root"))
    assert writer.json()["own"]["api_hint"] == "Bearer ${AGENT_EDITOR_TEST_KEY}"
    inherited = web.get(f"{BASE}/inherited", headers=as_user("root"), params={"type": "writer"})
    listed = web.get(f"{BASE}/agents", headers=as_user("root"))
    # A removed agent's dump comes from the running app; no file references the variable any more, but
    # secrets.env names it.
    path = team(tmp_path)
    path.write_bytes(path.read_bytes().split(b"    # --- a helper")[0].replace(b"    writer:", b"    old_writer:")
                     .replace(b"type: writer", b"type: old_writer")
                     .replace(b'      api_hint: "Bearer ${AGENT_EDITOR_TEST_KEY}"\r\n', b""))
    (tmp_path / "config/secrets.env").write_text("# local credentials\nAGENT_EDITOR_TEST_KEY=from-the-file\n", encoding=encoding)
    forget_snapshots(web)
    removed = web.get(f"{BASE}/agents/writer", headers=as_user("root"))
    assert removed.json()["state"] == "removed"
    assert removed.json()["effective"]["api_hint"] == "Bearer ${AGENT_EDITOR_TEST_KEY}"
    assert all(SECRET not in answer.text for answer in (writer, inherited, listed, removed))


def test_templates_show_relative_to_the_root_and_can_be_read(db, tree, tmp_path):
    state = started_app(tree)
    web = make_client(tree, state)
    critic = detail(web, "critic")
    assert critic["effective"]["agent_config"]["system_template"] == "config/agents/prompts/writer.md"
    assert critic["inherited"]["agent_config"]["system_template"] == "config/agents/prompts/writer.md"
    assert Path(state.config.plugins.servers["writer"].agent_config.system_template).is_absolute(), "the loader's form"
    parent = web.get(f"{BASE}/inherited", headers=as_user("root"), params={"type": "writer"}).json()["inherited"]
    assert parent["agent_config"]["system_template"] == "config/agents/prompts/writer.md"
    read = prompt(web, path=critic["effective"]["agent_config"]["system_template"])
    assert read.status_code == 200 and read.json()["text"] == "Write.\n"
    assert detail(web, "helper")["effective"]["agent_config"]["system_template"] == "config/prompts/system_prompt.md"
    # a copy of the inherited value into an entry is a valid template
    entry = detail(web, "helper")["own"] | {"agent_config": {"system_template": parent["agent_config"]["system_template"]}}
    assert put(web, "helper", entry, detail(web, "helper")["version"]).status_code == 200
    # a removed agent's dump comes from the running app
    path = team(tmp_path)
    path.write_bytes(path.read_bytes().replace(b"    writer:", b"    old_writer:").replace(b"type: writer", b"type: old_writer"))
    forget_snapshots(web)
    removed = detail(web, "writer")
    assert removed["state"] == "removed"
    assert removed["effective"]["agent_config"]["system_template"] == "config/agents/prompts/writer.md"


def test_inherited_answers_for_a_parent_or_a_class(web):
    as_parent = web.get(f"{BASE}/inherited", headers=as_user("root"), params={"type": "writer"}).json()["inherited"]
    writer = detail(web, "writer")["effective"]
    assert as_parent == {**writer, "enabled": False}
    as_class = web.get(f"{BASE}/inherited", headers=as_user("root"), params={"type": "basic_agent"}).json()["inherited"]
    assert (as_class["type"], as_class["enabled"], as_class["description"]) == ("basic_agent", False, None)
    assert as_class["agent_config"]["tools"]["allowed"] == ["files/*"]  # default_config
    # no type at all: the loader's default type
    untyped = web.get(f"{BASE}/inherited", headers=as_user("root")).json()["inherited"]
    assert untyped == {**as_class, "type": "basic_agent"}
    as_writer_class = web.get(f"{BASE}/inherited", headers=as_user("root"), params={"type": "agent"}).json()["inherited"]
    assert as_writer_class["type"] == "agent" and as_writer_class["enabled"] is False
    assert web.get(f"{BASE}/inherited", headers=as_user("root"), params={"type": "no_such_type"}).status_code == 404


def test_inherited_from_a_parent_that_does_not_resolve_is_a_clean_error(db, tree, tmp_path):
    (tmp_path / "config/agents/broken.yaml").write_text(
        "plugins:\n  servers:\n    broken:\n      agent_config:\n        tools: {allowed: ['+a', 'b']}\n", encoding="utf-8")
    web = make_client(tree, started_app(tree))
    answer = web.get(f"{BASE}/inherited", headers=as_user("root"), params={"type": "broken"})
    assert answer.status_code == 422 and "mixes list merge syntax" in answer.json()["detail"]


def test_a_missing_template_is_flagged_and_an_unknown_name_is_404(web, tmp_path):
    (tmp_path / "config/prompts/system_prompt.md").unlink()
    assert detail(web, "helper")["prompt"] == {"path": "config/prompts/system_prompt.md", "exists": False}
    assert web.get(f"{BASE}/agents/nobody", headers=as_user("root")).status_code == 404


def test_the_twin_detail_is_read_only(web):
    twin = detail(web, "twin")
    assert (twin["editable"], twin["version"]) == (False, None)
    assert twin["readonly_reason"].startswith("defined in 2 files")


# ---------------------------------------------------------------------- live state


def test_a_reloadable_change_needs_no_restart_only_where_the_reload_reaches(db, tree, tmp_path, reload_reaches):
    state = started_app(tree)
    web = make_client(tree, state)
    running = built_agent(state.config, "writer")
    state.tool_registry.register("writer", running)
    reload_reaches("writer", running)
    entry = detail(web, "writer")["own"]
    entry["agent_config"]["max_steps"] = 31
    assert put(web, "writer", entry, version_of(team(tmp_path).read_bytes())).status_code == 200
    writer = detail(web, "writer")
    assert (writer["state"], writer["changed"], writer["reload_fields"], writer["restart"]) == (
        "changed", ["max_steps"], ["max_steps"], False)
    # the critic inherits the change, but is not built: only a restart gives it the new value
    critic = detail(web, "critic")
    assert (critic["state"], critic["changed"], critic["reload_fields"], critic["restart"]) == (
        "changed", ["max_steps"], [], True)

    entry["agent_config"]["tools"]["allowed"].append("search/*")
    entry["metadata"]["visibility"] = "both"
    assert put(web, "writer", entry, version_of(team(tmp_path).read_bytes())).status_code == 200
    writer = detail(web, "writer")
    assert writer["changed"] == ["max_steps", "metadata.visibility", "tools.allowed"]
    assert (writer["reload_fields"], writer["restart"]) == (["max_steps"], True)


def test_a_built_agent_the_reload_does_not_reach_needs_a_restart(db, tree, tmp_path, reload_reaches):
    state = started_app(tree)
    web = make_client(tree, state)
    state.tool_registry.register("writer", built_agent(state.config, "writer"))  # built, but not in the plugin registry
    entry = detail(web, "writer")["own"]
    entry["agent_config"]["max_steps"] = 31
    assert put(web, "writer", entry, version_of(team(tmp_path).read_bytes())).status_code == 200
    writer = detail(web, "writer")
    assert (writer["changed"], writer["reload_fields"], writer["restart"]) == (["max_steps"], [], True)


def test_every_part_of_the_entry_counts_but_enabled(web, tmp_path):
    entry = detail(web, "writer")["own"]
    entry.update(description="Changed", api_hint="other", config={"depth": 2})
    entry["self_tool_descriptions"] = {"writer": "Writes"}
    assert put(web, "writer", entry, version_of(team(tmp_path).read_bytes())).status_code == 200
    writer = detail(web, "writer")
    assert writer["changed"] == ["api_hint", "config.depth", "description", "self_tool_descriptions.writer"]
    assert (writer["state"], writer["restart"]) == ("changed", True)


def test_new_removed_and_off(db, tree, tmp_path):
    state = started_app(tree)
    web = make_client(tree, state)
    created = web.post(f"{BASE}/agents", headers=as_user("root"),
                       json={"name": "scribe", "entry": {"type": "writer", "enabled": True}, "dry_run": False})
    assert created.status_code == 200, created.text
    version = version_of(team(tmp_path).read_bytes())
    critic = detail(web, "critic")["own"] | {"enabled": False}
    assert put(web, "critic", critic, version).status_code == 200
    deleted = web.request("DELETE", f"{BASE}/agents/helper", headers=as_user("root"),
                          json={"version": version_of(team(tmp_path).read_bytes()), "dry_run": False})
    assert deleted.status_code == 200, deleted.text
    state.runtime._decls["helper"] = state.runtime._decls["writer"]  # helper had been running
    listed = rows(web)
    assert {name: (row["state"], row["restart"]) for name, row in listed.items()} == {
        "scribe": ("new", True), "critic": ("removed", True), "helper": ("removed", True),
        "writer": ("in_sync", False), "demo_agent": ("off", False), "twin": ("off", False),
    }
    assert (listed["helper"]["file"], listed["helper"]["editable"]) == (None, False)
    gone = detail(web, "helper")
    assert (gone["own"], gone["editable"], gone["state"]) == (None, False, "removed")


def test_an_agent_a_plugin_offered_is_no_removed_one(db, tree):
    from dataclasses import replace

    state = started_app(tree)
    # a stategraph machine's agent: block: declared by the runtime, named in no file
    state.runtime._decls["helper_agent"] = replace(state.runtime._decls["writer"], name="helper_agent",
                                                  offered_by="stategraph_machine")

    listed = rows(make_client(tree, state))

    assert "writer" in listed and "helper_agent" not in listed, sorted(listed)


def test_a_built_agent_is_compared_by_its_own_config(db, tree, reload_reaches):
    state = started_app(tree)
    running = built_agent(load_settings(str(tree)), "writer")
    running.agent_config.max_steps = 99  # what a reload applied to the instance only
    state.tool_registry.register("writer", running)
    reload_reaches("writer", running)
    writer = detail(make_client(tree, state), "writer")
    assert (writer["state"], writer["changed"], writer["restart"]) == ("changed", ["max_steps"], False)


def test_without_a_runtime_there_is_no_live_state(db, tree):
    listed = rows(make_client(tree, SimpleState(config_path=str(tree))))
    assert {row["state"] for row in listed.values()} == {None}
    assert not any(row["restart"] for row in listed.values())


# ---------------------------------------------------------------------- pickers


def test_tools_lists_what_tool_discovery_finds(db, tree, plugin_registry):
    state = started_app(tree)
    # a hidden server of the app's registry is left out ...
    hidden = ToolServer("helper")
    hidden._tool_visible = False
    state.tool_registry.register("helper", hidden)
    # ... unless the plugin registry has it: discovery takes those without looking at the flag
    plugged = ToolServer("writer")
    plugged._tool_visible = False
    state.tool_registry.register("writer", plugged)
    plugin_registry("writer", plugged)
    # a server only the plugin registry holds is found there
    only_plugin = ToolServer("remote_ping")
    only_plugin.tools_description = f"Pings with {SECRET}"
    plugin_registry("remote", only_plugin)
    answer = make_client(tree, state).get(f"{BASE}/tools", headers=as_user("root"))
    servers = {row["server"]: row for row in answer.json()["servers"]}
    assert sorted(servers) == ["files", "remote", "search", "web", "writer"]
    assert servers["files"] == {"server": "files", "type": "file_ops", "tools": [
        {"name": "files_delete_file", "description": "does files_delete_file"},
        {"name": "files_read_file", "description": "does files_read_file"}]}
    assert servers["search"]["tools"] == [{"name": "search_web", "description": "Searches"}]
    assert servers["writer"] == {"server": "writer", "type": "basic_agent",
                                 "tools": [{"name": "writer", "description": "does writer"}]}
    assert servers["remote"]["tools"] == [{"name": "remote_ping", "description": "Pings with ${AGENT_EDITOR_TEST_KEY}"}]
    assert SECRET not in answer.text


def effective(web, **body) -> dict:
    answer = web.post(f"{BASE}/tools/effective", headers=as_user("root"), json=body)
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_effective_tools_merge_with_what_the_entry_inherits(web):
    inherited = effective(web, name="critic", allowed=None, blocked=None)
    assert set(inherited) == {"allowed", "blocked", "tools", "per_tool", "counts", "unmatched", "external"}
    assert inherited["allowed"] == ["files/*", "notes"]
    assert inherited["tools"] == ["files/files_delete_file", "files/files_read_file"]
    assert (inherited["counts"], inherited["unmatched"]) == ({"files/*": 2, "notes": 0}, ["notes"])
    assert inherited["per_tool"] == {
        "files/files_delete_file": {"allowed_by": ["files/*"], "blocked_by": []},
        "files/files_read_file": {"allowed_by": ["files/*"], "blocked_by": []},
    }

    extended = effective(web, name="critic", allowed=["+search/*", "!notes"], blocked=["files/files_delete*"])
    assert extended["allowed"] == ["files/*", "search/*"] and extended["blocked"] == ["files/files_delete*"]
    assert extended["tools"] == ["files/files_read_file", "search/search_web"]
    assert extended["counts"] == {"files/*": 2, "search/*": 1, "files/files_delete*": 1}
    assert extended["per_tool"]["files/files_delete_file"] == {"allowed_by": ["files/*"], "blocked_by": ["files/files_delete*"]}

    replaced = effective(web, name="critic", allowed=["search"])
    assert (replaced["allowed"], replaced["tools"]) == (["search"], ["search/search_web"])
    assert effective(web, type="writer", allowed=["+search/*"])["allowed"] == ["files/*", "notes", "search/*"]
    assert effective(web, allowed=["+search/*"])["allowed"] == ["files/*", "search/*"]  # default_config
    assert effective(web, name="critic", allowed=[])["tools"] == []

    mixed = web.post(f"{BASE}/tools/effective", headers=as_user("root"), json={"name": "critic", "allowed": ["+a", "b"]})
    assert mixed.status_code == 422 and "mixes list merge syntax" in mixed.json()["detail"]
    for body in ({"allowed": "files/*"}, {"name": 5, "allowed": ["*"]}, {"type": ["writer"], "allowed": ["*"]}):
        assert web.post(f"{BASE}/tools/effective", headers=as_user("root"), json=body).status_code == 422, body


def test_effective_tools_follow_the_two_passes_of_the_agent(web):
    # the server passes on the exact tool pattern; the wildcard then matches both tools
    both = effective(web, allowed=["web/web_search", "web/web_*"])
    assert both["tools"] == ["web/web_fetch", "web/web_search"]
    assert (both["counts"], both["unmatched"]) == ({"web/web_search": 1, "web/web_*": 2}, [])
    assert both["per_tool"] == {
        "web/web_fetch": {"allowed_by": ["web/web_*"], "blocked_by": []},
        "web/web_search": {"allowed_by": ["web/web_search", "web/web_*"], "blocked_by": []},
    }
    # alone, the wildcard does not let the server through: the agent gets nothing from it
    alone = effective(web, allowed=["web/web_*"])
    assert (alone["tools"], alone["per_tool"], alone["unmatched"]) == ([], {}, ["web/web_*"])
    # a blocked tool is named even where the allowed list does not grant it (the panel locks it in the allowed tree)
    narrow = effective(web, allowed=["web/web_search"], blocked=["web/web_fetch", "files/files_read*"])
    assert narrow["tools"] == ["web/web_search"]
    assert (narrow["counts"], narrow["unmatched"]) == (
        {"web/web_search": 1, "web/web_fetch": 1, "files/files_read*": 1}, [])
    assert narrow["per_tool"] == {
        "files/files_read_file": {"allowed_by": [], "blocked_by": ["files/files_read*"]},
        "web/web_fetch": {"allowed_by": [], "blocked_by": ["web/web_fetch"]},
        "web/web_search": {"allowed_by": ["web/web_search"], "blocked_by": []},
    }
    # blocking works on the tools the allowed list let through; a pattern that names no tool is unmatched
    blocked = effective(web, allowed=["web/*"], blocked=["web/web_fetch", "nothing/*"])
    assert blocked["tools"] == ["web/web_search"]
    assert (blocked["counts"], blocked["unmatched"]) == ({"web/*": 2, "web/web_fetch": 1, "nothing/*": 0}, ["nothing/*"])


def test_a_null_type_means_no_own_type(web):
    # critic's own type on disk is writer; without its own type the entry is a plain basic_agent
    assert effective(web, name="critic", allowed=["+search/*"])["allowed"] == ["files/*", "notes", "search/*"]
    assert effective(web, name="critic", type=None, allowed=["+search/*"])["allowed"] == ["files/*", "search/*"]


def test_effective_lists_show_placeholders(db, tree, tmp_path):
    # the lists a child inherits are the loader's, with the variable expanded
    (tmp_path / "config/agents/secretive.yaml").write_text(
        "plugins:\n  servers:\n    secretive:\n      agent_config:\n"
        "        tools: {allowed: ['files/${AGENT_EDITOR_TEST_KEY}'], blocked: ['x_${AGENT_EDITOR_TEST_KEY}']}\n"
        "    secret_child:\n      type: secretive\n", encoding="utf-8")
    web = make_client(tree, started_app(tree))
    answer = web.post(f"{BASE}/tools/effective", headers=as_user("root"), json={"name": "secret_child"})
    assert answer.json()["allowed"] == ["files/${AGENT_EDITOR_TEST_KEY}"]
    assert answer.json()["blocked"] == ["x_${AGENT_EDITOR_TEST_KEY}"]
    assert answer.json()["counts"] == {"files/${AGENT_EDITOR_TEST_KEY}": 0, "x_${AGENT_EDITOR_TEST_KEY}": 0}
    assert SECRET not in answer.text



def test_rules_and_load_errors_show_placeholders(db, tree, tmp_path, monkeypatch):
    # the pattern that decides and a broken list both carry a variable's value once the loader has read them
    monkeypatch.setenv("AGENT_EDITOR_TEST_CLASS", "rstuvwxyz")
    (tmp_path / "config/agents/leaky.yaml").write_text(
        "plugins:\n  servers:\n    leaky_sam:\n      type: sub_agent_manager\n      enabled: true\n"
        "      allowed_agents: ['w[${AGENT_EDITOR_TEST_CLASS}]iter']\n"
        "    leaky_child:\n      type: writer\n"
        "      agent_config: {tools: {allowed: ['+x', 'files/${AGENT_EDITOR_TEST_KEY}']}}\n", encoding="utf-8")
    web = make_client(tree, started_app(tree))
    listed = web.get(f"{BASE}/agents", headers=as_user("root"))
    writer = web.get(f"{BASE}/agents/writer", headers=as_user("root"))
    assert any("leaky_child" in error for error in listed.json()["errors"]), "the broken list must be reported"
    leaky = next(row for row in writer.json()["spawnable"] if row["sam"] == "leaky_sam")
    assert (leaky["allowed"], leaky["rule"]) == (True, "allowed_agents: w[${AGENT_EDITOR_TEST_CLASS}]iter")
    assert SECRET not in listed.text and "rstuvwxyz" not in writer.text

def test_a_masked_list_sent_back_matches_as_the_loader_reads_it(db, tree, tmp_path, monkeypatch):
    # "Override" copies the inherited list, placeholders and all, into the entry: it must still match the same tools
    monkeypatch.setenv("AGENT_EDITOR_TEST_TOOL", "files_read_file")
    (tmp_path / "config/agents/placeholder.yaml").write_text(
        "plugins:\n  servers:\n    placeholder:\n      agent_config:\n"
        "        tools: {allowed: ['files/${AGENT_EDITOR_TEST_TOOL}']}\n"
        "    placeholder_child:\n      type: placeholder\n", encoding="utf-8")
    web = make_client(tree, started_app(tree))
    inherited = web.post(f"{BASE}/tools/effective", headers=as_user("root"), json={"name": "placeholder_child"}).json()
    assert inherited["allowed"] == ["files/${AGENT_EDITOR_TEST_TOOL}"] and inherited["tools"] == ["files/files_read_file"]
    overridden = web.post(f"{BASE}/tools/effective", headers=as_user("root"),
                          json={"name": "placeholder_child", "allowed": inherited["allowed"]}).json()
    assert overridden["tools"] == ["files/files_read_file"]
    assert overridden["unmatched"] == []

def managers(web) -> dict:
    answer = web.get(f"{BASE}/managers", headers=as_user("root"))
    assert answer.status_code == 200, answer.text
    return {row["name"]: row for row in answer.json()["managers"]} | {"": answer.json()["agents"]}


def test_managers_list_their_lists_and_the_agents_they_start(web):
    found = managers(web)
    assert found[""] == ["critic", "demo_agent", "helper", "twin", "writer"]
    assert sorted(set(found) - {""}) == ["blocking_sam", "child_sam", "open_sam", "plain_sam", "team_sam"]
    team = found["team_sam"]
    assert team["file"] == "config/plugins.yaml" and team["editable"] and team["own"]["allowed_agents"] == ["writer", "helper"]
    assert team["spawns"] == ["helper", "writer"] and team["allowed"] == ["writer", "helper"] and team["blocked"] == []
    # the manager's own rule: blocked first, then `*`
    assert found["open_sam"]["spawns"] == ["demo_agent", "helper", "twin", "writer"]
    assert found["blocking_sam"]["spawns"] == ["demo_agent", "helper", "twin"]
    assert found["child_sam"]["spawns"] == ["critic", "helper", "writer"]


def test_a_manager_is_saved_through_its_own_entry(web, tmp_path):
    team = managers(web)["team_sam"]
    entry = {**team["own"], "max_nesting_depth": 2}
    url = f"{BASE}/managers/team_sam"
    before = plugins_file(tmp_path).read_bytes()
    preview = web.put(url, headers=as_user("root"), json={"entry": entry, "version": team["version"], "dry_run": True})
    assert preview.status_code == 200 and "+      max_nesting_depth: 2" in preview.json()["diff"]
    assert plugins_file(tmp_path).read_bytes() == before
    written = web.put(url, headers=as_user("root"), json={"entry": entry, "version": team["version"]})
    assert written.status_code == 200 and "max_nesting_depth: 2" in plugins_file(tmp_path).read_text(encoding="utf-8")
    assert managers(web)["team_sam"]["own"]["max_nesting_depth"] == 2
    # not the type or the switch, and only a manager
    for change in ({"enabled": False}, {"type": "basic_agent"}):
        refused = web.put(url, headers=as_user("root"), json={"entry": {**entry, **change}, "version": written.json()["version"]})
        assert refused.status_code == 400, change
    writer = detail(web, "writer")
    assert web.put(f"{BASE}/managers/writer", headers=as_user("root"),
                   json={"entry": writer["own"], "version": writer["version"]}).status_code == 404


def test_a_new_manager_gets_its_own_file(web, tmp_path):
    body = {"name": "scribe_sam", "allowed_agents": ["writer"]}
    preview = web.post(f"{BASE}/managers", headers=as_user("root"), json={**body, "dry_run": True})
    assert preview.status_code == 200 and "type: sub_agent_manager" in preview.json()["diff"]
    assert not (tmp_path / "config/agents/scribe_sam.yaml").exists()
    created = web.post(f"{BASE}/managers", headers=as_user("root"), json=body)
    assert created.status_code == 200 and created.json()["file"] == "config/agents/scribe_sam.yaml"
    scribe = managers(web)["scribe_sam"]
    assert scribe["spawns"] == ["writer"] and scribe["own"] == {"type": "sub_agent_manager", "enabled": True,
                                                                 "allowed_agents": ["writer"]}
    for bad in ({"name": "other_sam", "allowed_agents": "writer"}, {"name": "other_sam", "allowed_agents": [""]}):
        assert web.post(f"{BASE}/managers", headers=as_user("root"), json=bad).status_code == 422
    assert web.post(f"{BASE}/managers", headers=as_user("root"), json=body).status_code == 400  # taken


def test_a_placeholder_of_a_variable_the_config_does_not_name_stays_as_sent(web, monkeypatch):
    monkeypatch.setenv("AGENT_EDITOR_UNNAMED", "unnamed-secret-456")
    answer = effective(web, name="writer", allowed=["files/*", "x_${AGENT_EDITOR_UNNAMED}"],
                       blocked=["${AGENT_EDITOR_UNNAMED}"])
    assert "unnamed-secret-456" not in str(answer)
    assert answer["allowed"] == ["files/*", "x_${AGENT_EDITOR_UNNAMED}"] and "x_${AGENT_EDITOR_UNNAMED}" in answer["unmatched"]


def test_the_loader_report_stays_quiet_in_requests(db, tree, tmp_path, caplog):
    (tmp_path / "config/agents/loops.yaml").write_text(
        "plugins:\n  servers:\n    loop_a:\n      type: loop_b\n    loop_b:\n      type: loop_a\n", encoding="utf-8")
    web = make_client(tree, started_app(tree))
    caplog.set_level(logging.INFO, logger=load_settings.__module__)
    caplog.clear()
    assert web.get(f"{BASE}/inherited", headers=as_user("root"), params={"type": "loop_a"}).status_code in (200, 422)
    assert web.post(f"{BASE}/tools/effective", headers=as_user("root"),
                    json={"name": "loop_a", "allowed": ["files/*"]}).status_code in (200, 422)
    # the loader's own records only: an earlier test may have httpx log request URLs, which name loop_a too
    loader = load_settings.__module__
    assert not [record for record in caplog.records if record.name.startswith(loader) and "loop_a" in record.getMessage()]


def test_a_write_error_hides_a_value_only_the_files_name(db, tree, tmp_path):
    # the new child inherits a template path built from a variable; the sent entry names none
    (tmp_path / "config/agents/templated.yaml").write_text(
        "plugins:\n  servers:\n    templated:\n      type: basic_agent\n"
        "      agent_config:\n        system_template: \"prompts/${AGENT_EDITOR_TEST_KEY}.md\"\n", encoding="utf-8")
    web = make_client(tree, started_app(tree))
    answer = web.post(f"{BASE}/agents", headers=as_user("root"),
                      json={"name": "kid", "entry": {"type": "templated", "enabled": True}, "dry_run": False})
    assert answer.status_code == 422 and "prompts/${AGENT_EDITOR_TEST_KEY}.md" in answer.json()["detail"]
    assert SECRET not in answer.text


def test_a_load_error_hides_a_value_the_files_name(db, tree, tmp_path):
    (tmp_path / "config/agents/broken_template.yaml").write_text(
        "plugins:\n  servers:\n    broken_template:\n      type: basic_agent\n"
        "      agent_config:\n        system_template: \"${AGENT_EDITOR_TEST_KEY}.html\"\n", encoding="utf-8")
    web = make_client(tree, started_app_or_none(tree))
    answer = web.get(f"{BASE}/agents", headers=as_user("root"))
    assert answer.status_code == 500 and "${AGENT_EDITOR_TEST_KEY}.html" in answer.json()["detail"]
    assert SECRET not in answer.text


def test_an_entry_json_cannot_carry_is_shown_as_text_and_locked(db, tree, tmp_path):
    (tmp_path / "config/agents/odd.yaml").write_text(
        "plugins:\n  servers:\n    odd:\n      type: basic_agent\n      since: 2026-09-17\n      limit: .inf\n",
        encoding="utf-8")
    web = make_client(tree, started_app(tree))
    odd = detail(web, "odd")
    assert odd["own"]["since"] == "2026-09-17" and odd["own"]["limit"] == "inf"
    assert odd["editable"] is True and "the form cannot carry" in odd["form_reason"]
    assert rows(web)["odd"]["editable"] is False and "the form cannot carry" in rows(web)["odd"]["readonly_reason"]


def test_errors_show_no_environment_value(web, monkeypatch):
    monkeypatch.setenv("AGENT_EDITOR_UNNAMED", "unnamed-secret-456")
    monkeypatch.setenv("AGENT_EDITOR_UNRELATED", "list merge syntax")  # named nowhere: its words stay
    # a write expands every name, and the merge error quotes the plain items it found
    writer = detail(web, "writer")
    config = writer["own"]["agent_config"]
    entry = {**writer["own"], "agent_config": {**config, "tools": {"allowed": ["+x", "${AGENT_EDITOR_UNNAMED}"]}}}
    answer = put(web, "writer", entry, writer["version"])
    assert answer.status_code == 422 and "${AGENT_EDITOR_UNNAMED}" in answer.json()["detail"]
    assert "unnamed-secret-456" not in answer.text and "list merge syntax" in answer.json()["detail"]
    # the preview expands a name the config knows: its value is hidden in the error, too
    answer = web.post(f"{BASE}/tools/effective", headers=as_user("root"),
                      json={"name": "writer", "allowed": ["+x", "${AGENT_EDITOR_TEST_KEY}"]})
    assert answer.status_code == 422 and "${AGENT_EDITOR_TEST_KEY}" in answer.json()["detail"]
    assert SECRET not in answer.text


async def test_meta_names_classes_profiles_hooks_and_prompt_files(web, monkeypatch):
    registry = HookRegistry()
    await registry.register_hook(HookType.PRE_LLM_CALL, "files.guard", object(), order_spec={"after": ["x.y"]},
                                 timeout=5.0, description="Guards the files")
    await registry.register_hook(HookType.POST_TOOL_CALL, "files.guard", object(), order_spec={"after": ["x.y"]},
                                 timeout=5.0, description="Guards the files")
    monkeypatch.setattr(sources, "get_hook_registry", lambda: registry)
    meta = web.get(f"{BASE}/meta", headers=as_user("root")).json()
    assert {"name": "basic_agent", "description": "Basic agent plugin providing agent execution capabilities as tools"} in meta["classes"]
    assert "agent" in [row["name"] for row in meta["classes"]]
    assert meta["profiles"] == [
        {"name": "fast", "model_ref": "m-fast", "provider": "ollama", "model": "fast-model", "description": "Fast one"},
        {"name": "slow", "model_ref": "m-slow", "provider": "ollama", "model": "slow-model", "description": "slow"},
    ]
    assert "default_profile" not in meta  # no chain falls back to it
    assert meta["hooks"] == [{"name": "files.guard", "types": ["post_tool_call", "pre_llm_call"], "description": "Guards the files",
                              "enabled": True, "order": {"after": ["x.y"], "before": []}, "timeout": 5.0}]
    assert meta["prompt_files"] == ["config/agents/prompts/writer.md", "config/prompts/system_prompt.md",
                                    "src/plugins/demo/agents/prompts/demo.md"]
    assert meta["visibility"] == ["ui", "tool", "both", "private"]
    assert meta["sub_agents"] is True
    assert meta["reload_fields"] == list(Agent._RELOADABLE_AGENT_FIELDS)
    assert all(set(skill) == {"name", "description"} for skill in meta["skills"])


def test_meta_scans_the_configured_skill_roots(db, tmp_path):
    skill = tmp_path / "skills" / "house-style"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: house-style\ndescription: How we write\n---\nBody\n", encoding="utf-8")
    tree = build_tree(tmp_path)
    config = tree.read_text(encoding="utf-8") + f"skills:\n  skill_dirs:\n    - \"{(tmp_path / 'skills').as_posix()}\"\n"
    tree.write_text(config, encoding="utf-8")
    meta = make_client(tree, started_app(tree)).get(f"{BASE}/meta", headers=as_user("root")).json()
    assert meta["skills"] == [{"name": "house-style", "description": "How we write"}]


def prompt(web, **params):
    return web.get(f"{BASE}/prompt", headers=as_user("root"), params=params)


def test_prompt_reads_files_inside_the_root_only(web, tmp_path):
    shared = prompt(web, path="config/prompts/system_prompt.md").json()
    assert shared == {"path": "config/prompts/system_prompt.md", "exists": True, "text": "You are {{ name }}.\n"}
    relative = prompt(web, path="./prompts/writer.md", agent="writer").json()
    assert relative == {"path": "config/agents/prompts/writer.md", "exists": True, "text": "Write.\n"}
    assert prompt(web, path="./prompts/demo.md", agent="demo_agent").json()["path"] == "src/plugins/demo/agents/prompts/demo.md"
    assert prompt(web, path="config/prompts/missing.md").json() == {"path": "config/prompts/missing.md", "exists": False, "text": ""}
    for path in ["../../../outside.md", str(tmp_path.parent / "outside.md"), "./../../../outside.md",
                 "config/plugins.yaml", "config/prompts/system_prompt.md.bak", "config/prompts/a\x00.md"]:
        answer = prompt(web, path=path, agent="writer")
        assert answer.status_code == 400, path
    long = tmp_path / "config/prompts/long.md"
    long.write_text("x" * 250_000, encoding="utf-8")
    assert len(prompt(web, path="config/prompts/long.md").json()["text"]) == 200_000


@pytest.mark.parametrize("path", [
    "//evil-host/share/p.md", "\\\\evil-host\\share\\p.md", "/evil-host/p.md", "\\evil-host\\p.md",
    "C:/evil-host/p.md", "c:evil-host.md", "./\\\\evil-host\\share\\p.md", "config//evil-host.md",
    "config/prompts/evil-host.md:stream",
])
def test_prompt_refuses_network_and_drive_paths_before_touching_them(web, monkeypatch, path):
    from pathlib import Path as RealPath
    real = RealPath.resolve

    def guarded(self, strict=False):
        assert "evil-host" not in str(self), f"resolve() reached {self}"
        return real(self, strict)

    monkeypatch.setattr(RealPath, "resolve", guarded)
    for agent in (None, "writer"):
        answer = prompt(web, path=path, **({"agent": agent} if agent else {}))
        assert answer.status_code == 400, (path, agent, answer.text)


def test_prompt_reads_no_more_than_it_answers(web, tmp_path, monkeypatch):
    from pathlib import Path as RealPath
    sizes = []
    real_open = RealPath.open

    class Counting:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.handle.__exit__(*exc)

        def read(self, size=-1):
            sizes.append(size)
            return self.handle.read(size)

    def counting_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        return Counting(handle) if self.name == "long.md" else handle

    long = tmp_path / "config/prompts/long.md"
    long.write_text("x" * 250_000, encoding="utf-8")
    monkeypatch.setattr(RealPath, "open", counting_open)
    assert len(prompt(web, path="config/prompts/long.md").json()["text"]) == 200_000
    assert sizes and all(0 < size <= 200_001 for size in sizes), sizes


def test_yaml_renders_and_parses_an_entry(web):
    entry = {"type": "writer", "description": "yes", "agent_config": {"tools": {"allowed": ["+search/*"]}}}
    text = web.post(f"{BASE}/yaml", headers=as_user("root"), json={"entry": entry}).json()["yaml"]
    assert "description: 'yes'" in text
    assert web.post(f"{BASE}/yaml/parse", headers=as_user("root"), json={"yaml": text}).json() == {"entry": entry}
    assert web.post(f"{BASE}/yaml/parse", headers=as_user("root"), json={"yaml": ""}).json() == {"entry": {}}
    broken = web.post(f"{BASE}/yaml/parse", headers=as_user("root"), json={"yaml": "a: [1"})
    listed = web.post(f"{BASE}/yaml/parse", headers=as_user("root"), json={"yaml": "- a"})
    assert (broken.status_code, listed.status_code) == (422, 422)
    assert "line" in broken.json()["detail"]
    impossible = web.post(f"{BASE}/yaml/parse", headers=as_user("root"), json={"yaml": "a: 2001-02-30\n"})
    assert impossible.status_code == 422 and "ValueError" in impossible.json()["detail"]


@pytest.mark.parametrize("text, detail", [
    ("a: .nan\n", "yaml.a: nan is not a number JSON can carry"),
    ("a: [1, .inf]\n", "yaml.a[1]: inf is not a number JSON can carry"),
    ("a: !!binary gA==\n", "yaml.a: a bytes is not supported here (quote it)"),
    ("a: 2001-02-03\n", "yaml.a: a date is not supported here (quote it)"),
    ("a: !!set {x}\n", "yaml.a: a set is not supported here (quote it)"),
    ("a:\n  1: x\n", "yaml.a: the key 1 must be text (quote it)"),
])
def test_yaml_that_would_not_come_back_the_same_is_refused(web, text, detail):
    answer = web.post(f"{BASE}/yaml/parse", headers=as_user("root"), json={"yaml": text})
    assert (answer.status_code, answer.json()["detail"]) == (422, detail)


def test_an_unquoted_removal_entry_is_read_as_the_entry(web):
    """`- !files/*` is a YAML tag; typed into a tool list it means the removal, and it comes back quoted."""
    text = "agent_config:\n  tools:\n    allowed:\n      - +web/*\n      - !files/*\n      - !web/web_[fs]*\n"
    answer = web.post(f"{BASE}/yaml/parse", headers=as_user("root"), json={"yaml": text})
    assert answer.json() == {"entry": {"agent_config": {"tools": {"allowed": ["+web/*", "!files/*", "!web/web_[fs]*"]}}}}
    rendered = web.post(f"{BASE}/yaml", headers=as_user("root"), json=answer.json()).json()["yaml"]
    assert "- '!files/*'" in rendered


@pytest.mark.parametrize("text, tag", [
    ("a: [!web/*, +files/*]\n", "!web/*,"),  # the comma belongs to the tag: never one entry "!web/*, +files/*"
    ("a: [!web/*, ]\n", "!web/*,"),  # ... also with nothing after it
    ("a: [!web/*] ]\n", "!web/*]"),  # ... and so does a closing bracket before a space
    ("a:\n  - !web/* files\n", "!web/*"),
    ("a: !thing {b: 1}\n", "!thing"),
])
def test_a_tag_that_is_no_lone_entry_is_refused_with_the_way_out(web, text, tag):
    answer = web.post(f"{BASE}/yaml/parse", headers=as_user("root"), json={"yaml": text})
    assert answer.status_code == 422
    assert f"the tag {tag} is not supported here" in answer.json()["detail"] and "'!web/*'" in answer.json()["detail"]


@pytest.mark.parametrize("text", [
    "a: &shared [1, 2]\nb: *shared\n",
    "a: &loop\n  b: *loop\n",
    "base: &base {x: 1}\nchild:\n  <<: *base\n  y: 2\n",
])
def test_yaml_with_anchors_is_refused(web, text):
    answer = web.post(f"{BASE}/yaml/parse", headers=as_user("root"), json={"yaml": text})
    assert answer.status_code == 422, answer.text
    assert answer.json()["detail"] == "YAML anchors and aliases are not supported here"


# ---------------------------------------------------------------------- writes over HTTP


def test_save_over_http_dry_run_write_conflict_and_rollback(web, tmp_path):
    path = team(tmp_path)
    before = path.read_bytes()
    entry = detail(web, "writer")["own"]
    entry["description"] = "Writes better"
    dry = put(web, "writer", entry, version_of(before), dry_run=True)
    assert dry.status_code == 200 and dry.json() == {"diff": dry.json()["diff"]}
    assert '+      description: "Writes better"   # inline comment\n' in dry.json()["diff"]
    assert path.read_bytes() == before

    written = put(web, "writer", entry, version_of(before))
    assert written.status_code == 200 and written.json()["version"] == version_of(path.read_bytes())
    assert detail(web, "writer")["own"]["description"] == "Writes better"

    stale = put(web, "writer", entry | {"description": "again"}, version_of(before))
    assert stale.status_code == 409 and "changed on disk; reload" in stale.json()["detail"]

    current = path.read_bytes()
    invalid = put(web, "writer", entry | {"agent_config": {"llm_profile": "nope"}}, version_of(current))
    assert invalid.status_code == 422 and "llm profile 'nope' does not exist" in invalid.json()["detail"]
    assert path.read_bytes() == current
    read_only = put(web, "twin", {"type": "basic_agent"}, "x")
    assert read_only.status_code == 400 and "read-only" in read_only.json()["detail"]


def test_create_delete_and_spawnable_over_http(web, tmp_path):
    body = {"name": "demo_copy", "entry": detail(web, "demo_agent")["own"], "source": "demo_agent"}
    dry = web.post(f"{BASE}/agents", headers=as_user("root"), json={**body, "dry_run": True}).json()
    assert set(dry) == {"name", "file", "diff"} and not (tmp_path / "config/agents/demo_copy.yaml").exists()
    created = web.post(f"{BASE}/agents", headers=as_user("root"), json={**body, "dry_run": False})
    assert created.status_code == 200 and set(created.json()) == {"name", "file", "diff", "version"}
    assert detail(web, "demo_copy")["prompt"] == {"path": "src/plugins/demo/agents/prompts/demo.md", "exists": True}
    taken = web.post(f"{BASE}/agents", headers=as_user("root"), json={**body, "name": "file_ops"})
    assert taken.status_code == 400

    sam = plugins_file(tmp_path)
    allowed = web.put(f"{BASE}/agents/demo_copy/spawnable", headers=as_user("root"),
                      json={"sam": "team_sam", "allowed": True, "version": version_of(sam.read_bytes())})
    assert allowed.status_code == 200 and "+        - demo_copy\n" in allowed.json()["diff"]
    assert {row["sam"]: row["allowed"] for row in detail(web, "demo_copy")["spawnable"]}["team_sam"] is True
    wrong = web.put(f"{BASE}/agents/demo_copy/spawnable", headers=as_user("root"),
                    json={"sam": "team_sam", "allowed": "yes", "version": version_of(sam.read_bytes())})
    assert wrong.status_code == 422

    file = tmp_path / "config/agents/demo_copy.yaml"
    deleted = web.request("DELETE", f"{BASE}/agents/demo_copy", headers=as_user("root"),
                          json={"version": version_of(file.read_bytes()), "dry_run": False})
    assert deleted.status_code == 200
    assert (deleted.json()["deleted_file"], deleted.json()["notes"]) == (True, ["team_sam still lists demo_copy in allowed_agents"])
    assert not file.exists()
    refused = web.request("DELETE", f"{BASE}/agents/writer", headers=as_user("root"),
                          json={"version": version_of(team(tmp_path).read_bytes())})
    assert refused.status_code == 400 and "critic" in refused.json()["detail"]
