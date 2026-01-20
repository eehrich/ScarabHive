"""Tests for Gemini consecutive user message merging functionality.

Gemini API (unlike OpenAI) does NOT allow consecutive messages with the same role.
These tests verify that the conversion layer correctly merges consecutive user messages.

See: https://github.com/google/generative-ai-python/issues/209
"""
import pytest

from agent_system.llm.gemini_utils import (
    convert_openai_messages_to_gemini,
    _merge_consecutive_same_role_messages,
    filter_unavailable_tool_calls,
)
from agent_system.llm.models import ChatMessage


class TestMergeConsecutiveSameRoleMessages:
    """Test the internal _merge_consecutive_same_role_messages function."""

    def test_no_consecutive_messages(self):
        """Test that alternating messages are unchanged."""
        contents = [
            {"role": "user", "parts": [{"text": "Hello"}]},
            {"role": "model", "parts": [{"text": "Hi!"}]},
            {"role": "user", "parts": [{"text": "How are you?"}]},
        ]
        
        result = _merge_consecutive_same_role_messages(contents, "user")
        
        assert len(result) == 3
        assert result[0]["parts"][0]["text"] == "Hello"
        assert result[1]["parts"][0]["text"] == "Hi!"
        assert result[2]["parts"][0]["text"] == "How are you?"

    def test_two_consecutive_user_messages(self):
        """Test merging two consecutive user messages."""
        contents = [
            {"role": "user", "parts": [{"text": "First message"}]},
            {"role": "user", "parts": [{"text": "Second message"}]},
        ]
        
        result = _merge_consecutive_same_role_messages(contents, "user")
        
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert len(result[0]["parts"]) == 1
        assert "First message" in result[0]["parts"][0]["text"]
        assert "Second message" in result[0]["parts"][0]["text"]
        assert "\n\n" in result[0]["parts"][0]["text"]

    def test_multiple_consecutive_user_messages(self):
        """Test merging multiple consecutive user messages (like 'Continue' messages)."""
        contents = [
            {"role": "model", "parts": [{"text": "Here's the answer..."}]},
            {"role": "user", "parts": [{"text": "ok und pihle?"}]},
            {"role": "user", "parts": [{"text": "Continue with your task."}]},
            {"role": "user", "parts": [{"text": "Continue with your task."}]},
            {"role": "user", "parts": [{"text": "Continue with your task."}]},
        ]
        
        result = _merge_consecutive_same_role_messages(contents, "user")
        
        # Should be merged to 2 messages: model + merged user
        assert len(result) == 2
        assert result[0]["role"] == "model"
        assert result[1]["role"] == "user"
        
        merged_text = result[1]["parts"][0]["text"]
        assert "ok und pihle?" in merged_text
        assert merged_text.count("Continue with your task.") == 3

    def test_preserves_non_text_parts(self):
        """Test that non-text parts (images, etc.) are preserved."""
        contents = [
            {"role": "user", "parts": [
                {"text": "Look at this image"},
                {"inlineData": {"mimeType": "image/png", "data": "base64data"}}
            ]},
            {"role": "user", "parts": [{"text": "What do you see?"}]},
        ]
        
        result = _merge_consecutive_same_role_messages(contents, "user")
        
        assert len(result) == 1
        # Should have 3 parts: merged text, image, more text? 
        # Actually: text parts merged, image preserved
        parts = result[0]["parts"]
        
        # Find the text part and image part
        text_parts = [p for p in parts if "text" in p]
        image_parts = [p for p in parts if "inlineData" in p or "inline_data" in p]
        
        assert len(text_parts) >= 1
        assert len(image_parts) == 1
        assert image_parts[0]["inlineData"]["mimeType"] == "image/png"

    def test_preserves_interleaved_content_in_single_message(self):
        """Test that interleaved content within a single message is preserved.
        
        A single message with [text, image, text, image] should NOT have its
        text parts merged - they're interleaved with images for a reason.
        """
        contents = [
            {"role": "user", "parts": [
                {"text": "First text"},
                {"inlineData": {"mimeType": "image/png", "data": "img1"}},
                {"text": "Second text"},
                {"inlineData": {"mimeType": "image/png", "data": "img2"}},
            ]},
        ]
        
        result = _merge_consecutive_same_role_messages(contents, "user")
        
        # Single message should be unchanged
        assert len(result) == 1
        assert len(result[0]["parts"]) == 4
        assert result[0]["parts"][0]["text"] == "First text"
        assert result[0]["parts"][1]["inlineData"]["data"] == "img1"
        assert result[0]["parts"][2]["text"] == "Second text"
        assert result[0]["parts"][3]["inlineData"]["data"] == "img2"

    def test_only_merges_specified_role(self):
        """Test that only the specified role is merged."""
        contents = [
            {"role": "model", "parts": [{"text": "First"}]},
            {"role": "model", "parts": [{"text": "Second"}]},
            {"role": "user", "parts": [{"text": "Question"}]},
        ]
        
        # Merge only user messages - model messages should stay separate
        result = _merge_consecutive_same_role_messages(contents, "user")
        
        # Model messages are NOT merged (only user role was specified)
        assert len(result) == 3

    def test_empty_contents(self):
        """Test handling of empty contents list."""
        result = _merge_consecutive_same_role_messages([], "user")
        assert result == []

    def test_single_message(self):
        """Test handling of single message."""
        contents = [{"role": "user", "parts": [{"text": "Only message"}]}]
        result = _merge_consecutive_same_role_messages(contents, "user")
        
        assert len(result) == 1
        assert result[0]["parts"][0]["text"] == "Only message"


class TestConvertOpenAIMessagesToGeminiConsecutiveUsers:
    """Test that convert_openai_messages_to_gemini handles consecutive user messages."""

    def test_consecutive_user_messages_merged(self):
        """Test that consecutive user messages are merged during conversion."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi!"),
            ChatMessage(role="user", content="First question"),
            ChatMessage(role="user", content="Second question"),
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # Should have: user, model, merged_user (3 messages)
        assert len(contents) == 3
        assert contents[0]["role"] == "user"
        assert contents[1]["role"] == "model"
        assert contents[2]["role"] == "user"
        
        # Last user message should contain both questions
        merged_text = contents[2]["parts"][0]["text"]
        assert "First question" in merged_text
        assert "Second question" in merged_text

    def test_many_consecutive_user_messages_real_scenario(self):
        """Test real-world scenario: session with 'Continue' messages.
        
        This happens when the user (or system) sends multiple "Continue with your task."
        messages that accumulate in the session, causing Gemini to return empty responses.
        """
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Analyze this data"),
            ChatMessage(role="assistant", content="The data shows..."),
            ChatMessage(role="user", content="ok und pihle?"),
            ChatMessage(role="user", content="Continue with your task."),
            ChatMessage(role="user", content="Continue with your task."),
            ChatMessage(role="user", content="Continue with your task."),
            ChatMessage(role="user", content="Continue with your task."),
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # Should have: user, model, merged_user (3 messages, not 6!)
        assert len(contents) == 3
        
        # Verify the merged user message
        last_user_msg = contents[2]
        assert last_user_msg["role"] == "user"
        merged_text = last_user_msg["parts"][0]["text"]
        
        # All user messages should be merged
        assert "ok und pihle?" in merged_text
        assert merged_text.count("Continue with your task.") == 4

    def test_consecutive_users_with_tool_calls(self):
        """Test consecutive user messages in complex tool-call scenario."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Check weather"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Berlin"}'}
                }]
            ),
            ChatMessage(
                role="tool",
                content='{"temp": 20}',
                tool_call_id="call_1",
                name="get_weather"
            ),
            ChatMessage(role="assistant", content="The weather is nice."),
            ChatMessage(role="user", content="Thanks!"),
            ChatMessage(role="user", content="Now check London"),
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # Verify no consecutive user messages in output
        for i in range(len(contents) - 1):
            if contents[i]["role"] == "user" and contents[i + 1]["role"] == "user":
                pytest.fail(f"Found consecutive user messages at indices {i} and {i+1}")
        
        # Last user message should be merged
        last_content = contents[-1]
        if last_content["role"] == "user":
            text = last_content["parts"][0]["text"]
            assert "Thanks!" in text
            assert "Now check London" in text

    def test_multimodal_user_messages_merged(self):
        """Test merging consecutive multimodal user messages."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Look at this:"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}}
                ]
            ),
            ChatMessage(role="user", content="What do you see?"),
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # Should be merged to single user message
        assert len(contents) == 1
        assert contents[0]["role"] == "user"
        
        # Should preserve both text and image
        parts = contents[0]["parts"]
        text_parts = [p for p in parts if "text" in p]
        # Gemini uses camelCase "inlineData" for images
        image_parts = [p for p in parts if "inlineData" in p or "inline_data" in p]
        
        assert len(image_parts) >= 1  # Image preserved
        # Text should contain both messages
        all_text = " ".join(p["text"] for p in text_parts)
        assert "Look at this:" in all_text or "What do you see?" in all_text


class TestEdgeCases:
    """Test edge cases for consecutive message handling."""

    def test_empty_user_messages_not_merged(self):
        """Test that empty user messages are handled gracefully."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="user", content=""),
            ChatMessage(role="user", content="World"),
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # All should be merged despite empty middle message
        assert len(contents) == 1
        text = contents[0]["parts"][0]["text"]
        assert "Hello" in text
        assert "World" in text

    def test_whitespace_only_user_messages(self):
        """Test merging with whitespace-only messages."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Question 1"),
            ChatMessage(role="user", content="   "),
            ChatMessage(role="user", content="Question 2"),
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        assert len(contents) == 1
        # Both questions should be present
        text = contents[0]["parts"][0]["text"]
        assert "Question 1" in text
        assert "Question 2" in text

    def test_all_users_conversation(self):
        """Test handling a conversation with only user messages (unlikely but possible)."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Message 1"),
            ChatMessage(role="user", content="Message 2"),
            ChatMessage(role="user", content="Message 3"),
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # All user messages should be merged into one
        assert len(contents) == 1
        text = contents[0]["parts"][0]["text"]
        assert "Message 1" in text
        assert "Message 2" in text
        assert "Message 3" in text


class TestFilterUnavailableToolCalls:
    """Test filtering of tool calls for unavailable tools (agent switching scenario)."""

    def test_no_filtering_when_all_tools_available(self):
        """Test that messages are unchanged when all tools are available."""
        messages = [
            ChatMessage(role="user", content="Check weather"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Berlin"}'}
                }]
            ),
            ChatMessage(
                role="tool",
                content='{"temp": 20}',
                tool_call_id="call_1",
                name="get_weather"
            ),
        ]
        
        available_tools = {"get_weather", "get_time"}
        filtered = filter_unavailable_tool_calls(messages, available_tools)
        
        # All messages should remain unchanged
        assert len(filtered) == 3
        assert filtered[1].tool_calls is not None
        assert len(filtered[1].tool_calls) == 1

    def test_filter_unavailable_tool_calls(self):
        """Test that tool calls for unavailable tools are converted to text."""
        messages = [
            ChatMessage(role="user", content="Do stuff"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "comfyui_workflow", "arguments": '{"op": "list"}'}
                }]
            ),
            ChatMessage(
                role="tool",
                content='{"workflows": []}',
                tool_call_id="call_1",
                name="comfyui_workflow"
            ),
            ChatMessage(role="assistant", content="Here are the workflows."),
        ]
        
        # comfyui_workflow is NOT in available tools (switched agents)
        available_tools = {"get_weather", "get_time"}
        filtered = filter_unavailable_tool_calls(messages, available_tools)
        
        # Should have 3 messages: user, assistant (with text summary), last assistant
        # The tool response is removed
        assert len(filtered) == 3
        assert filtered[0].role == "user"
        assert filtered[1].role == "assistant"
        assert "comfyui_workflow" in filtered[1].content  # Tool call converted to text
        assert filtered[1].tool_calls is None
        assert filtered[2].role == "assistant"
        assert filtered[2].content == "Here are the workflows."

    def test_filter_preserves_available_tool_calls(self):
        """Test that available tool calls are kept while unavailable are removed."""
        messages = [
            ChatMessage(role="user", content="Do multiple things"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{}'}
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "comfyui_workflow", "arguments": '{}'}
                    }
                ]
            ),
            ChatMessage(
                role="tool",
                content='{"temp": 20}',
                tool_call_id="call_1",
                name="get_weather"
            ),
            ChatMessage(
                role="tool",
                content='{"workflows": []}',
                tool_call_id="call_2",
                name="comfyui_workflow"
            ),
        ]
        
        available_tools = {"get_weather"}
        filtered = filter_unavailable_tool_calls(messages, available_tools)
        
        # Should have: user, assistant (with only get_weather call + text summary), tool response for get_weather
        assert len(filtered) == 3
        
        # Assistant should have only get_weather tool call
        assistant_msg = filtered[1]
        assert assistant_msg.role == "assistant"
        assert len(assistant_msg.tool_calls) == 1
        assert assistant_msg.tool_calls[0]["function"]["name"] == "get_weather"
        
        # Content should have summary of unavailable tool call
        assert "comfyui_workflow" in assistant_msg.content
        
        # Only get_weather response should remain
        tool_msg = filtered[2]
        assert tool_msg.role == "tool"
        assert tool_msg.tool_call_id == "call_1"

    def test_convert_to_text_summary(self):
        """Test that unavailable tool calls are converted to readable text."""
        messages = [
            ChatMessage(role="user", content="Generate image"),
            ChatMessage(
                role="assistant",
                content="Let me generate that for you.",
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "comfyui_workflow", "arguments": '{"prompt": "cat"}'}
                }]
            ),
            ChatMessage(
                role="tool",
                content='{"image_url": "..."}',
                tool_call_id="call_1",
                name="comfyui_workflow"
            ),
        ]
        
        available_tools = set()  # No tools available
        filtered = filter_unavailable_tool_calls(messages, available_tools)
        
        # Should have 2 messages: user, assistant (with tool call converted to text)
        # Tool response is removed
        assert len(filtered) == 2
        
        assistant_msg = filtered[1]
        assert assistant_msg.tool_calls is None  # All tool calls removed
        # Original content + summary
        assert "Let me generate that for you." in assistant_msg.content
        assert "comfyui_workflow" in assistant_msg.content
        assert '{"prompt": "cat"}' in assistant_msg.content

    def test_empty_available_tools_filters_all(self):
        """Test that empty available tools removes all tool calls."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "any_tool", "arguments": "{}"}
                }]
            ),
            ChatMessage(
                role="tool",
                content="result",
                tool_call_id="call_1",
                name="any_tool"
            ),
        ]
        
        filtered = filter_unavailable_tool_calls(messages, set())
        
        # Tool call converted to text, tool response removed
        assert len(filtered) == 2
        assert filtered[0].role == "user"
        assert filtered[1].role == "assistant"
        assert filtered[1].tool_calls is None
        assert "any_tool" in (filtered[1].content or "")

    def test_no_tool_calls_unchanged(self):
        """Test that messages without tool calls are unchanged."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!"),
            ChatMessage(role="user", content="How are you?"),
        ]
        
        filtered = filter_unavailable_tool_calls(messages, {"any_tool"})
        
        assert len(filtered) == 3
        assert filtered[0].content == "Hello"
        assert filtered[1].content == "Hi there!"
        assert filtered[2].content == "How are you?"


class TestFilterUnavailableToolCallsDict:
    """Test dict-based filtering for batch processing."""

    def test_no_filtering_when_all_tools_available(self):
        """Test that dict messages are unchanged when all tools are available."""
        from agent_system.llm.gemini_utils import filter_unavailable_tool_calls_dict
        
        messages = [
            {"role": "user", "content": "Check weather"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Berlin"}'}
                }]
            },
            {
                "role": "tool",
                "content": '{"temp": 20}',
                "tool_call_id": "call_1",
                "name": "get_weather"
            },
        ]
        
        available_tools = {"get_weather", "get_time"}
        filtered = filter_unavailable_tool_calls_dict(messages, available_tools)
        
        assert len(filtered) == 3
        assert filtered[1]["tool_calls"] is not None
        assert len(filtered[1]["tool_calls"]) == 1

    def test_filter_unavailable_tool_calls_dict(self):
        """Test that unavailable tool calls are converted to text in dict format."""
        from agent_system.llm.gemini_utils import filter_unavailable_tool_calls_dict
        
        messages = [
            {"role": "user", "content": "Do stuff"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "comfyui_workflow", "arguments": '{"op": "list"}'}
                }]
            },
            {
                "role": "tool",
                "content": '{"workflows": []}',
                "tool_call_id": "call_1",
                "name": "comfyui_workflow"
            },
            {"role": "assistant", "content": "Here are the workflows."},
        ]
        
        available_tools = {"get_weather"}
        filtered = filter_unavailable_tool_calls_dict(messages, available_tools)
        
        # Should have 3 messages: user, assistant (with text summary), last assistant
        assert len(filtered) == 3
        assert filtered[0]["role"] == "user"
        assert filtered[1]["role"] == "assistant"
        assert "comfyui_workflow" in filtered[1]["content"]
        assert filtered[1]["tool_calls"] is None
        assert filtered[2]["content"] == "Here are the workflows."

    def test_empty_available_tools_filters_all_dict(self):
        """Test that empty available tools removes all tool calls in dict format."""
        from agent_system.llm.gemini_utils import filter_unavailable_tool_calls_dict
        
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "any_tool", "arguments": "{}"}
                }]
            },
            {
                "role": "tool",
                "content": "result",
                "tool_call_id": "call_1",
                "name": "any_tool"
            },
        ]
        
        filtered = filter_unavailable_tool_calls_dict(messages, set())
        
        assert len(filtered) == 2
        assert filtered[0]["role"] == "user"
        assert filtered[1]["role"] == "assistant"
        assert filtered[1]["tool_calls"] is None
        assert "any_tool" in (filtered[1]["content"] or "")

    def test_no_tool_calls_unchanged_dict(self):
        """Test that dict messages without tool calls are unchanged."""
        from agent_system.llm.gemini_utils import filter_unavailable_tool_calls_dict
        
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        
        filtered = filter_unavailable_tool_calls_dict(messages, {"any_tool"})
        
        assert len(filtered) == 2
        assert filtered[0]["content"] == "Hello"
        assert filtered[1]["content"] == "Hi there!"
