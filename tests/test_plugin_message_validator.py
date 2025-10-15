"""Unit tests for message_validator plugin."""

import pytest
from typing import List
from plugins.message_validator.hooks import (
    InternalMessageValidator,
    ValidationIssue,
    ValidationResult,
)
from agent_system.llm.models import ChatMessage


class TestMessageValidatorInitialization:
    """Test MessageValidator initialization."""

    def test_default_initialization(self):
        """Test validator with default log level."""
        validator = InternalMessageValidator()
        assert validator.log_level == "warning"

    def test_custom_log_level(self):
        """Test validator with custom log level."""
        validator = InternalMessageValidator(log_level="DEBUG")
        assert validator.log_level == "debug"


class TestToolCallConsistency:
    """Test tool call consistency validation."""

    def test_valid_tool_call_sequence(self):
        """Test validation passes for valid tool call sequence."""
        messages = [
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "get_weather"}}]
            ),
            ChatMessage(
                role="tool",
                content="Sunny, 72°F",
                tool_call_id="call_1",
                name="get_weather"
            ),
            ChatMessage(role="assistant", content="It's sunny and 72°F!")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert result.is_valid
        assert len(result.issues) == 0
        assert len(result.repaired_messages) == 4

    def test_orphaned_tool_call(self):
        """Test detection of orphaned tool call (no response).
        
        Note: If the assistant message with tool_calls is the LAST message,
        it's NOT considered orphaned because tool responses are expected
        to be added AFTER validation (e.g., in pre_llm_call hook scenario).
        """
        # Case 1: Assistant with tool_calls is NOT the last message -> orphaned
        messages = [
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "get_weather"}}]
            ),
            ChatMessage(role="user", content="Any update?")  # Another message after
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert not result.is_valid
        # Should detect orphaned tool call
        orphaned_issues = [i for i in result.issues if i.type == "orphaned_tool_call"]
        assert len(orphaned_issues) == 1
        assert orphaned_issues[0].severity == "error"
        # Repaired messages should remove the assistant with orphaned tool call
        assert len(result.repaired_messages) < len(messages)
        
        # Case 2: Assistant with tool_calls IS the last message -> NOT orphaned
        # (tool responses will be added after validation)
        messages_last = [
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "get_weather"}}]
            )
        ]
        
        result_last = validator.validate_and_repair(messages_last, "test")
        
        # Should be valid (tool responses expected to follow)
        assert result_last.is_valid
        orphaned_issues_last = [i for i in result_last.issues if i.type == "orphaned_tool_call"]
        assert len(orphaned_issues_last) == 0
        # Messages should remain unchanged
        assert len(result_last.repaired_messages) == len(messages_last)

    def test_orphaned_tool_response(self):
        """Test detection of orphaned tool response (no call)."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="tool",
                content="Some data",
                tool_call_id="call_999",
                name="unknown_tool"
            ),
            ChatMessage(role="assistant", content="Got it!")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert len(result.issues) == 1
        assert result.issues[0].type == "orphaned_tool_response"
        assert result.issues[0].severity == "warning"
        # Repaired messages should remove orphaned tool response
        assert len(result.repaired_messages) == 2

    def test_missing_tool_call_id(self):
        """Test detection of tool message without tool_call_id."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="tool", content="Some data", name="test_tool"),
            ChatMessage(role="assistant", content="Got it!")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert not result.is_valid
        assert len(result.issues) == 1
        assert result.issues[0].type == "missing_tool_call_id"
        assert result.issues[0].severity == "error"

    def test_multiple_tool_calls_with_responses(self):
        """Test multiple tool calls all with responses."""
        messages = [
            ChatMessage(role="user", content="Check weather and time"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {"id": "call_1", "function": {"name": "get_weather"}},
                    {"id": "call_2", "function": {"name": "get_time"}}
                ]
            ),
            ChatMessage(role="tool", content="Sunny", tool_call_id="call_1"),
            ChatMessage(role="tool", content="3:00 PM", tool_call_id="call_2"),
            ChatMessage(role="assistant", content="Weather is sunny, time is 3 PM")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert result.is_valid
        assert len(result.issues) == 0


class TestContentStructure:
    """Test content structure validation."""

    def test_empty_assistant_message(self):
        """Test detection of empty assistant message."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="user", content="Are you there?")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert len(result.issues) == 1
        assert result.issues[0].type == "empty_assistant_message"
        assert result.issues[0].severity == "warning"

    def test_valid_assistant_with_tool_calls_no_content(self):
        """Test that assistant with tool_calls but no content is valid."""
        messages = [
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "get_weather"}}]
            ),
            ChatMessage(role="tool", content="Sunny", tool_call_id="call_1")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        # Should not flag as empty assistant since it has tool_calls
        empty_assistant_issues = [i for i in result.issues if i.type == "empty_assistant_message"]
        assert len(empty_assistant_issues) == 0

    # Note: ChatMessage.content must be Optional[str], not list
    # Content segment tests removed as they test unsupported content types


class TestMessageSequence:
    """Test message sequence validation."""

    def test_consecutive_assistant_messages(self):
        """Test detection of consecutive assistant messages."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!"),
            ChatMessage(role="assistant", content="How can I help?")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert len(result.issues) == 1
        assert result.issues[0].type == "consecutive_assistant_messages"
        assert result.issues[0].severity == "warning"

    def test_valid_alternating_sequence(self):
        """Test valid alternating user/assistant sequence."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi!"),
            ChatMessage(role="user", content="How are you?"),
            ChatMessage(role="assistant", content="I'm good!")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        # Should not have sequence issues
        sequence_issues = [i for i in result.issues if i.type == "consecutive_assistant_messages"]
        assert len(sequence_issues) == 0


class TestRepairFunctionality:
    """Test message repair functionality."""

    def test_repair_removes_orphaned_tool_call(self):
        """Test that repair removes messages with orphaned tool calls."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "test"}}]
            ),
            ChatMessage(role="user", content="Anything?")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        # Repaired messages should have removed the assistant with orphaned tool call
        assert len(result.repaired_messages) == 2
        assert result.repaired_messages[0].role == "user"
        assert result.repaired_messages[1].role == "user"

    def test_repair_removes_orphaned_tool_response(self):
        """Test that repair removes orphaned tool responses."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="tool", content="Data", tool_call_id="call_999"),
            ChatMessage(role="assistant", content="Done")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        # Should remove the orphaned tool response
        assert len(result.repaired_messages) == 2
        assert all(msg.role != "tool" for msg in result.repaired_messages)

    def test_repair_removes_tool_without_id(self):
        """Test that repair removes tool messages without tool_call_id."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="tool", content="Data", name="test"),
            ChatMessage(role="assistant", content="Done")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        # Should remove the tool message without ID
        assert len(result.repaired_messages) == 2
        assert all(msg.role != "tool" for msg in result.repaired_messages)


class TestRepairSummary:
    """Test repair summary generation."""

    def test_summary_no_issues(self):
        """Test summary when no issues found."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi!")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert result.repair_summary == "No issues found"

    def test_summary_with_issues(self):
        """Test summary generation with issues."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "test"}}]
            ),
            ChatMessage(role="tool", content="Data", tool_call_id="call_999")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        # Should have error and warning
        assert "error" in result.repair_summary or "warning" in result.repair_summary
        assert "orphaned" in result.repair_summary


class TestValidationResult:
    """Test ValidationResult structure."""

    def test_validation_result_structure(self):
        """Test that ValidationResult has all required fields."""
        messages = [ChatMessage(role="user", content="Hello")]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert hasattr(result, 'is_valid')
        assert hasattr(result, 'issues')
        assert hasattr(result, 'repaired_messages')
        assert hasattr(result, 'repair_summary')
        assert isinstance(result.is_valid, bool)
        assert isinstance(result.issues, list)
        assert isinstance(result.repaired_messages, list)
        assert isinstance(result.repair_summary, str)

    def test_validation_issue_structure(self):
        """Test that ValidationIssue has all required fields."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content=None)
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        if result.issues:
            issue = result.issues[0]
            assert hasattr(issue, 'type')
            assert hasattr(issue, 'severity')
            assert hasattr(issue, 'message_index')
            assert hasattr(issue, 'description')
            assert hasattr(issue, 'details')


class TestComplexScenarios:
    """Test complex real-world scenarios."""

    def test_multiple_issues_in_sequence(self):
        """Test handling multiple issues in one sequence."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content=None),  # Empty assistant
            ChatMessage(role="assistant", content="Hi!"),  # Consecutive assistant
            ChatMessage(
                role="tool",
                content="Data",
                tool_call_id="call_999"  # Orphaned tool response
            )
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        # Should detect multiple issues
        assert len(result.issues) >= 2
        issue_types = [i.type for i in result.issues]
        assert "empty_assistant_message" in issue_types
        assert "consecutive_assistant_messages" in issue_types
        assert "orphaned_tool_response" in issue_types

    def test_nested_tool_calls_scenario(self):
        """Test scenario with nested tool calls."""
        messages = [
            ChatMessage(role="user", content="Complex task"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {"id": "call_1", "function": {"name": "tool1"}},
                    {"id": "call_2", "function": {"name": "tool2"}}
                ]
            ),
            ChatMessage(role="tool", content="Result 1", tool_call_id="call_1"),
            ChatMessage(role="tool", content="Result 2", tool_call_id="call_2"),
            ChatMessage(role="assistant", content="Done!")
        ]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert result.is_valid
        assert len(result.issues) == 0


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_message_list(self):
        """Test validation with empty message list."""
        messages = []
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert result.is_valid
        assert len(result.issues) == 0
        assert len(result.repaired_messages) == 0

    def test_single_message(self):
        """Test validation with single message."""
        messages = [ChatMessage(role="user", content="Hello")]
        
        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")
        
        assert result.is_valid
        assert len(result.repaired_messages) == 1

    # Note: ChatMessage.tool_calls must be list of dicts, not custom objects
    # Test removed as it tests unsupported tool_calls format
