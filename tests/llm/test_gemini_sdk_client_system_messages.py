"""Tests for Gemini SDK Client system message merging functionality."""
import pytest

from agent_system.llm.gemini_sdk_client import GeminiSDKClient
from agent_system.llm.models import ChatMessage


# The CRITICAL instruction is always prepended to system messages to prevent MALFORMED_FUNCTION_CALL
CRITICAL_INSTRUCTION = (
    "CRITICAL: When calling functions, output the function name exactly as defined. "
    "Do NOT prepend 'default_api.' or any other namespace. Always generate valid JSON "
    "for function arguments. Properly escape all special characters in JSON strings "
    "(quotes, backslashes, newlines)."
)


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
        
        # CRITICAL instruction is prepended to prevent MALFORMED_FUNCTION_CALL
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "You are a helpful assistant." in system_instruction
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
        
        # CRITICAL instruction is prepended, then user system messages follow
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "You are a helpful assistant." in system_instruction
        assert "You specialize in Python programming." in system_instruction
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
        
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "First instruction." in system_instruction
        assert "Second instruction." in system_instruction
        assert "Third instruction." in system_instruction
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
        
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "You are a helpful assistant." in system_instruction
        assert "You specialize in Python." in system_instruction

    def test_only_empty_system_messages(self, gemini_sdk_client):
        """Test that only empty system messages still get CRITICAL instruction."""
        messages = [
            ChatMessage(role="system", content=""),
            ChatMessage(role="system", content=""),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # Even with no user system messages, CRITICAL instruction is added
        assert system_instruction == CRITICAL_INSTRUCTION
        assert len(contents) == 1

    def test_no_system_messages(self, gemini_sdk_client):
        """Test that no system messages still get CRITICAL instruction."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # CRITICAL instruction is always added to prevent MALFORMED_FUNCTION_CALL
        assert system_instruction == CRITICAL_INSTRUCTION
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
        
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "Main system prompt." in system_instruction
        assert "Additional context." in system_instruction
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
        
        # CRITICAL instruction + user prompts
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert large_prompt in system_instruction
        assert context_addition in system_instruction

    def test_system_message_order_preserved(self, gemini_sdk_client):
        """Test that system messages maintain their order."""
        messages = [
            ChatMessage(role="system", content="First: Be formal."),
            ChatMessage(role="system", content="Second: Be concise."),
            ChatMessage(role="system", content="Third: Be helpful."),
            ChatMessage(role="user", content="Test")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        # Verify user messages follow CRITICAL in order
        critical_end = system_instruction.index("First:")
        assert "Second:" in system_instruction[critical_end:]
        assert "Third: Be helpful." in system_instruction[critical_end:]

    def test_system_message_separator(self, gemini_sdk_client):
        """Test that system messages are separated by exactly two newlines."""
        messages = [
            ChatMessage(role="system", content="First"),
            ChatMessage(role="system", content="Second"),
            ChatMessage(role="user", content="Test")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # CRITICAL instruction + separator + First + separator + Second
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "First" in system_instruction
        assert "Second" in system_instruction
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
        
        # Should contain CRITICAL instruction + both parts
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "Character Reviewer" in system_instruction
        assert "Sub-Agent Context" in system_instruction
        assert "Book #1" in system_instruction
        
        # Both user prompts should be present
        assert main_prompt in system_instruction
        assert sub_agent_context in system_instruction

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


class TestGeminiSDKClientMultimodalMessages:
    """Test multimodal message support in SDK client."""

    def test_convert_multimodal_message_with_image_dict(self, gemini_sdk_client):
        """Test conversion of multimodal message with image (dict format)."""
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "What's in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
                        }
                    }
                ]
            )
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # CRITICAL instruction always present
        assert system_instruction == CRITICAL_INSTRUCTION
        assert len(contents) == 1
        assert contents[0].role == "user"
        assert len(contents[0].parts) == 2
        
        # Check text part
        assert contents[0].parts[0].text == "What's in this image?"
        
        # Check image part (converted to SDK Blob format)
        image_part = contents[0].parts[1]
        assert hasattr(image_part, 'inline_data')
        assert image_part.inline_data.mime_type == "image/jpeg"
        # SDK expects bytes, not base64 string
        assert isinstance(image_part.inline_data.data, bytes)

    def test_convert_multimodal_message_with_image_pydantic(self, gemini_sdk_client):
        """Test conversion of multimodal message with Pydantic ImageContent model."""
        from agent_system.llm.models import TextContent, ImageContent
        
        messages = [
            ChatMessage(
                role="user",
                content=[
                    TextContent(type="text", text="Analyze this chart"),
                    ImageContent(
                        type="image_url",
                        image_url={"url": "data:image/png;base64,iVBORw0KGgo="}
                    )
                ]
            )
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert len(contents) == 1
        assert len(contents[0].parts) == 2
        
        # Check text part
        assert contents[0].parts[0].text == "Analyze this chart"
        
        # Check image part
        image_part = contents[0].parts[1]
        assert hasattr(image_part, 'inline_data')
        assert image_part.inline_data.mime_type == "image/png"
        assert isinstance(image_part.inline_data.data, bytes)

    def test_convert_multimodal_message_anthropic_format(self, gemini_sdk_client):
        """Test conversion of Anthropic-style image format."""
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Describe this"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/webp",
                            "data": "UklGRiQAAABXRUJQ"
                        }
                    }
                ]
            )
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert len(contents) == 1
        parts = contents[0].parts
        assert len(parts) == 2
        
        # Check image conversion from Anthropic format
        image_part = parts[1]
        assert hasattr(image_part, 'inline_data')
        assert image_part.inline_data.mime_type == "image/webp"
        assert isinstance(image_part.inline_data.data, bytes)

    def test_convert_multimodal_message_multiple_images(self, gemini_sdk_client):
        """Test conversion of message with multiple images."""
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Compare these images"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="}
                    },
                    {"type": "text", "text": "and"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}
                    }
                ]
            )
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert len(contents) == 1
        assert len(contents[0].parts) == 4
        
        # Check structure
        assert contents[0].parts[0].text == "Compare these images"
        assert hasattr(contents[0].parts[1], 'inline_data')
        assert contents[0].parts[2].text == "and"
        assert hasattr(contents[0].parts[3], 'inline_data')
