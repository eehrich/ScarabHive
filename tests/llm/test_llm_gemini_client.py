"""Tests for Google Gemini native API client."""
import asyncio
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch

from agent_system.llm.gemini_client import GeminiClient
from agent_system.llm.models import ChatMessage


# The CRITICAL instruction is always prepended to system messages to prevent MALFORMED_FUNCTION_CALL
CRITICAL_INSTRUCTION = (
    "CRITICAL: When calling functions, output the function name exactly as defined. "
    "Do NOT prepend 'default_api.' or any other namespace. Always generate valid JSON "
    "for function arguments. Properly escape all special characters in JSON strings "
    "(quotes, backslashes, newlines)."
)


@pytest.fixture
def gemini_client():
    """Create a GeminiClient instance for testing."""
    return GeminiClient(
        model="gemini-2.0-flash-exp",
        api_key="test-api-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        context_window=200000,
        request_timeout=180
    )


class TestGeminiClientMessageConversion:
    """Test message format conversion from ChatMessage to Gemini format."""

    def test_convert_simple_user_message(self, gemini_client):
        """Test conversion of simple user message."""
        messages = [
            ChatMessage(role="user", content="Hello, how are you?")
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        # CRITICAL instruction always present to prevent MALFORMED_FUNCTION_CALL
        assert system_instruction == CRITICAL_INSTRUCTION
        assert len(contents) == 1
        assert contents[0]["role"] == "user"
        assert contents[0]["parts"] == [{"text": "Hello, how are you?"}]

    def test_convert_system_message(self, gemini_client):
        """Test that system messages are extracted separately."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        # CRITICAL instruction prepended to user system message
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "You are a helpful assistant." in system_instruction
        assert len(contents) == 1  # Only user message
        assert contents[0]["role"] == "user"
        assert contents[0]["parts"][0]["text"] == "Hello"

    def test_convert_assistant_message(self, gemini_client):
        """Test conversion of assistant message."""
        messages = [
            ChatMessage(role="assistant", content="I'm doing well, thank you!")
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        # CRITICAL instruction always present
        assert system_instruction == CRITICAL_INSTRUCTION
        assert len(contents) == 1
        assert contents[0]["role"] == "model"  # Gemini uses "model" instead of "assistant"
        assert contents[0]["parts"] == [{"text": "I'm doing well, thank you!"}]

    def test_convert_tool_response(self, gemini_client):
        """Test conversion of tool response message.
        
        IMPORTANT: Must use role="tool" (not "function") per current Gemini API.
        The "function" role was deprecated in Gemini 1.5.
        """
        messages = [
            ChatMessage(
                role="tool",
                content='{"temperature": 22, "condition": "sunny"}',
                tool_call_id="call_123",
                name="get_weather"
            )
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        # CRITICAL instruction always present
        assert system_instruction == CRITICAL_INSTRUCTION
        assert len(contents) == 1
        # Must be "tool" per current Gemini API (not "function" which was old convention)
        assert contents[0]["role"] == "tool", "Function responses must use role='tool' not 'function'"
        assert "functionResponse" in contents[0]["parts"][0]
        func_response = contents[0]["parts"][0]["functionResponse"]
        assert func_response["name"] == "get_weather"
        assert func_response["response"]["temperature"] == 22
        assert func_response["response"]["condition"] == "sunny"

    def test_convert_assistant_with_tool_calls(self, gemini_client):
        """Test conversion of assistant message with tool calls."""
        messages = [
            ChatMessage(
                role="assistant",
                content="Let me check the weather for you.",
                tool_calls=[
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Berlin", "units": "celsius"}'
                        }
                    }
                ]
            )
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        # CRITICAL instruction always present
        assert system_instruction == CRITICAL_INSTRUCTION
        assert len(contents) == 1
        assert contents[0]["role"] == "model"
        assert len(contents[0]["parts"]) == 2  # Text + function call
        
        # Check text part
        assert contents[0]["parts"][0] == {"text": "Let me check the weather for you."}
        
        # Check function call part
        func_call = contents[0]["parts"][1]["functionCall"]
        assert func_call["name"] == "get_weather"
        assert func_call["args"]["city"] == "Berlin"
        assert func_call["args"]["units"] == "celsius"

    def test_convert_multiple_tool_calls(self, gemini_client):
        """Test conversion of assistant message with multiple tool calls."""
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Berlin"}'
                        }
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "get_time",
                            "arguments": '{}'
                        }
                    }
                ]
            )
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        assert len(contents) == 1
        assert len(contents[0]["parts"]) == 2  # Two function calls
        
        assert contents[0]["parts"][0]["functionCall"]["name"] == "get_weather"
        assert contents[0]["parts"][1]["functionCall"]["name"] == "get_time"

    def test_convert_conversation_flow(self, gemini_client):
        """Test conversion of complete conversation with tools."""
        messages = [
            ChatMessage(role="system", content="You are a weather assistant."),
            ChatMessage(role="user", content="What's the weather in Berlin?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "Berlin"}'
                    }
                }]
            ),
            ChatMessage(
                role="tool",
                content='{"temp": 22}',
                tool_call_id="call_1",
                name="get_weather"
            ),
            ChatMessage(role="assistant", content="The weather in Berlin is 22°C.")
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        # CRITICAL instruction prepended
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "You are a weather assistant." in system_instruction
        assert len(contents) == 4  # User, Assistant, Function Response, Assistant
        
        assert contents[0]["role"] == "user"
        assert contents[1]["role"] == "model"
        # Must use "tool" not "function" (old convention)
        assert contents[2]["role"] == "tool"
        assert contents[3]["role"] == "model"

    def test_convert_parallel_tool_calls_merged(self, gemini_client):
        """Test that multiple parallel tool responses are merged into single content block.
        
        When a model makes multiple parallel tool calls, Gemini requires all
        corresponding function_responses to be in a single role="tool" content
        block. If they're separate, Gemini returns 400 INVALID_ARGUMENT with
        "Mismatched function_call/function_response pairs".
        
        This test verifies that consecutive tool messages are merged.
        """
        messages = [
            ChatMessage(role="user", content="Get weather for Berlin and time for Tokyo"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Berlin"}'
                        }
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "get_time",
                            "arguments": '{"city": "Tokyo"}'
                        }
                    },
                    {
                        "id": "call_3",
                        "type": "function",
                        "function": {
                            "name": "get_currency",
                            "arguments": '{"from": "EUR", "to": "JPY"}'
                        }
                    }
                ]
            ),
            # These separate tool responses should be merged
            ChatMessage(
                role="tool",
                content='{"temp": 22}',
                tool_call_id="call_1",
                name="get_weather"
            ),
            ChatMessage(
                role="tool",
                content='{"time": "15:30"}',
                tool_call_id="call_2",
                name="get_time"
            ),
            ChatMessage(
                role="tool",
                content='{"rate": 162.5}',
                tool_call_id="call_3",
                name="get_currency"
            ),
            ChatMessage(role="assistant", content="Results: Berlin is 22°C, Tokyo time is 15:30, and 1 EUR = 162.5 JPY.")
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        # Should have: User, Assistant with 3 calls, SINGLE merged tool response, Assistant response
        assert len(contents) == 4, f"Expected 4 content blocks, got {len(contents)}: {[c.get('role') for c in contents]}"
        
        # Verify structure
        assert contents[0]["role"] == "user"
        assert contents[1]["role"] == "model"
        assert contents[2]["role"] == "tool"
        assert contents[3]["role"] == "model"
        
        # Verify the assistant message has 3 function calls
        assert len(contents[1]["parts"]) == 3
        assert contents[1]["parts"][0]["functionCall"]["name"] == "get_weather"
        assert contents[1]["parts"][1]["functionCall"]["name"] == "get_time"
        assert contents[1]["parts"][2]["functionCall"]["name"] == "get_currency"
        
        # CRITICAL: Verify all 3 tool responses are merged into ONE content block
        tool_response = contents[2]
        assert len(tool_response["parts"]) == 3, f"Expected 3 merged parts, got {len(tool_response['parts'])}"
        
        # Verify each function response is present
        func_names = [p["functionResponse"]["name"] for p in tool_response["parts"]]
        assert "get_weather" in func_names
        assert "get_time" in func_names
        assert "get_currency" in func_names

    def test_convert_tool_calls_with_thought_signature(self, gemini_client):
        """Test that thought signatures are preserved in tool calls (Gemini 3 Pro requirement)."""
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "Berlin"}'
                    },
                    # Store thought_signature in OpenAI-compatible format
                    "extra_content": {
                        "google": {
                            "thought_signature": "abc123_test_signature"
                        }
                    }
                }]
            )
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        assert len(contents) == 1
        assert len(contents[0]["parts"]) == 1
        
        part = contents[0]["parts"][0]
        assert "functionCall" in part
        assert part["functionCall"]["name"] == "get_weather"
        # Verify thoughtSignature is included
        assert "thoughtSignature" in part
        assert part["thoughtSignature"] == "abc123_test_signature"

    def test_convert_tool_calls_with_direct_thought_signature(self, gemini_client):
        """Test that direct thought_signature field is also supported."""
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "Berlin"}'
                    },
                    # Direct field (fallback)
                    "thought_signature": "direct_signature_xyz"
                }]
            )
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        part = contents[0]["parts"][0]
        assert "thoughtSignature" in part
        assert part["thoughtSignature"] == "direct_signature_xyz"

    def test_convert_multimodal_message_with_image_dict(self, gemini_client):
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
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        # CRITICAL instruction always present
        assert system_instruction == CRITICAL_INSTRUCTION
        assert len(contents) == 1
        assert contents[0]["role"] == "user"
        assert len(contents[0]["parts"]) == 2
        
        # Check text part
        assert contents[0]["parts"][0] == {"text": "What's in this image?"}
        
        # Check image part (converted to Gemini inlineData format)
        image_part = contents[0]["parts"][1]
        assert "inlineData" in image_part
        assert image_part["inlineData"]["mimeType"] == "image/jpeg"
        assert image_part["inlineData"]["data"] == "/9j/4AAQSkZJRg=="

    def test_convert_multimodal_message_with_image_pydantic(self, gemini_client):
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
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        assert len(contents) == 1
        assert len(contents[0]["parts"]) == 2
        
        # Check text part
        assert contents[0]["parts"][0] == {"text": "Analyze this chart"}
        
        # Check image part
        image_part = contents[0]["parts"][1]
        assert "inlineData" in image_part
        assert image_part["inlineData"]["mimeType"] == "image/png"
        assert image_part["inlineData"]["data"] == "iVBORw0KGgo="

    def test_convert_multimodal_message_anthropic_format(self, gemini_client):
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
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        assert len(contents) == 1
        parts = contents[0]["parts"]
        assert len(parts) == 2
        
        # Check image conversion from Anthropic format
        image_part = parts[1]
        assert "inlineData" in image_part
        assert image_part["inlineData"]["mimeType"] == "image/webp"
        assert image_part["inlineData"]["data"] == "UklGRiQAAABXRUJQ"

    def test_convert_tool_calls_without_thought_signature(self, gemini_client):
        """Test that missing thought signature is NOT replaced with bypass token.
        
        Google's validation is only for the CURRENT turn, not historical function calls.
        Setting bypass tokens on ALL function calls without signatures can cause
        MALFORMED_FUNCTION_CALL errors. We only restore existing signatures.
        """
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "Berlin"}'
                    }
                    # No thought_signature
                }]
            )
        ]
        
        system_instruction, contents = gemini_client._convert_messages_to_gemini(messages)
        
        part = contents[0]["parts"][0]
        # Should NOT have thoughtSignature when not present originally
        assert "thoughtSignature" not in part


class TestGeminiClientToolConversion:
    """Test tool schema conversion from OpenAI to Gemini format."""

    def test_convert_simple_tool(self, gemini_client):
        """Test conversion of simple tool schema."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather for a city",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "City name"}
                        },
                        "required": ["city"]
                    }
                }
            }
        ]
        
        function_declarations = gemini_client._convert_tools_to_gemini(tools)
        
        assert len(function_declarations) == 1
        assert function_declarations[0]["name"] == "get_weather"
        assert function_declarations[0]["description"] == "Get current weather for a city"
        assert "parameters" in function_declarations[0]
        assert function_declarations[0]["parameters"]["properties"]["city"]["type"] == "string"

    def test_convert_multiple_tools(self, gemini_client):
        """Test conversion of multiple tools."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": "Get current time",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        ]
        
        function_declarations = gemini_client._convert_tools_to_gemini(tools)
        
        assert len(function_declarations) == 2
        assert function_declarations[0]["name"] == "get_weather"
        assert function_declarations[1]["name"] == "get_time"

    def test_convert_tool_without_parameters(self, gemini_client):
        """Test conversion of tool without parameters."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "hello_world",
                    "description": "Say hello"
                }
            }
        ]
        
        function_declarations = gemini_client._convert_tools_to_gemini(tools)
        
        assert len(function_declarations) == 1
        assert function_declarations[0]["name"] == "hello_world"
        assert "parameters" not in function_declarations[0]

    def test_skip_non_function_tools(self, gemini_client):
        """Test that non-function tools are skipped."""
        tools = [
            {
                "type": "function",
                "function": {"name": "test", "description": "Test"}
            },
            {
                "type": "other",  # Invalid type
                "function": {"name": "skip_me"}
            }
        ]
        
        function_declarations = gemini_client._convert_tools_to_gemini(tools)
        
        assert len(function_declarations) == 1
        assert function_declarations[0]["name"] == "test"

    def test_clean_schema_removes_additional_properties(self, gemini_client):
        """Test that additionalProperties and other unsupported fields are removed."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "Test tool with additionalProperties",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "nested": {
                                "type": "object",
                                "properties": {
                                    "value": {"type": "number"}
                                },
                                "additionalProperties": False
                            }
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                        "$schema": "http://json-schema.org/draft-07/schema#"
                    }
                }
            }
        ]
        
        function_declarations = gemini_client._convert_tools_to_gemini(tools)
        
        assert len(function_declarations) == 1
        params = function_declarations[0]["parameters"]
        
        # Check that additionalProperties is removed at root level
        assert "additionalProperties" not in params
        assert "$schema" not in params
        
        # Check that additionalProperties is removed from nested objects
        assert "additionalProperties" not in params["properties"]["nested"]
        
        # Check that valid fields are kept
        assert params["type"] == "object"
        assert "name" in params["properties"]
        assert params["required"] == ["name"]


class TestGeminiClientStreaming:
    """Test streaming functionality."""

    @pytest.mark.asyncio
    async def test_streaming_text_response(self, gemini_client):
        """Test streaming a simple text response."""
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []

        # Mock HTTPX streaming response
        mock_response = MagicMock()
        mock_response.status_code = 200
        
        async def mock_aiter_lines():
            yield "data: " + json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{"text": "Hello"}]
                    }
                }]
            })
            yield "data: " + json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{"text": " there!"}]
                    }
                }]
            })
            yield "data: [DONE]"
        
        mock_response.aiter_lines = mock_aiter_lines

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            
            mock_stream = MagicMock()
            mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=mock_stream)
            
            mock_client_class.return_value = mock_client

            events = []
            async for event in gemini_client.chat_tools_streaming(messages, tools):
                events.append(event)

            # Check we got content deltas and final result
            content_deltas = [e for e in events if e["type"] == "content_delta"]
            assert len(content_deltas) == 2
            assert content_deltas[0]["delta"] == "Hello"
            assert content_deltas[1]["delta"] == " there!"
            
            final_events = [e for e in events if e["type"] == "final"]
            assert len(final_events) == 1
            assert final_events[0]["assistant"]["content"] == "Hello there!"

    @pytest.mark.asyncio
    async def test_streaming_function_call(self, gemini_client):
        """Test streaming a function call response."""
        messages = [ChatMessage(role="user", content="What's the weather?")]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        
        async def mock_aiter_lines():
            yield "data: " + json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"city": "Berlin"}
                            }
                        }]
                    }
                }]
            })
            yield "data: [DONE]"
        
        mock_response.aiter_lines = mock_aiter_lines

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            
            mock_stream = MagicMock()
            mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=mock_stream)
            
            mock_client_class.return_value = mock_client

            events = []
            async for event in gemini_client.chat_tools_streaming(messages, tools):
                events.append(event)

            # Check we got tool call delta and final result
            tool_deltas = [e for e in events if e["type"] == "tool_call_delta"]
            assert len(tool_deltas) == 1
            
            final_events = [e for e in events if e["type"] == "final"]
            assert len(final_events) == 1
            assert "tool_calls" in final_events[0]["assistant"]
            assert len(final_events[0]["assistant"]["tool_calls"]) == 1
            assert final_events[0]["assistant"]["tool_calls"][0]["function"]["name"] == "get_weather"

    @pytest.mark.asyncio
    async def test_streaming_function_call_with_thought_signature(self, gemini_client):
        """Test that thought signatures are extracted from streaming function calls (Gemini 3 Pro)."""
        messages = [ChatMessage(role="user", content="What's the weather?")]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        
        async def mock_aiter_lines():
            yield "data: " + json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"city": "Berlin"}
                            },
                            # Gemini 3 Pro returns thoughtSignature in the part
                            "thoughtSignature": "gemini3_thought_sig_abc123"
                        }]
                    }
                }]
            })
            yield "data: [DONE]"
        
        mock_response.aiter_lines = mock_aiter_lines

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            
            mock_stream = MagicMock()
            mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=mock_stream)
            
            mock_client_class.return_value = mock_client

            events = []
            async for event in gemini_client.chat_tools_streaming(messages, tools):
                events.append(event)

            final_events = [e for e in events if e["type"] == "final"]
            assert len(final_events) == 1
            
            tool_call = final_events[0]["assistant"]["tool_calls"][0]
            
            # Verify thoughtSignature is stored in both locations
            assert "thought_signature" in tool_call
            assert tool_call["thought_signature"] == "gemini3_thought_sig_abc123"
            
            # Also check extra_content format (OpenAI-compatible)
            assert "extra_content" in tool_call
            assert tool_call["extra_content"]["google"]["thought_signature"] == "gemini3_thought_sig_abc123"

    @pytest.mark.asyncio
    async def test_streaming_with_usage_metadata(self, gemini_client):
        """Test that usage metadata is captured."""
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []

        mock_response = MagicMock()
        mock_response.status_code = 200
        
        async def mock_aiter_lines():
            yield "data: " + json.dumps({
                "candidates": [{
                    "content": {"parts": [{"text": "Hi"}]}
                }],
                # usageMetadata is at top level, not in candidate
                "usageMetadata": {
                    # Gemini API uses snake_case
                    "prompt_token_count": 10,
                    "candidates_token_count": 5,
                    "total_token_count": 15
                }
            })
            yield "data: [DONE]"
        
        mock_response.aiter_lines = mock_aiter_lines

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            
            mock_stream = MagicMock()
            mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=mock_stream)
            
            mock_client_class.return_value = mock_client

            events = []
            async for event in gemini_client.chat_tools_streaming(messages, tools):
                events.append(event)

            final_events = [e for e in events if e["type"] == "final"]
            assert len(final_events) == 1
            assert "usage" in final_events[0]
            assert final_events[0]["usage"]["prompt_tokens"] == 10
            assert final_events[0]["usage"]["completion_tokens"] == 5
            assert final_events[0]["usage"]["total_tokens"] == 15

    @pytest.mark.asyncio
    async def test_streaming_thought_parts_stream_as_content_delta(self):
        """Gemini thought summaries (thought=true) stream as content_delta for unified response display."""
        gemini_client = GeminiClient(
            model="gemini-3-pro-preview",
            api_key="test-api-key",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            include_thoughts=True,
        )

        messages = [ChatMessage(role="user", content="Hello")]
        tools = []

        mock_response = MagicMock()
        mock_response.status_code = 200

        async def mock_aiter_lines():
            yield "data: " + json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [
                            {"text": "Thinking...", "thought": True},
                            {"text": "Hi"}
                        ]
                    }
                }]
            })
            yield "data: [DONE]"

        mock_response.aiter_lines = mock_aiter_lines

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            mock_stream = MagicMock()
            mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=mock_stream)

            mock_client_class.return_value = mock_client

            events = []
            async for event in gemini_client.chat_tools_streaming(messages, tools):
                events.append(event)

            # Both thought and content are streamed as content_delta
            content_deltas = [e for e in events if e["type"] == "content_delta"]
            assert len(content_deltas) == 2
            assert content_deltas[0]["delta"] == "Thinking..."
            assert content_deltas[1]["delta"] == "Hi"

            final_events = [e for e in events if e["type"] == "final"]
            assert len(final_events) == 1
            # Only non-thought content is stored in assistant response (thoughts are streamed only)
            assert final_events[0]["assistant"]["content"] == "Hi"

    @pytest.mark.asyncio
    async def test_streaming_payload_includes_thinking_config_when_enabled(self):
        """When include_thoughts is enabled, request payload must include thinkingConfig.includeThoughts."""
        gemini_client = GeminiClient(
            model="gemini-3-pro-preview",
            api_key="test-api-key",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            include_thoughts=True,
        )

        messages = [ChatMessage(role="user", content="Hello")]
        tools = []

        mock_response = MagicMock()
        mock_response.status_code = 200

        async def mock_aiter_lines():
            yield "data: " + json.dumps({
                "candidates": [{
                    "content": {"parts": [{"text": "Hi"}]}
                }]
            })
            yield "data: [DONE]"

        mock_response.aiter_lines = mock_aiter_lines

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            mock_stream = MagicMock()
            mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=mock_stream)

            mock_client_class.return_value = mock_client

            events = []
            async for event in gemini_client.chat_tools_streaming(messages, tools):
                events.append(event)

            # Verify payload passed into httpx stream
            assert mock_client.stream.call_count == 1
            _, kwargs = mock_client.stream.call_args
            assert "json" in kwargs
            # thinkingConfig is now inside generationConfig
            gen_config = kwargs["json"].get("generationConfig", {})
            assert gen_config.get("thinkingConfig") == {"thinkingBudget": 8192, "includeThoughts": True}

    @pytest.mark.asyncio
    async def test_streaming_http_error(self, gemini_client):
        """Test handling of HTTP errors."""
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.request = MagicMock()
        
        # Mock aread() to return bytes (not atext which doesn't exist)
        async def mock_aread():
            return json.dumps({
                "error": {
                    "code": 400,
                    "message": "Invalid request"
                }
            }).encode('utf-8')
        
        mock_response.aread = mock_aread

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            
            mock_stream = MagicMock()
            mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=mock_stream)
            
            mock_client_class.return_value = mock_client

            with pytest.raises(Exception) as exc_info:
                async for _ in gemini_client.chat_tools_streaming(messages, tools):
                    pass

            assert "HTTP 400" in str(exc_info.value) or "Invalid request" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_streaming_cancellation(self, gemini_client):
        """Test cancellation during streaming."""
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []

        # Mock cancellation token
        mock_token = MagicMock()
        mock_token.is_cancelled = True

        with pytest.raises(asyncio.CancelledError):
            async for _ in gemini_client.chat_tools_streaming(messages, tools, cancellation_token=mock_token):
                pass

    @pytest.mark.asyncio
    async def test_streaming_infinite_thinking_loop_detection(self, gemini_client):
        """Test detection and abort of infinite thinking loop (Gemini bug).
        
        When Gemini sends only thought=True chunks without progress (tool calls, 
        content, or finishReason), we detect this and abort after MAX_CONSECUTIVE_THOUGHT_CHUNKS.
        """
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []

        mock_response = MagicMock()
        mock_response.status_code = 200

        # Simulate infinite thinking loop: 150 thought-only chunks (exceeds 100 limit)
        thought_chunks = []
        for i in range(150):
            thought_chunks.append("data: " + json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{"text": f"Thinking step {i}...", "thought": True}]
                    }
                }]
            }))
        # Never send finishReason or tool calls - simulates the bug

        async def mock_aiter_lines():
            for chunk in thought_chunks:
                yield chunk

        mock_response.aiter_lines = mock_aiter_lines

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            mock_stream = MagicMock()
            mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=mock_stream)

            mock_client_class.return_value = mock_client

            # Should raise after detecting infinite loop (after retries exhausted)
            with pytest.raises(Exception) as exc_info:
                async for _ in gemini_client.chat_tools_streaming(messages, tools):
                    pass

            # Check error message indicates infinite thinking loop
            assert "infinite thinking loop" in str(exc_info.value).lower() or "chunks without progress" in str(exc_info.value).lower()


class TestGeminiClientNonStreaming:
    """Test non-streaming methods."""

    @pytest.mark.asyncio
    async def test_chat_tools_text_response(self, gemini_client):
        """Test non-streaming text response."""
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "candidates": [{
                "content": {
                    "parts": [{"text": "Hello there!"}]
                }
            }],
            # usageMetadata is at top level, not in candidate
            "usageMetadata": {
                # Gemini API uses snake_case
                "prompt_token_count": 5,
                "candidates_token_count": 3,
                "total_token_count": 8
            }
        })

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            
            mock_client_class.return_value = mock_client

            result = await gemini_client.chat_tools(messages, tools)

            assert result["assistant"]["content"] == "Hello there!"
            assert result["usage"]["prompt_tokens"] == 5
            assert result["usage"]["completion_tokens"] == 3

    @pytest.mark.asyncio
    async def test_chat_tools_function_call(self, gemini_client):
        """Test non-streaming function call response."""
        messages = [ChatMessage(role="user", content="What's the weather?")]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather"
                }
            }
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "candidates": [{
                "content": {
                    "parts": [{
                        "functionCall": {
                            "name": "get_weather",
                            "args": {"city": "Berlin"}
                        }
                    }]
                }
            }],
            # usageMetadata is at top level, not in candidate
            "usageMetadata": {
                # Gemini API uses snake_case
                "prompt_token_count": 10,
                "candidates_token_count": 5,
                "total_token_count": 15
            }
        })

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            
            mock_client_class.return_value = mock_client

            result = await gemini_client.chat_tools(messages, tools)

            assert "tool_calls" in result["assistant"]
            assert len(result["assistant"]["tool_calls"]) == 1
            assert result["assistant"]["tool_calls"][0]["function"]["name"] == "get_weather"

    @pytest.mark.asyncio
    async def test_chat_simple(self, gemini_client):
        """Test simple chat without tools."""
        messages = [ChatMessage(role="user", content="Hello")]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "candidates": [{
                "content": {
                    "parts": [{"text": "Hi there!"}]
                }
            }]
        })

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            
            mock_client_class.return_value = mock_client

            result = await gemini_client.chat(messages)

            assert result == "Hi there!"
