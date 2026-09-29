"""A prompt can ask what is there: tools, plugins, external MCP servers.

Agents are steered by what is installed -- "if you can spawn sub-agents, ...".
The answers go into the system prompt, which is the cached prefix, so they must
hold still from the first render to the last step, and the first render has to
branch the way the prompt that is sent does.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.plugins.discovery import _add_plugins
from agent_system.servers.agent import server as server_mod
from agent_system.servers.agent.prompt_strategies import PromptContext, build_context_values
from agent_system.tools.base import ToolServerRegistry


def _context(tools=(), plugins=(), mcp_servers=(), template_vars=None):
    return PromptContext(
        agent_name="a", agent_config=SimpleNamespace(template_vars=template_vars),
        system_config=SimpleNamespace(), available_tools=list(tools), max_steps=3,
        current_step=0, agent_instance=None, plugins=list(plugins),
        mcp_servers=list(mcp_servers))


def test_has_tool_matches_the_instance_prefixed_names_by_pattern():
    values = build_context_values(_context(
        tools=["coder_sam", "coder_sam_manage_sub_agent", "github.create_issue"]))

    assert values["has_tool"]("*_manage_sub_agent")
    assert values["has_tool"]("github.*")
    assert values["has_tool"]("coder_sam")
    assert not values["has_tool"]("tavily_*")
    # Case-sensitive everywhere: fnmatch would fold case on Windows only.
    assert not values["has_tool"]("CODER_SAM")


def test_plugins_and_mcp_servers_reach_the_template():
    values = build_context_values(_context(plugins=["writer_core"], mcp_servers=["github"]))

    assert values["plugins"] == ["writer_core"] and values["mcp_servers"] == ["github"]


def test_an_agents_own_template_var_still_wins():
    values = build_context_values(_context(plugins=["x"], template_vars={"plugins": "mine"}))

    assert values["plugins"] == "mine"


def _declared(**types):
    return {name: SimpleNamespace(type=typ, factory=None if typ == "agent" else object())
            for name, typ in types.items()}


def test_the_registry_names_each_enabled_plugin_type_once():
    registry = ToolServerRegistry()
    registry.bind(SimpleNamespace(declarations=lambda: _declared(
        coder_sam="sub_agent_manager", research_sam="sub_agent_manager",
        tavily_search="tavily_search", my_agent="agent")))

    # Sorted and without the direct `type: agent`, which is no plugin.
    assert registry.plugin_types() == ["sub_agent_manager", "tavily_search"]


def test_an_unbound_registry_knows_no_plugins():
    assert ToolServerRegistry().plugin_types() == []


def test_a_plugin_type_found_twice_keeps_the_first_and_says_so(caplog):
    first, second = object(), object()
    plugins = {"dup": first}

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        _add_plugins(plugins, {"dup": second, "new": second}, "src/plugins_other")

    assert plugins == {"dup": first, "new": second}
    assert "'dup'" in caplog.text and "src/plugins_other" in caplog.text


def test_the_same_plugin_found_again_is_no_duplicate(caplog):
    factory = object()
    plugins = {"same": factory}

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        _add_plugins(plugins, {"same": factory}, "src/plugins")

    assert caplog.text == ""


def _two_dirs_with_one_type(tmp_path, monkeypatch, module):
    first, second = object(), object()
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    found = {a: {"dup": first}, b: {"dup": second}}
    monkeypatch.setattr(module, "discover_plugins", lambda path, taken=None: found[Path(path)])
    return a, b, first


def test_discovery_keeps_the_first_source_of_a_type(tmp_path, monkeypatch, caplog):
    from agent_system.plugins import discovery

    a, b, first = _two_dirs_with_one_type(tmp_path, monkeypatch, discovery)
    monkeypatch.setattr(discovery, "discover_entrypoint_plugins", lambda group: {})

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        plugins = discovery.discover_all_plugins(dirs=[a, b])

    assert plugins["dup"] is first and "'dup'" in caplog.text


def test_the_tool_registry_keeps_the_same_one(tmp_path, monkeypatch):
    """Its own discovery builds the servers the Runtime did not -- with the
    other plugin of a doubled type, the two would disagree."""
    from agent_system.plugins import discovery
    from agent_system.plugins.tool_adapter import PluginToolRegistry

    a, b, first = _two_dirs_with_one_type(tmp_path, monkeypatch, discovery)
    registry = PluginToolRegistry()

    registry.discover_plugins([str(a), str(b)])

    assert registry.plugin_factories["dup"] is first


PROMPT = ("{% if has_tool('*_do_thing') %}EXPANDED{% else %}SERVER-LEVEL{% endif %}"
          " plugins={{ plugins|join(',') }} mcp={{ mcp_servers|join(',') }}")


def _agent_with_tools(monkeypatch):
    """A real agent whose discovery finds one server that expands to a tool,
    one enabled plugin and one external MCP server."""
    from test_reasoning_loop_wiring import _real_agent

    agent = _real_agent()
    agent.agent_config.system_prompt = PROMPT
    agent.agent_config.max_steps = 3
    agent.registry.bind(SimpleNamespace(declarations=lambda: _declared(srv="srv_plugin")))

    async def usable():
        return ["srv"], None, None

    async def build_schemas(self, usable_tools, allowed_patterns=None, blocked_patterns=None):
        return [], {}, [*usable_tools, "srv_do_thing"], list(usable_tools)

    async def no_setup():
        return None

    monkeypatch.setattr(agent, "list_usable_tools", usable)
    monkeypatch.setattr(server_mod.ToolSchemaBuilder, "build_schemas", build_schemas)
    monkeypatch.setattr(agent._tool_integration_manager, "setup_tool_integration", no_setup)
    agent._tool_integration_manager.tool_integration = SimpleNamespace(
        configured_external_servers={"github": object()})
    return agent


@pytest.mark.asyncio
async def test_the_first_render_branches_as_the_prompt_that_is_sent(monkeypatch):
    """Rendered from the server-level list, the first prompt -- the one the
    session-start hooks are handed -- took the other branch than every
    prompt the model then received."""
    agent = _agent_with_tools(monkeypatch)
    first = []

    async def session_start(session_id, request_id, messages):
        first.append(messages[0].content)
        return None

    monkeypatch.setattr(agent._hook_manager, "execute_session_start_hooks", session_start)

    class _Records:
        model = "test/model"

        def __init__(self):
            self.systems = []

        def supports_streaming(self):
            return True

        async def chat_tools_streaming(self, messages, tools, cancellation_token=None,
                                       status_scope=None):
            self.systems.append(messages[0].content)
            yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}

    agent.llm = _Records()
    [event async for event in agent.run_events("the task", session_id="presence")]

    sent = agent.llm.systems
    assert sent and first, "fixture: nothing rendered"
    assert sent[0] == "EXPANDED plugins=srv_plugin mcp=github", sent[0]
    assert first == [sent[0]], "the first render is not the prompt that was sent"


@pytest.mark.asyncio
async def test_the_diagnostics_show_the_branch_that_is_sent(monkeypatch):
    agent = _agent_with_tools(monkeypatch)

    shown, _schemas = await agent.describe_context_inputs()
    current = await agent.get_current_system_prompt()

    assert shown == current == "EXPANDED plugins=srv_plugin mcp=github", (shown, current)
