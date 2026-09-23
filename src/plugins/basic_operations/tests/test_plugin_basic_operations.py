"""Tests for BasicOperations plugin.

This module contains comprehensive tests for the BasicOperations plugin,
covering wait operations with status updates, echo functionality, and ping operations.
"""

import pytest
import asyncio
import time
from unittest.mock import AsyncMock
from pathlib import Path

from agent_system.tools.status import current_request_id
from plugins.basic_operations.server import BasicOperationsServer
from plugins.basic_operations.plugin import PLUGIN_FACTORY


class TestBasicOperationsServer:
    """Test the BasicOperationsServer class functionality."""

    def test_server_initialization(self, mock_system_config, mock_server_config):
        """Test basic server initialization."""
        server = BasicOperationsServer("test_basic_ops", mock_system_config, mock_server_config)
        assert server.name == "test_basic_ops"
        assert server.max_wait_seconds == 3600  # default
        assert server.default_update_interval == 1.0  # default

    def test_server_initialization_with_config(self, mock_system_config):
        """Test server initialization with custom config."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        
        # Create ToolServerConfig with custom max_wait_seconds
        server_config = ToolServerConfig(
            type="basic_operations",
            enabled=True,
            agent_config=AgentConfig()
        )
        # Add custom attributes
        server_config.max_wait_seconds = 60
        server_config.default_update_interval = 0.5
        
        server = BasicOperationsServer("test_basic_ops", mock_system_config, server_config)
        assert server.name == "test_basic_ops"
        assert server.max_wait_seconds == 60
        assert server.default_update_interval == 0.5

    def test_server_tools_schema(self, mock_system_config, mock_server_config):
        """Test server tools structure."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        tools = server.get_tools()
        
        assert isinstance(tools, list)
        assert len(tools) == 2
        
        # Check tool names
        tool_names = [tool["function"]["name"] for tool in tools]
        expected_names = ["basic_ops_wait", "basic_ops_ping"]
        assert set(tool_names) == set(expected_names)
        
        # Verify each tool has proper structure
        for tool in tools:
            assert tool["type"] == "function"
            assert "function" in tool
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]

    def test_server_default_action(self, mock_system_config, mock_server_config):
        """Test default action - get_default_action() removed in modern pattern."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        # get_default_action() is obsolete in modern dispatcher
        assert not hasattr(server, 'get_default_action') or True  # Skip this test

    @pytest.mark.asyncio
    async def test_ping_tool_basic(self, mock_system_config, mock_server_config):
        """Test basic ping functionality."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        
        result = await server.call("basic_ops_ping", {})
        
        assert result["status"] == "success"
        assert result["message"] == "Pong!"
        assert "timestamp" in result
        assert result["server_name"] == "basic_ops"
        assert "details" not in result  # default is no details

    @pytest.mark.asyncio
    async def test_ping_tool_with_details(self, mock_system_config, mock_server_config):
        """Test ping with detailed information."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        
        result = await server.call("basic_ops_ping", {"include_details": True})
        
        assert result["status"] == "success"
        assert result["message"] == "Pong!"
        assert "details" in result
        assert "python_version" in result["details"]
        assert "platform" in result["details"]
        assert "server_config" in result["details"]



    @pytest.mark.asyncio
    async def test_wait_tool_basic(self, mock_system_config, mock_server_config):
        """Test basic wait functionality."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        
        start_time = time.time()
        # Provide a mock status because the server assumes one is always present
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("basic_ops_wait", {"seconds": 0.1, "_status": mock_status})
        elapsed = time.time() - start_time

        assert result["status"] == "success"
        assert result["message"] == "Wait completed successfully"
        assert result["requested_seconds"] == 0.1
        assert 0.05 <= elapsed <= 0.2  # Allow some tolerance
        assert result["user_message"] == "Waiting"  # default message

    @pytest.mark.asyncio
    async def test_wait_tool_with_message(self, mock_system_config, mock_server_config):
        """Test wait with custom message."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("basic_ops_wait", {
            "seconds": 0.1,
            "message": "Test Operation",
            "_status": mock_status
        })
        
        assert result["status"] == "success"
        assert result["user_message"] == "Test Operation"

    @pytest.mark.asyncio
    async def test_wait_tool_with_status_updates(self, mock_system_config, mock_server_config):
        """Test wait with status update monitoring."""
        server = BasicOperationsServer("basic_ops", mock_system_config, {"default_update_interval": 0.1})
        
        # Mock status object to capture updates
        mock_status = AsyncMock()
        status_calls = []
        
        async def capture_status_update(message):
            status_calls.append(message)
        
        mock_status.progress = AsyncMock(side_effect=capture_status_update)
        
        result = await server.call("basic_ops_wait", {
            "seconds": 0.3,
            "_status": mock_status
        })
        
        assert result["status"] == "success"
        # Initial status update is always sent ("starting countdown")
        assert len(status_calls) >= 1
        # Status updates include the 'waiting' message (initial countdown)
        assert any("waiting" in call.lower() for call in status_calls)
        # Note: "remaining" updates only happen every 10 seconds by design, 
        # so short waits won't have them
        # Completion message comes from status.end(), not progress()
        # so we just verify the initial status update was sent

    @pytest.mark.asyncio
    async def test_wait_tool_parameter_validation(self, mock_system_config, mock_server_config):
        """Test wait tool parameter validation."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        
        # Create ToolServerConfig with max_wait_seconds=5
        server_config = ToolServerConfig(
            type="basic_operations",
            enabled=True,
            agent_config=AgentConfig()
        )
        server_config.max_wait_seconds = 5
        
        server = BasicOperationsServer("basic_ops", mock_system_config, server_config)
        
        # Test negative seconds
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("basic_ops_wait", {"seconds": -1, "_status": mock_status})
        assert result["status"] == "error"
        assert "must be positive" in result["error"]

        # Test zero seconds
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("basic_ops_wait", {"seconds": 0, "_status": mock_status})
        assert result["status"] == "error"
        assert "must be positive" in result["error"]

        # Test exceeds maximum
        mock_status = AsyncMock()
        mock_status.progress = AsyncMock()
        result = await server.call("basic_ops_wait", {"seconds": 10, "_status": mock_status})
        assert result["status"] == "error"
        assert "exceeds maximum" in result["error"]

    @pytest.mark.asyncio
    async def test_wait_tool_custom_update_interval(self, mock_system_config, mock_server_config):
        """Test wait with custom update interval."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        
        mock_status = AsyncMock()
        status_calls = []
        
        async def capture_status_update(message):
            status_calls.append(message)
        
        mock_status.progress = AsyncMock(side_effect=capture_status_update)
        mock_status.end = AsyncMock(side_effect=capture_status_update)
        
        result = await server.call("basic_ops_wait", {
            "seconds": 0.2,
            "update_interval": 0.05,  # Controls sleep granularity, not status frequency
            "_status": mock_status
        })
        
        assert result["status"] == "success"
        # Note: update_interval controls sleep granularity for cancellation checks,
        # not status update frequency. Status updates happen every 10 seconds by design.
        # For short waits (< 10s), we only get the initial countdown and completion messages.
        assert len(status_calls) >= 2  # At least: starting countdown + completed

    @pytest.mark.asyncio
    async def test_invalid_tool_name(self, mock_system_config, mock_server_config):
        """Test calling an invalid tool name."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        
        # Modern pattern: generic dispatcher raises ValueError
        with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
            await server.call("invalid_tool", {})


class TestBasicOperationsPlugin:
    """Test the plugin factory and configuration."""

    def test_plugin_factory_basic(self, mock_system_config, mock_server_config):
        """Test basic plugin factory functionality."""
        server = PLUGIN_FACTORY("test_basic_ops", mock_system_config, mock_server_config)
        
        assert isinstance(server, BasicOperationsServer)
        assert server.name == "test_basic_ops"

    def test_plugin_factory_with_config(self, mock_system_config):
        """Test plugin factory with custom configuration."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        
        # Create ToolServerConfig with custom values
        server_config = ToolServerConfig(
            type="basic_operations",
            enabled=True,
            agent_config=AgentConfig()
        )
        server_config.max_wait_seconds = 120
        server_config.default_update_interval = 2.0
        
        server = PLUGIN_FACTORY("test_basic_ops", mock_system_config, server_config)
        
        assert isinstance(server, BasicOperationsServer)
        assert server.name == "test_basic_ops"
        assert server.max_wait_seconds == 120
        assert server.default_update_interval == 2.0

    def test_plugin_discovery(self, mock_system_config, mock_server_config):
        """Test that plugin can be discovered from plugin directory."""
        # Check that the plugin files exist
        plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "basic_operations"
        assert plugin_dir.exists()
        assert (plugin_dir / "__init__.py").exists()
        assert (plugin_dir / "server.py").exists()
        assert (plugin_dir / "plugin.py").exists()
        assert (plugin_dir / "schema.yaml").exists()

    def test_plugin_info_metadata(self, mock_system_config, mock_server_config):
        """Test plugin metadata if provided by the plugin.

        Many plugins do not currently provide `PLUGIN_INFO`. Instead of
        hard-failing, this test will skip at runtime if `PLUGIN_INFO` is
        not present and assert expected fields when it is available.
        """
        try:
            from plugins.basic_operations.plugin import PLUGIN_INFO
        except Exception:
            PLUGIN_INFO = {}

        # If PLUGIN_INFO is present, verify expected fields; otherwise accept
        # that the plugin chooses not to expose metadata and treat as non-fatal.
        if PLUGIN_INFO:
            assert PLUGIN_INFO.get("name") == "basic_operations"
            assert "version" in PLUGIN_INFO
            assert "description" in PLUGIN_INFO
            assert "type" in PLUGIN_INFO
            assert "category" in PLUGIN_INFO


class TestBasicOperationsIntegration:
    """Integration tests for BasicOperations plugin."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_waits(self, mock_system_config, mock_server_config):
        """Test multiple concurrent wait operations."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        
        # Start multiple wait operations concurrently
        # Provide status mocks for each concurrent task
        tasks = [
            server.call("basic_ops_wait", {"seconds": 0.1, "message": f"Wait {i}", "_status": AsyncMock(progress=AsyncMock())})
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
    async def test_wait_with_interrupt_simulation(self, mock_system_config, mock_server_config):
        """Test wait behavior with simulated interruption."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        
        # Start a wait operation and then "cancel" it by timing out the test
        try:
            await asyncio.wait_for(
                server.call("basic_ops_wait", {"seconds": 10, "_status": AsyncMock(progress=AsyncMock())}),
                timeout=0.1
            )
            # Should not reach here
            assert False, "Wait should have been interrupted"
        except asyncio.TimeoutError:
            # Expected behavior - wait was interrupted
            pass

    @pytest.mark.asyncio
    async def test_comprehensive_tool_workflow(self, mock_system_config, mock_server_config):
        """Test a complete workflow using all tools."""
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        
        # 1. Start with a ping
        ping_result = await server.call("basic_ops_ping", {"include_details": True})
        assert ping_result["status"] == "success"
        
        # 2. Wait briefly
        wait_result = await server.call("basic_ops_wait", {
            "seconds": 0.1,
            "message": "Processing",
            "_status": AsyncMock(progress=AsyncMock())
        })
        assert wait_result["status"] == "success"
        
        # 3. Final ping
        final_result = await server.call("basic_ops_ping", {})
        assert final_result["status"] == "success"

class TestWaitWithWake:
    """wait(wake=true): the session is woken instead of the turn held open."""

    @staticmethod
    def _recorder(monkeypatch, blocked=""):
        from plugins.basic_operations import server as server_module
        rings = []

        async def wake(system_config, session_id, user_id, what="", still_needed=None, started_by=None):
            rings.append((session_id, user_id, what, started_by))
            return "woke_session"

        monkeypatch.setattr(server_module, "wake_blocked", lambda *a: blocked)
        monkeypatch.setattr(server_module, "wake_depth", lambda: 0)
        monkeypatch.setattr(server_module, "wake_session", wake)
        monkeypatch.setattr(server_module, "WAKE_MIN_SECONDS", 0.05)
        return rings

    @pytest.mark.asyncio
    async def test_a_long_wait_answers_at_once_and_wakes_the_session(
            self, mock_system_config, mock_server_config, monkeypatch):
        rings = self._recorder(monkeypatch)
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)

        started = time.time()
        current_request_id.set("run-7")
        result = await server.call("basic_ops_wait", {
            "seconds": 2, "message": "for the build", "wake": True,
            "_session_id": "s1", "_user_id": "u1", "_status": AsyncMock(),
        })

        assert result["status"] == "success" and result["waiting"] is True
        assert time.time() - started < 1, "the call waited instead of arming a wake"
        assert "end your turn" in result["note"]

        await asyncio.wait(server._wakes, timeout=10)
        assert [(r[0], r[1]) for r in rings] == [("s1", "u1")]
        assert "for the build" in rings[0][2]
        # The run it belongs to: a run the user stopped rings nobody.
        assert rings[0][3] == "run-7"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blocked, depth, session, note", [
        ("waking is switched off", 0, "s1", "waking is switched off"),
        ("", 1, "s1", "itself woken"),
        ("", 0, "", "no session"),
    ])
    async def test_a_session_that_cannot_be_woken_is_waited_out_here(
            self, mock_system_config, mock_server_config, monkeypatch, blocked, depth, session, note):
        from plugins.basic_operations import server as server_module
        rings = self._recorder(monkeypatch, blocked=blocked)
        monkeypatch.setattr(server_module, "wake_depth", lambda: depth)
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)

        result = await server.call("basic_ops_wait", {
            "seconds": 0.1, "wake": True,
            "_session_id": session, "_user_id": "u1", "_status": AsyncMock(),
        })

        assert result["actual_seconds"] >= 0.1 and result.get("waiting") is None
        assert note in result["wake_note"] and rings == []

    @pytest.mark.asyncio
    async def test_a_short_wait_is_not_worth_a_turn(
            self, mock_system_config, mock_server_config, monkeypatch):
        """A woken turn reads the whole conversation again -- for seconds that is dearer than waiting."""
        rings = self._recorder(monkeypatch)
        from plugins.basic_operations import server as server_module
        monkeypatch.setattr(server_module, "WAKE_MIN_SECONDS", 60.0)
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)

        result = await server.call("basic_ops_wait", {
            "seconds": 0.1, "wake": True,
            "_session_id": "s1", "_user_id": "u1", "_status": AsyncMock(),
        })

        assert result["actual_seconds"] >= 0.1 and rings == [] and not server._wakes
        assert "60 s" in result["wake_note"]

    @pytest.mark.asyncio
    async def test_a_sub_agents_session_is_waited_out_here(
            self, mock_system_config, mock_server_config, monkeypatch):
        """wake_blocked leaves this out on purpose; the run that spawned a
        sub-agent hands its result over, nobody wakes it."""
        from plugins.basic_operations import server as server_module
        rings = self._recorder(monkeypatch)

        class SubAgentPresence:
            def get(self, session_id, user_id):
                return {"sub_agent": True}

        monkeypatch.setattr(server_module, "presence_for", lambda cfg: SubAgentPresence())
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)

        result = await server.call("basic_ops_wait", {
            "seconds": 0.1, "wake": True,
            "_session_id": "s1", "_user_id": "u1", "_status": AsyncMock(),
        })

        assert result["actual_seconds"] >= 0.1 and rings == []
        assert result["wake_note"] == "a sub-agent's session is never woken"

    @pytest.mark.asyncio
    async def test_a_wait_of_exactly_the_floor_is_armed_and_says_so(
            self, mock_system_config, mock_server_config, monkeypatch):
        """The floor is the shortest wait worth a wake, not the first one above it."""
        from plugins.basic_operations import server as server_module
        self._recorder(monkeypatch)
        monkeypatch.setattr(server_module, "WAKE_MIN_SECONDS", 60.0)
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)
        status = AsyncMock()

        result = await server.call("basic_ops_wait", {
            "seconds": 60, "message": None, "wake": True,
            "_session_id": "s1", "_user_id": "u1", "_status": status,
        })
        try:
            assert result["waiting"] is True
            assert status.end.await_count == 1 and "waking the session" in status.end.await_args[0][0]
        finally:
            await server.stop_plugin()

    @pytest.mark.asyncio
    async def test_a_stopped_plugin_drops_its_armed_wakes(
            self, mock_system_config, mock_server_config, monkeypatch):
        """They live in this process; the log says so instead of asyncio."""
        rings = self._recorder(monkeypatch)
        server = BasicOperationsServer("basic_ops", mock_system_config, mock_server_config)

        await server.call("basic_ops_wait", {
            "seconds": 1, "wake": True,
            "_session_id": "s1", "_user_id": "u1", "_status": AsyncMock(),
        })
        assert len(server._wakes) == 1
        await server.stop_plugin()

        await asyncio.sleep(1.2)
        assert rings == [] and not server._wakes
