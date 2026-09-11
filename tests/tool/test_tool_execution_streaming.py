"""
Tests for streaming tool execution with real-time status events.

These tests verify:
1. execute_tools_streaming() yields events during execution
2. execute_tools_collect test helper produces same results
3. Parallel execution is maintained
4. Multiple requests work (state reset)
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from tool_execution_test_helpers import execute_tools_collect


@pytest.fixture
def mock_registry():
    """Mock plugin registry with test tool"""
    registry = MagicMock()
    registry.list.return_value = ["test_tool"]
    
    # Mock server
    mock_server = MagicMock()
    mock_server.get_default_action.return_value = "run"
    
    # Simulate delayed execution
    async def slow_call_with_status(action, params):
        await asyncio.sleep(0.1)  # Simulate work
        return {"content": "Result from test_tool"}
    
    mock_server.call_with_status = AsyncMock(side_effect=slow_call_with_status)
    registry.get.return_value = mock_server
    
    return registry


@pytest.fixture
async def manager_with_streaming(mock_registry):
    """ToolExecutionManager with StatusEventForwarder"""
    forwarder = StatusEventForwarder()
    manager = ToolExecutionManager(
        mock_registry,
        agent=None
    )
    
    yield manager, forwarder
    
    # Cleanup
    try:
        await forwarder.stop_forwarding()
    except Exception:
        pass


class TestStreamingToolExecution:
    """Test real-time streaming of tool execution"""
    
    @pytest.mark.asyncio
    async def test_streaming_yields_complete_event(self, manager_with_streaming):
        """execute_tools_streaming() should yield complete event with results"""
        manager, forwarder = manager_with_streaming
        
        tool_calls = [{
            "id": "call_1",
            "function": {
                "name": "test_tool",
                "arguments": "{}"
            }
        }]
        
        complete_received = False
        tool_messages = []
        
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="test123",
            status_forwarder=forwarder
        ):
            if item["type"] == "complete":
                complete_received = True
                tool_messages = item["messages"]
        
        assert complete_received, "No complete event received!"
        assert len(tool_messages) == 1
        assert tool_messages[0].role == "tool"
    
    @pytest.mark.asyncio
    async def test_parallel_execution_maintained(self, manager_with_streaming):
        """Tools should still execute in parallel with streaming"""
        manager, forwarder = manager_with_streaming
        
        # Create 3 tool calls (each takes 0.1s)
        tool_calls = [
            {
                "id": f"call_{i}",
                "function": {"name": "test_tool", "arguments": "{}"}
            }
            for i in range(3)
        ]
        
        start_time = asyncio.get_event_loop().time()
        
        # Collect results
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="test456",
            status_forwarder=forwarder
        ):
            if item["type"] == "complete":
                messages = item["messages"]
                break
        
        elapsed = asyncio.get_event_loop().time() - start_time
        
        # 3 tools @ 0.1s each should complete in ~0.1s (parallel), not 0.3s (sequential)
        assert elapsed < 0.25, f"Took {elapsed:.2f}s - not parallel! (expected <0.25s)"
        assert len(messages) == 3, "Missing tool results"
    
    @pytest.mark.asyncio
    async def test_wrapper_compatibility(self, manager_with_streaming):
        """execute_tools_collect (Test-Helper) liefert dieselben Ergebnisse wie Streaming"""
        manager, forwarder = manager_with_streaming
        
        tool_calls = [{
            "id": "call_1",
            "function": {"name": "test_tool", "arguments": "{}"}
        }]
        
        # Call collector helper
        messages, events, results = await execute_tools_collect(manager,
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="test999"
        )
        
        # Should get same messages as streaming version
        assert len(messages) == 1
        assert messages[0].role == "tool"
    
    @pytest.mark.asyncio
    async def test_multiple_streaming_calls(self, manager_with_streaming):
        """Multiple sequential streaming calls should work (state reset)"""
        manager, forwarder = manager_with_streaming
        
        tool_calls = [{
            "id": "call_1",
            "function": {"name": "test_tool", "arguments": "{}"}
        }]
        
        # First call
        completed_first = False
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="req1",
            status_forwarder=forwarder
        ):
            if item["type"] == "complete":
                completed_first = True
                break
        
        assert completed_first
        
        # Second call should work without hanging
        completed_second = False
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="req2",
            status_forwarder=forwarder
        ):
            if item["type"] == "complete":
                completed_second = True
                break
        
        assert completed_second, "Second streaming call hung!"
    
    @pytest.mark.asyncio
    async def test_wrapper_and_streaming_produce_same_results(self, manager_with_streaming):
        """Collector-Helper und Streaming-Version liefern identische Ergebnisse"""
        manager, forwarder = manager_with_streaming
        
        tool_calls = [
            {"id": f"call_{i}", "function": {"name": "test_tool", "arguments": "{}"}}
            for i in range(2)
        ]
        
        # Streaming version
        streaming_messages = []
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="stream_test",
            status_forwarder=forwarder
        ):
            if item["type"] == "complete":
                streaming_messages = item["messages"]
        
        # Collector helper
        wrapper_messages, _, _ = await execute_tools_collect(manager,
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="wrapper_test"
        )
        
        # Same number of messages
        assert len(streaming_messages) == len(wrapper_messages)
        
        # Same message roles
        assert all(m1.role == m2.role for m1, m2 in zip(streaming_messages, wrapper_messages))



class TestMalformedToolArguments:
    """Tool arguments that are JSON, but not an object.

    Arguments can read as a list, a string or a number instead of a dict. The
    downstream code calls params.get(...), which crashed with AttributeError.

    Triggering case observed in production: v5b_synopsis_moderator posting a
    ~15KB synopsis as tool argument, which was read as a list.
    """

    @pytest.mark.asyncio
    async def test_list_wrapped_single_dict_unwraps(self, manager_with_streaming):
        """[{...}] → {...} — LLM accidentally wrapped args in a single-item list."""
        manager, forwarder = manager_with_streaming

        # A single object wrapped in a list (the LLM put brackets around the args)
        raw = '[{"key": "value"}]'
        tool_calls = [{
            "id": "call_1",
            "function": {"name": "test_tool", "arguments": raw},
        }]

        tool_messages = []
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="unwrap_test",
            status_forwarder=forwarder,
        ):
            if item["type"] == "complete":
                tool_messages = item["messages"]
                break

        # Must complete without AttributeError — params was unwrapped to dict
        assert len(tool_messages) == 1
        assert tool_messages[0].role == "tool"

    @pytest.mark.asyncio
    async def test_non_dict_args_return_parse_error(self, manager_with_streaming):
        """[...multiple...] / bare list → treated as parse failure, not crash."""
        manager, forwarder = manager_with_streaming

        # List with 2 dicts — ambiguous, can't unwrap
        raw = '[{"a": 1}, {"b": 2}]'
        tool_calls = [{
            "id": "call_1",
            "function": {"name": "test_tool", "arguments": raw},
        }]

        tool_messages = []
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="parse_err_test",
            status_forwarder=forwarder,
        ):
            if item["type"] == "complete":
                tool_messages = item["messages"]
                break

        # Must complete with one error-tool-message (no crash)
        assert len(tool_messages) == 1
        assert tool_messages[0].role == "tool"
        # The error content should mention JSON parse failure
        import json as _json
        content = _json.loads(tool_messages[0].content)
        assert content.get("type") == "JSONParseError"

    @pytest.mark.asyncio
    async def test_bare_string_args_return_parse_error(self, manager_with_streaming):
        """A bare JSON string → parse failure, not crash."""
        manager, forwarder = manager_with_streaming

        # Valid JSON, but a string instead of an object
        raw = '"just a string"'
        tool_calls = [{
            "id": "call_1",
            "function": {"name": "test_tool", "arguments": raw},
        }]

        tool_messages = []
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="string_test",
            status_forwarder=forwarder,
        ):
            if item["type"] == "complete":
                tool_messages = item["messages"]
                break

        # Must complete with error message, not crash with AttributeError
        assert len(tool_messages) == 1
        assert tool_messages[0].role == "tool"


async def _messages_of(manager, forwarder, raw_arguments, request_id):
    tool_calls = [{"id": "call_1", "function": {"name": "test_tool", "arguments": raw_arguments}}]
    async for item in manager.execute_tools_streaming(
        tool_calls=tool_calls,
        tool_name_mapping={"test_tool": "test_tool"},
        available_tools=["test_tool"],
        step=1,
        request_id=request_id,
        status_forwarder=forwarder,
    ):
        if item["type"] == "complete":
            return item["messages"]
    return []


class TestMalformedArgumentsAreRejectedNotRepaired:
    """Arguments that are not valid JSON go back to the model; the tool never runs.

    2026-09-11: json-repair read a spliced token stream ("... und erkenn" +
    "idas Schuld ...", "background": "...") as one string, the write went
    through with status ok, and the garbage stood in the story document.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", [
        '{"doc": "synopsis", "data": {"background": "und erkenn"idas Schuld", "age": 3}}',
        '{"doc": "synopsis", "data": {"background": "cut off',
    ], ids=["spliced-string", "cut-off"])
    async def test_the_tool_is_not_called_and_the_model_gets_a_parse_error(
            self, manager_with_streaming, mock_registry, raw):
        import json as _json
        from agent_system.utils.json_utils import repair_json
        assert isinstance(repair_json(raw), dict), (
            "json-repair cannot make a dict of this -- the old code refused it "
            "as well, so the test would measure nothing")
        manager, forwarder = manager_with_streaming

        tool_messages = await _messages_of(manager, forwarder, raw, "reject_test")

        mock_registry.get.return_value.call_with_status.assert_not_awaited()
        assert len(tool_messages) == 1
        assert _json.loads(tool_messages[0].content)["type"] == "JSONParseError"

    @pytest.mark.asyncio
    async def test_a_line_break_inside_a_string_is_read_not_rejected(
            self, manager_with_streaming, mock_registry):
        # Not strict JSON, but nothing to guess: the value is the text with its
        # line break. Rejecting it would send a usable call back to the model.
        import json as _json
        raw = '{"text": "line1\nline2"}'
        with pytest.raises(_json.JSONDecodeError):
            _json.loads(raw)  # fixture assurance: really not strict JSON
        call = mock_registry.get.return_value.call_with_status = AsyncMock(
            return_value={"status": "ok"})
        manager, forwarder = manager_with_streaming

        await _messages_of(manager, forwarder, raw, "line_break_test")

        call.assert_awaited_once()
        assert call.await_args.args[1]["text"] == "line1\nline2"


class TestAToolResultAlwaysReachesTheModel:
    """A plugin's return value is turned into text for the model, whatever it holds."""

    @pytest.mark.asyncio
    async def test_a_set_in_the_result_arrives_as_text(self, manager_with_streaming, mock_registry):
        # json.dumps without default raised on the set, and the model got
        # "invocation failed: Object of type set is not JSON serializable"
        # instead of the result (tool_script failure report, 2026-09-11).
        mock_registry.get.return_value.call_with_status = AsyncMock(
            return_value={"status": "ok", "seen": {"B01"}})
        manager, forwarder = manager_with_streaming

        tool_messages = await _messages_of(manager, forwarder, '{"x": 1}', "set_result_test")

        assert len(tool_messages) == 1
        assert "B01" in tool_messages[0].content
