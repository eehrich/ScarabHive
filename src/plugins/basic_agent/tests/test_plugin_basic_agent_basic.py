"""Basic tests for basic_agent plugin.

This module contains fundamental tests for the BasicAgent plugin,
covering plugin discovery, instantiation, tool schema, and basic functionality.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig
from plugins.basic_agent.plugin import PLUGIN_FACTORY
from plugins.basic_agent.server import BasicAgent


class TestBasicAgentPluginFactory:
    """Test the BasicAgent plugin factory functionality."""

    def test_plugin_discovery(self):
        """Test that the plugin can be discovered and the factory exists."""
        assert PLUGIN_FACTORY is not None
        assert callable(PLUGIN_FACTORY)

    def test_plugin_instantiation_with_valid_config(self):
        """Test factory instantiation with proper config."""
        # Create mock system config with LLM system
        system_config = Mock(spec=AgentSystemConfig)
        system_config.llm_system = Mock()
        system_config.llm_system.profiles = {
            "normal": Mock(model_ref="gpt-5-nano"),
            "fast": Mock(model_ref="gpt-5-nano")
        }
        system_config.llm_system.models = {
            "gpt-5-nano": Mock(provider="openai", model="gpt-5-nano")
        }
        system_config.llm_system.default_profile = "normal"
        system_config.network = Mock()
        system_config.network.ssl_verify = False
        
        # Create MCP config with agent config
        agent_config = AgentConfig(llm_profile="normal", max_steps=25)
        mcp_config = MCPConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )
        
        agent = PLUGIN_FACTORY("test_basic_agent", system_config, mcp_config)
        
        assert isinstance(agent, BasicAgent)
        assert agent.name == "test_basic_agent"

    def test_plugin_instantiation_missing_llm_config(self):
        """Test factory requires agent_config in MCPConfig."""
        system_config = Mock(spec=AgentSystemConfig)
        system_config.llm_system = None
        system_config.network = Mock()
        system_config.network.ssl_verify = False
        
        mcp_config = MCPConfig(
            type="basic_agent",
            enabled=True
        )
        
        # Should raise ValueError because agent_config is missing
        with pytest.raises(ValueError, match="requires agent_config in MCPConfig"):
            PLUGIN_FACTORY("test_basic_agent", system_config, mcp_config)

    def test_plugin_instantiation_with_debug_config(self):
        """Test factory with debug configuration."""
        system_config = Mock(spec=AgentSystemConfig)
        system_config.llm_system = Mock()
        system_config.llm_system.profiles = {
            "normal": Mock(model_ref="gpt-5-nano")
        }
        system_config.llm_system.models = {
            "gpt-5-nano": Mock(provider="openai", model="gpt-5-nano")
        }
        system_config.llm_system.default_profile = "normal"
        system_config.network = Mock()
        system_config.network.ssl_verify = True
        
        agent_config = AgentConfig(llm_profile="normal", max_steps=50)
        mcp_config = MCPConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config,
            enable_debug=True
        )
        
        agent = PLUGIN_FACTORY("debug_agent", system_config, mcp_config)
        
        assert isinstance(agent, BasicAgent)
        assert agent.name == "debug_agent"


class TestBasicAgentServer:
    """Test the BasicAgent server class functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        # Mock registry with some basic servers
        self.mock_registry = MagicMock()
        self.mock_registry.list.return_value = ["datetime", "script_interpreter", "weather"]
        
        # Mock servers
        self.mock_datetime_server = MagicMock()
        self.mock_datetime_server.get_tools.return_value = [
            {
                "function": {
                    "name": "datetime_operations",
                    "description": "Date/time operations",
                    "parameters": {"type": "object"}
                }
            }
        ]
        
        self.mock_script_server = MagicMock()
        self.mock_script_server.get_tools.return_value = [
            {
                "function": {
                    "name": "execute_python_sandbox",
                    "description": "Execute Python code",
                    "parameters": {"type": "object"}
                }
            }
        ]
        
        self.mock_registry.get.side_effect = lambda name: {
            "datetime": self.mock_datetime_server,
            "script_interpreter": self.mock_script_server,
            "weather": None  # Test server without tools
        }.get(name)
        
        # Create mock system config
        system_config = Mock(spec=AgentSystemConfig)
        system_config.network = Mock()
        system_config.network.ssl_verify = True
        system_config.llm_system = Mock()
        system_config.llm_system.profiles = {"normal": Mock(model_ref="gpt-5-nano")}
        system_config.llm_system.models = {"gpt-5-nano": Mock(provider="openai", model="gpt-5-nano")}
        
        # Create MCP config. tools.allowed is EXPLICIT: an agent without it is
        # deny-all in the LLM schema (tool_discovery), and the detail listing
        # follows the same pipeline now -- it used to silently mean allow-all
        # here, reporting tools the model never had.
        from agent_system.config.models import ToolConfig
        agent_config = AgentConfig(
            llm_profile="normal",
            tools=ToolConfig(allowed=["datetime/*", "script_interpreter/*", "weather/*"]),
        )
        mcp_config = MCPConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )
        
        # Create agent instance with new signature
        self.agent = BasicAgent("test_agent", system_config, mcp_config, self.mock_registry)

    def test_agent_initialization(self):
        """Test basic agent initialization."""
        assert self.agent.name == "test_agent"
        assert self.agent.registry == self.mock_registry

    def test_get_tools_schema(self):
        """Test agent tools schema loading."""
        tools = self.agent.get_tools()
        
        assert isinstance(tools, list)
        assert len(tools) == 2  # execute_task and list_available_tools
        
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "test_agent_execute_task" in tool_names
        assert "test_agent_list_available_tools" in tool_names

    @pytest.mark.asyncio
    async def test_list_available_tools_success(self):
        """The listing follows the SAME pipeline as the LLM schema. The mock
        servers here are MagicMocks whose auto-generated list_tools cannot be
        awaited -- the builder's get_tools fallback has to rescue them, which
        doubles as a regression test for that fallback."""
        params = {}
        
        result = await self.agent._list_usable_tools_with_details(params)
        
        assert isinstance(result, list)
        assert len(result) == 2  # datetime and script_interpreter
        
        # Check tool details (simplified structure)
        tool_names = [tool["name"] for tool in result]
        assert "datetime_operations" in tool_names
        assert "execute_python_sandbox" in tool_names
        
        # Verify simplified structure (only name and description)
        for tool in result:
            assert "name" in tool
            assert "description" in tool
            assert "server" not in tool  # No longer included
            assert "parameters" not in tool  # No longer included

    @pytest.mark.asyncio
    async def test_list_available_tools_empty_registry(self):
        """Test listing tools with empty registry."""
        system_config = Mock(spec=AgentSystemConfig)
        system_config.network = Mock()
        system_config.network.ssl_verify = True
        
        agent_config = AgentConfig(llm_profile="normal")
        mcp_config = MCPConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )
        
        # Create empty registry
        empty_registry = MagicMock()
        empty_registry.list.return_value = []
        empty_registry.get.return_value = None
        
        agent = BasicAgent("empty_agent", system_config, mcp_config, empty_registry)
        
        result = await agent._list_usable_tools_with_details({})
        
        assert isinstance(result, list)
        assert result == []

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self):
        """Test calling unknown tool raises ValueError."""
        params = {"task": "test task"}
        
        with pytest.raises(ValueError, match="Tool 'unknown_tool' not found"):
            await self.agent.call("unknown_tool", params)

    @pytest.mark.asyncio
    async def test_call_execute_task_missing_params(self):
        """Test calling execute_task without required task parameter."""
        params = {}  # Missing task
        
        result = await self.agent.call("test_agent_execute_task", params)
        
        assert result["status"] == "error"
        assert "Missing required parameter 'task'" in result["error"]

    @pytest.mark.asyncio
    async def test_call_list_tools(self):
        """Test calling list_available_tools function."""
        params = {}
        
        result = await self.agent.call("test_agent_list_available_tools", params)
        
        assert isinstance(result, list)


class TestBasicAgentSchemaLoading:
    """Test schema loading functionality."""

    def test_schema_file_exists(self):
        """Test that schema.yaml exists in plugin directory."""
        schema_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "basic_agent" / "schema.yaml"
        assert schema_path.exists(), f"Schema file not found at {schema_path}"

    def test_schema_loading_with_template_vars(self):
        """Test schema loading with template variables."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        
        plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "basic_agent"
        schema_data = load_schema_from_dir(plugin_dir, template_vars={"name": "test_agent"})
        
        assert schema_data is not None
        assert "tools" in schema_data
        
        tools = schema_data["tools"]
        assert len(tools) == 2
        
        # Check that template variables were replaced
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "test_agent_execute_task" in tool_names
        assert "test_agent_list_available_tools" in tool_names

    def test_schema_loading_missing_template_vars(self):
        """Test schema loading without template variables."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        
        plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "basic_agent"
        
        # Should still work, but template variables won't be replaced
        schema_data = load_schema_from_dir(plugin_dir, template_vars={})
        
        assert schema_data is not None
        assert "tools" in schema_data


if __name__ == "__main__":
    pytest.main([__file__])