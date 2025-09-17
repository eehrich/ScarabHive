"""Tests for enhanced MCPServer supporting multiple tools per plugin."""
from __future__ import annotations

import pytest
from typing import Any

from agent_system.mcp.base import MCPServer
from plugins.test_multi_tool.server import MultiToolTestServer


class SingleToolMockServer(MCPServer):
    """Mock server implementing old single-tool interface for backward compatibility testing."""
    
    def get_schema(self) -> dict[str, Any]:
        return {
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
        }
    
    def _get_default_action_impl(self) -> str:
        return f"{self.name}_single"
    
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        return {"tool": tool, "message": params["message"], "server": self.name}


class OldStyleServer(MCPServer):
    """Mock server implementing only the old abstract methods."""
    
    def get_schema(self) -> dict[str, Any]:
        return {
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
        }
    
    def _get_default_action_impl(self) -> str:
        return "old_style"
        
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        return {"legacy": True, "data": params["data"]}


@pytest.mark.asyncio
class TestEnhancedMCPServer:
    """Test the enhanced MCPServer interface."""
    
    def test_multi_tool_server_get_tools(self):
        """Test that multi-tool server returns multiple tools."""
        server = MultiToolTestServer(name="test")
        tools = server.get_tools()
        
        assert len(tools) == 3
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "test_calculator" in tool_names
        assert "test_formatter" in tool_names
        assert "test_status" in tool_names
    
    def test_multi_tool_server_get_schema_backward_compat(self):
        """Test that get_schema() works for multi-tool servers (returns first tool)."""
        server = MultiToolTestServer(name="test")
        schema = server.get_schema()
        
        assert schema["function"]["name"] == "test_calculator"
        assert "arithmetic operations" in schema["function"]["description"]
    
    def test_multi_tool_server_get_default_action(self):
        """Test that get_default_action() extracts from first tool."""
        server = MultiToolTestServer(name="test")
        default_action = server.get_default_action()
        
        assert default_action == "test_calculator"
    
    async def test_multi_tool_server_calculator_call(self):
        """Test calling the calculator tool."""
        server = MultiToolTestServer(name="test")
        
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
        server = MultiToolTestServer(name="test")
        
        result = await server.call("test_formatter", {
            "text": "hello world",
            "format": "uppercase"
        })
        
        assert result["original"] == "hello world"
        assert result["format"] == "uppercase"
        assert result["formatted"] == "HELLO WORLD"
    
    async def test_multi_tool_server_status_call(self):
        """Test calling the status tool."""
        server = MultiToolTestServer(name="test")
        
        result = await server.call("test_status", {"verbose": True})
        
        assert result["server_name"] == "test"
        assert result["status"] == "active"
        assert result["tools_count"] == 3
        assert "available_tools" in result
        assert len(result["available_tools"]) == 3
    
    async def test_multi_tool_server_invalid_tool(self):
        """Test calling an invalid tool raises error."""
        server = MultiToolTestServer(name="test")
        
        with pytest.raises(ValueError, match="Unknown tool"):
            await server.call("test_invalid", {})
    
    async def test_calculator_division_by_zero(self):
        """Test division by zero error handling."""
        server = MultiToolTestServer(name="test")
        
        with pytest.raises(ValueError, match="Division by zero"):
            await server.call("test_calculator", {
                "operation": "divide",
                "a": 10,
                "b": 0
            })
    
    def test_single_tool_backward_compatibility(self):
        """Test that single-tool servers still work."""
        server = SingleToolMockServer(name="single")
        
        # Test get_tools() returns single tool
        tools = server.get_tools()
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "single_single"
        
        # Test get_schema() works
        schema = server.get_schema()
        assert schema["function"]["name"] == "single_single"
        
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
    
    def test_old_style_server_compatibility(self):
        """Test old-style servers that only implement abstract methods."""
        server = OldStyleServer(name="old")
        
        # Test get_tools() returns wrapped single tool
        tools = server.get_tools()
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "old_style"
        
        # Test get_schema() works
        schema = server.get_schema()
        assert schema["function"]["name"] == "old_style"
        
        # Test get_default_action() extracts from tool name
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
    
    def test_empty_tools_error(self):
        """Test server with empty tools list raises error."""
        
        class EmptyToolsServer(MCPServer):
            def get_tools(self) -> list[dict[str, Any]]:
                return []
            
            async def call(self, tool: str, params: dict[str, Any]) -> Any:
                return {}
        
        server = EmptyToolsServer(name="empty")
        
        with pytest.raises(NotImplementedError, match="must implement either get_schema"):
            server.get_schema()
    
    def test_missing_implementation_error(self):
        """Test server missing both implementations raises error."""
        
        class IncompleteServer(MCPServer):
            async def call(self, tool: str, params: dict[str, Any]) -> Any:
                return {}
        
        server = IncompleteServer(name="incomplete")
        
        # This will recursively call itself and should raise NotImplementedError
        with pytest.raises(NotImplementedError):
            server.get_default_action()


if __name__ == "__main__":
    # Run tests if executed directly
    pytest.main([__file__, "-v"])