"""Integration tests for the example plugin."""

from __future__ import annotations

import pytest

from plugins.example.server import ExampleServer
from plugins.example.plugin import PLUGIN_FACTORY


class TestExamplePluginIntegration:
    """Test complete plugin integration and functionality."""
    
    def test_example_plugin_factory(self):
        """Test plugin factory creates server correctly."""
        config = {
            "precision": 3,
            "max_text_length": 500,
            "enable_debug": True
        }
        
        server = PLUGIN_FACTORY("integration_test", config)
        
        assert isinstance(server, ExampleServer)
        assert server.name == "integration_test"
        assert server.precision == 3
        assert server.max_text_length == 500
        assert server.debug_enabled is True
    
    def test_example_plugin_factory_default_config(self):
        """Test plugin factory with default configuration."""
        server = PLUGIN_FACTORY("test")
        
        assert server.precision == 2
        assert server.max_text_length == 1000
        assert server.debug_enabled is False
    
    def test_example_plugin_factory_invalid_config(self):
        """Test plugin factory with invalid configuration."""
        with pytest.raises(ValueError, match="Invalid plugin configuration"):
            PLUGIN_FACTORY("test", {"precision": -1})
    
    def test_example_get_tools_count(self):
        """Test that server provides expected number of tools."""
        server = ExampleServer(name="test")
        tools = server.get_tools()
        
        assert len(tools) == 3
        
        tool_names = [tool["function"]["name"] for tool in tools]
        expected_names = ["test_calculator", "test_formatter", "test_status"]
        
        assert all(name in tool_names for name in expected_names)
    
    def test_example_schema_loading(self):
        """Test schema loading from external file."""
        server = ExampleServer(name="test")
        
        tools = server.get_tools()
        
        # Should have tools regardless of schema source
        assert len(tools) >= 3
        
        # First tool should be calculator
        calc_tool = tools[0]
        assert calc_tool["function"]["name"] == "test_calculator"
        assert "arithmetic operations" in calc_tool["function"]["description"].lower()
    
    def test_example_tools_naming_convention(self):
        """Test that tools follow modern naming conventions."""
        server = ExampleServer(name="test")
        
        tools = server.get_tools()
        
        # Should have tools with proper names
        assert len(tools) >= 3
        
        # First tool should be calculator
        calc_tool = tools[0]
        assert calc_tool["function"]["name"] == "test_calculator"
        assert "arithmetic operations" in calc_tool["function"]["description"].lower()
    
    def test_example_tools_availability(self):
        """Test tool availability and naming."""
        server = ExampleServer(name="test")
        tools = server.get_tools()
        
        # Check we have the expected tools
        tool_names = [tool["function"]["name"] for tool in tools]
        expected_names = ["test_calculator", "test_formatter", "test_status"]
        
        assert all(name in tool_names for name in expected_names)
    
    async def test_example_tool_routing(self):
        """Test that tool calls are routed correctly."""
        server = ExampleServer(name="test")
        
        # Test each tool type
        calc_result = await server.call("test_calculator", {
            "operation": "add", "a": 1, "b": 2
        })
        assert calc_result["result"] == 3.0
        
        fmt_result = await server.call("test_formatter", {
            "text": "test", "format": "uppercase"
        })
        assert fmt_result["formatted"] == "TEST"
        
        status_result = await server.call("test_status", {})
        assert status_result["server_name"] == "test"
    
    async def test_example_invalid_tool_call(self):
        """Test calling non-existent tool."""
        server = ExampleServer(name="test")
        
        with pytest.raises(ValueError, match="Unknown tool 'test_invalid'"):
            await server.call("test_invalid", {})
    
    async def test_example_wrong_prefix_tool_call(self):
        """Test calling tool with wrong prefix."""
        server = ExampleServer(name="test")
        
        with pytest.raises(ValueError, match="does not match plugin prefix"):
            await server.call("other_calculator", {"operation": "add", "a": 1, "b": 2})
    
    async def test_example_configuration_affects_behavior(self):
        """Test that configuration affects tool behavior."""
        # High precision server
        high_prec_server = ExampleServer(name="test", config={"precision": 5})
        result = await high_prec_server.call("test_calculator", {
            "operation": "divide", "a": 1, "b": 3
        })
        assert result["precision"] == 5
        assert abs(result["result"] - 0.33333) < 1e-5
        
        # Limited text length server
        limited_server = ExampleServer(name="test", config={"max_text_length": 5})
        with pytest.raises(ValueError, match="exceeds maximum 5"):
            await limited_server.call("test_formatter", {
                "text": "too long text", "format": "uppercase"
            })
    
    async def test_example_status_tool_verbose(self):
        """Test status tool with verbose output."""
        server = ExampleServer(name="test", config={
            "precision": 3,
            "max_text_length": 200,
            "enable_debug": True
        })
        
        result = await server.call("test_status", {"verbose": True})
        
        assert result["server_name"] == "test"
        assert result["status"] == "active"
        assert result["tools_count"] == 3
        assert result["version"] == "1.0.0"
        
        # Check verbose fields
        assert "config" in result
        assert result["config"]["precision"] == 3
        assert result["config"]["max_text_length"] == 200
        assert result["config"]["debug_enabled"] is True
        
        assert "available_tools" in result
        assert len(result["available_tools"]) == 3
        
        assert "schema_source" in result
        assert result["schema_source"] in ["external", "inline"]
    
    async def test_example_error_handling_and_logging(self):
        """Test error handling includes proper context."""
        server = ExampleServer(name="test")
        
        # Test that errors include tool context for invalid operation
        try:
            await server.call("test_calculator", {
                "operation": "invalid",
                "a": 1,
                "b": 2
            })
        except ValueError as e:
            assert "invalid" in str(e).lower()
    
    def test_example_template_variable_replacement(self):
        """Test that template variables are replaced correctly."""
        server = ExampleServer(name="myPlugin")
        tools = server.get_tools()
        
        # All tool names should have the plugin name
        for tool in tools:
            tool_name = tool["function"]["name"]
            assert tool_name.startswith("myPlugin_")
    
    async def test_example_concurrent_calls(self):
        """Test that multiple concurrent calls work correctly."""
        import asyncio
        
        server = ExampleServer(name="test")
        
        # Create multiple concurrent tasks
        tasks = [
            server.call("test_calculator", {"operation": "add", "a": i, "b": i+1})
            for i in range(5)
        ]
        
        results = await asyncio.gather(*tasks)
        
        # Check all results are correct
        for i, result in enumerate(results):
            expected = i + (i + 1)
            assert result["result"] == expected