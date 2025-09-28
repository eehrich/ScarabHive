"""Test plugin for example plugin functionality."""

import pytest
from unittest.mock import AsyncMock

from plugins.example.plugin import PLUGIN_FACTORY
from plugins.example.server import ExampleServer


class TestPluginExample:
    """Test the example plugin functionality."""

    @pytest.fixture
    def example_server(self):
        """Create example server for testing."""
        return ExampleServer("test_example", {"precision": 2, "max_text_length": 1000})

    @pytest.fixture
    def mock_status(self):
        """Create mock status for testing."""
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        mock_status.error = AsyncMock()
        mock_status.end = AsyncMock()
        return mock_status

    def test_plugin_factory_basic(self):
        """Test basic plugin factory functionality."""
        server = PLUGIN_FACTORY("test_example")
        assert isinstance(server, ExampleServer)
        assert server.name == "test_example"
        assert server.precision == 2
        assert server.max_text_length == 1000

    def test_plugin_factory_with_config(self):
        """Test plugin factory with custom configuration."""
        config = {
            "precision": 4,
            "max_text_length": 500,
            "enable_debug": True
        }
        server = PLUGIN_FACTORY("test_example", config)
        assert server.precision == 4
        assert server.max_text_length == 500

    def test_plugin_factory_config_validation(self):
        """Test plugin factory configuration validation."""
        # Test invalid precision
        with pytest.raises(ValueError, match="precision must be between 0 and 10"):
            PLUGIN_FACTORY("test", {"precision": 15})
        
        # Test invalid max_text_length
        with pytest.raises(ValueError, match="max_text_length must be between 1 and 100000"):
            PLUGIN_FACTORY("test", {"max_text_length": -1})

    def test_server_initialization(self):
        """Test server initialization with different configs."""
        server = ExampleServer("test", {"precision": 3, "max_text_length": 2000})
        assert server.name == "test"
        assert server.precision == 3
        assert server.max_text_length == 2000

    def test_server_get_tools(self):
        """Test that server exposes expected tools."""
        server = ExampleServer("test")
        tools = server.get_tools()
        
        assert len(tools) == 3
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "test_calculator" in tool_names
        assert "test_formatter" in tool_names
        assert "test_status" in tool_names

    @pytest.mark.asyncio
    async def test_calculator_add(self, example_server):
        """Test calculator addition operation."""
        result = await example_server.call("test_example_calculator", {
            "operation": "add",
            "a": 5.5,
            "b": 3.2
        })
        
        assert result["operation"] == "add"
        assert result["operands"] == [5.5, 3.2]
        assert result["result"] == 8.7
        assert result["precision"] == 2

    @pytest.mark.asyncio
    async def test_calculator_subtract(self, example_server):
        """Test calculator subtraction operation."""
        result = await example_server.call("test_example_calculator", {
            "operation": "subtract",
            "a": 10,
            "b": 3
        })
        
        assert result["operation"] == "subtract"
        assert result["operands"] == [10.0, 3.0]
        assert result["result"] == 7.0

    @pytest.mark.asyncio
    async def test_calculator_multiply(self, example_server):
        """Test calculator multiplication operation."""
        result = await example_server.call("test_example_calculator", {
            "operation": "multiply",
            "a": 4,
            "b": 2.5
        })
        
        assert result["operation"] == "multiply"
        assert result["operands"] == [4.0, 2.5]
        assert result["result"] == 10.0

    @pytest.mark.asyncio
    async def test_calculator_divide(self, example_server):
        """Test calculator division operation."""
        result = await example_server.call("test_example_calculator", {
            "operation": "divide",
            "a": 15,
            "b": 3
        })
        
        assert result["operation"] == "divide"
        assert result["operands"] == [15.0, 3.0]
        assert result["result"] == 5.0

    @pytest.mark.asyncio
    async def test_calculator_division_by_zero(self, example_server):
        """Test calculator division by zero handling."""
        with pytest.raises(ValueError, match="Division by zero is not allowed"):
            await example_server.call("test_example_calculator", {
                "operation": "divide",
                "a": 10,
                "b": 0
            })

    @pytest.mark.asyncio
    async def test_calculator_invalid_operation(self, example_server):
        """Test calculator with invalid operation."""
        with pytest.raises(ValueError, match="Invalid operation 'power'"):
            await example_server.call("test_example_calculator", {
                "operation": "power",
                "a": 2,
                "b": 3
            })

    @pytest.mark.asyncio
    async def test_calculator_missing_parameters(self, example_server):
        """Test calculator with missing parameters."""
        with pytest.raises(ValueError, match="Missing required parameters"):
            await example_server.call("test_example_calculator", {
                "operation": "add",
                "a": 5
                # Missing 'b' parameter
            })

    @pytest.mark.asyncio
    async def test_calculator_invalid_number_format(self, example_server):
        """Test calculator with invalid number format."""
        with pytest.raises(TypeError, match="Invalid number format"):
            await example_server.call("test_example_calculator", {
                "operation": "add",
                "a": "not_a_number",
                "b": 5
            })

    @pytest.mark.asyncio
    async def test_calculator_precision_handling(self):
        """Test calculator precision configuration."""
        server = ExampleServer("test", {"precision": 4})
        result = await server.call("test_calculator", {
            "operation": "divide",
            "a": 1,
            "b": 3
        })
        
        assert result["precision"] == 4
        # 1/3 = 0.3333 with 4 decimal places
        assert result["result"] == 0.3333

    @pytest.mark.asyncio
    async def test_formatter_uppercase(self, example_server):
        """Test text formatter uppercase operation."""
        result = await example_server.call("test_example_formatter", {
            "text": "hello world",
            "format": "uppercase"
        })
        
        assert result["original"] == "hello world"
        assert result["format"] == "uppercase"
        assert result["formatted"] == "HELLO WORLD"
        assert result["length"] == 11

    @pytest.mark.asyncio
    async def test_formatter_lowercase(self, example_server):
        """Test text formatter lowercase operation."""
        result = await example_server.call("test_example_formatter", {
            "text": "HELLO WORLD",
            "format": "lowercase"
        })
        
        assert result["original"] == "HELLO WORLD"
        assert result["format"] == "lowercase"
        assert result["formatted"] == "hello world"

    @pytest.mark.asyncio
    async def test_formatter_title(self, example_server):
        """Test text formatter title case operation."""
        result = await example_server.call("test_example_formatter", {
            "text": "hello world test",
            "format": "title"
        })
        
        assert result["original"] == "hello world test"
        assert result["format"] == "title"
        assert result["formatted"] == "Hello World Test"

    @pytest.mark.asyncio
    async def test_formatter_reverse(self, example_server):
        """Test text formatter reverse operation."""
        result = await example_server.call("test_example_formatter", {
            "text": "hello",
            "format": "reverse"
        })
        
        assert result["original"] == "hello"
        assert result["format"] == "reverse"
        assert result["formatted"] == "olleh"

    @pytest.mark.asyncio
    async def test_formatter_invalid_format(self, example_server):
        """Test formatter with invalid format type."""
        with pytest.raises(ValueError, match="Invalid format 'capitalize'"):
            await example_server.call("test_example_formatter", {
                "text": "hello world",
                "format": "capitalize"
            })

    @pytest.mark.asyncio
    async def test_formatter_missing_parameters(self, example_server):
        """Test formatter with missing parameters."""
        with pytest.raises(ValueError, match="Missing required parameters"):
            await example_server.call("test_example_formatter", {
                "text": "hello world"
                # Missing 'format' parameter
            })

    @pytest.mark.asyncio
    async def test_formatter_invalid_text_type(self, example_server):
        """Test formatter with invalid text type."""
        with pytest.raises(TypeError, match="Text parameter must be a string"):
            await example_server.call("test_example_formatter", {
                "text": 12345,
                "format": "uppercase"
            })

    @pytest.mark.asyncio
    async def test_formatter_text_length_limit(self):
        """Test formatter text length validation."""
        server = ExampleServer("test", {"max_text_length": 5})
        
        with pytest.raises(ValueError, match="Text length 10 exceeds maximum 5"):
            await server.call("test_formatter", {
                "text": "1234567890",  # 10 characters
                "format": "uppercase"
            })

    @pytest.mark.asyncio
    async def test_status_basic(self, example_server):
        """Test status information basic request."""
        result = await example_server.call("test_example_status", {})
        
        assert result["server_name"] == "test_example"
        assert result["status"] == "active"
        assert result["tools_count"] == 3
        assert result["version"] == "1.0.0"
        assert "config" not in result  # Not verbose

    @pytest.mark.asyncio
    async def test_status_verbose(self, example_server):
        """Test status information verbose request."""
        result = await example_server.call("test_example_status", {"verbose": True})
        
        assert result["server_name"] == "test_example"
        assert result["status"] == "active"
        assert result["tools_count"] == 3
        assert result["version"] == "1.0.0"
        
        # Verbose information
        assert "config" in result
        assert result["config"]["precision"] == 2
        assert result["config"]["max_text_length"] == 1000
        assert result["config"]["ssl_verify"] is True
        
        assert "available_tools" in result
        assert len(result["available_tools"]) == 3

    @pytest.mark.asyncio
    async def test_invalid_tool_call(self, example_server):
        """Test calling non-existent tool."""
        with pytest.raises(ValueError, match="Unknown tool 'test_example_nonexistent'"):
            await example_server.call("test_example_nonexistent", {})

    def test_plugin_discovery(self):
        """Test that example plugin can be discovered."""
        from pathlib import Path
        from agent_system.plugins import discover_all_plugins
        
        repo_root = Path(__file__).parent.parent
        plugin_dirs = [repo_root / 'src' / 'plugins']
        
        plugins = discover_all_plugins(plugin_dirs)
        assert 'example' in plugins
        
        factory = plugins['example']
        server = factory("test_example")
        assert isinstance(server, ExampleServer)


class TestPluginExampleEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_large_number_calculation(self):
        """Test calculator with very large numbers."""
        server = ExampleServer("test", {"precision": 2})
        result = await server.call("test_calculator", {
            "operation": "multiply",
            "a": 999999999999,
            "b": 999999999999
        })
        
        assert result["operation"] == "multiply"
        # Should handle large numbers correctly
        assert isinstance(result["result"], float)

    @pytest.mark.asyncio
    async def test_decimal_precision_edge_cases(self):
        """Test decimal precision with edge cases."""
        server = ExampleServer("test", {"precision": 0})
        result = await server.call("test_calculator", {
            "operation": "divide",
            "a": 7,
            "b": 3
        })
        
        assert result["precision"] == 0
        assert result["result"] == 2  # Should round to 0 decimal places

    @pytest.mark.asyncio
    async def test_empty_text_formatting(self):
        """Test formatting empty text."""
        server = ExampleServer("test")
        result = await server.call("test_formatter", {
            "text": "",
            "format": "uppercase"
        })
        
        assert result["original"] == ""
        assert result["formatted"] == ""
        assert result["length"] == 0

    @pytest.mark.asyncio
    async def test_special_characters_formatting(self):
        """Test formatting text with special characters."""
        server = ExampleServer("test")
        result = await server.call("test_formatter", {
            "text": "héllo wørld! 123 @#$",
            "format": "uppercase"
        })
        
        assert result["formatted"] == "HÉLLO WØRLD! 123 @#$"

    @pytest.mark.asyncio
    async def test_unicode_text_formatting(self):
        """Test formatting Unicode text."""
        server = ExampleServer("test")
        result = await server.call("test_formatter", {
            "text": "🌟 Hello 世界 🌟",
            "format": "reverse"
        })
        
        assert result["formatted"] == "🌟 界世 olleH 🌟"


class TestPluginExampleIntegration:
    """Integration tests for example plugin."""

    @pytest.mark.asyncio
    async def test_plugin_lifecycle(self):
        """Test complete plugin lifecycle."""
        # Create plugin
        server = PLUGIN_FACTORY("integration_test", {
            "precision": 3,
            "max_text_length": 100
        })
        
        # Test status
        status = await server.call("integration_test_status", {"verbose": True})
        assert status["server_name"] == "integration_test"
        assert status["config"]["precision"] == 3
        
        # Test calculator
        calc_result = await server.call("integration_test_calculator", {
            "operation": "add",
            "a": 1.111,
            "b": 2.222
        })
        assert calc_result["result"] == 3.333
        
        # Test formatter
        format_result = await server.call("integration_test_formatter", {
            "text": "test",
            "format": "title"
        })
        assert format_result["formatted"] == "Test"

    @pytest.mark.asyncio
    async def test_configuration_inheritance(self):
        """Test that configuration is properly inherited."""
        config = {
            "precision": 5,
            "max_text_length": 50,
            "enable_debug": True
        }
        
        server = PLUGIN_FACTORY("config_test", config)
        
        # Verify config is applied
        assert server.precision == 5
        assert server.max_text_length == 50
        
        # Test precision in action
        result = await server.call("config_test_calculator", {
            "operation": "divide",
            "a": 22,
            "b": 7
        })
        assert result["precision"] == 5
        # 22/7 = 3.14286 with 5 decimal places
        assert result["result"] == 3.14286

    def test_ssl_verify_configuration(self):
        """Test SSL verification configuration."""
        server = PLUGIN_FACTORY("ssl_test", ssl_verify=False)
        assert server.ssl_verify is False
        
        server = PLUGIN_FACTORY("ssl_test", ssl_verify=True)
        assert server.ssl_verify is True