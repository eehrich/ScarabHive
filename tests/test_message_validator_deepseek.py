"""Test DeepSeek marker parsing in message_validator."""

from agent_system.llm.models import ChatMessage
from plugins.message_validator.hooks import InternalMessageValidator


def test_parse_deepseek_markers_in_validator():
    """Test that message_validator detects and parses DeepSeek markers."""
    validator = InternalMessageValidator()
    
    # Create message with DeepSeek markers (like in the screenshot)
    messages = [
        ChatMessage(
            role="assistant",
            content="Teste das Löschen eines Tasks:<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>todo<｜tool▁sep｜>{\"operation\": \"delete\", \"task_id\": \"task_003\"}<｜tool▁call▁end｜><｜tool▁calls▁end｜>"
        )
    ]
    
    result = validator.validate_and_repair(messages, context="test")
    
    # Should find issue
    assert len(result.issues) == 1
    assert result.issues[0].type == "deepseek_tool_markers"
    
    # Should repair by parsing markers
    assert len(result.repaired_messages) == 1
    repaired = result.repaired_messages[0]
    
    # Should have tool_calls now
    assert repaired.tool_calls is not None
    assert len(repaired.tool_calls) == 1
    assert repaired.tool_calls[0]["function"]["name"] == "todo"
    assert '"operation": "delete"' in repaired.tool_calls[0]["function"]["arguments"]
    
    # Content should be cleaned
    assert repaired.content == "Teste das Löschen eines Tasks:"
    
    print(f"✅ DeepSeek markers detected and parsed successfully")
    print(f"   Tool call: {repaired.tool_calls[0]['function']['name']}")
    print(f"   Clean content: {repaired.content}")


def test_ascii_deepseek_markers():
    """Test ASCII variant of DeepSeek markers."""
    validator = InternalMessageValidator()
    
    messages = [
        ChatMessage(
            role="assistant",
            content="Test:<|tool_calls_begin|><|tool_call_begin|>ping<|tool_sep|>{\"include_details\": true}<|tool_call_end|><|tool_calls_end|>"
        )
    ]
    
    result = validator.validate_and_repair(messages, context="test")
    
    assert len(result.repaired_messages) == 1
    repaired = result.repaired_messages[0]
    assert repaired.tool_calls is not None
    assert len(repaired.tool_calls) == 1
    assert repaired.tool_calls[0]["function"]["name"] == "ping"
    
    print(f"✅ ASCII DeepSeek markers parsed successfully")


def test_no_markers_unchanged():
    """Test that normal messages are not affected."""
    validator = InternalMessageValidator()
    
    messages = [
        ChatMessage(role="user", content="Hello"),
        ChatMessage(role="assistant", content="Hi there!")
    ]
    
    result = validator.validate_and_repair(messages, context="test")
    
    # No issues
    assert len(result.issues) == 0
    # Messages unchanged
    assert result.repaired_messages == messages
    
    print(f"✅ Normal messages pass through unchanged")


if __name__ == "__main__":
    test_parse_deepseek_markers_in_validator()
    test_ascii_deepseek_markers()
    test_no_markers_unchanged()
    print("\n🎉 All DeepSeek marker tests passed!")
