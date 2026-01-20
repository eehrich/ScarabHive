"""Unit tests for message_validator plugin."""

from plugins.message_validator.hooks import (
    InternalMessageValidator,
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
                tool_calls=[{"id": "call_1", "function": {"name": "weather_forecast"}}]
            ),
            ChatMessage(
                role="tool",
                content='{"weather": "Sunny", "temperature": "72°F"}',
                tool_call_id="call_1",
                name="weather_forecast"
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

        CRITICAL FIX: ALL tool_calls without responses are orphaned!
        This happens when previous LLM calls made tool_calls but execution
        was interrupted. The conversation history contains assistant messages
        with tool_calls but no tool responses, which violates OpenAI API rules.

        Repair strategy: Strip tool_calls from assistant message (keep message).
        """
        # Case 1: Assistant with tool_calls is NOT the last message -> orphaned
        messages = [
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "weather_forecast"}}]
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
        # Repaired messages should KEEP same count but strip tool_calls
        assert len(result.repaired_messages) == len(messages)
        # Assistant message should have no tool_calls anymore
        assert result.repaired_messages[1].role == "assistant"
        assert result.repaired_messages[1].tool_calls is None

        # Case 2: Assistant with tool_calls IS the last message -> ALSO orphaned!
        # This is the critical fix - these MUST be detected to prevent OpenAI 400 errors
        messages_last = [
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "weather_forecast"}}]
            )
        ]

        result_last = validator.validate_and_repair(messages_last, "test")

        # Should be INVALID now (orphaned tool_calls detected)
        assert not result_last.is_valid
        orphaned_issues_last = [i for i in result_last.issues if i.type == "orphaned_tool_call"]
        assert len(orphaned_issues_last) == 1
        # Messages count stays same, but tool_calls are stripped
        assert len(result_last.repaired_messages) == len(messages_last)
        assert result_last.repaired_messages[1].tool_calls is None

    def test_orphaned_tool_response(self):
        """Test detection of orphaned tool response (no call)."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="tool",
                content='{"data": "Some data"}',
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
            ChatMessage(role="tool", content='{"data": "Some data"}', name="test_tool"),
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
                    {"id": "call_1", "function": {"name": "weather_forecast"}},
                    {"id": "call_2", "function": {"name": "get_time"}}
                ]
            ),
            ChatMessage(role="tool", content='{"weather": "Sunny"}', tool_call_id="call_1"),
            ChatMessage(role="tool", content='{"time": "3:00 PM"}', tool_call_id="call_2"),
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
                tool_calls=[{"id": "call_1", "function": {"name": "weather_forecast"}}]
            ),
            ChatMessage(role="tool", content="Sunny", tool_call_id="call_1")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should not flag as empty assistant since it has tool_calls
        empty_assistant_issues = [i for i in result.issues if i.type == "empty_assistant_message"]
        assert len(empty_assistant_issues) == 0

    def test_empty_string_assistant_message(self):
        """Test detection of empty string assistant message (not just None)."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content=""),
            ChatMessage(role="user", content="Are you there?")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Empty string should be detected as empty
        empty_issues = [i for i in result.issues if i.type == "empty_assistant_message"]
        assert len(empty_issues) == 1

    def test_whitespace_only_assistant_message(self):
        """Test detection of whitespace-only assistant message."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="   \n\t  "),
            ChatMessage(role="user", content="Are you there?")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Whitespace-only should be detected as empty
        empty_issues = [i for i in result.issues if i.type == "empty_assistant_message"]
        assert len(empty_issues) == 1

    def test_empty_assistant_message_repaired(self):
        """Test that empty assistant messages are removed during repair."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="user", content="Are you there?"),
            ChatMessage(role="assistant", content="Yes, I'm here!")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should have detected empty assistant
        assert len(result.issues) == 1
        assert result.issues[0].type == "empty_assistant_message"

        # Repaired messages should have empty assistant removed
        assert len(result.repaired_messages) == 3
        roles = [m.role for m in result.repaired_messages]
        assert roles == ["user", "user", "assistant"]
        assert result.repaired_messages[2].content == "Yes, I'm here!"

    def test_multiple_empty_assistant_messages_repaired(self):
        """Test that multiple empty assistant messages are all removed."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="user", content="Still there?"),
            ChatMessage(role="assistant", content=""),
            ChatMessage(role="user", content="Hello?"),
            ChatMessage(role="assistant", content="   "),
            ChatMessage(role="user", content="Anyone?"),
            ChatMessage(role="assistant", content="Finally responding!")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect all three empty assistants
        empty_issues = [i for i in result.issues if i.type == "empty_assistant_message"]
        assert len(empty_issues) == 3

        # Repaired should have all empty assistants removed
        assert len(result.repaired_messages) == 5
        
        # Check structure: 4 users + 1 assistant with actual content
        repaired_roles = [m.role for m in result.repaired_messages]
        assert repaired_roles.count("user") == 4
        assert repaired_roles.count("assistant") == 1
        assert result.repaired_messages[-1].content == "Finally responding!"

    def test_empty_assistant_at_end_removed(self):
        """Test that empty assistant at end of conversation is removed.
        
        This is a common scenario where LLM returns empty response and it
        gets saved to session, then user sends "Continue" message.
        """
        messages = [
            ChatMessage(role="user", content="Do something complex"),
            ChatMessage(role="assistant", content="Working on it..."),
            ChatMessage(role="user", content="Continue with your task."),
            ChatMessage(role="assistant", content=""),  # Empty response saved
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect the empty assistant
        empty_issues = [i for i in result.issues if i.type == "empty_assistant_message"]
        assert len(empty_issues) == 1

        # Empty assistant at end should be removed
        assert len(result.repaired_messages) == 3
        assert result.repaired_messages[-1].role == "user"
        assert result.repaired_messages[-1].content == "Continue with your task."

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

    def test_consecutive_assistant_messages_merged(self):
        """Test that consecutive assistant messages are merged into one."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!"),
            ChatMessage(role="assistant", content="How can I help?")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should have detected the issue
        assert len(result.issues) == 1
        assert result.issues[0].type == "consecutive_assistant_messages"

        # Should have merged the messages - now only 2 messages
        assert len(result.repaired_messages) == 2
        assert result.repaired_messages[0].role == "user"
        assert result.repaired_messages[1].role == "assistant"
        # Content should be merged
        assert "Hi there!" in result.repaired_messages[1].content
        assert "How can I help?" in result.repaired_messages[1].content

    def test_consecutive_assistant_messages_with_tool_calls_merged(self):
        """Test that consecutive assistant messages with tool_calls are merged.
        
        Note: Tool calls are properly matched with their responses in this test
        to avoid orphaned_tool_call issues interfering with the merge test.
        """
        messages = [
            ChatMessage(role="user", content="Do tasks"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "task1", "arguments": "{}"}}]
            ),
            ChatMessage(role="tool", content="Result 1", tool_call_id="call_1", name="task1"),
            ChatMessage(
                role="assistant",
                content="First part",
            ),
            ChatMessage(
                role="assistant",
                content="Second part",
            )
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should have merged the consecutive assistant messages (indices 3 and 4)
        # Result should be: user, assistant+tool_call, tool, merged_assistant
        assert len(result.repaired_messages) == 4
        
        # Last message should be merged assistant
        merged_msg = result.repaired_messages[3]
        assert merged_msg.role == "assistant"
        assert "First part" in merged_msg.content
        assert "Second part" in merged_msg.content

    def test_consecutive_assistant_messages_preserves_all_fields(self):
        """Test that merging consecutive assistant messages preserves all fields.
        
        This is important for Gemini which uses reasoning_content for "thinking"
        and other providers that use multimodal_content.
        """
        messages = [
            ChatMessage(role="user", content="Think about this"),
            ChatMessage(
                role="assistant",
                content="First thought",
                reasoning_content="Internal reasoning part 1",
                content_format="markdown"
            ),
            ChatMessage(
                role="assistant",
                content="Second thought",
                reasoning_content="Internal reasoning part 2",
            )
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should have detected and repaired the issue
        assert len(result.issues) == 1
        assert result.issues[0].type == "consecutive_assistant_messages"

        # Should have merged into 2 messages
        assert len(result.repaired_messages) == 2
        
        merged_msg = result.repaired_messages[1]
        assert merged_msg.role == "assistant"
        
        # Content should be merged
        assert "First thought" in merged_msg.content
        assert "Second thought" in merged_msg.content
        
        # reasoning_content should be merged
        assert merged_msg.reasoning_content is not None
        assert "Internal reasoning part 1" in merged_msg.reasoning_content
        assert "Internal reasoning part 2" in merged_msg.reasoning_content
        
        # content_format should be preserved from first message
        assert merged_msg.content_format == "markdown"

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
        """Test that repair strips tool_calls from messages with orphaned calls.

        NEW BEHAVIOR: Keep the assistant message, but remove tool_calls.
        This is cleaner than removing the entire message or adding fake responses.
        """
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

        # Repaired messages should KEEP all messages but strip tool_calls
        assert len(result.repaired_messages) == 3
        assert result.repaired_messages[0].role == "user"
        assert result.repaired_messages[1].role == "assistant"
        assert result.repaired_messages[1].tool_calls is None  # tool_calls removed!
        assert result.repaired_messages[2].role == "user"

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
            ChatMessage(role="tool", content='{"result": "Result 1"}', tool_call_id="call_1"),
            ChatMessage(role="tool", content='{"result": "Result 2"}', tool_call_id="call_2"),
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


class TestHookIntegration:
    """Test Hook integration to verify modified flag and context propagation."""

    def test_hook_sets_modified_true_when_repairs_made(self):
        """Test that Hook returns modified=True when validator makes repairs."""
        from plugins.message_validator.hooks import MessageValidatorPlugin
        from agent_system.hooks import HookContext, HookType
        from pathlib import Path
        import asyncio

        plugin_dir = Path(__file__).parent.parent.parent / "src" / "plugins" / "message_validator"
        plugin = MessageValidatorPlugin(plugin_dir)

        # Create context with orphaned tool_calls
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "function": {"name": "test_tool"}}]
            },
            {"role": "user", "content": "continue"}
        ]
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="test_req",
            session_id="test_session",
            messages=messages
        )

        # Execute hook
        result = asyncio.run(plugin.validate_messages(context))

        # Verify modified flag is set
        assert result.success
        assert result.modified, "Hook should return modified=True when repairs are made"
        assert result.metadata["issues_count"] > 0
        assert "orphaned" in result.metadata["validation_result"].lower()

    def test_hook_sets_modified_false_when_no_issues(self):
        """Test that Hook returns modified=False when no repairs needed."""
        from plugins.message_validator.hooks import MessageValidatorPlugin
        from agent_system.hooks import HookContext, HookType
        from pathlib import Path
        import asyncio

        plugin_dir = Path(__file__).parent.parent.parent / "src" / "plugins" / "message_validator"
        plugin = MessageValidatorPlugin(plugin_dir)

        # Create context with valid messages
        messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "response"}
        ]
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="test_req",
            session_id="test_session",
            messages=messages
        )

        # Execute hook
        result = asyncio.run(plugin.validate_messages(context))

        # Verify modified flag is NOT set
        assert result.success
        assert not result.modified, "Hook should return modified=False when no repairs needed"
        assert result.metadata["issues_count"] == 0

    def test_hook_propagates_repaired_messages_in_context(self):
        """Test that Hook propagates repaired messages back in context."""
        from plugins.message_validator.hooks import MessageValidatorPlugin
        from agent_system.hooks import HookContext, HookType
        from pathlib import Path
        import asyncio

        plugin_dir = Path(__file__).parent.parent.parent / "src" / "plugins" / "message_validator"
        plugin = MessageValidatorPlugin(plugin_dir)

        # Create context with orphaned tool_calls
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "function": {"name": "test_tool"}}]
            }
        ]
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="test_req",
            session_id="test_session",
            messages=messages
        )

        # Execute hook
        result = asyncio.run(plugin.validate_messages(context))

        # Verify repaired messages are in context
        assert result.success
        assert result.modified
        assert len(result.context.messages) == 2
        # Verify tool_calls were stripped
        repaired_assistant = result.context.messages[1]
        assert repaired_assistant.tool_calls is None
        assert repaired_assistant.content == "Tool execution was interrupted"


class TestOrphanedToolCallsAdvanced:
    """Advanced tests for orphaned tool_calls detection and repair."""

    def test_orphaned_toolcall_at_end_of_history(self):
        """Test critical case: orphaned tool_call as last message.

        This is the case that causes OpenAI 400 errors:
        - Previous LLM call made tool_calls
        - Request was cancelled/interrupted
        - tool_calls persisted in history without responses
        - Next LLM call fails with "insufficient tool messages"
        """
        messages = [
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "get_weather"}}]
            )
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect orphaned tool_call even at end of history
        assert not result.is_valid
        orphaned_issues = [i for i in result.issues if i.type == "orphaned_tool_call"]
        assert len(orphaned_issues) == 1

        # Should strip tool_calls from assistant message
        assert len(result.repaired_messages) == 2
        assert result.repaired_messages[1].role == "assistant"
        assert result.repaired_messages[1].tool_calls is None
        assert result.repaired_messages[1].content == "Tool execution was interrupted"

    def test_multiple_orphaned_toolcalls_same_message(self):
        """Test multiple orphaned tool_calls in one assistant message."""
        messages = [
            ChatMessage(role="user", content="Tell me about weather and news"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {"id": "call_1", "function": {"name": "get_weather"}},
                    {"id": "call_2", "function": {"name": "get_news"}},
                    {"id": "call_3", "function": {"name": "search_web"}}
                ]
            ),
            ChatMessage(role="user", content="Still waiting...")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect all 3 orphaned tool_calls
        assert not result.is_valid
        orphaned_issues = [i for i in result.issues if i.type == "orphaned_tool_call"]
        assert len(orphaned_issues) == 3

        # Should strip all tool_calls
        assert result.repaired_messages[1].tool_calls is None

    def test_partial_tool_responses(self):
        """Test case where some tool_calls have responses, others don't."""
        messages = [
            ChatMessage(role="user", content="Get weather and news"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {"id": "call_1", "function": {"name": "get_weather"}},
                    {"id": "call_2", "function": {"name": "get_news"}}
                ]
            ),
            ChatMessage(role="tool", content="Sunny", tool_call_id="call_1"),
            ChatMessage(role="user", content="What about the news?")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect orphaned call_2
        assert not result.is_valid
        orphaned_issues = [i for i in result.issues if i.type == "orphaned_tool_call"]
        assert len(orphaned_issues) == 1
        assert orphaned_issues[0].details["tool_call_id"] == "call_2"

    def test_orphaned_tool_calls_with_content(self):
        """Test orphaned tool_calls with existing content in assistant message."""
        messages = [
            ChatMessage(role="user", content="Search for Python docs"),
            ChatMessage(
                role="assistant",
                content="Let me search for that...",
                tool_calls=[{"id": "call_1", "function": {"name": "web_search"}}]
            ),
            ChatMessage(role="user", content="Never mind")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect orphaned tool_call
        assert not result.is_valid

        # Should preserve original content when stripping tool_calls
        assert result.repaired_messages[1].content == "Let me search for that..."
        assert result.repaired_messages[1].tool_calls is None

    def test_consecutive_orphaned_tool_calls(self):
        """Test multiple orphaned tool_calls in consecutive messages."""
        messages = [
            ChatMessage(role="user", content="First request"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "tool1"}}]
            ),
            ChatMessage(role="user", content="Second request"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_2", "function": {"name": "tool2"}}]
            ),
            ChatMessage(role="user", content="Third request")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect both orphaned tool_calls
        orphaned_issues = [i for i in result.issues if i.type == "orphaned_tool_call"]
        assert len(orphaned_issues) == 2

        # Should strip tool_calls from both assistant messages
        assert result.repaired_messages[1].tool_calls is None
        assert result.repaired_messages[3].tool_calls is None

    def test_orphaned_then_valid_sequence(self):
        """Test orphaned tool_call followed by valid tool_call sequence."""
        messages = [
            ChatMessage(role="user", content="First try"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_orphaned", "function": {"name": "tool1"}}]
            ),
            ChatMessage(role="user", content="Try again"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_valid", "function": {"name": "tool2"}}]
            ),
            ChatMessage(role="tool", content="Success", tool_call_id="call_valid")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect only the orphaned one
        orphaned_issues = [i for i in result.issues if i.type == "orphaned_tool_call"]
        assert len(orphaned_issues) == 1
        assert orphaned_issues[0].details["tool_call_id"] == "call_orphaned"

        # Should only strip first assistant's tool_calls
        assert result.repaired_messages[1].tool_calls is None
        assert result.repaired_messages[3].tool_calls is not None

    # Note: ChatMessage.tool_calls must be list of dicts, not custom objects
    # Test removed as it tests unsupported tool_calls format


class TestToolResponseJsonValidation:
    """Test tool response JSON validation."""

    def test_valid_json_object_response(self):
        """Test validation passes for valid JSON object in tool response."""
        messages = [
            ChatMessage(role="user", content="Get data"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "get_data"}}]
            ),
            ChatMessage(
                role="tool",
                content='{"status": "success", "data": [1, 2, 3]}',
                tool_call_id="call_1",
                name="get_data"
            ),
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        assert result.is_valid
        json_issues = [i for i in result.issues if i.type == "invalid_tool_response_json"]
        assert len(json_issues) == 0

    def test_invalid_json_in_tool_response(self):
        """Test detection of invalid JSON in tool response."""
        messages = [
            ChatMessage(role="user", content="Get data"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "get_data"}}]
            ),
            ChatMessage(
                role="tool",
                content='{"invalid json',
                tool_call_id="call_1",
                name="get_data"
            ),
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should have warning but still be valid (warnings don't fail validation)
        json_issues = [i for i in result.issues if i.type == "invalid_tool_response_json"]
        assert len(json_issues) == 1
        assert json_issues[0].severity == "warning"
        assert "malformed JSON" in json_issues[0].description

    def test_non_object_json_in_tool_response(self):
        """Test that non-object JSON (array, string, etc) in tool response is OK.
        
        Tool responses can legitimately be JSON arrays or strings, not just objects.
        This is valid for tools like writer_content that return paginated JSON as strings.
        """
        messages = [
            ChatMessage(role="user", content="Get data"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "get_data"}}]
            ),
            ChatMessage(
                role="tool",
                content='[1, 2, 3]',  # Valid JSON array - should be OK
                tool_call_id="call_1",
                name="get_data"
            ),
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Arrays are now valid - no issues expected
        json_issues = [i for i in result.issues if i.type == "invalid_tool_response_json"]
        assert len(json_issues) == 0

    def test_primitive_json_in_tool_response(self):
        """Test that primitive JSON values in tool response are OK.
        
        Tool responses can be JSON strings (e.g., paginated JSON content).
        """
        messages = [
            ChatMessage(role="user", content="Get data"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "get_data"}}]
            ),
            ChatMessage(
                role="tool",
                content='"just a string"',  # Valid JSON string - should be OK
                tool_call_id="call_1",
                name="get_data"
            ),
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # String JSON values are now valid - no issues expected
        json_issues = [i for i in result.issues if i.type == "invalid_tool_response_json"]
        assert len(json_issues) == 0
