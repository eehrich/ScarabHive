"""A server's resources, prompts, instructions and list_changed notifications, against the real probe server.

Resources and prompts reach an agent as tools of their server (``<server>.mcp_read_resource``), so the
allowlist that grants a server's tools grants them, and a server's blocked list blocks them.
"""
import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

from plugins.mcp_client.manager import PROMPT_TOOLS, RESOURCE_TOOLS, ExternalServerPool

PROBE = str(Path(__file__).parent / "probe_server.py")
MARKER = "AGENT_SYSTEM_TEST_SESSION"


def _config(blocked=None, offered=True):
    return types.SimpleNamespace(
        url=None, transport="stdio", enabled=True, auth=None, initialization_options=None, description=None,
        tools=types.SimpleNamespace(blocked=blocked or [], allowed=[]), resources_and_prompts=offered,
        command=sys.executable, args=[PROBE], env={MARKER: os.environ[MARKER]})


@pytest.fixture
async def pool():
    pool = ExternalServerPool(timeout=20)
    yield pool
    await pool.close_all()


async def _listed(pool, **kw):
    return {t["name"]: t for t in (await pool.list_tools_by_server(**kw))["probe"]}


@pytest.mark.asyncio
async def test_resources_and_prompts_are_tools_of_their_server(pool):
    pool.configure({"probe": _config()})
    await pool.connect("probe")
    listed = await _listed(pool)
    assert set(RESOURCE_TOOLS) | set(PROMPT_TOOLS) <= set(listed), sorted(listed)

    resources = await pool.call_tool("probe", "mcp_list_resources", {})
    assert [r["uri"] for r in resources["resources"]] == ["probe://notes"], resources
    assert [t["uriTemplate"] for t in resources["templates"]] == ["probe://item/{item_id}"], resources
    assert await pool.call_tool("probe", "mcp_read_resource", {"uri": "probe://notes"}) == "the probe adds numbers"
    assert await pool.call_tool("probe", "mcp_read_resource", {"uri": "probe://item/7"}) == "item 7"

    prompts = await pool.call_tool("probe", "mcp_list_prompts", {})
    assert [p["name"] for p in prompts["prompts"]] == ["greet"], prompts
    filled = await pool.call_tool("probe", "mcp_get_prompt", {"name": "greet", "arguments": {"name": "Ada"}})
    assert "Say hello to Ada." in filled, filled


@pytest.mark.asyncio
async def test_resources_and_prompts_are_offered_only_where_the_config_says_so(pool):
    """Off by default: on, <server>.* admits them -- and a resource can read what tools.blocked keeps a
    tool from reading. An allowlist written before must not widen by itself."""
    pool.configure({"probe": _config(offered=False)})
    await pool.connect("probe")
    # before any listing too: then what the server announced would decide
    with pytest.raises(Exception, match="mcp_read_resource"):   # the server's answer: no such tool
        await pool.call_tool("probe", "mcp_read_resource", {"uri": "probe://notes"})
    listed = await _listed(pool)
    assert "add" in listed, "fixture: the server's own tools are listed"
    assert not (set(RESOURCE_TOOLS) | set(PROMPT_TOOLS)) & set(listed), sorted(listed)
    with pytest.raises(Exception, match="mcp_read_resource"):
        await pool.call_tool("probe", "mcp_read_resource", {"uri": "probe://notes"})


@pytest.mark.asyncio
async def test_a_blocked_resource_tool_is_refused(pool):
    pool.configure({"probe": _config(blocked=["mcp_read_resource"])})
    await pool.connect("probe")
    assert (await _listed(pool))["mcp_read_resource"]["blocked"]
    with pytest.raises(PermissionError):
        await pool.call_tool("probe", "mcp_read_resource", {"uri": "probe://notes"})


@pytest.mark.asyncio
async def test_the_servers_instructions_are_shown_with_its_state(pool):
    pool.configure({"probe": _config()})
    await pool.connect("probe")
    assert pool.status()["probe"]["instructions"].startswith("Use add for sums"), pool.status()["probe"]


@pytest.mark.asyncio
async def test_a_changed_tool_list_reaches_the_catalogue_without_waiting_for_the_ttl(pool):
    pool.cache_ttl = 3600
    told = []
    pool.on_catalog_changed = lambda: told.append(True)
    pool.configure({"probe": _config()})
    await pool.connect("probe")
    assert "grown" not in await _listed(pool), "fixture: the tool exists before it grew"

    assert await pool.call_tool("probe", "grow", {}) == "grew"
    for _ in range(50):
        if told:
            break
        await asyncio.sleep(0.05)
    assert told, "the plugin was not told the catalogue changed"
    assert "grown" in await _listed(pool), "the cached list (TTL 1 h) was served after the server said it changed"


@pytest.mark.asyncio
async def test_a_server_without_resource_templates_still_lists_its_resources():
    """Templates are optional in the protocol: a server that does not answer them lists no templates."""
    from plugins.mcp_client.connection import ServerConnection

    class Session:
        async def list_resources(self, params=None):
            return types.SimpleNamespace(resources=[{"uri": "a://1", "name": "one"}], next_cursor=None)

        async def list_resource_templates(self, params=None):
            raise RuntimeError("Method not found")

    connection = ServerConnection("probe", _config())

    async def submit(run, timeout=None):
        return await run(Session())

    connection._submit = submit
    assert await connection.list_resources() == {"resources": [{"uri": "a://1", "name": "one"}], "templates": []}


@pytest.mark.asyncio
async def test_a_real_server_entry_offers_no_resources_unless_it_says_so(pool):
    from agent_system.config.models.external_servers import RemoteMCPConfig

    entry = RemoteMCPConfig(transport="stdio", enabled=True, command=sys.executable, args=[PROBE],
                            env={MARKER: os.environ[MARKER]})
    pool.configure({"probe": entry})
    await pool.connect("probe")
    listed = await _listed(pool)
    assert "add" in listed, "fixture: the server's own tools are listed"
    assert "mcp_read_resource" not in listed


def test_a_changed_catalogue_reaches_the_core_through_the_plugin():
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from agent_system.plugins import capabilities
    from plugins.mcp_client.server import MCPClientServer

    told = []

    def listener():
        told.append(True)

    capabilities.on_tool_catalog_changed(listener)
    try:
        plugin = MCPClientServer("mcp_client", AgentSystemConfig(), ToolServerConfig(type="mcp_client"))
        plugin.pool.on_catalog_changed()
        assert told, "the core was not told its tool catalogue is stale"
    finally:
        capabilities.remove_tool_catalog_listener(listener)
