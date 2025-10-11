"""
Tests for MessageValidator tool name validation and repair.
"""

import pytest

from agent_system.llm.message_validator import (
    MessageValidator,
    OPENAI_TOOL_NAME_PATTERN
)
from agent_system.llm.models import ChatMessage


class TestToolNameValidation:
    """Test tool name validation against OpenAI requirements."""
    
    def test_openai_pattern_valid_names(self):
        """Test that OpenAI pattern accepts valid tool names."""
        valid_names = [
            "calculator",
            "example__example_calculator",
            "plugin_name__tool_name",
            "ssh__execute_command",
            "tool-with-hyphens",
            "tool_with_underscores",
            "MixedCase123",
            "abc123_def-456"
        ]
        
        for name in valid_names:
            assert OPENAI_TOOL_NAME_PATTERN.match(name), f"Expected '{name}' to be valid"
    
    def test_openai_pattern_invalid_names(self):
        """Test that OpenAI pattern rejects invalid tool names."""
        invalid_names = [
            "plugin/tool",  # slash
            "plugin.tool",  # dot
            "tool name",    # space
            "tool@name",    # special char
            "tool#name",    # special char
            "tool(name)",   # parentheses
            "tool[name]",   # brackets
        ]
        
        for name in invalid_names:
            assert not OPENAI_TOOL_NAME_PATTERN.match(name), f"Expected '{name}' to be invalid"
    
    def test_detect_invalid_tool_name_in_assistant_message(self):
        """Test detection of invalid tool names in assistant messages."""
        validator = MessageValidator()
        
        # Create message with invalid tool name (using slash) - dict format
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "plugin/invalid_tool",  # Invalid: contains slash
                        "arguments": "{}"
                    }
                }]
            )
        ]
        
        result = validator.validate_and_repair(messages, context="test")
        
        # Should detect the invalid tool name
        invalid_name_issues = [i for i in result.issues if i.type == "invalid_tool_name"]
        assert len(invalid_name_issues) == 1
        assert invalid_name_issues[0].details["tool_name"] == "plugin/invalid_tool"
        assert invalid_name_issues[0].severity == "error"
    
    def test_detect_invalid_tool_name_dict_format(self):
        """Test detection of invalid tool names in dict format tool calls."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_456",
                    "function": {
                        "name": "example.calculator",  # Invalid: contains dot
                        "arguments": "{}"
                    }
                }]
            )
        ]
        
        result = validator.validate_and_repair(messages, context="test")
        
        invalid_name_issues = [i for i in result.issues if i.type == "invalid_tool_name"]
        assert len(invalid_name_issues) == 1
        assert invalid_name_issues[0].details["tool_name"] == "example.calculator"
    
    def test_repair_invalid_tool_name_slash_to_underscore(self):
        """Test automatic repair of tool names with slashes."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_789",
                    "type": "function",
                    "function": {
                        "name": "example/example_calculator",  # Should be repaired
                        "arguments": "{}"
                    }
                }]
            ),
            ChatMessage(
                role="tool",
                content="result",
                tool_call_id="call_789"
            )
        ]
        
        result = validator.validate_and_repair(messages, context="test")
        
        # Should repair the tool name
        assert len(result.repaired_messages) >= 1
        repaired_msg = result.repaired_messages[0]
        assert len(repaired_msg.tool_calls) == 1
        repaired_name = repaired_msg.tool_calls[0]["function"]["name"]
        assert repaired_name == "example__example_calculator"
        assert OPENAI_TOOL_NAME_PATTERN.match(repaired_name)
    
    def test_repair_invalid_tool_name_dot_to_underscore(self):
        """Test automatic repair of tool names with dots."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "plugin.tool.name",
                        "arguments": "{}"
                    }
                }]
            ),
            # Add matching tool response to avoid orphaned_tool_call issue
            ChatMessage(
                role="tool",
                content="result",
                tool_call_id="call_abc"
            )
        ]
        
        result = validator.validate_and_repair(messages, context="test")
        
        # Should repair dots to underscores
        assert len(result.repaired_messages) >= 1
        repaired_msg = result.repaired_messages[0]
        repaired_name = repaired_msg.tool_calls[0]["function"]["name"]
        assert repaired_name == "plugin_tool_name"
        assert OPENAI_TOOL_NAME_PATTERN.match(repaired_name)
    
    def test_sanitize_tool_name_edge_cases(self):
        """Test tool name sanitization edge cases."""
        validator = MessageValidator()
        
        test_cases = [
            ("plugin/tool", "plugin__tool"),
            ("plugin.tool", "plugin_tool"),
            ("plugin tool", "plugin_tool"),
            ("plugin@#$%tool", "plugintool"),
            ("_leading_underscore_", "leading_underscore"),
            ("multiple///slashes", "multiple__slashes"),  # consecutive slashes collapse
        ]
        
        for original, expected in test_cases:
            sanitized = validator._sanitize_tool_name(original)
            assert sanitized == expected, f"Expected '{original}' -> '{expected}', got '{sanitized}'"
            if sanitized:  # Only check if not empty
                assert OPENAI_TOOL_NAME_PATTERN.match(sanitized), f"Sanitized name '{sanitized}' is still invalid"
    
    def test_valid_tool_names_pass_validation(self):
        """Test that valid tool names pass validation without issues."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{
                    "id": "call_valid",
                    "type": "function",
                    "function": {
                        "name": "example__example_calculator",  # Valid
                        "arguments": "{}"
                    }
                }]
            ),
            ChatMessage(
                role="tool",
                content="42",
                tool_call_id="call_valid"
            )
        ]
        
        result = validator.validate_and_repair(messages, context="test")
        
        # Should not detect any invalid tool name issues
        invalid_name_issues = [i for i in result.issues if i.type == "invalid_tool_name"]
        assert len(invalid_name_issues) == 0
        assert result.is_valid
    
    def test_multiple_tool_calls_with_mixed_validity(self):
        """Test message with multiple tool calls, some valid and some invalid."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_valid",
                        "type": "function",
                        "function": {
                            "name": "plugin__valid_tool",
                            "arguments": "{}"
                        }
                    },
                    {
                        "id": "call_invalid",
                        "type": "function",
                        "function": {
                            "name": "plugin/invalid_tool",
                            "arguments": "{}"
                        }
                    }
                ]
            ),
            ChatMessage(
                role="tool",
                content="valid result",
                tool_call_id="call_valid"
            ),
            ChatMessage(
                role="tool",
                content="invalid result",
                tool_call_id="call_invalid"
            )
        ]
        
        result = validator.validate_and_repair(messages, context="test")
        
        # Should detect one invalid tool name
        invalid_name_issues = [i for i in result.issues if i.type == "invalid_tool_name"]
        assert len(invalid_name_issues) == 1
        assert invalid_name_issues[0].details["tool_index"] == 1  # Second tool call
        
        # Should repair the invalid one
        repaired_msg = result.repaired_messages[0]
        assert repaired_msg.tool_calls[0]["function"]["name"] == "plugin__valid_tool"  # Still valid
        assert repaired_msg.tool_calls[1]["function"]["name"] == "plugin__invalid_tool"  # Repaired


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
