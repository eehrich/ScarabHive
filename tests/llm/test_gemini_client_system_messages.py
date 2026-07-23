"""Tests for Gemini Client system message merging functionality."""

from agent_system.llm.gemini_utils import convert_openai_messages_to_gemini
from agent_system.llm.models import ChatMessage


# The CRITICAL instruction is always prepended to system messages to prevent MALFORMED_FUNCTION_CALL
CRITICAL_INSTRUCTION = (
    "CRITICAL: When calling functions, output the function name exactly as defined. "
    "Do NOT prepend 'default_api.' or any other namespace. Always generate valid JSON "
    "for function arguments. Properly escape all special characters in JSON strings "
    "(quotes, backslashes, newlines)."
)


class TestGeminiClientSystemMessageMerging:
    """Test that multiple system messages are correctly merged."""

    def test_single_system_message(self):
        """Test that a single system message is handled correctly."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # CRITICAL instruction is prepended to prevent MALFORMED_FUNCTION_CALL
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "You are a helpful assistant." in system_instruction
        assert len(contents) == 1
        assert contents[0]["role"] == "user"

    def test_multiple_system_messages_merged(self):
        """Test that multiple system messages are merged with newlines."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="system", content="You specialize in Python programming."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # CRITICAL instruction is prepended, then user system messages follow
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "You are a helpful assistant." in system_instruction
        assert "You specialize in Python programming." in system_instruction
        assert len(contents) == 1
        assert contents[0]["role"] == "user"

    def test_three_system_messages_merged(self):
        """Test merging three system messages."""
        messages = [
            ChatMessage(role="system", content="First instruction."),
            ChatMessage(role="system", content="Second instruction."),
            ChatMessage(role="system", content="Third instruction."),
            ChatMessage(role="user", content="Test")
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # CRITICAL instruction prepended, then user messages in order
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "First instruction." in system_instruction
        assert "Second instruction." in system_instruction
        assert "Third instruction." in system_instruction
        assert len(contents) == 1

    def test_empty_system_messages_ignored(self):
        """Test that empty system messages are ignored."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="system", content=""),
            ChatMessage(role="system", content="You specialize in Python."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "You are a helpful assistant." in system_instruction
        assert "You specialize in Python." in system_instruction

    def test_only_empty_system_messages(self):
        """Test that only empty system messages still get CRITICAL instruction."""
        messages = [
            ChatMessage(role="system", content=""),
            ChatMessage(role="system", content=""),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # Even with no user system messages, CRITICAL instruction is added
        assert system_instruction == CRITICAL_INSTRUCTION
        assert len(contents) == 1

    def test_no_system_messages(self):
        """Test that no system messages still get CRITICAL instruction."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # CRITICAL instruction is always added to prevent MALFORMED_FUNCTION_CALL
        assert system_instruction == CRITICAL_INSTRUCTION
        assert len(contents) == 2

    def test_system_messages_with_tool_calls(self):
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
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert "Main system prompt." in system_instruction
        assert "Additional context." in system_instruction
        # Should have: user, model (with tool call), function response, user
        assert len(contents) == 4

    def test_large_system_messages_preserved(self):
        """Test that large system messages are fully preserved."""
        large_prompt = "A" * 10000
        context_addition = "B" * 2000
        
        messages = [
            ChatMessage(role="system", content=large_prompt),
            ChatMessage(role="system", content=context_addition),
            ChatMessage(role="user", content="Test")
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # CRITICAL instruction + user prompts
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        assert large_prompt in system_instruction
        assert context_addition in system_instruction
        # Length should be CRITICAL + separator + large + separator + context
        assert len(system_instruction) >= len(CRITICAL_INSTRUCTION) + len(large_prompt) + len(context_addition)

    def test_system_message_order_preserved(self):
        """Test that system messages maintain their order."""
        messages = [
            ChatMessage(role="system", content="First: Be formal."),
            ChatMessage(role="system", content="Second: Be concise."),
            ChatMessage(role="system", content="Third: Be helpful."),
            ChatMessage(role="user", content="Test")
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        assert system_instruction.startswith(CRITICAL_INSTRUCTION)
        # Verify user messages follow CRITICAL in order
        critical_end = system_instruction.index("First:")
        assert "Second:" in system_instruction[critical_end:]
        assert "Third: Be helpful." in system_instruction[critical_end:]

    def test_tool_response_uses_correct_role(self):
        """Test that tool responses use role='tool' not 'function'.
        
        CRITICAL: This test verifies the fix for MALFORMED_FUNCTION_CALL errors.
        Function responses MUST use role="tool" per current Gemini API.
        The "function" role was deprecated in Gemini 1.5.
        Using role="function" causes MALFORMED_FUNCTION_CALL errors.
        """
        messages = [
            ChatMessage(role="user", content="What's the weather in Paris?"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "call_weather_789",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location": "Paris"}'
                    }
                }]
            ),
            ChatMessage(
                role="tool",
                content='{"temperature": 18, "condition": "cloudy"}',
                tool_call_id="call_weather_789",
                name="get_weather"
            )
        ]
        
        system_instruction, contents = convert_openai_messages_to_gemini(messages)
        
        # Find the tool response in contents
        tool_response = None
        for content in contents:
            if content.get("role") == "tool":
                tool_response = content
                break
        
        assert tool_response is not None, "Tool response not found in converted messages"
        
        # CRITICAL: Must be "tool" not "function" to avoid MALFORMED_FUNCTION_CALL
        assert tool_response["role"] == "tool", (
            f"Function responses must use role='tool' not '{tool_response['role']}'. "
            "The 'function' role was deprecated in Gemini 1.5!"
        )
        
        # Verify the functionResponse structure
        assert "parts" in tool_response
        assert len(tool_response["parts"]) > 0
        assert "functionResponse" in tool_response["parts"][0]
        func_resp = tool_response["parts"][0]["functionResponse"]
        assert func_resp["name"] == "get_weather"
        assert func_resp["response"]["temperature"] == 18
