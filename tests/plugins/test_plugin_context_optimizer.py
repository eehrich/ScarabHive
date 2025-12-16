"""Tests for context_optimizer plugin - tool_call/tool_response pair preservation."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from agent_system.llm.models import ChatMessage
from agent_system.hooks import HookContext


class TestContextOptimizerToolPairs:
    """Test that tool_call/tool_response pairs are removed together."""
    
    @pytest.fixture
    def plugin(self):
        """Create plugin instance."""
        from plugins.context_optimizer.hooks import ContextOptimizerPlugin
        plugin_dir = Path("src/plugins/context_optimizer")
        return ContextOptimizerPlugin(plugin_dir)
    
    @pytest.fixture
    def mock_llm(self):
        """Create mock LLM with context window."""
        llm = MagicMock()
        llm.context_window = 10000  # Small window to trigger optimization
        return llm
    
    def test_get_tool_call_ids(self, plugin):
        """Test extraction of tool_call IDs from assistant message."""
        # Assistant with tool calls
        msg = ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {"id": "call_123", "function": {"name": "test"}},
                {"id": "call_456", "function": {"name": "test2"}}
            ]
        )
        ids = plugin._get_tool_call_ids(msg)
        assert ids == {"call_123", "call_456"}
        
        # Assistant without tool calls
        msg2 = ChatMessage(role="assistant", content="Hello")
        assert plugin._get_tool_call_ids(msg2) == set()
        
        # User message
        msg3 = ChatMessage(role="user", content="Hi")
        assert plugin._get_tool_call_ids(msg3) == set()
    
    def test_get_tool_response_id(self, plugin):
        """Test extraction of tool_call_id from tool response."""
        # Tool response
        msg = ChatMessage(
            role="tool",
            content="result",
            tool_call_id="call_123"
        )
        assert plugin._get_tool_response_id(msg) == "call_123"
        
        # Non-tool message
        msg2 = ChatMessage(role="user", content="Hi")
        assert plugin._get_tool_response_id(msg2) is None
    
    def test_find_removable_message_groups_simple(self, plugin):
        """Test grouping of tool_call + response pairs."""
        messages = [
            ChatMessage(role="system", content="System"),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": "call_1", "function": {"name": "tool1"}}]
            ),
            ChatMessage(role="tool", content="result1", tool_call_id="call_1"),
            ChatMessage(role="assistant", content="Done"),
        ]
        
        system_msgs = [messages[0]]
        preserved = [messages[-1]]  # Preserve last message
        
        groups = plugin._find_removable_message_groups(messages, system_msgs, preserved)
        
        # Should find: [1] (user), [2, 3] (assistant+tool pair)
        assert len(groups) == 2
        assert [1] in groups  # User message alone
        assert [2, 3] in groups  # Tool pair together
    
    def test_find_removable_message_groups_incomplete_responses(self, plugin):
        """Test that incomplete tool_call groups are NOT removed (missing responses)."""
        messages = [
            ChatMessage(role="system", content="System"),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {"id": "call_1", "function": {"name": "tool1"}},
                    {"id": "call_2", "function": {"name": "tool2"}}
                ]
            ),
            # Only ONE response (call_1) - call_2 is missing!
            ChatMessage(role="tool", content="result1", tool_call_id="call_1"),
            ChatMessage(role="assistant", content="Done"),
        ]
        
        system_msgs = [messages[0]]
        preserved = [messages[-1]]  # Preserve last message
        
        groups = plugin._find_removable_message_groups(messages, system_msgs, preserved)
        
        # Should find: [1] (user message alone)
        # Should NOT find the incomplete tool_call group (missing call_2 response)
        assert len(groups) == 1
        assert [1] in groups  # User message
        # [2, 3] should NOT be in groups - incomplete pair!
        assert [2, 3] not in groups
        assert [2] not in groups
    
    def test_find_removable_message_groups_parallel_tools(self, plugin):
        """Test grouping with parallel tool calls."""
        messages = [
            ChatMessage(role="system", content="System"),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {"id": "call_1", "function": {"name": "tool1"}},
                    {"id": "call_2", "function": {"name": "tool2"}}
                ]
            ),
            ChatMessage(role="tool", content="result1", tool_call_id="call_1"),
            ChatMessage(role="tool", content="result2", tool_call_id="call_2"),
            ChatMessage(role="assistant", content="Done"),
        ]
        
        system_msgs = [messages[0]]
        preserved = [messages[-1]]
        
        groups = plugin._find_removable_message_groups(messages, system_msgs, preserved)
        
        # Should group: [1], [2, 3, 4] (assistant + both tool responses)
        assert len(groups) == 2
        assert [1] in groups
        assert [2, 3, 4] in groups  # All 3 together
    
    @pytest.mark.asyncio
    async def test_enforce_token_limits_removes_pairs_together(self, plugin, mock_llm):
        """Test that token limit enforcement removes tool pairs together."""
        # Create a conversation with tool calls that will need trimming
        messages = [
            ChatMessage(role="system", content="System prompt"),
            ChatMessage(role="user", content="First request"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": "call_old", "function": {"name": "old_tool"}}]
            ),
            ChatMessage(role="tool", content="old result" * 100, tool_call_id="call_old"),  # Large
            ChatMessage(role="user", content="Second request"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": "call_new", "function": {"name": "new_tool"}}]
            ),
            ChatMessage(role="tool", content="new result", tool_call_id="call_new"),
            ChatMessage(role="assistant", content="Final answer"),
        ]
        
        # Use very small max_tokens to force removal
        result = plugin._enforce_token_limits(
            messages,
            max_tokens=500,  # Very small
            preserve_system=True,
            preserve_last_n=3  # Preserve last 3 non-system messages
        )
        
        # Check that if tool response was removed, its tool_call was also removed
        tool_call_ids = set()
        tool_response_ids = set()
        
        for msg in result:
            if msg.role == 'assistant' and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_call_ids.add(tc['id'])
            elif msg.role == 'tool' and hasattr(msg, 'tool_call_id'):
                tool_response_ids.add(msg.tool_call_id)
        
        # All tool_calls should have matching responses
        assert tool_call_ids == tool_response_ids, \
            f"Mismatch: tool_calls={tool_call_ids}, responses={tool_response_ids}"
    
    @pytest.mark.asyncio
    async def test_optimize_context_preserves_tool_pairs(self, plugin, mock_llm):
        """Integration test: optimize_context should not create orphaned tool_calls."""
        messages = [
            ChatMessage(role="system", content="System"),
            ChatMessage(role="user", content="Request"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": "call_1", "function": {"name": "tool1"}}]
            ),
            ChatMessage(role="tool", content="result" * 500, tool_call_id="call_1"),  # Large
            ChatMessage(role="assistant", content="Answer"),
        ]
        
        context = HookContext(
            hook_type="pre_llm_call",
            messages=messages,
            llm=mock_llm,
            session_id="test",
            request_id="test"
        )
        
        # Temporarily set small max_context_percentage for test
        original_config = plugin.get_config()
        plugin._config = {**original_config, 'max_context_percentage': 0.01}  # Very small
        
        try:
            result = await plugin.optimize_context(context)
            
            # Check no orphaned tool calls
            result_messages = result.context.messages
            tool_call_ids = set()
            tool_response_ids = set()
            
            for msg in result_messages:
                if msg.role == 'assistant' and msg.tool_calls:
                    for tc in msg.tool_calls:
                        tool_call_ids.add(tc['id'])
                elif msg.role == 'tool' and hasattr(msg, 'tool_call_id'):
                    tool_response_ids.add(msg.tool_call_id)
            
            # No orphaned tool_calls or responses
            orphaned_calls = tool_call_ids - tool_response_ids
            orphaned_responses = tool_response_ids - tool_call_ids
            
            assert not orphaned_calls, f"Orphaned tool_calls: {orphaned_calls}"
            assert not orphaned_responses, f"Orphaned tool_responses: {orphaned_responses}"
            
        finally:
            plugin._config = original_config
    
    def test_truncate_smart_truncates_tool_responses(self, plugin):
        """Test that tool responses with JSON get smart truncation (not dumly cut off)."""
        import json
        
        messages = [
            ChatMessage(role="user", content="x" * 60000),  # Very long
            ChatMessage(role="tool", content='{"result": "' + "x" * 60000 + '"}', tool_call_id="call_1"),  # Very long JSON
            ChatMessage(role="assistant", content="y" * 60000),  # Very long
        ]
        
        result = plugin._truncate_messages(messages, max_length=50000)
        
        # User and assistant should be truncated (simple cut)
        assert len(result[0].content) <= 50020  # 50000 + '... [truncated]'
        assert "[truncated]" in result[0].content
        assert len(result[2].content) <= 50020
        assert "[truncated]" in result[2].content
        
        # Tool response should be SMART truncated - JSON stays valid
        assert len(result[1].content) < 60000  # Reduced
        parsed = json.loads(result[1].content)  # Should still be valid JSON
        assert "result" in parsed
        assert "chars removed" in parsed["result"]  # String was truncated
    
    def test_truncate_warns_on_json_content(self, plugin, caplog):
        """Test that truncation warns when content looks like JSON."""
        import logging
        caplog.set_level(logging.WARNING)
        
        messages = [
            ChatMessage(role="user", content='{"data": "' + "x" * 60000 + '"}'),  # JSON-like
            ChatMessage(role="assistant", content="Normal text " * 10000),  # Not JSON
        ]
        
        plugin._truncate_messages(messages, max_length=50000)
        
        # Should warn about JSON truncation
        assert any("JSON-like content" in record.message for record in caplog.records)
    
    def test_truncate_smart_truncates_large_tool_response(self, plugin, caplog):
        """Test that large tool response gets smart-truncated."""
        import json
        import logging
        caplog.set_level(logging.INFO)
        
        messages = [
            ChatMessage(role="tool", content='{"huge": "' + "x" * 100000 + '"}', tool_call_id="call_1"),
        ]
        
        result = plugin._truncate_messages(messages, max_length=50000)
        
        # Should be reduced via smart truncation
        assert len(result[0].content) < 100000
        
        # Should still be valid JSON
        parsed = json.loads(result[0].content)
        assert "huge" in parsed
        assert "chars removed" in parsed["huge"]
        
        # Should log info about smart truncation
        assert any("Smart-truncated" in record.message for record in caplog.records)
    
    def test_smart_truncate_json_strings_keeps_structure(self, plugin):
        """Test that JSON string truncation keeps structure valid."""
        data = {
            "short": "keep this",
            "long": "x" * 10000,
            "nested": {
                "array": ["item1", "y" * 8000, "item3"],
                "number": 42
            }
        }
        
        result = plugin._smart_truncate_json_strings(data, max_string_length=1000, keep_end=True)
        
        # Structure should be preserved
        assert "short" in result
        assert "nested" in result
        assert "array" in result["nested"]
        assert result["nested"]["number"] == 42
        
        # Short strings unchanged
        assert result["short"] == "keep this"
        
        # Long strings truncated but keeping END
        assert len(result["long"]) < 10000
        assert result["long"].endswith("x" * 1000)  # End preserved
        assert "chars removed" in result["long"]
        
        # Nested long strings also truncated
        assert len(result["nested"]["array"][1]) < 8000
        assert result["nested"]["array"][1].endswith("y" * 1000)
    
    def test_truncate_tool_response_with_json_smart_truncation(self, plugin, caplog):
        """Test that tool responses with JSON get smart truncation."""
        import json
        import logging
        caplog.set_level(logging.INFO)
        
        # Large JSON with long strings
        large_data = {
            "status": "success",
            "data": {
                "content": "This is content " * 10000,  # Very long string
                "metadata": {"id": 123, "name": "test"}
            }
        }
        
        messages = [
            ChatMessage(
                role="tool",
                content=json.dumps(large_data),
                tool_call_id="call_1"
            ),
        ]
        
        result = plugin._truncate_messages(messages, max_length=50000)
        
        # Should be truncated
        original_len = len(messages[0].content)
        result_len = len(result[0].content)
        assert result_len < original_len
        
        # But still valid JSON
        parsed = json.loads(result[0].content)
        assert parsed["status"] == "success"
        assert parsed["data"]["metadata"]["id"] == 123
        assert "content" in parsed["data"]
        
        # Content should be truncated but end preserved
        assert len(parsed["data"]["content"]) < len(large_data["data"]["content"])
        assert "chars removed" in parsed["data"]["content"]
        
        # Should log info about smart truncation
        assert any("Smart-truncated tool response" in record.message for record in caplog.records)
    
    def test_smart_truncate_keeps_end_of_strings(self, plugin):
        """Test that END of strings is kept (newest data)."""
        data = {"log": "old_data_" + "middle_" * 1000 + "new_data"}
        
        result = plugin._smart_truncate_json_strings(data, max_string_length=100, keep_end=True)
        
        # Should keep the END
        assert result["log"].endswith("new_data")
        assert "old_data_" not in result["log"]  # Beginning removed
        assert "chars removed" in result["log"]
    
    @pytest.mark.asyncio
    async def test_config_parameters_are_used(self, plugin, mock_llm):
        """Test that config parameters from plugins.yaml are respected."""
        # Override config for test
        plugin._config = {
            'max_context_percentage': 0.90,
            'max_message_length_chars': 10000,
            'max_json_string_length': 1000,
            'keep_string_end': False,  # Keep beginning instead of end
            'preserve_system_messages': True,
            'preserve_last_n_messages': 3,
            'remove_duplicates': False
        }
        
        messages = [
            ChatMessage(role="system", content="System"),
            ChatMessage(role="user", content="Test"),
            ChatMessage(role="assistant", content="Response"),
        ]
        
        context = HookContext(
            hook_type="pre_llm_call",
            messages=messages,
            llm=mock_llm,
            session_id="test",
            request_id="test"
        )
        
        result = await plugin.optimize_context(context)
        
        # Verify config was used (check metadata)
        assert result.metadata['max_percentage'] == 0.90
        assert result.metadata['max_tokens'] == int(10000 * 0.90)  # 90% of context_window
