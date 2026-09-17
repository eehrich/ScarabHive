"""The Agent Editor panel in a real browser, against the real plugin: its router behind the app's route security, its
static files, a users database, a config tree under tmp_path and two registered hooks, ``probe.mark`` and ``probe.trace``.

The config tree (``config/config.yaml`` includes ``llm.yaml``, ``plugins.yaml``, ``agents/*.yaml`` and
``../src/plugins*/*/agents/*.yaml``):

- ``llm.yaml``: the profiles ``fast`` (ollama small-1), ``smart`` (openai large-2) and ``deep`` (anthropic deep-3).
- ``plugins.yaml``: ``default_config`` (``llm_profile: [fast]``, ``max_steps: 20``, the template
  ``config/prompts/base.md``, no tools), the sub-agent manager ``manager`` (allows ``writer``), the tool servers
  ``files`` and ``web``, and the agent ``twin``.
- ``agents/team.yaml``: ``writer`` (own model chain, ``max_steps: 30``, ``files/*``) and its child ``editor``
  (``+web/web_search``, a description in markup), with comments.
- ``agents/extra.yaml``: ``scout`` (disabled, an inline prompt) and ``twin`` a second time: read-only.
- ``src/plugins/demo/agents/demo.yaml``: ``demo``, in its own group, with the template ``./prompts/demo.md``.
- ``skills/``: the skills ``alpha`` and ``beta``; ``writer`` takes ``beta`` on demand, ``editor`` has the bare list
  ``[alpha]``.

The running app, as the plugin sees it: every enabled server declared except ``demo`` (new, needs a restart);
``writer`` runs unbuilt with ``max_steps: 25`` (changed; unbuilt, only a restart applies that). Built are only the
tool servers ``files`` (``files_read``, ``files_write``) and ``web`` (``web_search``, ``web_fetch``). A second
instance, ``ae_off``, runs with authentication off.

Behind the panel's back: GET /__stub/token?user= signs a token; GET /__stub/file?path= reads a file of the tree
(``{exists, text}``); POST /__stub/replace?path=&old=&new= changes one.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from agent_system.auth import database
from agent_system.auth.models import UserCreate, UserRole
from agent_system.auth.security import create_access_token
from agent_system.config.models import AgentSystemConfig, AuthConfig, MCPConfig
from agent_system.config.settings import get_mcp_config_by_name, load_settings
from agent_system.hooks import registry as hook_registry
from agent_system.hooks.plugin_hook import HookType, PluginHook
from agent_system.mcp.base import MCPRegistry, MCPServer
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.runtime import ServerDecl, ServerView
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 180
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[3]
AGENTS = ("writer", "editor", "scout", "twin", "demo")


def config_files(root: Path) -> dict[str, str]:
    """The config tree, path (relative to the root) -> text. Every YAML file round-trips byte-exact."""
    plugins = (REPO / "src" / "plugins").as_posix()
    return {
        "config/config.yaml": (
            "name: agent editor test\n"
            "includes:\n"
            "  - llm.yaml\n"
            "  - plugins.yaml\n"
            "  - agents/*.yaml\n"
            "  - ../src/plugins*/*/agents/*.yaml\n"
            "skills:\n"
            "  skill_dirs:\n"
            f"    - {(root / 'skills').as_posix()}\n"
        ),
        "config/llm.yaml": (
            "llm_system:\n"
            "  default_profile: fast\n"
            "  models:\n"
            "    small-model:\n"
            "      provider: ollama\n"
            "      model: small-1\n"
            "    large-model:\n"
            "      provider: openai\n"
            "      model: large-2\n"
            "    deep-model:\n"
            "      provider: anthropic\n"
            "      model: deep-3\n"
            "  profiles:\n"
            "    fast:\n"
            "      model_ref: small-model\n"
            "      description: Quick answers\n"
            "    smart:\n"
            "      model_ref: large-model\n"
            "      description: Careful answers\n"
            "    deep:\n"
            "      model_ref: deep-model\n"
            "      description: Long thinking\n"
        ),
        "config/plugins.yaml": (
            "plugins:\n"
            "  plugin_dirs:\n"
            f"    - {plugins}\n"
            "  default_config:\n"
            "    type: basic_agent\n"
            "    enabled: false\n"
            "    agent_config:\n"
            "      llm_profile: [fast]\n"
            "      max_steps: 20\n"
            "      system_template: config/prompts/base.md\n"
            "      tools:\n"
            "        allowed: []\n"
            "        blocked: []\n"
            "  servers:\n"
            "    manager:\n"
            "      type: sub_agent_manager\n"
            "      enabled: true\n"
            "      allowed_agents: [writer]\n"
            "    files:\n"
            "      type: example\n"
            "      enabled: true\n"
            "    web:\n"
            "      type: example\n"
            "      enabled: true\n"
            "    twin:\n"
            "      type: basic_agent\n"
            "      enabled: true\n"
            "      description: Defined twice\n"
        ),
        "config/agents/team.yaml": (
            "# Agents of the writing team.\n"
            "plugins:\n"
            "  servers:\n"
            "    # The base writer: everything the team shares.\n"
            "    writer:\n"
            "      type: basic_agent\n"
            "      enabled: true\n"
            "      description: Writes things\n"
            "      metadata:\n"
            "        visibility: ui\n"
            "        category: writing\n"
            "        tags: [prose]\n"
            "      agent_config:\n"
            "        llm_profile: [smart, fast]\n"
            "        max_steps: 30  # more than the default\n"
            "        skills:\n"
            "          on_demand: [beta]\n"
            "        tools:\n"
            "          allowed: [files/*]\n"
            "    # A writer that also searches the web.\n"
            "    editor:\n"
            "      type: writer\n"
            "      enabled: true\n"
            "      description: <img src=x onerror=parent.__xss=1>\n"
            "      agent_config:\n"
            "        skills: [alpha]\n"
            "        tools:\n"
            "          allowed: [+web/web_search]\n"
        ),
        "config/agents/extra.yaml": (
            "# Agents that are not part of the team.\n"
            "plugins:\n"
            "  servers:\n"
            "    scout:\n"
            "      type: basic_agent\n"
            "      enabled: false\n"
            "      description: Looks around\n"
            "      agent_config:\n"
            "        system_prompt: You scout.\n"
            "    twin:\n"
            "      description: Defined here too\n"
        ),
        "src/plugins/demo/agents/demo.yaml": (
            "plugins:\n"
            "  servers:\n"
            "    demo:\n"
            "      type: basic_agent\n"
            "      enabled: true\n"
            "      description: Shows the plugin group\n"
            "      agent_config:\n"
            "        system_template: ./prompts/demo.md\n"
        ),
        "src/plugins/demo/agents/prompts/demo.md": "You are the demo.\n",
        "config/prompts/base.md": "You are a base agent.\n",
        "skills/alpha/SKILL.md": "---\nname: alpha\ndescription: The first skill\n---\nAlpha.\n",
        "skills/beta/SKILL.md": "---\nname: beta\ndescription: The second skill\n---\nBeta.\n",
    }


class ToolServer(MCPServer):
    """A built tool server: its tools carry the instance prefix, as every plugin's do."""

    def __init__(self, name: str, tools: dict[str, str]):
        super().__init__(name, AgentSystemConfig(), MCPConfig(type="example", enabled=True))
        self._tools = tools

    def get_tools(self):
        return [{"type": "function", "function": {"name": f"{self.name}_{tool}", "description": text,
                                                  "parameters": {"type": "object", "properties": {}}}}
                for tool, text in self._tools.items()]


class RuntimeStandIn:
    """What the running app declared: ``declarations``, ``describe`` and ``view`` like ``agent_system.runtime.Runtime``."""

    def __init__(self, declarations: dict[str, ServerDecl]):
        self._decls = declarations

    def declarations(self):
        return dict(self._decls)

    def describe(self, name):
        return self._decls.get(name)

    def view(self, name):
        decl = self._decls.get(name)
        if decl is None:
            return None
        agent = name in AGENTS
        return ServerView(name=name, is_agent=agent, mcp_public=agent, mcp_tool_visible=False, built=False)


def running_app(config, as_started: bool = True) -> RuntimeStandIn:
    """As started, writer runs with other max_steps than its file says; after a reload with what the file says."""
    declarations = {}
    for name, server in config.plugins.servers.items():
        if not server.enabled or name == "demo":
            continue
        merged = get_mcp_config_by_name(name, config)
        if name == "writer" and as_started:
            merged = merged.model_copy(update={
                "agent_config": merged.agent_config.model_copy(update={"max_steps": 25})})
        declarations[name] = ServerDecl(name=name, type=merged.type, mcp_config=merged, factory=None,
                                        plugin_metadata={"lazy": True} if name in AGENTS else None)
    return RuntimeStandIn(declarations)


def panel_app(root: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from plugins.agent_editor.plugin import PLUGIN_FACTORY

    for relative, text in config_files(root).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    config_path = root / "config" / "config.yaml"

    db = database.UserDatabase(root / "users.db")
    monkeypatch.setattr(database, "_db", db)
    db.create_user(UserCreate(username="root", email="root@example.com", password="correct-horse", role=UserRole.ADMIN))

    hooks = hook_registry.HookRegistry()  # the process registry stays untouched
    asyncio.run(hooks.register_hook(HookType.PRE_LLM_CALL, "probe.mark", PluginHook("probe.mark"),
                                    description="Marks every call"))
    asyncio.run(hooks.register_hook(HookType.POST_LLM_CALL, "probe.trace", PluginHook("probe.trace"),
                                    description="Traces every answer"))
    asyncio.run(hooks.register_hook(HookType.PRE_TOOL_CALL, "probe.mark", PluginHook("probe.mark"),
                                    description="Marks every call"))
    monkeypatch.setattr(hook_registry, "_global_hook_registry", hooks)

    auth = AuthConfig(enabled=True)
    auth.endpoint_security.audit_enabled = False
    config = load_settings(str(config_path))
    registry = MCPRegistry()
    registry.register("files", ToolServer("files", {"read": "Read a file", "write": "Write a file"}))
    registry.register("web", ToolServer("web", {"search": "Search the web", "fetch": "Fetch a page"}))

    app = FastAPI()
    app.state.config = config
    app.state.config_path = str(config_path)
    app.state.runtime = running_app(config)
    app.state.mcp_registry = registry
    app.state.agent = None

    @app.get("/__stub/token")
    async def token(user: str):
        return create_access_token({"sub": user, "user_id": db.get_user_by_username(user).id, "role": "admin"})

    def inside(relative: str) -> Path:
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            raise HTTPException(status_code=400, detail="outside the tree")
        return path

    @app.post("/admin/reload-config")
    async def reload_config():
        # a stand-in for the core route: the running app takes what the files say now
        app.state.runtime = running_app(load_settings(str(config_path)), as_started=False)
        return {"report": {"refreshed": ["writer"], "errors": []}}

    @app.post("/__stub/register")
    async def register(name: str):
        # a manager that runs from now on: its tool is in the catalogue
        registry.register(name, ToolServer(name, {"manage_sub_agent": "Manage sub-agents"}))
        return {}

    @app.get("/__stub/file")
    async def read_file(path: str):
        target = inside(path)
        return {"exists": target.is_file(), "text": target.read_text(encoding="utf-8") if target.is_file() else ""}

    @app.post("/__stub/replace")
    async def replace(path: str, old: str, new: str):
        target = inside(path)
        text = target.read_text(encoding="utf-8")
        assert old in text, f"{old!r} is not in {path}"
        target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")
        return {}

    plugin_config = {"type": "agent_editor", "enabled": True, "config_path": str(config_path), "root": str(root)}
    web = PluginWebRegistry()  # the plugin's router and static files, mounted and secured as the app does it
    web.register_web_plugin("agent_editor", PLUGIN_FACTORY(
        "agent_editor", config.model_copy(update={"auth": auth}), MCPConfig(**plugin_config)))
    web.register_web_plugin("ae_off", PLUGIN_FACTORY("ae_off", config, MCPConfig(**plugin_config)))
    web.apply_to_app(app, auth)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/agent_editor", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        app = panel_app(tmp_path_factory.mktemp("agent_editor_panel"), monkeypatch)
        yield run_app_test_page(BROWSER, app, "tests/agent_editor/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the list groups the agents by where they are defined, and search and filter narrow it',
    'descriptions from the files are drawn as text, never as markup',
    'a selected agent shows its own values apart from the inherited ones',
    'an edited field marks the form dirty, and Save writes the diff it showed into the file',
    'resetting a field to the inherited value removes its key from the file',
    'the model chain adds, reorders and removes profiles',
    'a small YAML field keeps text that does not parse, and the head counts it as a change',
    'the tool tree grants whole servers and single tools as production does, blocks, and the count follows',
    'the prompt tab previews the template file and flags a missing one',
    'the YAML tab applies typed text by its button or on leaving, and keeps text that does not parse',
    'an unquoted !entry typed in the YAML tab is the removal it looks like, and an error goes with its text',
    'resetting Enabled on a child removes the key and shows it off',
    'editing a bare skills list writes only the always list, and on demand stays inherited',
    'the skill lists filter what they show, and a click keeps its row',
    'resetting Based on makes the tree follow the defaults',
    'the hooks tab writes an override for one agent',
    'the tool tree and the hook table filter what they show, on a read-only agent too',
    'another agent opens every tab at its top',
    'an entry whose parent has a ./ template previews the inherited file',
    'switching to another agent with unsaved changes asks first',
    'a file changed behind the panel is not overwritten, and Reload shows what is on disk',
    'a sub-agent switch writes the manager file through its diff',
    'the sub-agents tab grants a manager, configures it, and creates a new one',
    'without the manager plugin the sub-agents tab is gone, from the arrow keys too',
    'a new agent starts blank, as a child or as a copy, and saving creates its file',
    'a template file under a parent with an inline prompt writes an empty system_prompt',
    'deleting shows the diff with a danger button and removes the entry and its emptied file',
    'a read-only agent says why and cannot be edited',
    'Reload config refreshes what the running app has, and keeps unsaved edits',
    'the panel never calls a native dialog',
    'with authentication off the panel says so and the API refuses',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_agent_editor_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
