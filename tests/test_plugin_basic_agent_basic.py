"""Basic tests for basic_agent plugin.

This module contains fundamental tests for the BasicAgent plugin,
covering plugin discovery, instantiation, tool schema, and basic functionality.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from plugins.basic_agent.plugin import PLUGIN_FACTORY
from plugins.basic_agent.server import BasicAgent


class TestBasicAgentPluginFactory:
    """Test the BasicAgent plugin factory functionality."""

    def test_plugin_discovery(self):
        """Test that the plugin can be discovered and the factory exists."""
        assert PLUGIN_FACTORY is not None
        assert callable(PLUGIN_FACTORY)

    def test_plugin_instantiation_with_valid_config(self):
        """Test factory instantiation with proper LLM config."""
        config = {
            "parent_llm": {
                "llm_system": {
                    "profiles": {
                        "normal": {"model_ref": "gpt-5-nano"},
                        "fast": {"model_ref": "gpt-5-nano"}
                    },
                    "models": {
                        "gpt-5-nano": {"provider": "openai", "model": "gpt-5-nano"}
                    },
                    "default_profile": "normal"
                },
                "agent_llm_profiles": {}
            },
            "max_steps": 25,
            "enable_debug": True
        }
        
        agent = PLUGIN_FACTORY("test_basic_agent", config, ssl_verify=False)
        
        assert isinstance(agent, BasicAgent)
        assert agent.name == "test_basic_agent"
        assert agent.ssl_verify is False

    def test_plugin_instantiation_missing_llm_config(self):
        """Test factory fails with missing LLM config."""
        config = {}  # Missing parent_llm
        
        with pytest.raises(ValueError, match="requires LLM system configuration"):
            PLUGIN_FACTORY("test_basic_agent", config, ssl_verify=False)

    def test_plugin_instantiation_with_debug_config(self):
        """Test factory with debug configuration."""
        config = {
            "parent_llm": {
                "llm_system": {
                    "profiles": {
                        "normal": {"model_ref": "gpt-5-nano"}
                    },
                    "models": {
                        "gpt-5-nano": {"provider": "openai", "model": "gpt-5-nano"}
                    },
                    "default_profile": "normal"
                },
                "agent_llm_profiles": {}
            },
            "enable_debug": True,
            "max_steps": 50
        }
        
        agent = PLUGIN_FACTORY("debug_agent", config, ssl_verify=True)
        
        assert isinstance(agent, BasicAgent)
        assert agent.name == "debug_agent"
        assert agent.ssl_verify is True


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
        
        # Mock config
        self.mock_config = MagicMock()
        
        # Create agent instance
        self.agent = BasicAgent("test_agent", self.mock_config, self.mock_registry, ssl_verify=True)

    def test_agent_initialization(self):
        """Test basic agent initialization."""
        assert self.agent.name == "test_agent"
        assert self.agent.ssl_verify is True
        assert self.agent.registry == self.mock_registry

    def test_get_tools_schema(self):
        """Test agent tools schema loading."""
        tools = self.agent.get_tools()
        
        assert isinstance(tools, list)
        assert len(tools) == 2  # basic_agent and basic_agent_list_tools
        
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "test_agent_execute_task" in tool_names
        assert "test_agent_list_tools" in tool_names

    def test_get_default_action(self):
        """Test default action returns agent name."""
        default_action = self.agent.get_default_action()
        assert default_action == "test_agent"

    @pytest.mark.asyncio
    async def test_list_available_tools_success(self):
        """Test successful listing of available tools."""
        params = {}
        
        result = await self.agent._list_available_tools(params)
        
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
        agent = BasicAgent("empty_agent", self.mock_config, None, ssl_verify=True)
        
        result = await agent._list_available_tools({})
        
        assert isinstance(result, list)
        assert result == []

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self):
        """Test calling unknown tool raises ValueError."""
        params = {"task": "test task"}
        
        with pytest.raises(ValueError, match="Unknown tool: unknown_tool"):
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
        """Test calling list_tools function."""
        params = {}
        
        result = await self.agent.call("test_agent_list_tools", params)
        
        assert isinstance(result, list)


class TestBasicAgentSchemaLoading:
    """Test schema loading functionality."""

    def test_schema_file_exists(self):
        """Test that schema.yaml exists in plugin directory."""
        schema_path = Path(__file__).parent.parent / "src" / "plugins" / "basic_agent" / "schema.yaml"
        assert schema_path.exists(), f"Schema file not found at {schema_path}"

    def test_schema_loading_with_template_vars(self):
        """Test schema loading with template variables."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        
        plugin_dir = Path(__file__).parent.parent / "src" / "plugins" / "basic_agent"
        schema_data = load_schema_from_dir(plugin_dir, template_vars={"name": "test_agent"})
        
        assert schema_data is not None
        assert "tools" in schema_data
        
        tools = schema_data["tools"]
        assert len(tools) == 2
        
        # Check that template variables were replaced
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "test_agent_execute_task" in tool_names
        assert "test_agent_list_tools" in tool_names

    def test_schema_loading_missing_template_vars(self):
        """Test schema loading without template variables."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        
        plugin_dir = Path(__file__).parent.parent / "src" / "plugins" / "basic_agent"
        
        # Should still work, but template variables won't be replaced
        schema_data = load_schema_from_dir(plugin_dir, template_vars={})
        
        assert schema_data is not None
        assert "tools" in schema_data


if __name__ == "__main__":
    pytest.main([__file__])