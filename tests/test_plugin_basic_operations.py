"""Tests for BasicOperations MCP plugin.

This module contains comprehensive tests for the BasicOperations plugin,
covering wait operations with status updates, echo functionality, and ping operations.
"""

import pytest
import asyncio
import time
from unittest.mock import AsyncMock
from pathlib import Path

from plugins.basic_operations.server import BasicOperationsServer
from plugins.basic_operations.plugin import PLUGIN_FACTORY


class TestBasicOperationsServer:
    """Test the BasicOperationsServer class functionality."""

    def test_server_initialization(self):
        """Test basic server initialization."""
        server = BasicOperationsServer("test_basic_ops", {}, True)
        assert server.name == "test_basic_ops"
        assert server.ssl_verify is True
        assert server.max_wait_seconds == 3600  # new default
        assert server.default_update_interval == 1.0  # default

    def test_server_initialization_with_config(self):
        """Test server initialization with custom config."""
        config = {
            "max_wait_seconds": 60,
            "default_update_interval": 0.5
        }
        server = BasicOperationsServer("test_basic_ops", config, False)
        assert server.name == "test_basic_ops"
        assert server.ssl_verify is False
        assert server.max_wait_seconds == 60
        assert server.default_update_interval == 0.5

    def test_server_tools_schema(self):
        """Test server tools structure."""
        server = BasicOperationsServer("basic_ops", {}, True)
        tools = server.get_tools()
        
        assert isinstance(tools, list)
        assert len(tools) == 2
        
        # Check tool names
        tool_names = [tool["function"]["name"] for tool in tools]
        expected_names = ["wait", "ping"]
        assert set(tool_names) == set(expected_names)
        
        # Verify each tool has proper structure
        for tool in tools:
            assert tool["type"] == "function"
            assert "function" in tool
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]

    def test_server_default_action(self):
        """Test default action."""
        server = BasicOperationsServer("basic_ops", {}, True)
        assert server.get_default_action() == "ping"

    @pytest.mark.asyncio
    async def test_ping_tool_basic(self):
        """Test basic ping functionality."""
        server = BasicOperationsServer("basic_ops", {}, True)
        
        result = await server.call("ping", {})
        
        assert result["status"] == "success"
        assert result["message"] == "Pong!"
        assert "timestamp" in result
        assert result["server_name"] == "basic_ops"
        assert "details" not in result  # default is no details

    @pytest.mark.asyncio
    async def test_ping_tool_with_details(self):
        """Test ping with detailed information."""
        server = BasicOperationsServer("basic_ops", {}, True)
        
        result = await server.call("ping", {"include_details": True})
        
        assert result["status"] == "success"
        assert result["message"] == "Pong!"
        assert "details" in result
        assert "python_version" in result["details"]
        assert "platform" in result["details"]
        assert "server_config" in result["details"]



    @pytest.mark.asyncio
    async def test_wait_tool_basic(self):
        """Test basic wait functionality."""
        server = BasicOperationsServer("basic_ops", {}, True)
        
        start_time = time.time()
        # Provide a mock status because the server assumes one is always present
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("wait", {"seconds": 0.1, "_status": mock_status})
        elapsed = time.time() - start_time

        assert result["status"] == "success"
        assert result["message"] == "Wait completed successfully"
        assert result["requested_seconds"] == 0.1
        assert 0.05 <= elapsed <= 0.2  # Allow some tolerance
        assert result["user_message"] == "Waiting"  # default message

    @pytest.mark.asyncio
    async def test_wait_tool_with_message(self):
        """Test wait with custom message."""
        server = BasicOperationsServer("basic_ops", {}, True)
        
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("wait", {
            "seconds": 0.1,
            "message": "Test Operation",
            "_status": mock_status
        })
        
        assert result["status"] == "success"
        assert result["user_message"] == "Test Operation"

    @pytest.mark.asyncio
    async def test_wait_tool_with_status_updates(self):
        """Test wait with status update monitoring."""
        server = BasicOperationsServer("basic_ops", {"default_update_interval": 0.1}, True)
        
        # Mock status object to capture updates
        mock_status = AsyncMock()
        status_calls = []
        
        async def capture_status_update(message):
            status_calls.append(message)
        
        mock_status.progress = AsyncMock(side_effect=capture_status_update)
        
        result = await server.call("wait", {
            "seconds": 0.3,
            "_status": mock_status
        })
        
        assert result["status"] == "success"
        assert len(status_calls) >= 2  # Should have multiple updates
        # Status updates should include the default 'waiting' message and countdown/completion info
        assert any("waiting" in call.lower() for call in status_calls)
        assert any("remaining" in call for call in status_calls)
        assert any("completed" in call.lower() for call in status_calls)

    @pytest.mark.asyncio
    async def test_wait_tool_parameter_validation(self):
        """Test wait tool parameter validation."""
        server = BasicOperationsServer("basic_ops", {"max_wait_seconds": 5}, True)
        
        # Test negative seconds
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("wait", {"seconds": -1, "_status": mock_status})
        assert result["status"] == "error"
        assert "must be positive" in result["error"]

        # Test zero seconds
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("wait", {"seconds": 0, "_status": mock_status})
        assert result["status"] == "error"
        assert "must be positive" in result["error"]

        # Test exceeds maximum
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("wait", {"seconds": 10, "_status": mock_status})
        assert result["status"] == "error"
        assert "exceeds maximum" in result["error"]

    @pytest.mark.asyncio
    async def test_wait_tool_custom_update_interval(self):
        """Test wait with custom update interval."""
        server = BasicOperationsServer("basic_ops", {}, True)
        
        mock_status = AsyncMock()
        status_calls = []
        
        async def capture_status_update(message):
            status_calls.append(message)
        
        mock_status.progress = AsyncMock(side_effect=capture_status_update)
        
        result = await server.call("wait", {
            "seconds": 0.2,
            "update_interval": 0.05,  # More frequent updates
            "_status": mock_status
        })
        
        assert result["status"] == "success"
        # With smaller interval, should get more status updates
        assert len(status_calls) >= 3

    @pytest.mark.asyncio
    async def test_invalid_tool_name(self):
        """Test calling an invalid tool name."""
        server = BasicOperationsServer("basic_ops", {}, True)
        
        result = await server.call("invalid_tool", {})
        
        assert result["status"] == "error"
        assert "Unknown tool" in result["error"]
        assert "invalid_tool" in result["error"]


class TestBasicOperationsPlugin:
    """Test the plugin factory and configuration."""

    def test_plugin_factory_basic(self):
        """Test basic plugin factory functionality."""
        server = PLUGIN_FACTORY("test_basic_ops")
        
        assert isinstance(server, BasicOperationsServer)
        assert server.name == "test_basic_ops"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self):
        """Test plugin factory with custom configuration."""
        config = {
            "max_wait_seconds": 120,
            "default_update_interval": 2.0
        }
        
        server = PLUGIN_FACTORY("test_basic_ops", config, False)
        
        assert isinstance(server, BasicOperationsServer)
        assert server.name == "test_basic_ops"
        assert server.ssl_verify is False
        assert server.max_wait_seconds == 120
        assert server.default_update_interval == 2.0

    def test_plugin_discovery(self):
        """Test that plugin can be discovered from plugin directory."""
        # Check that the plugin files exist
        plugin_dir = Path(__file__).parent.parent / "src" / "plugins" / "basic_operations"
        assert plugin_dir.exists()
        assert (plugin_dir / "__init__.py").exists()
        assert (plugin_dir / "server.py").exists()
        assert (plugin_dir / "plugin.py").exists()
        assert (plugin_dir / "schema.yaml").exists()

    def test_plugin_info_metadata(self):
        """Test plugin metadata."""
        from plugins.basic_operations.plugin import PLUGIN_INFO
        
        assert PLUGIN_INFO["name"] == "basic_operations"
        assert PLUGIN_INFO["version"] == "1.0.0"
        assert "description" in PLUGIN_INFO
        assert "tools" in PLUGIN_INFO
        assert len(PLUGIN_INFO["tools"]) == 2


class TestBasicOperationsIntegration:
    """Integration tests for BasicOperations plugin."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_waits(self):
        """Test multiple concurrent wait operations."""
        server = BasicOperationsServer("basic_ops", {}, True)
        
        # Start multiple wait operations concurrently
        # Provide status mocks for each concurrent task
        tasks = [
            server.call("wait", {"seconds": 0.1, "message": f"Wait {i}", "_status": AsyncMock(progress=AsyncMock())})
            for i in range(3)
        ]
        
        start_time = time.time()
        results = await asyncio.gather(*tasks)
        elapsed = time.time() - start_time
        
        # All should complete successfully
        for result in results:
            assert result["status"] == "success"
        
        # Should complete roughly in parallel (not sequentially)
        assert elapsed < 0.5  # Much less than 3 * 0.1 = 0.3

    @pytest.mark.asyncio
    async def test_wait_with_interrupt_simulation(self):
        """Test wait behavior with simulated interruption."""
        server = BasicOperationsServer("basic_ops", {}, True)
        
        # Start a wait operation and then "cancel" it by timing out the test
        try:
            await asyncio.wait_for(
                server.call("wait", {"seconds": 10, "_status": AsyncMock(progress=AsyncMock())}),
                timeout=0.1
            )
            # Should not reach here
            assert False, "Wait should have been interrupted"
        except asyncio.TimeoutError:
            # Expected behavior - wait was interrupted
            pass

    @pytest.mark.asyncio
    async def test_comprehensive_tool_workflow(self):
        """Test a complete workflow using all tools."""
        server = BasicOperationsServer("basic_ops", {}, True)
        
        # 1. Start with a ping
        ping_result = await server.call("ping", {"include_details": True})
        assert ping_result["status"] == "success"
        
        # 2. Wait briefly
        wait_result = await server.call("wait", {
            "seconds": 0.1,
            "message": "Processing",
            "_status": AsyncMock(progress=AsyncMock())
        })
        assert wait_result["status"] == "success"
        
        # 3. Final ping
        final_result = await server.call("ping", {})
        assert final_result["status"] == "success"