"""
Tests for MCP Plugin Integration
"""

import pytest
from unittest.mock import AsyncMock
from typing import Any, Dict

from agent_system.plugins.mcp_adapter import PluginMCPAdapter, PluginMCPRegistry
from agent_system.mcp.core import MCPCapability


class MockPluginServer:
    """Mock plugin server for testing"""

    def __init__(self, name: str, tools_data: list = None):
        self.name = name
        self.tools_data = tools_data or [
            {
                "name": "test_tool",
                "description": "A test tool",
                "parameters": {
                    "type": "object",
                    "properties": {"param": {"type": "string"}},
                    "required": ["param"]
                }
            }
        ]

    async def call(self, tool: str, params: Dict[str, Any]) -> Any:
        return f"Called {tool} with {params}"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "functions": self.tools_data
        }

    def get_default_action(self) -> str:
        return "default_action"


@pytest.fixture
def mock_plugin_server():
    return MockPluginServer("test_plugin")


@pytest.fixture
def plugin_adapter(mock_plugin_server):
    return PluginMCPAdapter("test_plugin", mock_plugin_server)


class TestPluginMCPAdapter:
    """Test plugin MCP adapter"""

    def test_plugins_mcp_adapter_initialization(self, plugin_adapter):
        assert plugin_adapter.name == "test_plugin"
        assert plugin_adapter.description == "AgentSystem test_plugin plugin"
        assert MCPCapability.TOOLS in plugin_adapter.capabilities

    @pytest.mark.asyncio
    async def test_plugins_mcp_adapter_list_tools_from_schema(self, plugin_adapter):
        tools = await plugin_adapter.list_tools()

        assert len(tools) == 1
        assert tools[0].name == "test_tool"
        assert tools[0].description == "A test tool"
        assert tools[0].input_schema["type"] == "object"
        assert "param" in tools[0].input_schema["properties"]

    @pytest.mark.asyncio
    async def test_plugins_mcp_adapter_list_tools_caching(self, plugin_adapter):
        # First call to populate cache
        tools1 = await plugin_adapter.list_tools()
        # Second call should use cache
        tools2 = await plugin_adapter.list_tools()

        assert tools1 == tools2
        # Cache should return same instance/content
        assert len(tools1) == 1

    @pytest.mark.asyncio
    async def test_plugins_mcp_adapter_call_tool(self, plugin_adapter):
        result = await plugin_adapter.call_tool("test_tool", {"param": "test_value"})
        
        # The mock returns a string directly
        assert "test_value" in str(result)

    @pytest.mark.asyncio
    async def test_plugins_mcp_adapter_call_tool_error(self, plugin_adapter):
        # Mock plugin that raises an exception
        plugin_adapter.plugin_server.call = AsyncMock(side_effect=Exception("Test error"))

        with pytest.raises(Exception, match="Test error"):
            await plugin_adapter.call_tool("test_tool", {})


class TestPluginMCPAdapterWithoutSchema:
    """Test plugin adapter without schema"""

    @pytest.fixture
    def plugin_without_schema(self):
        class PluginWithoutSchema:
            def __init__(self):
                self.name = "no_schema_plugin"

            async def call(self, tool: str, params: Dict[str, Any]) -> Any:
                return f"Called {tool}"

            def get_default_action(self) -> str:
                return "default"

        plugin_server = PluginWithoutSchema()
        return PluginMCPAdapter("no_schema_plugin", plugin_server)

    @pytest.mark.asyncio
    async def test_plugins_mcp_adapter_list_tools_fallback_to_default(self, plugin_without_schema):
        tools = await plugin_without_schema.list_tools()

        assert len(tools) == 1
        assert tools[0].name == "default"
        assert tools[0].description == "Default action for no_schema_plugin"
        assert tools[0].input_schema["type"] == "object"


class TestPluginMCPAdapterWithExternalSchema:
    """Test plugin adapter with external schema"""

    @pytest.fixture
    def plugin_with_external_schema(self):
        class PluginWithExternalSchema:
            def __init__(self):
                self.name = "external_schema_plugin"

            async def call(self, tool: str, params: Dict[str, Any]) -> Any:
                return f"Called {tool}"

        schema = {
            "functions": [
                {
                    "name": "external_tool",
                    "description": "Tool from external schema",
                    "parameters": {"type": "object"}
                }
            ]
        }

        plugin_server = PluginWithExternalSchema()
        return PluginMCPAdapter("external_schema_plugin", plugin_server, schema)

    @pytest.mark.asyncio
    async def test_plugins_mcp_adapter_list_tools_from_external_schema(self, plugin_with_external_schema):
        tools = await plugin_with_external_schema.list_tools()

        assert len(tools) == 1
        assert tools[0].name == "external_tool"
        assert tools[0].description == "Tool from external schema"


class TestPluginMCPRegistry:
    """Test plugin MCP registry"""

    @pytest.fixture
    def registry(self):
        return PluginMCPRegistry()

    def test_plugins_mcp_registry_initialization(self, registry):
        assert len(registry.plugin_servers) == 0
        assert len(registry.plugin_factories) == 0

    def test_plugins_mcp_registry_list_servers_empty(self, registry):
        assert registry.list_servers() == []
        assert registry.list_available_plugins() == []

    @pytest.mark.asyncio
    async def test_plugins_mcp_registry_register_plugin(self, registry):
        # Mock factory
        def mock_factory(name, config, ssl_verify=True):
            return MockPluginServer(name)

        registry.plugin_factories["test_plugin"] = mock_factory

        await registry.register_plugin("test_plugin", {"param": "value"})

        assert "test_plugin" in registry.list_servers()
        assert registry.get_server("test_plugin") is not None

    @pytest.mark.asyncio
    async def test_plugins_mcp_registry_register_unknown_plugin(self, registry):
        with pytest.raises(Exception, match="Unknown plugin: unknown"):
            await registry.register_plugin("unknown")

    @pytest.mark.asyncio
    async def test_plugins_mcp_registry_unregister_plugin(self, registry):
        # Mock factory and register plugin
        def mock_factory(name, config, ssl_verify=True):
            return MockPluginServer(name)

        registry.plugin_factories["test_plugin"] = mock_factory
        await registry.register_plugin("test_plugin")

        # Unregister
        await registry.unregister_plugin("test_plugin")

        assert "test_plugin" not in registry.list_servers()
        assert registry.get_server("test_plugin") is None

    @pytest.mark.asyncio
    async def test_plugins_mcp_registry_register_from_config(self, registry):
        # Mock factories
        def mock_factory1(name, config, ssl_verify=True):
            return MockPluginServer(name)

        def mock_factory2(name, config, ssl_verify=True):
            return MockPluginServer(name)

        registry.plugin_factories["plugin1"] = mock_factory1
        registry.plugin_factories["plugin2"] = mock_factory2

        enabled_servers = ["plugin1", "plugin2"]
        servers_config = {
            "plugin1": {"param1": "value1"},
            "plugin2": {"param2": "value2"}
        }

        await registry.register_from_config(enabled_servers, servers_config)

        assert len(registry.list_servers()) == 2
        assert "plugin1" in registry.list_servers()
        assert "plugin2" in registry.list_servers()

    @pytest.mark.asyncio
    async def test_plugins_mcp_registry_get_all_tools(self, registry):
        # Mock factory
        def mock_factory(name, config, ssl_verify=True):
            return MockPluginServer(name)

        registry.plugin_factories["test_plugin"] = mock_factory
        await registry.register_plugin("test_plugin")

        all_tools = await registry.get_all_tools()

        assert "test_plugin" in all_tools
        assert len(all_tools["test_plugin"]) == 1
        assert all_tools["test_plugin"][0].name == "test_tool"

    @pytest.mark.asyncio
    async def test_plugins_mcp_registry_call_plugin_tool(self, registry):
        # Mock factory
        def mock_factory(name, config, ssl_verify=True):
            return MockPluginServer(name)

        registry.plugin_factories["test_plugin"] = mock_factory
        await registry.register_plugin("test_plugin")

        result = await registry.call_plugin_tool("test_plugin", "test_tool", {"param": "value"})

        assert result == "Called test_tool with {'param': 'value'}"

    @pytest.mark.asyncio
    async def test_plugins_mcp_registry_call_plugin_tool_unknown_plugin(self, registry):
        with pytest.raises(Exception, match="Plugin unknown not registered"):
            await registry.call_plugin_tool("unknown", "tool", {})