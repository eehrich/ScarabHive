"""Tests for enhanced ToolServer supporting multiple tools per plugin."""
from __future__ import annotations

import pytest
from typing import Any

from agent_system.tools.base import ToolServer
from agent_system.config.models import AgentSystemConfig, ToolServerConfig, AgentConfig
from plugins.example.server import ExampleServer


@pytest.fixture
def test_configs():
    """Fixture providing test config objects."""
    system_config = AgentSystemConfig()
    server_config = ToolServerConfig(type="test", enabled=True, agent_config=AgentConfig())
    return system_config, server_config


class SingleToolMockServer(ToolServer):
    """Mock server implementing single-tool interface using new list_tools() method."""
    
    def __init__(self, name: str):
        system_config = AgentSystemConfig()
        server_config = ToolServerConfig(type=name, enabled=True, agent_config=AgentConfig())
        super().__init__(name, system_config, server_config)
    
    async def list_tools(self) -> list[dict[str, Any]]:
        return [{
            "type": "function",
            "function": {
                "name": f"{self.name}_single",
                "description": "Single tool for testing",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"}
                    },
                    "required": ["message"]
                }
            }
        }]
    
    def get_default_action(self) -> str:
        return f"{self.name}_single"
    
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        return {"tool": tool, "message": params["message"], "server": self.name}


class OldStyleServer(ToolServer):
    """Mock server implementing the new interface with single tool."""
    
    def __init__(self, name: str):
        system_config = AgentSystemConfig()
        server_config = ToolServerConfig(type=name, enabled=True, agent_config=AgentConfig())
        super().__init__(name, system_config, server_config)
    
    async def list_tools(self) -> list[dict[str, Any]]:
        return [{
            "type": "function", 
            "function": {
                "name": "old_style",
                "description": "Old style server",
                "parameters": {
                    "type": "object",
                    "properties": {"data": {"type": "string"}},
                    "required": ["data"]
                }
            }
        }]
    
    def get_default_action(self) -> str:
        return "old_style"
        
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        return {"legacy": True, "data": params["data"]}


def _get_tool_name(tool):
    """Extract the function.name from either a dict or a ToolDef-like object."""
    # dict form: tool["function"]["name"]
    if isinstance(tool, dict):
        func = tool.get("function") or {}
        return func.get("name")
    # object form: tool.function.name or tool.name
    func = getattr(tool, "function", None)
    if func is None:
        return getattr(tool, "name", None)
    if isinstance(func, dict):
        return func.get("name")
    return getattr(func, "name", None)


def _get_tool_description(tool):
    """Extract the function.description from either a dict or a ToolDef-like object."""
    if isinstance(tool, dict):
        func = tool.get("function") or {}
        return func.get("description")
    func = getattr(tool, "function", None)
    if func is None:
        return getattr(tool, "description", None)
    if isinstance(func, dict):
        return func.get("description")
    return getattr(func, "description", None)
class TestEnhancedToolServer:
    """Test the enhanced ToolServer interface."""

    async def test_multi_tool_server_get_tools(self, test_configs):
        """Test that multi-tool server returns multiple tools."""
        system_config, server_config = test_configs
        server = ExampleServer("example", system_config, server_config)
        tools = await server.list_tools()

        assert len(tools) == 3
        tool_names = [_get_tool_name(t) for t in tools]
        assert "example_calculator" in tool_names
        assert "example_formatter" in tool_names
        assert "example_status" in tool_names

    async def test_multi_tool_server_get_schema_backward_compat(self, test_configs):
        system_config, server_config = test_configs
        """Test that get_schema() works for multi-tool servers (returns first tool)."""
        server = ExampleServer("example", system_config, server_config)
        tools = await server.list_tools()
        schema = tools[0]  # Get first tool as schema

        # Use helper to extract name/description robustly
        fname = _get_tool_name(schema)
        fdesc = _get_tool_description(schema)

        assert fname == "example_calculator"
        assert "arithmetic operations" in (fdesc or "")

    def test_multi_tool_server_has_name(self, test_configs):
        """Test that server has a name attribute."""
        system_config, server_config = test_configs
        server = ExampleServer("example", system_config, server_config)
        
        # Server should have name attribute
        assert server.name == "example"
        assert isinstance(server.name, str)

    async def test_multi_tool_server_calculator_call(self, test_configs):
        """Test calling the calculator tool."""
        system_config, server_config = test_configs
        server = ExampleServer("example", system_config, server_config)

        # Tool is named using the server name prefix: test_calculator
        result = await server.call("example_calculator", {
            "operation": "add",
            "a": 5,
            "b": 3
        })

        assert result["operation"] == "add"
        assert result["operands"] == [5.0, 3.0]
        assert result["result"] == 8.0

    async def test_multi_tool_server_formatter_call(self, test_configs):
        system_config, server_config = test_configs
        """Test calling the formatter tool."""
        server = ExampleServer("example", system_config, server_config)

        result = await server.call("example_formatter", {
            "text": "hello world",
            "format": "uppercase"
        })

        assert result["original"] == "hello world"
        assert result["format"] == "uppercase"
        assert result["formatted"] == "HELLO WORLD"

    async def test_multi_tool_server_status_call(self, test_configs):
        system_config, server_config = test_configs
        """Test calling the status tool."""
        server = ExampleServer("example", system_config, server_config)

        result = await server.call("example_status", {"verbose": True})

        assert result["server_name"] == "example"
        assert result["status"] == "active"
        assert result["tools_count"] == 3
        assert "available_tools" in result
        assert len(result["available_tools"]) == 3

    async def test_multi_tool_server_invalid_tool(self, test_configs):
        system_config, server_config = test_configs
        """Test calling an invalid tool raises error."""
        server = ExampleServer("example", system_config, server_config)

        with pytest.raises(ValueError, match="Tool .* not found"):
            await server.call("example_invalid", {})

    async def test_calculator_division_by_zero(self, test_configs):
        system_config, server_config = test_configs
        """Test division by zero error handling."""
        server = ExampleServer("example", system_config, server_config)

        result = await server.call("example_calculator", {
            "operation": "divide",
            "a": 10,
            "b": 0
        })
        assert result["status"] == "error"
        assert "Division by zero" in result["error"]

    async def test_single_tool_backward_compatibility(self):
        """Test that single-tool servers still work."""
        server = SingleToolMockServer(name="single")

        # Test list_tools() returns single tool
        tools = await server.list_tools()
        assert len(tools) == 1
        assert _get_tool_name(tools[0]) == "single_single"

        # Test get_default_action() works
        default_action = server.get_default_action()
        assert default_action == "single_single"

    async def test_single_tool_call(self):
        """Test calling single-tool server."""
        server = SingleToolMockServer(name="single")

        result = await server.call("single_single", {"message": "test"})
        assert result["tool"] == "single_single"
        assert result["message"] == "test"
        assert result["server"] == "single"

    async def test_old_style_server_compatibility(self):
        """Test old-style servers that only implement abstract methods."""
        server = OldStyleServer(name="old")

        # Test list_tools() returns single tool
        tools = await server.list_tools()
        assert len(tools) == 1
        assert _get_tool_name(tools[0]) == "old_style"

        # Test get_default_action() works
        default_action = server.get_default_action()
        assert default_action == "old_style"

    async def test_old_style_server_call(self):
        """Test calling old-style server."""
        server = OldStyleServer(name="old")

        result = await server.call("old_style", {"data": "legacy"})
        assert result["legacy"] is True
        assert result["data"] == "legacy"


class TestErrorConditions:
    """Test error conditions and edge cases."""
    
    async def test_empty_tools_error(self, test_configs):
        """Test server with empty tools list."""
        system_config, server_config = test_configs
        
        class EmptyToolsServer(ToolServer):
            def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
                super().__init__(name, system_config, server_config)
            
            async def list_tools(self) -> list[dict[str, Any]]:
                return []
            
            async def call(self, tool: str, params: dict[str, Any]) -> Any:
                return {}
        
        server = EmptyToolsServer("empty", system_config, server_config)
        
        # Should return empty list, not raise error
        tools = await server.list_tools()
        assert tools == []
    
    async def test_missing_implementation_fallback(self, test_configs):
        """Test server missing list_tools implementation raises NotImplementedError."""
        system_config, server_config = test_configs
        
        class IncompleteServer(ToolServer):
            def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
                super().__init__(name, system_config, server_config)
            
            async def call(self, tool: str, params: dict[str, Any]) -> Any:
                return {}
        
        server = IncompleteServer("incomplete", system_config, server_config)
        
        # Should raise NotImplementedError if list_tools() or get_tools() not implemented
        with pytest.raises(NotImplementedError, match="must implement list_tools"):
            await server.list_tools()


if __name__ == "__main__":
    # Run tests if executed directly
    pytest.main([__file__, "-v"])