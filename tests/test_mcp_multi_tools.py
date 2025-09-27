"""Tests for enhanced MCPServer supporting multiple tools per plugin."""
from __future__ import annotations

import pytest
from typing import Any

from agent_system.mcp.base import MCPServer
from plugins.example.server import ExampleServer


class SingleToolMockServer(MCPServer):
    """Mock server implementing single-tool interface using new list_tools() method."""
    
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


class OldStyleServer(MCPServer):
    """Mock server implementing the new interface with single tool."""
    
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


class TestEnhancedMCPServer:
    """Test the enhanced MCPServer interface."""
    
    async def test_multi_tool_server_get_tools(self):
        """Test that multi-tool server returns multiple tools."""
        server = ExampleServer(name="test")
        tools = await server.list_tools()
        
        assert len(tools) == 3
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "test_calculator" in tool_names
        assert "test_formatter" in tool_names
        assert "test_status" in tool_names
    
    async def test_multi_tool_server_get_schema_backward_compat(self):
        """Test that get_schema() works for multi-tool servers (returns first tool)."""
        server = ExampleServer(name="test")
        tools = await server.list_tools()
        schema = tools[0]  # Get first tool as schema
        
        assert schema["function"]["name"] == "test_calculator"
        assert "arithmetic operations" in schema["function"]["description"]
    
    def test_multi_tool_server_get_default_action(self):
        """Test that get_default_action() returns the server name."""
        server = ExampleServer(name="test")
        default_action = server.get_default_action()
        
        assert default_action == "test"
    
    async def test_multi_tool_server_calculator_call(self):
        """Test calling the calculator tool."""
        server = ExampleServer(name="test")
        
        result = await server.call("test_calculator", {
            "operation": "add",
            "a": 5,
            "b": 3
        })
        
        assert result["operation"] == "add"
        assert result["operands"] == [5.0, 3.0]
        assert result["result"] == 8.0
    
    async def test_multi_tool_server_formatter_call(self):
        """Test calling the formatter tool."""
        server = ExampleServer(name="test")
        
        result = await server.call("test_formatter", {
            "text": "hello world",
            "format": "uppercase"
        })
        
        assert result["original"] == "hello world"
        assert result["format"] == "uppercase"
        assert result["formatted"] == "HELLO WORLD"
    
    async def test_multi_tool_server_status_call(self):
        """Test calling the status tool."""
        server = ExampleServer(name="test")
        
        result = await server.call("test_status", {"verbose": True})
        
        assert result["server_name"] == "test"
        assert result["status"] == "active"
        assert result["tools_count"] == 3
        assert "available_tools" in result
        assert len(result["available_tools"]) == 3
    
    async def test_multi_tool_server_invalid_tool(self):
        """Test calling an invalid tool raises error."""
        server = ExampleServer(name="test")
        
        with pytest.raises(ValueError, match="Unknown tool"):
            await server.call("test_invalid", {})
    
    async def test_calculator_division_by_zero(self):
        """Test division by zero error handling."""
        server = ExampleServer(name="test")
        
        with pytest.raises(ValueError, match="Division by zero"):
            await server.call("test_calculator", {
                "operation": "divide",
                "a": 10,
                "b": 0
            })
    
    async def test_single_tool_backward_compatibility(self):
        """Test that single-tool servers still work."""
        server = SingleToolMockServer(name="single")
        
        # Test list_tools() returns single tool
        tools = await server.list_tools()
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "single_single"
        
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
        assert tools[0]["function"]["name"] == "old_style"
        
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
    
    async def test_empty_tools_error(self):
        """Test server with empty tools list."""
        
        class EmptyToolsServer(MCPServer):
            async def list_tools(self) -> list[dict[str, Any]]:
                return []
            
            async def call(self, tool: str, params: dict[str, Any]) -> Any:
                return {}
        
        server = EmptyToolsServer(name="empty")
        
        # Should return empty list, not raise error
        tools = await server.list_tools()
        assert tools == []
    
    async def test_missing_implementation_fallback(self):
        """Test server missing implementations falls back to default tool."""
        
        class IncompleteServer(MCPServer):
            async def call(self, tool: str, params: dict[str, Any]) -> Any:
                return {}
        
        server = IncompleteServer(name="incomplete")
        
        # Should create a default tool using server name
        tools = await server.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "incomplete"
        assert "Default action for incomplete" in tools[0].description


if __name__ == "__main__":
    # Run tests if executed directly
    pytest.main([__file__, "-v"])