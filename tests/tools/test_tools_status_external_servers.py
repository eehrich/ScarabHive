"""External MCP servers as seen from the core.

Connecting to foreign servers moved into the ``mcp_client`` plugin; connection
behaviour itself is tested there, against a real server. What is left to prove
HERE is the seam: that the core still finds the external servers, that it does
so through the capability registry rather than by owning connections, and that
none of it is required for the core to come up.

The old version of this file asserted that ``ToolServerIntegration`` stored the server
configs itself and called ``client_manager.add_client`` per server. Both are
gone by design -- keeping those assertions would have pinned the very coupling
this change removes.
"""

import pytest
from unittest.mock import AsyncMock, Mock

from agent_system.tools.integration import ToolServerIntegration
from agent_system.plugins import capabilities
from agent_system.config.models import (
    AgentSystemConfig,
    MCPServersConfig,
    RemoteMCPConfig,
)


@pytest.fixture
def mock_server_config():
    """AgentSystemConfig with two enabled external servers."""
    remote_servers = {
        'localhost': RemoteMCPConfig(
            url='http://127.0.0.1:8081',
            description='Local streaming MCP server running on localhost.',
            transport='streaming',
            enabled=True
        ),
        'remote_server': RemoteMCPConfig(
            url='http://example.com:8080',
            description='Remote MCP server',
            transport='streaming',
            enabled=True
        )
    }
    return AgentSystemConfig(external_servers=MCPServersConfig(remote_servers=remote_servers))


@pytest.fixture
def mock_plugin_registry():
    registry = Mock()
    registry.plugin_factories = {}
    registry.plugin_servers = {}
    registry.discover_plugins = Mock()
    registry.register_from_config = AsyncMock()
    registry.list_servers = Mock(return_value=[])
    registry.get_all_tools = AsyncMock(return_value={})
    registry.start_all = AsyncMock()
    registry.shutdown_all = AsyncMock()
    return registry


@pytest.fixture
def tool_integration(mock_plugin_registry, mock_server_config):
    integration = ToolServerIntegration(config=mock_server_config)
    integration.plugin_registry = mock_plugin_registry
    return integration


@pytest.fixture
def fake_provider(mock_server_config):
    """Stand-in for the mcp_client plugin, registered as the tool provider."""
    from plugins.mcp_client.manager import ExternalServerPool

    provider = Mock()
    provider.pool = ExternalServerPool()
    provider.pool.configure(mock_server_config.external_servers.remote_servers)
    provider.list_external_tools = AsyncMock(return_value={
        'localhost': [{'name': 'ping', 'description': 'p', 'input_schema': {}, 'blocked': False}],
    })
    provider.call_external_tool = AsyncMock(return_value='ok')

    capabilities.reset()
    capabilities.register_provider(capabilities.EXTERNAL_TOOLS, provider)
    yield provider
    capabilities.reset()


class TestExternalServersThroughTheProvider:
    async def test_configured_servers_come_from_the_client_plugin(self, tool_integration, fake_provider):
        """The core reads the server list; it no longer keeps one."""
        servers = tool_integration.configured_external_servers
        assert set(servers) == {'localhost', 'remote_server'}
        assert servers['localhost'].url == 'http://127.0.0.1:8081'
        assert servers['localhost'].description == 'Local streaming MCP server running on localhost.'
        assert servers['remote_server'].transport == 'streaming'

    async def test_disabled_servers_are_not_offered(self, tool_integration):
        """A disabled server must not show up as configured."""
        from plugins.mcp_client.manager import ExternalServerPool

        provider = Mock()
        provider.pool = ExternalServerPool()
        provider.pool.configure({
            'off': RemoteMCPConfig(url='http://example.com', enabled=False),
            'on': RemoteMCPConfig(url='http://example.com', enabled=True),
        })
        capabilities.reset()
        capabilities.register_provider(capabilities.EXTERNAL_TOOLS, provider)
        try:
            assert set(tool_integration.configured_external_servers) == {'on'}
        finally:
            capabilities.reset()

    async def test_external_tools_reach_list_all_tools(self, tool_integration, fake_provider):
        tools = await tool_integration.list_all_tools()
        assert 'localhost' in tools['external_servers']
        assert tools['external_servers']['localhost'][0]['name'] == 'ping'

    async def test_a_failing_provider_does_not_break_the_tool_listing(self, tool_integration, fake_provider):
        """An external outage must not take the plugin tools down with it."""
        fake_provider.list_external_tools.side_effect = RuntimeError("server exploded")
        tools = await tool_integration.list_all_tools()
        assert tools['external_servers'] == {}
        assert 'plugins' in tools


class TestCoreWithoutAnyClientPlugin:
    """Everything must still work when no client plugin is loaded at all."""

    def setup_method(self):
        capabilities.reset()

    def teardown_method(self):
        capabilities.reset()

    async def test_initialize_succeeds_without_a_provider(self, tool_integration, mock_server_config):
        await tool_integration.initialize(mock_server_config)
        assert tool_integration.initialized

    async def test_configured_servers_is_empty_not_an_error(self, tool_integration):
        assert tool_integration.configured_external_servers == {}
        assert tool_integration.list_external_clients() == []

    async def test_connecting_reports_failure_rather_than_raising(self, tool_integration):
        assert await tool_integration.retry_connect_server('localhost') is False

    async def test_calling_an_external_tool_says_why_it_cannot(self, tool_integration):
        with pytest.raises(Exception, match="no external MCP client plugin"):
            await tool_integration.call_tool('localhost', 'ping', {}, server_type='external')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
