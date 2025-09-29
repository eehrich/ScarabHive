"""
Tests for the message validation module.

This test suite verifies that the message validator can detect and repair
common issues that cause LLM API failures, particularly around tool call integrity.
"""

import pytest

from agent_system.core.message_validator import (
    MessageValidator,
    validate_messages_before_llm
)
from agent_system.llm.models import ChatMessage


class TestMessageValidator:
    """Test cases for MessageValidator class."""
    
    def test_valid_messages_pass_validation(self):
        """Test that valid message sequences pass validation without issues."""
        validator = MessageValidator()
        
        # Simple valid conversation
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant"),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
        
        result = validator.validate_and_repair(messages, "test")
        
        assert result.is_valid
        assert len(result.issues) == 0
        assert len(result.repaired_messages) == len(messages)
        assert result.repair_summary == "No issues found"
    
    def test_orphaned_tool_call_detection(self):
        """Test detection of assistant tool calls without corresponding tool responses."""
        validator = MessageValidator()
        
        # Assistant makes tool call but no tool response follows
        messages = [
            ChatMessage(role="user", content="Search for something"),
            ChatMessage(
                role="assistant", 
                content="I'll search for that",
                tool_calls=[{"id": "call_123", "function": {"name": "search", "arguments": "{}"}}]
            ),
            # Missing tool response here
            ChatMessage(role="assistant", content="Done")
        ]
        
        result = validator.validate_and_repair(messages, "test")
        
        assert not result.is_valid
        assert len(result.issues) >= 1  # May detect multiple issues (orphaned + consecutive)
        
        # Should have orphaned tool call issue
        orphaned_issues = [i for i in result.issues if i.type == "orphaned_tool_call"]
        assert len(orphaned_issues) == 1
        assert orphaned_issues[0].severity == "error"
        assert "call_123" in str(orphaned_issues[0].details)
        
        # Should remove the problematic assistant message
        assert len(result.repaired_messages) == 2  # Original 3 minus 1 removed
    
    def test_orphaned_tool_response_detection(self):
        """Test detection of tool responses without matching assistant calls."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="tool",
                content="Search result: found something",
                tool_call_id="call_999"  # No matching assistant call
            ),
            ChatMessage(role="assistant", content="Done")
        ]
        
        result = validator.validate_and_repair(messages, "test")
        
        # Should have warnings but still be valid (warnings don't fail validation)
        assert result.is_valid  # Warnings don't make it invalid
        assert len(result.issues) == 1
        assert result.issues[0].type == "orphaned_tool_response"
        assert result.issues[0].severity == "warning"
        
        # Should remove the orphaned tool response
        assert len(result.repaired_messages) == 2
    
    def test_missing_tool_call_id_detection(self):
        """Test detection of tool messages without tool_call_id."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="tool",
                content="Some tool result"
                # Missing tool_call_id
            )
        ]
        
        result = validator.validate_and_repair(messages, "test")
        
        assert not result.is_valid
        assert len(result.issues) == 1
        assert result.issues[0].type == "missing_tool_call_id"
        assert result.issues[0].severity == "error"
        
        # Should remove the malformed tool message
        assert len(result.repaired_messages) == 1
    
    def test_empty_content_detection(self):
        """Test detection of various empty content patterns."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(role="assistant", content=None),  # Completely empty
            ChatMessage(role="assistant", content=""),    # Empty string
            ChatMessage(role="assistant", content="   "), # Whitespace only
            # Note: ChatMessage expects content as string, not list in this implementation
        ]
        
        result = validator.validate_and_repair(messages, "test")
        
        # Should detect empty content issues
        empty_issues = [i for i in result.issues if "empty" in i.type]
        assert len(empty_issues) >= 1  # At least some empty content detected
    
    def test_valid_tool_call_sequence(self):
        """Test that valid tool call sequences pass validation."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(role="user", content="Search for Python tutorials"),
            ChatMessage(
                role="assistant",
                content="I'll search for Python tutorials",
                tool_calls=[{"id": "call_abc123", "function": {"name": "search", "arguments": "{}"}}]
            ),
            ChatMessage(
                role="tool",
                content="Found 10 Python tutorials",
                tool_call_id="call_abc123"
            ),
            ChatMessage(role="assistant", content="Here are the tutorials I found...")
        ]
        
        result = validator.validate_and_repair(messages, "test")
        
        assert result.is_valid
        assert len(result.issues) == 0
        assert len(result.repaired_messages) == len(messages)
    
    def test_consecutive_assistant_messages(self):
        """Test detection of consecutive assistant messages."""
        validator = MessageValidator()
        
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!"),
            ChatMessage(role="assistant", content="How can I help?"),  # Consecutive
        ]
        
        result = validator.validate_and_repair(messages, "test")
        
        consecutive_issues = [i for i in result.issues if i.type == "consecutive_assistant_messages"]
        assert len(consecutive_issues) == 1
        assert consecutive_issues[0].severity == "warning"
    
    def test_complex_repair_scenario(self):
        """Test repair of complex message sequence with multiple issues."""
        validator = MessageValidator()
        
        # Create a problematic sequence
        messages = [
            ChatMessage(role="user", content="Do multiple searches"),
            ChatMessage(
                role="assistant",
                content="I'll do search 1",
                tool_calls=[{"id": "call_orphaned", "function": {"name": "search", "arguments": "{}"}}]
            ),
            # Missing tool response for call_orphaned
            ChatMessage(
                role="assistant", 
                content="Now search 2",
                tool_calls=[{"id": "call_valid", "function": {"name": "search", "arguments": "{}"}}]
            ),
            ChatMessage(
                role="tool",
                content="Search 2 results",
                tool_call_id="call_valid"  # This one is valid
            ),
            ChatMessage(
                role="tool",
                content="Orphaned result",
                tool_call_id="call_mystery"  # No matching assistant call
            ),
            ChatMessage(role="assistant", content="Done with searches")
        ]
        
        result = validator.validate_and_repair(messages, "test")
        
        # Should find multiple issues
        assert len(result.issues) >= 2
        
        # Should have orphaned tool call and orphaned tool response
        issue_types = [issue.type for issue in result.issues]
        assert "orphaned_tool_call" in issue_types
        assert "orphaned_tool_response" in issue_types
        
        # Repaired messages should be shorter (removed problematic ones)
        assert len(result.repaired_messages) < len(messages)
        
        # But should keep the valid tool sequence
        repaired_tool_messages = [m for m in result.repaired_messages if m.role == "tool"]
        assert len(repaired_tool_messages) == 1  # Only the valid one
        assert repaired_tool_messages[0].tool_call_id == "call_valid"


class TestValidationConvenienceFunction:
    """Test the convenience function validate_messages_before_llm."""
    
    def test_convenience_function_basic_usage(self):
        """Test basic usage of the convenience function."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi!")
        ]
        
        result = validate_messages_before_llm(messages, "test_context")
        
        assert len(result) == len(messages)
        assert all(isinstance(msg, ChatMessage) for msg in result)
    
    def test_convenience_function_with_repairs(self):
        """Test convenience function handles repairs correctly."""
        # Create messages with orphaned tool response
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="tool", 
                content="Orphaned result",
                tool_call_id="call_nonexistent"
            ),
            ChatMessage(role="assistant", content="Done")
        ]
        
        result = validate_messages_before_llm(messages, "test_repair")
        
        # Should remove the orphaned tool response
        assert len(result) == 2
        tool_messages = [m for m in result if m.role == "tool"]
        assert len(tool_messages) == 0


@pytest.mark.asyncio
async def test_integration_with_actual_workflow():
    """Integration test simulating real usage in agent workflow."""
    # This test simulates the kind of message sequence that might cause
    # the "consecutive empty LLM responses" issue
    
    validator = MessageValidator()
    
    # Simulate a sequence where summarizer or optimizer corrupted tool_call structure
    problematic_sequence = [
        ChatMessage(role="system", content="You are a helpful assistant"),
        ChatMessage(role="user", content="Search for information about Python"),
        ChatMessage(
            role="assistant",
            content="I'll search for Python information",
            tool_calls=[{"id": "call_missing_response", "function": {"name": "search", "arguments": "{}"}}]
        ),
        # Tool response "lost" during summarization/optimization
        ChatMessage(role="user", content="What did you find?"),
        # This would cause LLM to potentially return empty response due to confusion
    ]
    
    result = validator.validate_and_repair(problematic_sequence, "integration_test")
    
    # Should detect and fix the orphaned tool call
    assert not result.is_valid
    assert len([i for i in result.issues if i.type == "orphaned_tool_call"]) == 1
    
    # Repaired sequence should be safe for LLM
    repaired = result.repaired_messages
    assert len(repaired) < len(problematic_sequence)
    
    # Should not have any assistant messages with orphaned tool calls
    for msg in repaired:
        if msg.role == "assistant" and msg.tool_calls:
            # The problematic assistant message should have been removed
            assert False, "Should not have assistant messages with tool calls in repaired sequence"