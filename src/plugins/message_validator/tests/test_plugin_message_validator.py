"""Unit tests for message_validator plugin."""

import re

import pytest

from plugins.message_validator.hooks import (
    InternalMessageValidator,
)
from agent_system.llm.models import ChatMessage


class TestMessageValidatorInitialization:
    """Test MessageValidator initialization."""

    def test_server_config_overrides_schema_defaults(self):
        """The plugins.yaml `config:` block reaches the size checks."""
        from types import SimpleNamespace
        from plugins.message_validator.plugin import PLUGIN_FACTORY

        plugin = PLUGIN_FACTORY("mv", {}, SimpleNamespace(config={"warn_tool_response_size_kb": 1}))
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None,
                        tool_calls=[{"id": "c1", "function": {"name": "t"}}]),
            ChatMessage(role="tool", tool_call_id="c1", content="x" * 2048),
        ]
        issues = plugin.validator.validate_and_repair(messages).issues
        assert [i.type for i in issues] == ["tool_response_large"]


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

    def test_merge_preserves_reasoning_details_and_thinking_blocks(self):
        """Audit 2026-08-25: the merge rebuilt ChatMessage from scratch and
        silently dropped reasoning_details (Gemini thought_signature →
        MALFORMED_FUNCTION_CALL next turn) and thinking_blocks (Anthropic →
        400 on the next tool turn).

        reasoning_details are CONCATENATED (the Responses client collects
        all matching blocks in order); thinking_blocks keep only the NEWER
        turn's blocks — Anthropic validates the last turn's block sequence
        verbatim, a concatenated sequence was never produced by the model."""
        messages = [
            ChatMessage(role="user", content="Think"),
            ChatMessage(
                role="assistant", content="A",
                reasoning_details=[{"type": "reasoning.encrypted",
                                    "data": "sig-1", "index": 0}],
                thinking_blocks=[{"type": "thinking", "thinking": "t1",
                                  "signature": "s1"}],
                thinking_model="claude-opus-5",
                served_by="Google",
            ),
            ChatMessage(
                role="assistant", content="B",
                reasoning_details=[{"type": "reasoning.encrypted",
                                    "data": "sig-2", "index": 1}],
                thinking_blocks=[{"type": "thinking", "thinking": "t2",
                                  "signature": "s2"}],
                thinking_model="claude-opus-5",
                served_by="Google AI Studio",
            ),
        ]

        result = InternalMessageValidator().validate_and_repair(messages, "test")

        assert len(result.repaired_messages) == 2
        merged = result.repaired_messages[1]
        assert merged.role == "assistant"
        assert [rd["data"] for rd in merged.reasoning_details] == ["sig-1", "sig-2"]
        assert [tb["signature"] for tb in merged.thinking_blocks] == ["s2"]
        assert merged.thinking_model == "claude-opus-5"
        # The later turn's backend holds the cache; the provider pin follows it.
        assert merged.served_by == "Google AI Studio"

    def test_merge_keeps_the_earlier_backend_when_the_later_turn_names_none(self):
        messages = [
            ChatMessage(role="user", content="Think"),
            ChatMessage(role="assistant", content="A", served_by="backend-a"),
            ChatMessage(role="assistant", content="B"),
        ]

        result = InternalMessageValidator().validate_and_repair(messages, "test")

        assert len(result.repaired_messages) == 2
        assert result.repaired_messages[1].served_by == "backend-a"

    @pytest.mark.parametrize("first, second, expected", [
        (("small", True), (None, False), "small"),
        ((None, False), ("small", True), "small"),
        # Two producers match no model: the next call strips the merged list.
        (("small", True), ("big", True), "small|big"),
        # A tag whose artifacts a compaction already removed names nothing.
        (("small", False), ("big", True), "big"),
    ])
    def test_merge_names_the_producers_of_the_merged_reasoning(self, first, second, expected):
        def turn(content, tag, has_details):
            return ChatMessage(role="assistant", content=content, reasoning_model=tag,
                               reasoning_details=[{"type": "reasoning.encrypted", "data": content}]
                               if has_details else None)

        messages = [ChatMessage(role="user", content="Think"), turn("A", *first), turn("B", *second)]

        result = InternalMessageValidator().validate_and_repair(messages, "test")

        assert len(result.repaired_messages) == 2
        assert result.repaired_messages[1].reasoning_model == expected

    def test_merge_thinking_blocks_from_different_models_keeps_newer(self):
        """Signatures are model-bound: merging blocks from two different
        models would replay foreign signatures under one thinking_model."""
        messages = [
            ChatMessage(role="user", content="Think"),
            ChatMessage(
                role="assistant", content="A",
                thinking_blocks=[{"type": "thinking", "thinking": "t1",
                                  "signature": "s1"}],
                thinking_model="claude-sonnet-5",
            ),
            ChatMessage(
                role="assistant", content="B",
                thinking_blocks=[{"type": "thinking", "thinking": "t2",
                                  "signature": "s2"}],
                thinking_model="claude-opus-5",
            ),
        ]

        result = InternalMessageValidator().validate_and_repair(messages, "test")

        merged = result.repaired_messages[1]
        assert [tb["signature"] for tb in merged.thinking_blocks] == ["s2"]
        assert merged.thinking_model == "claude-opus-5"

    def test_merge_keeps_first_turn_blocks_when_second_has_none(self):
        """Only the first message carries thinking blocks — they survive."""
        messages = [
            ChatMessage(role="user", content="Think"),
            ChatMessage(
                role="assistant", content="A",
                thinking_blocks=[{"type": "thinking", "thinking": "t1",
                                  "signature": "s1"}],
                thinking_model="claude-opus-5",
            ),
            ChatMessage(role="assistant", content="B"),
        ]

        result = InternalMessageValidator().validate_and_repair(messages, "test")

        merged = result.repaired_messages[1]
        assert [tb["signature"] for tb in merged.thinking_blocks] == ["s1"]
        assert merged.thinking_model == "claude-opus-5"

    def test_merge_marks_orphaned_when_either_side_is(self):
        """A broken reasoning chain on either side stays broken in the merge."""
        messages = [
            ChatMessage(role="user", content="Think"),
            ChatMessage(role="assistant", content="A", rd_orphaned=True,
                        reasoning_details=[{"data": "sig-1", "index": 0}]),
            ChatMessage(role="assistant", content="B"),
        ]

        result = InternalMessageValidator().validate_and_repair(messages, "test")

        assert result.repaired_messages[1].rd_orphaned is True

    def test_strip_orphaned_tool_calls_preserves_reasoning_fields(self):
        """Audit 2026-08-25: stripping orphaned tool_calls rebuilt the
        message with role/content/name only — reasoning_content,
        reasoning_details and thinking_blocks were silently dropped."""
        messages = [
            ChatMessage(role="user", content="Weather?"),
            ChatMessage(
                role="assistant", content=None,
                tool_calls=[{"id": "call_1",
                             "function": {"name": "weather_forecast"}}],
                reasoning_content="thought about it",
                reasoning_details=[{"data": "sig-1", "index": 0}],
                thinking_blocks=[{"type": "thinking", "thinking": "t1",
                                  "signature": "s1"}],
                thinking_model="claude-opus-5",
            ),
            ChatMessage(role="user", content="Any update?"),
        ]

        result = InternalMessageValidator().validate_and_repair(messages, "test")

        stripped = result.repaired_messages[1]
        assert stripped.role == "assistant"
        assert stripped.tool_calls is None
        assert stripped.content  # fallback text present
        assert stripped.reasoning_content == "thought about it"
        assert [rd["data"] for rd in stripped.reasoning_details] == ["sig-1"]
        assert [tb["signature"] for tb in stripped.thinking_blocks] == ["s1"]
        assert stripped.thinking_model == "claude-opus-5"
        # The kept reasoning_details signed the removed tool_calls: the chain
        # is mutated, so chain-verified providers (keep_all) must reset.
        assert stripped.rd_orphaned is True

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

    def test_invalid_first_message_assistant(self):
        """Test detection when first non-system message is assistant (not user).
        
        Gemini requires: user -> assistant (with tool_calls) -> tool responses
        If first message after system is assistant, it's invalid.
        """
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="assistant", content="Hello!"),  # Invalid - should be user
            ChatMessage(role="user", content="Hi there")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect invalid first message
        invalid_first_issues = [i for i in result.issues if i.type == "invalid_first_message"]
        assert len(invalid_first_issues) == 1
        assert invalid_first_issues[0].severity == "error"
        assert invalid_first_issues[0].message_index == 1  # Index of assistant message

    def test_invalid_first_message_tool(self):
        """Test detection when first non-system message is tool response."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="tool", content="Some result", tool_call_id="call_1"),  # Invalid
            ChatMessage(role="user", content="Hi")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect invalid first message
        invalid_first_issues = [i for i in result.issues if i.type == "invalid_first_message"]
        assert len(invalid_first_issues) == 1

    def test_invalid_first_message_repaired(self):
        """Test that invalid first message is repaired by removal."""
        messages = [
            ChatMessage(role="system", content="System prompt"),
            ChatMessage(role="assistant", content="Orphaned assistant"),
            ChatMessage(role="user", content="Real user message"),
            ChatMessage(role="assistant", content="Valid response")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Repaired messages should have the invalid assistant removed
        assert len(result.repaired_messages) == 3
        roles = [m.role for m in result.repaired_messages]
        assert roles == ["system", "user", "assistant"]
        assert result.repaired_messages[1].content == "Real user message"

    def test_invalid_first_message_with_tool_calls_repaired(self):
        """Test that invalid first assistant with tool_calls also removes tool responses."""
        messages = [
            ChatMessage(role="system", content="System prompt"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "test_tool"}}]
            ),
            ChatMessage(role="tool", content="Result", tool_call_id="call_1", name="test_tool"),
            ChatMessage(role="user", content="Real user message"),
            ChatMessage(role="assistant", content="Valid response")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Both invalid assistant AND its tool response should be removed
        assert len(result.repaired_messages) == 3
        roles = [m.role for m in result.repaired_messages]
        assert roles == ["system", "user", "assistant"]

    def test_invalid_first_message_cascading_removal(self):
        """Test cascading removal when multiple bad messages at start.
        
        If removing first bad message exposes another bad message,
        that should also be removed.
        """
        messages = [
            ChatMessage(role="system", content="System prompt"),
            ChatMessage(role="assistant", content="First bad"),
            ChatMessage(role="assistant", content="Second bad"),
            ChatMessage(role="tool", content="Orphaned tool", tool_call_id="x"),
            ChatMessage(role="user", content="Finally a user message"),
            ChatMessage(role="assistant", content="Valid response")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # All bad messages at start should be removed until we hit user
        roles = [m.role for m in result.repaired_messages]
        # First non-system should be user
        non_system_roles = [r for r in roles if r != "system"]
        assert non_system_roles[0] == "user"
        assert "Finally a user message" in [m.content for m in result.repaired_messages]

    def test_valid_first_message_user(self):
        """Test that valid sequence starting with user passes."""
        messages = [
            ChatMessage(role="system", content="System prompt"),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi!")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should not have invalid first message issue
        invalid_first_issues = [i for i in result.issues if i.type == "invalid_first_message"]
        assert len(invalid_first_issues) == 0

    def test_no_system_message_user_first(self):
        """Test validation when no system message and user is first."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi!")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should be valid
        invalid_first_issues = [i for i in result.issues if i.type == "invalid_first_message"]
        assert len(invalid_first_issues) == 0

    def test_no_system_message_assistant_first(self):
        """Test validation when no system message and assistant is first (invalid)."""
        messages = [
            ChatMessage(role="assistant", content="I'm starting first"),
            ChatMessage(role="user", content="Hello")
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect invalid first message
        invalid_first_issues = [i for i in result.issues if i.type == "invalid_first_message"]
        assert len(invalid_first_issues) == 1

        # Should repair by removing the assistant
        assert len(result.repaired_messages) == 1
        assert result.repaired_messages[0].role == "user"


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

        plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "message_validator"
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

        plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "message_validator"
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

        plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "message_validator"
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


class TestInterleavedMessageInToolBlock:
    """Test detection and repair of messages interleaved between assistant(tool_calls) and tool responses.
    
    This addresses the bug where a user message (e.g. loop detection warning) gets
    inserted between an assistant message with tool_calls and the corresponding
    tool response, breaking the OpenAI/DeepSeek message protocol.
    """

    def test_detect_user_message_interleaved(self):
        """User message between assistant(tool_calls) and tool response is detected."""
        messages = [
            ChatMessage(role="user", content="Do something"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "my_tool"}}]
            ),
            ChatMessage(role="user", content="NOTICE: You've called this 3 times"),
            ChatMessage(
                role="tool",
                content='{"result": "ok"}',
                tool_call_id="call_1",
                name="my_tool"
            ),
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        interleaved = [i for i in result.issues if i.type == "interleaved_message_in_tool_block"]
        assert len(interleaved) == 1
        assert interleaved[0].message_index == 2
        assert interleaved[0].severity == "error"
        assert interleaved[0].details["interleaved_role"] == "user"

    def test_repair_relocates_interleaved_message(self):
        """Interleaved user message is moved after the tool response."""
        messages = [
            ChatMessage(role="user", content="Do something"),
            ChatMessage(
                role="assistant",
                content="calling tool",
                tool_calls=[{"id": "call_1", "function": {"name": "my_tool"}}]
            ),
            ChatMessage(role="user", content="NOTICE: loop warning"),
            ChatMessage(
                role="tool",
                content='{"result": "ok"}',
                tool_call_id="call_1",
                name="my_tool"
            ),
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        repaired = result.repaired_messages
        # Expected order: user, assistant(tool_calls), tool, user(relocated)
        assert len(repaired) == 4
        assert repaired[0].role == "user"
        assert repaired[1].role == "assistant"
        assert repaired[1].tool_calls is not None
        assert repaired[2].role == "tool"
        assert repaired[2].tool_call_id == "call_1"
        assert repaired[3].role == "user"
        assert "loop warning" in repaired[3].content

    def test_repair_with_multiple_tool_calls(self):
        """Interleaved message is relocated after all tool responses in a batch."""
        messages = [
            ChatMessage(role="user", content="Do something"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {"id": "call_1", "function": {"name": "tool_a"}},
                    {"id": "call_2", "function": {"name": "tool_b"}},
                ]
            ),
            ChatMessage(role="user", content="Loop warning"),
            ChatMessage(
                role="tool",
                content='{"a": 1}',
                tool_call_id="call_1",
                name="tool_a"
            ),
            ChatMessage(
                role="tool",
                content='{"b": 2}',
                tool_call_id="call_2",
                name="tool_b"
            ),
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        repaired = result.repaired_messages
        # Expected: user, assistant, tool(call_1), tool(call_2), user(relocated)
        assert len(repaired) == 5
        assert repaired[0].role == "user"
        assert repaired[1].role == "assistant"
        assert repaired[2].role == "tool"
        assert repaired[3].role == "tool"
        assert repaired[4].role == "user"
        assert "Loop warning" in repaired[4].content

    def test_no_issue_when_tool_responses_immediate(self):
        """No issue when tool responses immediately follow assistant(tool_calls)."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "my_tool"}}]
            ),
            ChatMessage(
                role="tool",
                content='{"ok": true}',
                tool_call_id="call_1",
                name="my_tool"
            ),
            ChatMessage(role="assistant", content="Done!"),
            ChatMessage(role="user", content="Thanks"),
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        interleaved = [i for i in result.issues if i.type == "interleaved_message_in_tool_block"]
        assert len(interleaved) == 0

    def test_interleaved_at_session_reload(self):
        """Simulate the exact scenario from the bug: session persisted with interleaved message."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Start working"),
            ChatMessage(
                role="assistant",
                content="I'll call the workflow tool",
                tool_calls=[{"id": "call_k70", "function": {"name": "writer_workflow_set_task"}}]
            ),
            # This was injected by loop detection between tool_call and response
            ChatMessage(
                role="user",
                content="NOTICE: You've called 'writer_workflow_set_task' 3 times with the same arguments."
            ),
            ChatMessage(
                role="tool",
                content='{"status": "blocked", "task_name": "content"}',
                tool_call_id="call_k70",
                name="writer_workflow_set_task"
            ),
        ]

        validator = InternalMessageValidator()
        result = validator.validate_and_repair(messages, "test")

        # Should detect and fix
        interleaved = [i for i in result.issues if i.type == "interleaved_message_in_tool_block"]
        assert len(interleaved) == 1

        repaired = result.repaired_messages
        # System, user, assistant(tool_calls), tool, user(relocated)
        assert len(repaired) == 5
        assert repaired[0].role == "system"
        assert repaired[1].role == "user"
        assert repaired[2].role == "assistant"
        assert repaired[2].tool_calls is not None
        assert repaired[3].role == "tool"
        assert repaired[3].tool_call_id == "call_k70"
        assert repaired[4].role == "user"
        assert "NOTICE" in repaired[4].content


STRUCTURAL_ISSUES = {"orphaned_tool_call", "orphaned_tool_response", "missing_tool_call_id",
                     "interleaved_message_in_tool_block", "invalid_first_message"}


def _call(call_id, name="t"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _structural_issues(messages):
    """What a second pass still finds that a provider would refuse."""
    return [i.type for i in InternalMessageValidator().validate_and_repair(messages).issues
            if i.type in STRUCTURAL_ISSUES]


class TestRepairsFindMessagesByIdentity:
    """Repairs used indices of the input after earlier repairs had moved or
    removed messages: they stripped a valid call, kept an orphaned one, or
    dropped merged tool calls while keeping their responses."""

    def test_partially_answered_message_keeps_its_answered_call(self):
        messages = [
            ChatMessage(role="user", content="weather and news"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1"), _call("c2")]),
            ChatMessage(role="tool", tool_call_id="c1", content="sunny"),
            ChatMessage(role="user", content="and?"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert [tc["id"] for tc in repaired[1].tool_calls] == ["c1"]
        assert repaired[2].tool_call_id == "c1"
        assert _structural_issues(repaired) == []

    def test_orphan_is_stripped_after_the_head_was_removed(self):
        messages = [
            ChatMessage(role="system", content="s"),
            ChatMessage(role="assistant", content="t", tool_calls=[_call("c0")]),
            ChatMessage(role="tool", tool_call_id="c0", content="r"),
            ChatMessage(role="user", content="u"),
            ChatMessage(role="assistant", content="t", tool_calls=[_call("c2"), _call("c3")]),
            ChatMessage(role="tool", tool_call_id="c2", content="r"),
            ChatMessage(role="user", content="u"),
            ChatMessage(role="assistant", content="t", tool_calls=[_call("c4")]),
            ChatMessage(role="tool", tool_call_id="c4", content="r"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        calls = [[tc["id"] for tc in m.tool_calls or []] for m in repaired if m.role == "assistant"]
        assert calls == [["c2"], ["c4"]]
        assert _structural_issues(repaired) == []

    def test_merged_run_of_assistants_keeps_calls_and_responses(self):
        messages = [
            ChatMessage(role="user", content="u"),
            ChatMessage(role="assistant", content=""),
            ChatMessage(role="assistant", content="a"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1"), _call("c2")]),
            ChatMessage(role="tool", tool_call_id="c1", content="r"),
            ChatMessage(role="tool", tool_call_id="c2", content="r"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert [m.role for m in repaired] == ["user", "assistant", "tool", "tool"]
        assert repaired[1].content == "a"
        assert [tc["id"] for tc in repaired[1].tool_calls] == ["c1", "c2"]
        assert _structural_issues(repaired) == []


class TestThroughTheHookRegistry:
    """The hooks as the agent loop runs them: registered in a HookRegistry,
    each on a deep copy, the result mirrored into the next step's history."""

    @staticmethod
    async def _registry():
        from pathlib import Path
        from agent_system.hooks import HookType
        from agent_system.hooks.registry import HookRegistry
        from plugins.message_validator.hooks import MessageValidatorPlugin

        plugin = MessageValidatorPlugin(Path(__file__).resolve().parents[1])
        registry = HookRegistry()
        for hook in plugin.get_hooks():
            await registry.register_hook(HookType.PRE_LLM_CALL, f"message_validator.{hook['name']}",
                                         plugin, category=hook.get("category"))
        return registry

    @staticmethod
    async def _send(registry, messages):
        from agent_system.hooks import HookContext, HookType

        context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r",
                              session_id="s", messages=messages)
        return (await registry.execute_hooks(HookType.PRE_LLM_CALL, context)).messages

    async def test_issue_without_repair_leaves_every_request_a_prefix_of_the_next(self):
        """A tool result that only looks like JSON is a warning with nothing
        to repair; the history must not change from call to call because of
        it (it used to strip the previous turn's reasoning_details)."""
        registry = await self._registry()
        history = [ChatMessage(role="user", content="go")]
        requests = []
        for step in range(3):
            sent = await self._send(registry, list(history))
            requests.append([m.model_dump(exclude_none=True) for m in sent])
            history = list(sent)
            history.append(ChatMessage(role="assistant", content=None, tool_calls=[_call(f"c{step}")],
                                       reasoning_details=[{"type": "reasoning.encrypted", "data": f"x{step}"}]))
            history.append(ChatMessage(role="tool", tool_call_id=f"c{step}",
                                       content="[1/3] fetched" if step == 0 else "ok"))

        for earlier, later in zip(requests, requests[1:]):
            assert later[:len(earlier)] == earlier

    async def test_invalid_tool_name_is_renamed(self):
        """ChatMessage.tool_calls holds dicts; the repair only knew objects
        with a `function` attribute and renamed nothing."""
        registry = await self._registry()
        sent = await self._send(registry, [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1", "file_ops.read")]),
            ChatMessage(role="tool", tool_call_id="c1", content="ok"),
        ])

        assert sent[1].tool_calls[0]["function"]["name"] == "file_ops_read"

    async def test_repair_keeps_the_reasoning_of_turns_before_it(self):
        """Invalidation starts at the first repaired message: an earlier turn
        came from an unchanged history, and stripping its reasoning moved the
        cache break to the front of the conversation."""
        registry = await self._registry()
        sent = await self._send(registry, [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c0")],
                        reasoning_details=[{"type": "reasoning.encrypted", "data": "x0"}]),
            ChatMessage(role="tool", tool_call_id="c0", content="ok"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")],
                        reasoning_details=[{"type": "reasoning.encrypted", "data": "x1"}]),
            ChatMessage(role="user", content="and?"),
        ])

        assert sent[1].reasoning_details == [{"type": "reasoning.encrypted", "data": "x0"}]
        assert sent[3].tool_calls is None
        assert sent[3].rd_orphaned is True

    async def test_issue_without_repair_is_no_change(self):
        """A warning with nothing to repair must not report a change: the
        agent loop takes a changed list as a rewrite of the session history
        and saves it again on every call."""
        registry = await self._registry()
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c0")]),
            ChatMessage(role="tool", tool_call_id="c0", content="[1/3] fetched"),
        ]
        sent = await self._send(registry, messages)

        assert sent is messages


class TestBlocksAndPasses:
    """Pairing is per tool block, a dropped call keeps its turn, and the
    repair runs until nothing changes."""

    def test_placeholder_is_not_glued_onto_a_final_answer(self):
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")]),
            ChatMessage(role="assistant", content="Here is the answer."),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert [(m.role, m.content, m.tool_calls) for m in repaired] == [
            ("user", "go", None), ("assistant", "Here is the answer.", None)]

    def test_placeholder_on_a_turn_left_with_nothing(self):
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")]),
            ChatMessage(role="user", content="and?"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert repaired[1].content == "Tool execution was interrupted"
        assert repaired[1].tool_calls is None

    def test_answer_after_a_later_call_turn_is_removed(self):
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1"), _call("c2")]),
            ChatMessage(role="tool", tool_call_id="c1", content="ok"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c3")]),
            ChatMessage(role="tool", tool_call_id="c3", content="ok"),
            ChatMessage(role="tool", tool_call_id="c2", content="ok"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert [(m.role, m.tool_call_id, [tc["id"] for tc in m.tool_calls or []]) for m in repaired] == [
            ("user", None, []), ("assistant", None, ["c1"]), ("tool", "c1", []),
            ("assistant", None, ["c3"]), ("tool", "c3", [])]

    def test_call_id_reused_across_turns(self):
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")]),
            ChatMessage(role="user", content="again"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")]),
            ChatMessage(role="tool", tool_call_id="c1", content="ok"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert repaired[1].tool_calls is None
        assert [tc["id"] for tc in repaired[3].tool_calls] == ["c1"]
        assert repaired[4].tool_call_id == "c1"

    def test_head_removal_keeps_a_later_answer_with_the_same_id(self):
        messages = [
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")]),
            ChatMessage(role="tool", tool_call_id="c1", content="old"),
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")]),
            ChatMessage(role="tool", tool_call_id="c1", content="new"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert [(m.role, m.content, [tc["id"] for tc in m.tool_calls or []]) for m in repaired] == [
            ("user", "go", []), ("assistant", None, ["c1"]), ("tool", "new", [])]

    def test_unanswered_call_of_an_earlier_block_moves_nothing(self):
        """A late answer to a closed block is removed; the messages before it
        were never inside an open block and are not reported as interleaved."""
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")]),
            ChatMessage(role="user", content="again"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c2")]),
            ChatMessage(role="tool", tool_call_id="c2", content="ok"),
            ChatMessage(role="user", content="more"),
            ChatMessage(role="tool", tool_call_id="c1", content="late"),
        ]
        result = InternalMessageValidator().validate_and_repair(messages)

        assert "interleaved_message_in_tool_block" not in [i.type for i in result.issues]
        assert [m.content for m in result.repaired_messages if m.role == "user"] == ["go", "again", "more"]
        assert result.repaired_messages[-1].content == "more"

    def test_duplicate_id_answer_goes_to_the_call_that_stays(self):
        """With one id twice in a block and one of the names unusable, the
        answer belongs to the usable call; pairing it with the dropped one
        lost the valid call as unanswered."""
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1", "!!"), _call("c1")]),
            ChatMessage(role="tool", tool_call_id="c1", content="ok"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert [(m.role, [tc["function"]["name"] for tc in m.tool_calls or []], m.tool_call_id)
                for m in repaired] == [("user", [], None), ("assistant", ["t"], None), ("tool", [], "c1")]

    def test_unusable_tool_name_drops_only_that_call_and_its_answer(self):
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content="x", tool_calls=[_call("c1", "!!"), _call("c2")]),
            ChatMessage(role="tool", tool_call_id="c1", content="ok"),
            ChatMessage(role="tool", tool_call_id="c2", content="ok"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert [(m.role, m.content, m.tool_call_id, [tc["id"] for tc in m.tool_calls or []])
                for m in repaired] == [
            ("user", "go", None, []), ("assistant", "x", None, ["c2"]), ("tool", "ok", "c2", [])]

    def test_merge_exposed_by_a_removal_happens_in_the_same_call(self):
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content="a"),
            ChatMessage(role="tool", tool_call_id="zz", content="stray"),
            ChatMessage(role="assistant", content="b"),
        ]
        repaired = InternalMessageValidator().validate_and_repair(messages).repaired_messages

        assert [(m.role, m.content) for m in repaired] == [("user", "go"), ("assistant", "a\n\nb")]


def _provider_violations(messages):
    """What a provider refuses, checked without the validator: the first
    non-instruction message asks, every call turn is followed directly by
    exactly its answers, no stray tool answer, no two assistant turns in a row."""
    from agent_system.llm.message_roles import INSTRUCTION_ROLES, is_input

    found = []
    first = next((m for m in messages if m.role not in INSTRUCTION_ROLES or is_input(m)), None)
    if first is not None and not is_input(first):
        found.append("first")
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.role == "tool":
            found.append(f"stray@{i}")
        elif m.role == "assistant" and m.tool_calls:
            j = i + 1
            while j < len(messages) and messages[j].role == "tool":
                j += 1
            answers = sorted(t.tool_call_id for t in messages[i + 1:j])
            if answers != sorted(tc["id"] for tc in m.tool_calls):
                found.append(f"block@{i}")
            i = j
            continue
        i += 1
    found += [f"consecutive@{k}" for k in range(len(messages) - 1)
              if messages[k].role == messages[k + 1].role == "assistant"]
    return found


def _random_history(rng):
    messages = [ChatMessage(role="system", content="sys")] if rng.random() < 0.8 else []
    ids = [f"c{k}" for k in range(rng.randint(1, 5))]
    for n in range(rng.randint(1, 12)):
        r = rng.random()
        if r < 0.25:
            messages.append(ChatMessage(role="user", content=f"U{n}"))
        elif r < 0.55:
            calls = [_call(rng.choice(ids), rng.choice(["t", "t", "a.b", "!!"]))
                     for _ in range(rng.randint(0, 3))]
            messages.append(ChatMessage(role="assistant", content=rng.choice([None, "", " ", f"A{n}"]),
                                        tool_calls=calls or None))
        elif r < 0.9:
            messages.append(ChatMessage(role="tool", tool_call_id=rng.choice(ids + [None]),
                                        content=rng.choice(["ok", "[1/2]", "{}"])))
        else:
            messages.append(ChatMessage(role=rng.choice(["system", "developer"]), content="note"))
    return messages


VALID_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def _usable_name(name):
    """Whether a provider can take the name after the documented rewrite
    (/ -> __, . and space -> _, other characters dropped, 3+ underscores ->
    two, underscores trimmed). Written from the guide, not from hooks.py."""
    if VALID_TOOL_NAME.match(name):
        return True
    rewritten = name.replace("/", "__").replace(".", "_").replace(" ", "_")
    rewritten = re.sub(r"[^a-zA-Z0-9_-]", "", rewritten)
    rewritten = re.sub(r"_{3,}", "__", rewritten).strip("_")
    return bool(rewritten) and bool(VALID_TOOL_NAME.match(rewritten))


def _answerable(history):
    """Tool answers a complete repair keeps: in the block of the latest call
    turn, for a call with a usable name, paired first in, first out."""
    pending, count = {}, 0
    for m in history:
        if m.role == "assistant" and m.tool_calls:
            pending = {}
            for tc in m.tool_calls:
                if _usable_name(tc["function"]["name"]):
                    pending[tc["id"]] = pending.get(tc["id"], 0) + 1
        elif m.role == "tool" and m.tool_call_id and pending.get(m.tool_call_id):
            pending[m.tool_call_id] -= 1
            count += 1
    return count


def test_random_histories_come_out_valid_complete_and_stable():
    """Seeded fuzz, judged by _provider_violations, not by the validator."""
    import random

    rng = random.Random(20260930)
    for _ in range(300):
        history = _random_history(rng)
        repaired = InternalMessageValidator().validate_and_repair(list(history)).repaired_messages

        assert _provider_violations(repaired) == [], history
        assert all(VALID_TOOL_NAME.match(tc["function"]["name"])
                   for m in repaired for tc in m.tool_calls or [])
        assert all(m.content or m.tool_calls for m in repaired if m.role == "assistant")
        opening = next((m for m in history if m.role not in ("system", "developer")), None)
        if opening is not None and opening.role == "user":
            assert sum(m.role == "tool" for m in repaired) == _answerable(history), history
        assert [m.content for m in repaired if m.role == "user"] == \
            [m.content for m in history if m.role == "user"]
        assistants = [m for m in history if m.role == "assistant"]
        final = assistants[-1] if assistants else None
        if (final is not None and not final.tool_calls and (final.content or "").strip()
                and any(m.role == "user" for m in history[:history.index(final)])):
            assert any(final.content in (m.content or "") for m in repaired if m.role == "assistant")
        again = InternalMessageValidator().validate_and_repair(list(repaired)).repaired_messages
        assert [m.model_dump() for m in again] == [m.model_dump() for m in repaired]
