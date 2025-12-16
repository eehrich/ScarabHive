"""Tests for Gemini SDK Client system message merging functionality."""
import pytest

from agent_system.llm.gemini_sdk_client import GeminiSDKClient
from agent_system.llm.models import ChatMessage


@pytest.fixture
def gemini_sdk_client():
    """Create a GeminiSDKClient instance for testing."""
    return GeminiSDKClient(
        model="gemini-2.5-flash",
        api_key="test-api-key",
        context_window=200000,
        request_timeout=180
    )


class TestGeminiSDKClientSystemMessageMerging:
    """Test that multiple system messages are correctly merged in SDK client."""

    def test_single_system_message(self, gemini_sdk_client):
        """Test that a single system message is handled correctly."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert system_instruction == "You are a helpful assistant."
        assert len(contents) == 1
        assert contents[0].role == "user"

    def test_multiple_system_messages_merged(self, gemini_sdk_client):
        """Test that multiple system messages are merged with newlines."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="system", content="You specialize in Python programming."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        expected = "You are a helpful assistant.\n\nYou specialize in Python programming."
        assert system_instruction == expected
        assert len(contents) == 1
        assert contents[0].role == "user"

    def test_three_system_messages_merged(self, gemini_sdk_client):
        """Test merging three system messages."""
        messages = [
            ChatMessage(role="system", content="First instruction."),
            ChatMessage(role="system", content="Second instruction."),
            ChatMessage(role="system", content="Third instruction."),
            ChatMessage(role="user", content="Test")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        expected = "First instruction.\n\nSecond instruction.\n\nThird instruction."
        assert system_instruction == expected
        assert len(contents) == 1

    def test_empty_system_messages_ignored(self, gemini_sdk_client):
        """Test that empty system messages are ignored."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="system", content=""),
            ChatMessage(role="system", content="You specialize in Python."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        expected = "You are a helpful assistant.\n\nYou specialize in Python."
        assert system_instruction == expected

    def test_only_empty_system_messages(self, gemini_sdk_client):
        """Test that only empty system messages result in None."""
        messages = [
            ChatMessage(role="system", content=""),
            ChatMessage(role="system", content=""),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert system_instruction is None
        assert len(contents) == 1

    def test_no_system_messages(self, gemini_sdk_client):
        """Test that no system messages result in None."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert system_instruction is None
        assert len(contents) == 2

    def test_system_messages_with_tool_calls(self, gemini_sdk_client):
        """Test that system messages are merged correctly in complex conversations."""
        messages = [
            ChatMessage(role="system", content="Main system prompt."),
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location": "NYC"}'
                    }
                }]
            ),
            ChatMessage(
                role="tool",
                content='{"temp": 22}',
                tool_call_id="call_123",
                name="get_weather"
            ),
            ChatMessage(role="system", content="Additional context."),
            ChatMessage(role="user", content="Continue")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        expected = "Main system prompt.\n\nAdditional context."
        assert system_instruction == expected
        # Should have: user, model (with tool call), function response, user
        assert len(contents) == 4

    def test_large_system_messages_preserved(self, gemini_sdk_client):
        """Test that large system messages are fully preserved."""
        large_prompt = "A" * 10000
        context_addition = "B" * 2000
        
        messages = [
            ChatMessage(role="system", content=large_prompt),
            ChatMessage(role="system", content=context_addition),
            ChatMessage(role="user", content="Test")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        expected = f"{large_prompt}\n\n{context_addition}"
        assert system_instruction == expected
        assert len(system_instruction) == 10000 + 2 + 2000  # Including "\n\n" (2 chars)

    def test_system_message_order_preserved(self, gemini_sdk_client):
        """Test that system messages maintain their order."""
        messages = [
            ChatMessage(role="system", content="First: Be formal."),
            ChatMessage(role="system", content="Second: Be concise."),
            ChatMessage(role="system", content="Third: Be helpful."),
            ChatMessage(role="user", content="Test")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        expected = "First: Be formal.\n\nSecond: Be concise.\n\nThird: Be helpful."
        assert system_instruction == expected
        # Verify order is maintained
        assert system_instruction.startswith("First:")
        assert "Second:" in system_instruction
        assert system_instruction.endswith("Third: Be helpful.")

    def test_system_message_separator(self, gemini_sdk_client):
        """Test that system messages are separated by exactly two newlines."""
        messages = [
            ChatMessage(role="system", content="First"),
            ChatMessage(role="system", content="Second"),
            ChatMessage(role="user", content="Test")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert system_instruction == "First\n\nSecond"
        # Verify separator
        assert "\n\n" in system_instruction
        assert "\n\n\n" not in system_instruction  # No triple newlines

    def test_realistic_character_reviewer_scenario(self, gemini_sdk_client):
        """Test realistic scenario with main prompt + sub-agent context."""
        main_prompt = """# Character Reviewer

## 🔍 ROLLE
Kritischer Gutachter für Charakterprofile und Character-Arcs.

## 🚫 ABSOLUTES VERBOT
Du bist REVIEWER, nicht CREATOR!

## 🎯 ZIEL
Qualitätssicherung für Charakterprofile und Character-Arcs."""

        sub_agent_context = """# Sub-Agent Context

Current book: Book #1 "Test Novel"
Current chapter: Chapter 2
Available characters: 3 characters"""

        messages = [
            ChatMessage(role="system", content=main_prompt),
            ChatMessage(role="system", content=sub_agent_context),
            ChatMessage(role="user", content="Review character profiles for Book #1")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # Should contain both parts
        assert "Character Reviewer" in system_instruction
        assert "Sub-Agent Context" in system_instruction
        assert "Book #1" in system_instruction
        
        # Should be separated by double newline
        assert main_prompt in system_instruction
        assert sub_agent_context in system_instruction
        
        # Total length should be sum of both + separator
        expected_length = len(main_prompt) + 2 + len(sub_agent_context)  # 2 = len("\n\n")
        assert len(system_instruction) == expected_length

    def test_tool_response_uses_correct_role(self, gemini_sdk_client):
        """Test that tool responses use role='tool' not 'user'.
        
        CRITICAL: This test verifies the fix for MALFORMED_FUNCTION_CALL errors.
        Function responses MUST use role="tool" per Gemini SDK documentation.
        Using role="user" causes MALFORMED_FUNCTION_CALL errors with empty responses.
        """
        messages = [
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "call_weather_123",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location": "Berlin"}'
                    }
                }]
            ),
            ChatMessage(
                role="tool",
                content='{"temperature": 22, "condition": "sunny"}',
                tool_call_id="call_weather_123",
                name="get_weather"
            )
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # Find the tool response in contents
        tool_response = None
        for part in contents:
            if hasattr(part, 'parts') and part.parts:
                for p in part.parts:
                    if hasattr(p, 'function_response'):
                        tool_response = part
                        break
        
        assert tool_response is not None, "Tool response not found in converted messages"
        
        # CRITICAL: Must be "tool" not "user" to avoid MALFORMED_FUNCTION_CALL
        assert tool_response.role == "tool", (
            f"Function responses must use role='tool' not '{tool_response.role}'. "
            "Using role='user' causes MALFORMED_FUNCTION_CALL errors!"
        )
