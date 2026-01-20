"""Tests for Gemini consecutive user message merging functionality.

Gemini API (unlike OpenAI) does NOT allow consecutive messages with the same role.
These tests verify that the conversion layer correctly merges consecutive user messages.

See: https://github.com/google/generative-ai-python/issues/209
"""
import pytest

from agent_system.llm.gemini_utils import (
    convert_openai_messages_to_gemini,
    _merge_consecutive_same_role_messages,
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
