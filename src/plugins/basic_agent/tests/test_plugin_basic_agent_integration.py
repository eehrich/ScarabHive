"""Integration tests for basic_agent plugin.

This module contains integration tests for the BasicAgent plugin,
testing real agent execution, tool integration, and end-to-end functionality.
"""

import pytest
from unittest.mock import AsyncMock

from plugins.basic_agent.plugin import PLUGIN_FACTORY


class TestBasicAgentIntegration:
    """Test BasicAgent integration with real agent execution."""

    def setup_method(self):
        """Set up test fixtures with proper LLM configuration."""
        from agent_system.config.models import (
            AgentSystemConfig, ToolServerConfig, AgentConfig,
            LLMSystemConfig, LLMModelConfig, LLMProfile
        )

        # Create proper config objects
        self.system_config = AgentSystemConfig(
            llm_system=LLMSystemConfig(
                models={
                    "gpt-5-nano": LLMModelConfig(provider="openai", model="gpt-5-nano")
                },
                profiles={
                    "normal": LLMProfile(model_ref="gpt-5-nano"),
                    "fast": LLMProfile(model_ref="gpt-5-nano")
                },
                default_profile="normal"
            )
        )

        self.server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=AgentConfig(
                max_steps=10
            )
        )

        # Keep old dict for backward compat if needed
        self.config = {
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
            "max_steps": 10,
            "enable_debug": False
        }

    @pytest.mark.asyncio
    async def test_execute_task_with_mocked_events(self):
        """Test task execution with mocked agent events."""
        # Create agent instance
        agent = PLUGIN_FACTORY("integration_agent", self.system_config, self.server_config)

        # Mock the run_events method to simulate agent execution
        mock_events = [
            {"type": "start"},
            {"type": "tool_call", "server": "script_interpreter", "action": "execute", "params": {"code": "2+2"}},
            {"type": "tool_result", "server": "script_interpreter"},
            {"type": "final", "summary": "The calculation 2+2 equals 4."},
        ]

        async def mock_run_events(*args, **kwargs):
            for event in mock_events:
                yield event

        agent.run_events = mock_run_events

        # Mock status object
        mock_status = AsyncMock()

        # Test task execution
        params = {
            "task": "Calculate 2+2",
            "request_id": "test_req_123",
            "_status": mock_status
        }

        result = await agent.call("integration_agent_execute_task", params)

        # Verify result
        assert result["status"] == "success"
        assert "The calculation 2+2 equals 4." in result["result"]
        assert result["request_id"] == "test_req_123"
        assert "tool_calls" in result
        assert result["steps"] >= 1

        # Verify status updates were called
        mock_status.progress.assert_called()

    @pytest.mark.asyncio
    async def test_execute_task_with_error_event(self):
        """Test task execution handling error events."""
        agent = PLUGIN_FACTORY("error_agent", self.system_config, self.server_config)

        # Mock events with error
        mock_events = [
            {"type": "start"},
            {"type": "error", "message": "Test error occurred"},
        ]

        async def mock_run_events(*args, **kwargs):
            for event in mock_events:
                yield event

        agent.run_events = mock_run_events

        # Mock status object
        mock_status = AsyncMock()

        # Test task execution
        params = {
            "task": "Failing task",
            "_status": mock_status
        }

        result = await agent.call("error_agent_execute_task", params)

        # Verify error handling
        assert result["status"] == "error"
        assert "Test error occurred" in result["error"]

        # Verify error status was called
        mock_status.error.assert_called()

    @pytest.mark.asyncio
    async def test_execute_task_with_exception(self):
        """Test task execution with exception handling."""
        agent = PLUGIN_FACTORY("exception_agent", self.system_config, self.server_config)

        # Mock run_events to raise exception (needs to be async generator)
        async def mock_run_events(*args, **kwargs):
            raise RuntimeError("Simulated runtime error")
            yield  # Never reached, but makes it a generator

        agent.run_events = mock_run_events

        # Mock status object
        mock_status = AsyncMock()

        # Test task execution
        params = {
            "task": "Exception task",
            "_status": mock_status
        }

        result = await agent.call("exception_agent_execute_task", params)

        # Verify exception handling
        assert result["status"] == "error"
        assert "Simulated runtime error" in result["error"]

    @pytest.mark.asyncio
    async def test_a_cancelled_run_is_not_answered_as_success(self):
        """The run's own "cancelled" event (Agent._run_events) ends the call as cancelled,
        not as "success" with an invented result."""
        agent = PLUGIN_FACTORY("cancel_agent", self.system_config, self.server_config)
        read_to_end = []

        async def run_events(*args, **kwargs):
            yield {"type": "start"}
            yield {"type": "tool_call", "server": "files", "action": "files_read", "params": {"path": "a"}}
            yield {"type": "cancelled", "request_id": "r1", "step": 2}
            yield {"type": "end"}
            read_to_end.append(True)

        agent.run_events = run_events
        status = AsyncMock()

        result = await agent.call("cancel_agent_execute_task", {"task": "Long task", "_status": status})

        assert result["status"] == "cancelled"
        assert result["cancelled"] is True
        assert result["error"] == "Task cancelled at step 2"
        assert "result" not in result
        assert [c["action"] for c in result["tool_calls"]] == ["files_read"]
        assert read_to_end == [True]
        status.error.assert_awaited_with("Task cancelled at step 2")
        status.end.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_list_tools_integration(self):
        """Test list_tools integration with real registry."""
        agent = PLUGIN_FACTORY("list_agent", self.system_config, self.server_config)

        # Test list_available_tools call (the actual tool name from schema)
        params = {}

        result = await agent.call("list_agent_list_available_tools", params)

        # Verify result structure
        assert isinstance(result, list)
        # Verify simplified tool structure
        if result:
            tool = result[0]
            assert "name" in tool
            assert "description" in tool

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_tracking(self):
        """Test tracking of multiple tool calls in execution."""
        agent = PLUGIN_FACTORY("multi_agent", self.system_config, self.server_config)

        # Mock events with multiple tool calls
        mock_events = [
            {"type": "start"},
            {"type": "tool_call", "server": "datetime", "action": "current_time", "params": {"timezone": "UTC"}},
            {"type": "tool_result", "server": "datetime"},
            {"type": "tool_call", "server": "script_interpreter", "action": "execute", "params": {"code": "print('hello')"}},
            {"type": "tool_result", "server": "script_interpreter"},
            {"type": "final", "summary": "Time checked and code executed successfully."},
        ]

        async def mock_run_events(*args, **kwargs):
            for event in mock_events:
                yield event

        agent.run_events = mock_run_events

        # Test task execution
        params = {
            "task": "Check time and run code",
            "_status": AsyncMock()
        }

        result = await agent.call("multi_agent_execute_task", params)

        # Verify multiple tool calls were tracked
        assert result["status"] == "success"
        assert len(result["tool_calls"]) == 2

        # Check tool call details
        tool_calls = result["tool_calls"]
        assert tool_calls[0]["tool"] == "datetime"
        assert tool_calls[0]["action"] == "current_time"
        assert tool_calls[1]["tool"] == "script_interpreter"
        assert tool_calls[1]["action"] == "execute"

    @pytest.mark.asyncio
    async def test_task_execution_without_status(self):
        """Test task execution when no status object is provided."""
        agent = PLUGIN_FACTORY("no_status_agent", self.system_config, self.server_config)

        # Mock simple successful execution
        mock_events = [
            {"type": "final", "summary": "Task completed without status tracking."},
        ]

        async def mock_run_events(*args, **kwargs):
            for event in mock_events:
                yield event

        agent.run_events = mock_run_events

        # Test task execution without status
        params = {
            "task": "Simple task",
            # No _status provided
        }

        result = await agent.call("no_status_agent_execute_task", params)

        # Should work without status object
        assert result["status"] == "success"
        assert "Task completed without status tracking." in result["result"]

    def test_plugin_schema_consistency(self):
        """Test that plugin schema is consistent with implementation."""
        agent = PLUGIN_FACTORY("schema_agent", self.system_config, self.server_config)

        # Get schema tools
        tools = agent.get_tools()

        # Verify schema structure
        assert len(tools) == 2

        execute_tool = next((t for t in tools if t["function"]["name"] == "schema_agent_execute_task"), None)
        list_tool = next((t for t in tools if t["function"]["name"] == "schema_agent_list_available_tools"), None)

        assert execute_tool is not None
        assert list_tool is not None

        # Verify execute tool schema
        execute_params = execute_tool["function"]["parameters"]
        assert "task" in execute_params["properties"]
        assert execute_params["required"] == ["task"]

        # Verify list tool has no required params
        list_params = list_tool["function"]["parameters"]
        assert list_params.get("required", []) == []

    @pytest.mark.asyncio
    async def test_parameter_filtering(self):
        """Test that internal parameters are filtered from tool call tracking."""
        agent = PLUGIN_FACTORY("filter_agent", self.system_config, self.server_config)

        # Mock events with internal parameters
        mock_events = [
            {
                "type": "tool_call",
                "server": "test_server",
                "action": "test_action",
                "params": {
                    "public_param": "value1",
                    "_status": "internal_status",
                    "_cancellation_token": "internal_token",
                    "another_param": "value2"
                }
            },
            {"type": "final", "summary": "Parameter filtering test completed."},
        ]

        async def mock_run_events(*args, **kwargs):
            for event in mock_events:
                yield event

        agent.run_events = mock_run_events

        # Test task execution
        params = {
            "task": "Parameter filtering test",
            "_status": AsyncMock()
        }

        result = await agent.call("filter_agent_execute_task", params)

        # Verify internal parameters were filtered out
        assert result["status"] == "success"
        assert len(result["tool_calls"]) == 1

        tool_call = result["tool_calls"][0]
        filtered_params = tool_call["params"]

        # Should contain public parameters
        assert "public_param" in filtered_params
        assert "another_param" in filtered_params

        # Should not contain internal parameters
        assert "_status" not in filtered_params
        assert "_cancellation_token" not in filtered_params


if __name__ == "__main__":
    pytest.main([__file__])