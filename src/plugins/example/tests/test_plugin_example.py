"""Test example plugin with modernized ToolServer pattern.

Tests the example plugin using the new SchemaBasedToolServer pattern
with automatic tool dispatching.
"""

from __future__ import annotations

import inspect
import pytest
from unittest.mock import Mock

from plugins.example.server import ExampleServer
from agent_system.config.models import AgentSystemConfig, ToolServerConfig


@pytest.fixture
def system_config():
    """Create a mock system config."""
    config = Mock(spec=AgentSystemConfig)
    config.network = Mock()
    config.network.ssl_verify = True
    return config


@pytest.fixture
def server_config():
    """Create a mock tool server config with example settings."""
    config = Mock(spec=ToolServerConfig)
    config.precision = 2
    config.max_text_length = 1000
    return config


@pytest.fixture
def example_server(system_config, server_config):
    """Create example server for testing."""
    return ExampleServer("example", system_config, server_config)


class TestExampleServerConstructor:
    """Test ExampleServer constructor and initialization."""

    def test_constructor_signature(self, system_config, server_config):
        """Test that constructor has modern signature."""
        server = ExampleServer("example", system_config, server_config)
        
        assert server.name == "example"
        assert server.system_config is system_config
        assert server.server_config is server_config

    def test_config_extraction(self, system_config):
        """Test configuration extraction from server_config."""
        config = Mock(spec=ToolServerConfig)
        config.precision = 4
        config.max_text_length = 500
        
        server = ExampleServer("example", system_config, config)
        
        assert server.precision == 4
        assert server.max_text_length == 500

    def test_config_defaults(self, system_config):
        """Test that default values are used when config attrs missing."""
        config = Mock(spec=ToolServerConfig)
        # Don't set precision or max_text_length attributes
        
        server = ExampleServer("example", system_config, config)
        
        # Should use defaults
        assert server.precision == 2
        assert server.max_text_length == 1000

    def test_no_legacy_attributes(self, example_server):
        """Test that legacy attributes are not present."""
        # Should not have ssl_verify attribute
        assert not hasattr(example_server, 'ssl_verify')
        
        # Should not have agent_config backwards compatibility
        assert not hasattr(example_server, 'agent_config')


class TestExampleServerTools:
    """Test tool definition and loading."""

    def test_get_tools_returns_three_tools(self, example_server):
        """Test that server exposes exactly 3 tools."""
        tools = example_server.get_tools()
        
        assert len(tools) == 3
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "example_calculator" in tool_names
        assert "example_formatter" in tool_names
        assert "example_status" in tool_names

    def test_tool_names_match_methods(self, example_server):
        """Test that tool names map to methods correctly with prefix stripping."""
        tools = example_server.get_tools()
        
        for tool in tools:
            tool_name = tool["function"]["name"]
            # Method name should be the stripped version
            method_name = example_server._get_method_name(tool_name)
            assert hasattr(example_server, method_name), \
                f"Method {method_name} not found for tool {tool_name}"
            assert callable(getattr(example_server, method_name))

    def test_no_manual_call_override(self):
        """Test that ExampleServer doesn't override call()."""
        # Should not have call() in its own __dict__ (inherits from SchemaBasedToolMixin)
        assert 'call' not in ExampleServer.__dict__


class TestCalculatorTool:
    """Test calculator tool functionality."""

    @pytest.mark.asyncio
    async def test_add_operation(self, example_server):
        """Test calculator addition operation."""
        result = await example_server.call("example_calculator", {
            "operation": "add",
            "a": 5.5,
            "b": 3.2
        })
        
        assert result["operation"] == "add"
        assert result["operands"] == [5.5, 3.2]
        assert result["result"] == 8.7
        assert result["precision"] == 2

    @pytest.mark.asyncio
    async def test_subtract_operation(self, example_server):
        """Test calculator subtraction operation."""
        result = await example_server.call("example_calculator", {
            "operation": "subtract",
            "a": 10,
            "b": 3
        })
        
        assert result["operation"] == "subtract"
        assert result["operands"] == [10.0, 3.0]
        assert result["result"] == 7.0

    @pytest.mark.asyncio
    async def test_multiply_operation(self, example_server):
        """Test calculator multiplication operation."""
        result = await example_server.call("example_calculator", {
            "operation": "multiply",
            "a": 4,
            "b": 2.5
        })
        
        assert result["operation"] == "multiply"
        assert result["operands"] == [4.0, 2.5]
        assert result["result"] == 10.0

    @pytest.mark.asyncio
    async def test_divide_operation(self, example_server):
        """Test calculator division operation."""
        result = await example_server.call("example_calculator", {
            "operation": "divide",
            "a": 15,
            "b": 3
        })
        
        assert result["operation"] == "divide"
        assert result["operands"] == [15.0, 3.0]
        assert result["result"] == 5.0

    @pytest.mark.asyncio
    async def test_division_by_zero(self, example_server):
        """Test calculator division by zero handling."""
        with pytest.raises(ValueError, match="Division by zero is not allowed"):
            await example_server.call("example_calculator", {
                "operation": "divide",
                "a": 10,
                "b": 0
            })

    @pytest.mark.asyncio
    async def test_invalid_operation(self, example_server):
        """Test calculator with invalid operation."""
        with pytest.raises(ValueError, match="Invalid operation 'power'"):
            await example_server.call("example_calculator", {
                "operation": "power",
                "a": 2,
                "b": 3
            })

    @pytest.mark.asyncio
    async def test_missing_parameters(self, example_server):
        """Test calculator with missing parameters."""
        with pytest.raises(ValueError, match="Missing required parameters"):
            await example_server.call("example_calculator", {
                "operation": "add",
                "a": 5
                # Missing 'b' parameter
            })

    @pytest.mark.asyncio
    async def test_invalid_number_format(self, example_server):
        """Test calculator with invalid number format."""
        with pytest.raises(TypeError, match="Invalid number format"):
            await example_server.call("example_calculator", {
                "operation": "add",
                "a": "not_a_number",
                "b": 5
            })

    @pytest.mark.asyncio
    async def test_custom_precision(self, system_config):
        """Test calculator with custom precision."""
        config = Mock(spec=ToolServerConfig)
        config.precision = 4
        config.max_text_length = 1000
        
        server = ExampleServer("example", system_config, config)
        
        result = await server.call("example_calculator", {
            "operation": "divide",
            "a": 1,
            "b": 3
        })
        
        assert result["precision"] == 4
        # 1/3 = 0.3333 with 4 decimal places
        assert result["result"] == 0.3333

    @pytest.mark.asyncio
    async def test_large_numbers(self, system_config, server_config):
        """Test calculator with very large numbers."""
        server = ExampleServer("example", system_config, server_config)
        
        result = await server.call("example_calculator", {
            "operation": "multiply",
            "a": 999999999999,
            "b": 999999999999
        })
        
        assert result["operation"] == "multiply"
        assert isinstance(result["result"], float)

    @pytest.mark.asyncio
    async def test_precision_zero(self, system_config):
        """Test calculator with zero precision."""
        config = Mock(spec=ToolServerConfig)
        config.precision = 0
        config.max_text_length = 1000
        
        server = ExampleServer("example", system_config, config)
        
        result = await server.call("example_calculator", {
            "operation": "divide",
            "a": 7,
            "b": 3
        })
        
        assert result["precision"] == 0
        assert result["result"] == 2  # Should round to 0 decimal places


class TestFormatterTool:
    """Test formatter tool functionality."""

    @pytest.mark.asyncio
    async def test_uppercase_format(self, example_server):
        """Test text formatter uppercase operation."""
        result = await example_server.call("example_formatter", {
            "text": "hello world",
            "format": "uppercase"
        })
        
        assert result["original"] == "hello world"
        assert result["format"] == "uppercase"
        assert result["formatted"] == "HELLO WORLD"
        assert result["length"] == 11

    @pytest.mark.asyncio
    async def test_lowercase_format(self, example_server):
        """Test text formatter lowercase operation."""
        result = await example_server.call("example_formatter", {
            "text": "HELLO WORLD",
            "format": "lowercase"
        })
        
        assert result["original"] == "HELLO WORLD"
        assert result["format"] == "lowercase"
        assert result["formatted"] == "hello world"

    @pytest.mark.asyncio
    async def test_title_format(self, example_server):
        """Test text formatter title case operation."""
        result = await example_server.call("example_formatter", {
            "text": "hello world test",
            "format": "title"
        })
        
        assert result["original"] == "hello world test"
        assert result["format"] == "title"
        assert result["formatted"] == "Hello World Test"

    @pytest.mark.asyncio
    async def test_reverse_format(self, example_server):
        """Test text formatter reverse operation."""
        result = await example_server.call("example_formatter", {
            "text": "hello",
            "format": "reverse"
        })
        
        assert result["original"] == "hello"
        assert result["format"] == "reverse"
        assert result["formatted"] == "olleh"

    @pytest.mark.asyncio
    async def test_invalid_format(self, example_server):
        """Test formatter with invalid format type."""
        with pytest.raises(ValueError, match="Invalid format 'capitalize'"):
            await example_server.call("example_formatter", {
                "text": "hello world",
                "format": "capitalize"
            })

    @pytest.mark.asyncio
    async def test_missing_parameters(self, example_server):
        """Test formatter with missing parameters."""
        with pytest.raises(ValueError, match="Missing required parameters"):
            await example_server.call("example_formatter", {
                "text": "hello world"
                # Missing 'format' parameter
            })

    @pytest.mark.asyncio
    async def test_invalid_text_type(self, example_server):
        """Test formatter with invalid text type."""
        with pytest.raises(TypeError, match="Text parameter must be a string"):
            await example_server.call("example_formatter", {
                "text": 12345,
                "format": "uppercase"
            })

    @pytest.mark.asyncio
    async def test_text_length_limit(self, system_config):
        """Test formatter text length validation."""
        config = Mock(spec=ToolServerConfig)
        config.precision = 2
        config.max_text_length = 5
        
        server = ExampleServer("example", system_config, config)
        
        with pytest.raises(ValueError, match="Text length 10 exceeds maximum 5"):
            await server.call("example_formatter", {
                "text": "1234567890",  # 10 characters
                "format": "uppercase"
            })

    @pytest.mark.asyncio
    async def test_empty_text(self, example_server):
        """Test formatting empty text."""
        result = await example_server.call("example_formatter", {
            "text": "",
            "format": "uppercase"
        })
        
        assert result["original"] == ""
        assert result["formatted"] == ""
        assert result["length"] == 0

    @pytest.mark.asyncio
    async def test_special_characters(self, example_server):
        """Test formatting text with special characters."""
        result = await example_server.call("example_formatter", {
            "text": "héllo wørld! 123 @#$",
            "format": "uppercase"
        })
        
        assert result["formatted"] == "HÉLLO WØRLD! 123 @#$"

    @pytest.mark.asyncio
    async def test_unicode_text(self, example_server):
        """Test formatting Unicode text."""
        result = await example_server.call("example_formatter", {
            "text": "🌟 Hello 世界 🌟",
            "format": "reverse"
        })
        
        assert result["formatted"] == "🌟 界世 olleH 🌟"


class TestStatusTool:
    """Test status tool functionality."""

    @pytest.mark.asyncio
    async def test_status_basic(self, example_server):
        """Test status information basic request."""
        result = await example_server.call("example_status", {})
        
        assert result["server_name"] == "example"
        assert result["status"] == "active"
        assert result["tools_count"] == 3
        assert result["version"] == "1.0.0"
        assert "config" not in result  # Not verbose

    @pytest.mark.asyncio
    async def test_status_verbose(self, example_server):
        """Test status information verbose request."""
        result = await example_server.call("example_status", {"verbose": True})
        
        assert result["server_name"] == "example"
        assert result["status"] == "active"
        assert result["tools_count"] == 3
        assert result["version"] == "1.0.0"
        
        # Verbose information
        assert "config" in result
        assert result["config"]["precision"] == 2
        assert result["config"]["max_text_length"] == 1000
        # Note: ssl_verify removed from modern pattern
        assert "ssl_verify" not in result["config"]
        
        assert "available_tools" in result
        assert len(result["available_tools"]) == 3

    @pytest.mark.asyncio
    async def test_status_non_verbose_default(self, example_server):
        """Test that status defaults to non-verbose."""
        result = await example_server.call("example_status", {})
        
        assert "config" not in result
        assert "available_tools" not in result


class TestGenericDispatcher:
    """Test generic dispatcher integration."""

    @pytest.mark.asyncio
    async def test_automatic_routing(self, example_server):
        """Test that tools are automatically routed to methods."""
        # Call through generic dispatcher
        result = await example_server.call("example_calculator", {
            "operation": "add",
            "a": 1,
            "b": 2
        })
        
        assert result["result"] == 3

    @pytest.mark.asyncio
    async def test_invalid_tool_error(self, example_server):
        """Test calling non-existent tool gives helpful error."""
        with pytest.raises(ValueError) as exc_info:
            await example_server.call("example_nonexistent", {})
        
        error_msg = str(exc_info.value)
        assert "nonexistent" in error_msg
        # Should list available tools
        assert "example_calculator" in error_msg or "Available" in error_msg


class TestIntegration:
    """Integration tests for example plugin."""

    @pytest.mark.asyncio
    async def test_complete_workflow(self, system_config):
        """Test complete plugin workflow with all tools."""
        config = Mock(spec=ToolServerConfig)
        config.precision = 3
        config.max_text_length = 100
        
        server = ExampleServer("example", system_config, config)
        
        # Test status
        status = await server.call("example_status", {"verbose": True})
        assert status["server_name"] == "example"
        assert status["config"]["precision"] == 3
        
        # Test calculator
        calc_result = await server.call("example_calculator", {
            "operation": "add",
            "a": 1.111,
            "b": 2.222
        })
        assert calc_result["result"] == 3.333
        
        # Test formatter
        format_result = await server.call("example_formatter", {
            "text": "example",
            "format": "title"
        })
        assert format_result["formatted"] == "Example"

    @pytest.mark.asyncio
    async def test_config_isolation(self, system_config):
        """Test that different instances have isolated configs."""
        config1 = Mock(spec=ToolServerConfig)
        config1.precision = 2
        config1.max_text_length = 100
        
        config2 = Mock(spec=ToolServerConfig)
        config2.precision = 5
        config2.max_text_length = 500
        
        server1 = ExampleServer("server1", system_config, config1)
        server2 = ExampleServer("server2", system_config, config2)
        
        assert server1.precision == 2
        assert server2.precision == 5
        assert server1.max_text_length == 100
        assert server2.max_text_length == 500


class TestModernPattern:
    """Test modern ToolServer pattern compliance."""

    def test_inherits_from_schema_based_mcp_server(self):
        """Test that ExampleServer inherits from SchemaBasedToolServer."""
        from agent_system.tools.schema_based import SchemaBasedToolServer
        from agent_system.tools.base import ToolServer
        
        assert issubclass(ExampleServer, SchemaBasedToolServer)
        assert issubclass(ExampleServer, ToolServer)

    def test_uses_modern_constructor(self, system_config, server_config):
        """Test modern constructor signature."""
        sig = inspect.signature(ExampleServer.__init__)
        params = list(sig.parameters.keys())
        
        assert params == ['self', 'name', 'system_config', 'server_config']

    def test_no_backwards_compatibility(self, example_server):
        """Test that backwards compatibility is completely removed."""
        # No agent_config alias
        assert not hasattr(example_server, 'agent_config')
        
        # No ssl_verify attribute
        assert not hasattr(example_server, 'ssl_verify')
        
        # No registry parameter
        sig = inspect.signature(ExampleServer.__init__)
        assert 'registry' not in sig.parameters

    @pytest.mark.asyncio
    async def test_method_names_match_tools(self, example_server):
        """Test that all tool methods follow naming convention with prefix stripping."""
        tools = example_server.get_tools()
        
        for tool in tools:
            tool_name = tool["function"]["name"]
            # Method name should be the tool name with prefix stripped
            # Tool: "example_calculator" → Method: "calculator"
            method_name = example_server._get_method_name(tool_name)
            
            assert hasattr(example_server, method_name), \
                f"Method {method_name} not found for tool {tool_name}"
            
            method = getattr(example_server, method_name)
            assert callable(method)
            
            # Should be async
            assert inspect.iscoroutinefunction(method), \
                f"Method {method_name} should be async"
