"""Tests for Gemini SDK Client system message merging functionality."""
import pytest

from plugins_llm.llm_gemini.gemini_sdk_client import GeminiSDKClient
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

    def test_convert_multimodal_message_with_audio_dict(self, gemini_sdk_client):
        """Test conversion of message with audio content as dict."""
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Transcribe this audio"},
                    {
                        "type": "audio",
                        "audio_url": "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAA==",
                        "media_type": "audio/wav"
                    }
                ]
            )
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert len(contents) == 1
        parts = contents[0].parts
        assert len(parts) == 2
        
        # Check text part
        assert parts[0].text == "Transcribe this audio"
        
        # Check audio converted to inlineData
        assert hasattr(parts[1], 'inline_data')
        assert parts[1].inline_data.mime_type == "audio/wav"
        assert isinstance(parts[1].inline_data.data, bytes)

    def test_convert_multimodal_message_with_audio_pydantic(self, gemini_sdk_client):
        """Test conversion of message with audio content as AudioContent model."""
        from agent_system.llm.models import AudioContent
        
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "What is being said?"},
                    AudioContent(
                        type="audio",
                        audio_url="data:audio/mp3;base64,//uQxAAAAAANIAAAAAE=",
                        media_type="audio/mp3"
                    )
                ]
            )
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        assert len(contents) == 1
        parts = contents[0].parts
        assert len(parts) == 2
        
        # Check audio converted to inlineData
        assert hasattr(parts[1], 'inline_data')
        assert parts[1].inline_data.mime_type == "audio/mp3"
        assert isinstance(parts[1].inline_data.data, bytes)


class TestGeminiSDKClientInfiniteThinkingLoop:
    """Test detection and abort of infinite thinking loop (Gemini bug)."""

    @pytest.mark.asyncio
    async def test_streaming_infinite_thinking_loop_detection(self):
        """Test that infinite thinking loop is detected and raises error.
        
        When Gemini sends only thought=True chunks without progress (tool calls, 
        content, or finishReason), we detect this and abort after MAX_CONSECUTIVE_THOUGHT_CHUNKS.
        """
        from unittest.mock import AsyncMock, MagicMock, patch
        
        # Create client with no retries to simplify test
        gemini_sdk_client = GeminiSDKClient(
            model="gemini-2.5-flash",
            api_key="test-api-key",
            context_window=200000,
            request_timeout=180,
            max_retries=0  # No retries - fail immediately after detection
        )
        
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []
        
        # Create mock parts that simulate thinking-only chunks
        mock_thought_part = MagicMock()
        mock_thought_part.thought = True
        mock_thought_part.text = "Thinking..."
        mock_thought_part.function_call = None
        mock_thought_part.function_response = None
        
        mock_content = MagicMock()
        mock_content.parts = [mock_thought_part]
        
        mock_candidate = MagicMock()
        mock_candidate.content = mock_content
        mock_candidate.finish_reason = None  # No finish reason - simulates the bug
        
        mock_chunk = MagicMock()
        mock_chunk.candidates = [mock_candidate]
        mock_chunk.usage_metadata = None
        
        # Create async generator that yields 150 thought-only chunks (exceeds 100 limit)
        async def mock_stream():
            for _ in range(150):
                yield mock_chunk
        
        # Mock the client's generate_content_stream method
        mock_generate = AsyncMock(return_value=mock_stream())
        
        with patch.object(gemini_sdk_client._client.aio.models, 'generate_content_stream', mock_generate):
            # Should raise after detecting infinite loop
            with pytest.raises(Exception) as exc_info:
                async for _ in gemini_sdk_client.chat_tools_streaming(messages, tools):
                    pass
            
            # Check error message indicates infinite thinking loop
            error_msg = str(exc_info.value).lower()
            assert "infinite thinking loop" in error_msg or "chunks without progress" in error_msg


class TestGeminiSDKClientThoughtSignatureRestoration:
    """Test that thought_signature is correctly restored for historical function calls."""

    def test_thought_signature_restored_for_historical_function_calls(self, gemini_sdk_client):
        """Test that thought_signature is preserved when converting historical tool calls."""
        # Create a message with tool_calls that have thought_signature in extra_content
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant", 
                content="Let me check the weather.",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Berlin"}'
                        },
                        "extra_content": {
                            "google": {
                                "thought_signature": b"test_signature_bytes"
                            }
                        }
                    }
                ]
            ),
            ChatMessage(role="tool", name="get_weather", content='{"temp": 20}'),
            ChatMessage(role="user", content="Thanks!")
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # Find the assistant message with function call
        assistant_content = None
        for content in contents:
            if content.role == "model" and content.parts:
                for part in content.parts:
                    if part.function_call:
                        assistant_content = content
                        break
        
        assert assistant_content is not None, "Should have assistant content with function call"
        
        # Check that thought_signature is present
        function_call_part = None
        for part in assistant_content.parts:
            if part.function_call:
                function_call_part = part
                break
        
        assert function_call_part is not None
        assert function_call_part.thought_signature == b"test_signature_bytes"

    def test_bypass_token_when_thought_signature_not_present(self, gemini_sdk_client):
        """Test that missing thought_signature uses bypass token for Gemini 3."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant", 
                content="Let me check the weather.",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Berlin"}'
                        }
                        # No extra_content with thought_signature
                    }
                ]
            ),
            ChatMessage(role="tool", name="get_weather", content='{"temp": 20}'),
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # Find the assistant message with function call
        function_call_part = None
        for content in contents:
            if content.role == "model" and content.parts:
                for part in content.parts:
                    if part.function_call:
                        function_call_part = part
                        break
        
        assert function_call_part is not None
        # For Gemini 3 compatibility, when no thought_signature is present,
        # we use Google's documented bypass token to skip validation
        # See: https://ai.google.dev/gemini-api/docs/thought-signatures#faqs
        assert function_call_part.thought_signature == b"skip_thought_signature_validator"


class TestGeminiSDKClientMultimodalToolResponse:
    """Test native multimodal content in tool responses."""
    
    def test_tool_response_with_multimodal_content(self, gemini_sdk_client, tmp_path):
        """Test that multimodal content is added as inlineData parts."""
        from agent_system.llm.models import MultimodalToolContent
        
        # Create test image
        image_path = tmp_path / "test.png"
        image_path.write_bytes(b"fake image data")
        
        messages = [
            ChatMessage(role="user", content="Generate an image"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "function": {"name": "comfyui_workflow", "arguments": "{}"}
                }]
            ),
            ChatMessage(
                role="tool",
                name="comfyui_workflow",
                content='{"status": "completed"}',
                multimodal_content=[
                    MultimodalToolContent(
                        type="image",
                        path=str(image_path),
                        mime_type="image/png",
                        description="Generated image"
                    )
                ]
            ),
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # Find the tool response message
        tool_content = None
        for content in contents:
            if content.role == "tool":
                tool_content = content
                break
        
        assert tool_content is not None
        # Should have 2 parts: functionResponse + inlineData
        assert len(tool_content.parts) == 2
        
        # First part is functionResponse
        assert tool_content.parts[0].function_response is not None
        assert tool_content.parts[0].function_response.name == "comfyui_workflow"
        
        # Second part is inlineData with the image
        assert tool_content.parts[1].inline_data is not None
        assert tool_content.parts[1].inline_data.mime_type == "image/png"
        assert len(tool_content.parts[1].inline_data.data) > 0  # Base64 encoded
    
    def test_tool_response_without_multimodal_content(self, gemini_sdk_client):
        """Test that tool responses without multimodal work normally."""
        messages = [
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "function": {"name": "get_weather", "arguments": "{}"}
                }]
            ),
            ChatMessage(
                role="tool",
                name="get_weather",
                content='{"temp": 20, "condition": "sunny"}'
                # No multimodal_content
            ),
        ]
        
        system_instruction, contents = gemini_sdk_client._convert_messages_to_sdk(messages)
        
        # Find the tool response message
        tool_content = None
        for content in contents:
            if content.role == "tool":
                tool_content = content
                break
        
        assert tool_content is not None
        # Should have only 1 part: functionResponse
        assert len(tool_content.parts) == 1
        assert tool_content.parts[0].function_response is not None
