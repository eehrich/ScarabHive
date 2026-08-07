"""
Tests for HookIntegrationManager auto-sync functionality.

Tests the automatic session message synchronization when hooks modify messages,
ensuring that:
1. Modified messages are automatically synced to session tracker
2. Subsequent hooks see the updated messages
3. Session persistence uses the compacted messages
4. archived_ref system messages are preserved
5. Regular system messages are filtered out
"""

import json
import pytest
from unittest.mock import Mock

from agent_system.hooks import (
    HookRegistry,
    PluginHook,
    HookContext,
    HookResult,
    HookType,
)
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.components.hook_integration import HookIntegrationManager


class MessageModifyingHook(PluginHook):
    """Hook that modifies messages by removing some."""
    
    def __init__(self, name: str, keep_count: int = 5, config: dict | None = None):
        super().__init__(name, config or {})
        self.keep_count = keep_count
        self.call_count = 0
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Keep only the last N messages."""
        self.call_count += 1
        if context.messages and len(context.messages) > self.keep_count:
            # Simulate compaction - keep system + last N non-system
            system_msgs = [m for m in context.messages if m.role == 'system']
            non_system = [m for m in context.messages if m.role != 'system']
            context.messages = system_msgs + non_system[-self.keep_count:]
            return HookResult(success=True, modified=True, context=context)
        return HookResult(success=True, modified=False, context=context)


class ArchivedRefPreservingHook(PluginHook):
    """Hook that creates archived_ref system messages."""
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Add an archived_ref system message."""
        if context.messages:
            archived_ref = ChatMessage(
                role='system',
                content=json.dumps({
                    "type": "archived_ref",
                    "summary": "Previous conversation archived",
                    "message_count": 10
                })
            )
            # Insert after first system message or at start
            insert_pos = 0
            for i, msg in enumerate(context.messages):
                if msg.role == 'system':
                    insert_pos = i + 1
                else:
                    break
            context.messages.insert(insert_pos, archived_ref)
            return HookResult(success=True, modified=True, context=context)
        return HookResult(success=True, modified=False, context=context)


class MessageObserverHook(PluginHook):
    """Hook that observes messages without modifying."""
    
    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config or {})
        self.observed_messages: list[dict] = []
        self.call_count = 0
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Record the messages we see."""
        self.call_count += 1
        if context.messages:
            self.observed_messages = [
                {'role': m.role, 'content': m.content[:50] if m.content else None}
                for m in context.messages
            ]
        return HookResult(success=True, modified=False, context=context)


@pytest.fixture
def mock_agent():
    """Create a mock agent with session tracker."""
    agent = Mock()
    agent.name = "test_agent"
    agent.agent_config = Mock()
    agent.agent_config.hooks = None
    
    # Mock session tracker
    session_tracker = Mock()
    session_tracker.set_compacted_messages = Mock()
    session_tracker.get_compacted_messages = Mock(return_value=None)
    agent._session_tracker = session_tracker
    
    return agent


@pytest.fixture
def hook_manager(mock_agent):
    """Create HookIntegrationManager with mock agent."""
    return HookIntegrationManager(mock_agent)


@pytest.fixture
def registry():
    """Create a fresh hook registry."""
    return HookRegistry(default_timeout=5.0)


@pytest.fixture
def sample_messages():
    """Create sample messages for testing."""
    return [
        ChatMessage(role="system", content="You are a helpful assistant."),
        ChatMessage(role="user", content="Hello"),
        ChatMessage(role="assistant", content="Hi there!"),
        ChatMessage(role="user", content="How are you?"),
        ChatMessage(role="assistant", content="I'm doing well."),
        ChatMessage(role="user", content="Tell me a story"),
        ChatMessage(role="assistant", content="Once upon a time..."),
        ChatMessage(role="user", content="Continue"),
        ChatMessage(role="assistant", content="The hero journeyed far..."),
        ChatMessage(role="user", content="What happened next?"),
    ]


class TestAutoSyncSessionMessages:
    """Test suite for _auto_sync_session_messages functionality."""
    
    def test_auto_sync_filters_system_messages(self, mock_agent):
        """Test that regular system messages are filtered out during auto-sync."""
        manager = HookIntegrationManager(mock_agent)
        
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi!"),
        ]
        
        manager._auto_sync_session_messages("session-123", messages)
        
        # Should have been called with only non-system messages
        mock_agent._session_tracker.set_compacted_messages.assert_called_once()
        call_args = mock_agent._session_tracker.set_compacted_messages.call_args
        session_id, synced_messages = call_args[0]
        
        assert session_id == "session-123"
        assert len(synced_messages) == 2  # Only user + assistant
        assert all(m.role != 'system' for m in synced_messages)
    
    def test_auto_sync_preserves_archived_ref_messages(self, mock_agent):
        """Test that archived_ref system messages are preserved during auto-sync."""
        manager = HookIntegrationManager(mock_agent)
        
        archived_ref_content = json.dumps({
            "type": "archived_ref",
            "summary": "Previous conversation",
            "message_count": 5
        })
        
        messages = [
            ChatMessage(role="system", content="You are helpful."),  # Should be removed
            ChatMessage(role="system", content=archived_ref_content),  # Should be kept!
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi!"),
        ]
        
        manager._auto_sync_session_messages("session-123", messages)
        
        call_args = mock_agent._session_tracker.set_compacted_messages.call_args
        session_id, synced_messages = call_args[0]
        
        assert len(synced_messages) == 3  # archived_ref + user + assistant
        
        # Check that archived_ref is preserved
        archived_msgs = [m for m in synced_messages if m.role == 'system']
        assert len(archived_msgs) == 1
        assert json.loads(archived_msgs[0].content)["type"] == "archived_ref"
    
    def test_auto_sync_skips_when_no_session_tracker(self, mock_agent):
        """Test that auto-sync gracefully handles missing session tracker."""
        mock_agent._session_tracker = None
        manager = HookIntegrationManager(mock_agent)
        
        messages = [ChatMessage(role="user", content="Hello")]
        
        # Should not raise, just log and skip
        manager._auto_sync_session_messages("session-123", messages)
        # No assertion needed - just verifying no exception
    
    def test_auto_sync_handles_empty_messages(self, mock_agent):
        """Test auto-sync with empty message list."""
        manager = HookIntegrationManager(mock_agent)
        
        manager._auto_sync_session_messages("session-123", [])
        
        call_args = mock_agent._session_tracker.set_compacted_messages.call_args
        session_id, synced_messages = call_args[0]
        
        assert session_id == "session-123"
        assert synced_messages == []


class TestPreLlmHooksAutoSync:
    """Test pre_llm hooks auto-sync integration."""
    
    @pytest.mark.asyncio
    async def test_modified_messages_trigger_auto_sync(self, mock_agent, registry, sample_messages):
        """Test that hooks that modify messages trigger auto-sync."""
        # Register a message-modifying hook
        compaction_hook = MessageModifyingHook("compaction", keep_count=3)
        await registry.register_hook(HookType.PRE_LLM_CALL, "compaction", compaction_hook)
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        result_messages = await manager.execute_pre_llm_hooks(
            messages=sample_messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Hook should have modified messages
        assert compaction_hook.call_count == 1
        assert len(result_messages) < len(sample_messages)
        
        # Auto-sync should have been called
        mock_agent._session_tracker.set_compacted_messages.assert_called()
    
    @pytest.mark.asyncio
    async def test_unmodified_messages_skip_auto_sync(self, mock_agent, registry, sample_messages):
        """Test that hooks that don't modify messages skip auto-sync."""
        # Register an observer hook that doesn't modify
        observer_hook = MessageObserverHook("observer")
        await registry.register_hook(HookType.PRE_LLM_CALL, "observer", observer_hook)
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        # Use a short message list that won't trigger modification
        short_messages = sample_messages[:3]
        
        await manager.execute_pre_llm_hooks(
            messages=short_messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Hook executed but didn't modify
        assert observer_hook.call_count == 1
        
        # Auto-sync should NOT have been called (messages unchanged)
        mock_agent._session_tracker.set_compacted_messages.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_chained_hooks_see_previous_modifications(self, mock_agent, registry, sample_messages):
        """Test that hooks in chain see modifications from previous hooks."""
        # First hook modifies messages
        compaction_hook = MessageModifyingHook("compaction", keep_count=4)
        await registry.register_hook(
            HookType.PRE_LLM_CALL, "compaction", compaction_hook,
            order={"before": ["observer"]}
        )
        
        # Second hook observes (should see compacted messages)
        observer_hook = MessageObserverHook("observer")
        await registry.register_hook(
            HookType.PRE_LLM_CALL, "observer", observer_hook,
            order={"after": ["compaction"]}
        )
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        await manager.execute_pre_llm_hooks(
            messages=sample_messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Both hooks should have executed
        assert compaction_hook.call_count == 1
        assert observer_hook.call_count == 1
        
        # Observer should have seen compacted messages (fewer than original)
        # Original has 10 messages, compaction keeps system + 4 non-system = 5
        assert len(observer_hook.observed_messages) == 5
    
    @pytest.mark.asyncio
    async def test_archived_ref_preserved_through_auto_sync(self, mock_agent, registry):
        """Test that archived_ref messages survive auto-sync."""
        # Hook that creates archived_ref
        archive_hook = ArchivedRefPreservingHook("archiver")
        await registry.register_hook(HookType.PRE_LLM_CALL, "archiver", archive_hook)
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hello"),
        ]
        
        await manager.execute_pre_llm_hooks(
            messages=messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Auto-sync should have been called
        mock_agent._session_tracker.set_compacted_messages.assert_called()
        
        # Check that archived_ref was preserved
        call_args = mock_agent._session_tracker.set_compacted_messages.call_args
        _, synced_messages = call_args[0]
        
        system_msgs = [m for m in synced_messages if m.role == 'system']
        assert len(system_msgs) == 1
        
        content = json.loads(system_msgs[0].content)
        assert content["type"] == "archived_ref"


class TestAutoSyncWithContextOptimization:
    """Test auto-sync interaction with context optimization hooks."""
    
    @pytest.mark.asyncio
    async def test_context_engineer_style_compaction(self, mock_agent, registry):
        """Test compaction similar to context_engineer plugin."""
        
        class ContextEngineerStyleHook(PluginHook):
            """Simulates context_engineer behavior."""
            
            async def on_pre_llm_call(self, context: HookContext) -> HookResult:
                if not context.messages or len(context.messages) <= 5:
                    return HookResult(success=True, modified=False, context=context)
                
                # Create archived_ref for old messages
                old_msgs = [m for m in context.messages if m.role != 'system'][:-3]
                archived_ref = ChatMessage(
                    role='system',
                    content=json.dumps({
                        "type": "archived_ref",
                        "summary": f"Archived {len(old_msgs)} messages",
                        "message_count": len(old_msgs)
                    })
                )
                
                # Keep system + archived_ref + last 3 non-system
                system_msgs = [m for m in context.messages if m.role == 'system']
                recent = [m for m in context.messages if m.role != 'system'][-3:]
                
                context.messages = system_msgs + [archived_ref] + recent
                return HookResult(success=True, modified=True, context=context)
        
        hook = ContextEngineerStyleHook("context_engineer")
        await registry.register_hook(HookType.PRE_LLM_CALL, "context_engineer", hook)
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        # Large conversation
        messages = [
            ChatMessage(role="system", content="You are helpful."),
        ] + [
            ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"Message {i}")
            for i in range(10)
        ]
        
        result = await manager.execute_pre_llm_hooks(
            messages=messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Should have compacted
        assert len(result) < len(messages)
        
        # Auto-sync should preserve archived_ref
        call_args = mock_agent._session_tracker.set_compacted_messages.call_args
        _, synced = call_args[0]
        
        archived = [m for m in synced if m.role == 'system' and 'archived_ref' in (m.content or '')]
        assert len(archived) == 1
    
    @pytest.mark.asyncio
    async def test_context_summarizer_style_hook(self, mock_agent, registry):
        """Test summarization similar to context_summarizer plugin."""
        
        class ContextSummarizerStyleHook(PluginHook):
            """Simulates context_summarizer behavior."""
            
            async def on_pre_llm_call(self, context: HookContext) -> HookResult:
                if not context.messages or len(context.messages) <= 5:
                    return HookResult(success=True, modified=False, context=context)
                
                # Summarize old messages
                system_msgs = [m for m in context.messages if m.role == 'system']
                non_system = [m for m in context.messages if m.role != 'system']
                
                old_msgs = non_system[:-3]
                recent = non_system[-3:]
                
                # Create summary message
                summary = ChatMessage(
                    role="assistant",
                    content=f"[Summary of {len(old_msgs)} messages: Discussion about various topics]"
                )
                
                context.messages = system_msgs + [summary] + recent
                return HookResult(success=True, modified=True, context=context)
        
        hook = ContextSummarizerStyleHook("context_summarizer")
        await registry.register_hook(HookType.PRE_LLM_CALL, "context_summarizer", hook)
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        messages = [
            ChatMessage(role="system", content="You are helpful."),
        ] + [
            ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"Message {i}")
            for i in range(10)
        ]
        
        result = await manager.execute_pre_llm_hooks(
            messages=messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Should have summarized
        assert len(result) < len(messages)
        
        # Should contain summary
        summaries = [m for m in result if '[Summary of' in (m.content or '')]
        assert len(summaries) == 1


class TestEdgeCases:
    """Test edge cases and error handling."""
    
    @pytest.mark.asyncio
    async def test_hooks_disabled_skips_auto_sync(self, mock_agent, registry, sample_messages):
        """Test that disabled hooks don't trigger auto-sync."""
        mock_agent.agent_config.hooks = Mock()
        mock_agent.agent_config.hooks.enabled = False
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        result = await manager.execute_pre_llm_hooks(
            messages=sample_messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Should return original messages
        assert result is sample_messages
        
        # Auto-sync should not be called
        mock_agent._session_tracker.set_compacted_messages.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_none_messages_from_hook(self, mock_agent, registry):
        """Test handling when hook returns None messages."""
        
        class NoneMessagesHook(PluginHook):
            async def on_pre_llm_call(self, context: HookContext) -> HookResult:
                context.messages = None
                return HookResult(success=True, modified=True, context=context)
        
        hook = NoneMessagesHook("none_hook")
        await registry.register_hook(HookType.PRE_LLM_CALL, "none_hook", hook)
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        messages = [ChatMessage(role="user", content="Hello")]
        
        result = await manager.execute_pre_llm_hooks(
            messages=messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Should return original messages when hook returns None
        assert result is messages
    
    @pytest.mark.asyncio
    async def test_malformed_archived_ref_filtered(self, mock_agent):
        """Test that malformed archived_ref messages are filtered."""
        manager = HookIntegrationManager(mock_agent)
        
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="system", content='{"type": "not_archived_ref"}'),  # Not archived_ref
            ChatMessage(role="system", content='invalid json'),  # Invalid JSON
            ChatMessage(role="user", content="Hello"),
        ]
        
        manager._auto_sync_session_messages("session-123", messages)
        
        call_args = mock_agent._session_tracker.set_compacted_messages.call_args
        _, synced = call_args[0]
        
        # Only user message should remain (all system filtered)
        assert len(synced) == 1
        assert synced[0].role == "user"
    
    @pytest.mark.asyncio
    async def test_identical_messages_reference_no_sync(self, mock_agent, registry):
        """Test that returning same message list reference doesn't trigger sync."""
        
        class IdentityHook(PluginHook):
            """Returns the same messages object."""
            async def on_pre_llm_call(self, context: HookContext) -> HookResult:
                # Return context as-is, messages same reference
                return HookResult(success=True, modified=False, context=context)
        
        hook = IdentityHook("identity")
        await registry.register_hook(HookType.PRE_LLM_CALL, "identity", hook)
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        messages = [ChatMessage(role="user", content="Hello")]
        
        await manager.execute_pre_llm_hooks(
            messages=messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Auto-sync should NOT be called (same object reference)
        mock_agent._session_tracker.set_compacted_messages.assert_not_called()


class TestMultipleArchivedRefs:
    """Test handling of multiple archived_ref messages."""
    
    def test_multiple_archived_refs_preserved(self, mock_agent):
        """Test that multiple archived_ref messages are all preserved."""
        manager = HookIntegrationManager(mock_agent)
        
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="system", content=json.dumps({
                "type": "archived_ref", "summary": "First archive"
            })),
            ChatMessage(role="system", content=json.dumps({
                "type": "archived_ref", "summary": "Second archive"
            })),
            ChatMessage(role="user", content="Hello"),
        ]
        
        manager._auto_sync_session_messages("session-123", messages)
        
        call_args = mock_agent._session_tracker.set_compacted_messages.call_args
        _, synced = call_args[0]
        
        # Both archived_refs + user should be preserved
        assert len(synced) == 3
        
        archived = [m for m in synced if m.role == 'system']
        assert len(archived) == 2


class TestCompactionSystemMessages:
    """The rule for "which system messages ARE conversation" lives once.

    It used to be written out by hand at four places, and they disagreed: the
    prune breadcrumb was kept by one and dropped by the others, so its running
    total reset depending on which path ran last.
    """

    @staticmethod
    def _notice(total=7):
        return ChatMessage(role="system", content=json.dumps(
            {"type": "pruned_notice", "total_removed": total, "hint": "…"}))

    @pytest.mark.parametrize("content, expected", [
        (json.dumps({"type": "archived_ref", "ref_id": "arch_1"}), True),
        (json.dumps({"type": "pruned_notice", "total_removed": 3}), True),
        ("Du bist ein hilfreicher Assistent.", False),
        # A prompt that merely TALKS about the markers is not one of them.
        ("Erwähne niemals archived_ref oder pruned_notice.", False),
        (json.dumps({"type": "archived_ref_explainer"}), False),
        # Nested, not top-level.
        (json.dumps({"beispiel": {"type": "archived_ref"}}), False),
        (json.dumps([{"type": "archived_ref"}]), False),
        ('{"type": "archived_ref"', False),
    ])
    def test_predicate(self, content, expected):
        from agent_system.servers.agent.components.hook_integration import (
            is_compaction_system_message,
        )
        assert is_compaction_system_message(
            ChatMessage(role="system", content=content)) is expected
        # Both shapes must work: the compaction plugin passes plain dicts.
        assert is_compaction_system_message(
            {"role": "system", "content": content}) is expected

    def test_key_order_does_not_decide(self):
        """No head window: a long hint before "type" must not hide it.

        A `sort_keys=True` anywhere in the producers would otherwise push the
        type past the window and silently drop every breadcrumb from
        persistence — the exact failure this rule exists to prevent.
        """
        from agent_system.servers.agent.components.hook_integration import (
            is_compaction_system_message,
        )
        payload = json.dumps(
            {"hint": "x" * 400, "total_removed": 12, "type": "pruned_notice"},
            sort_keys=True)
        assert is_compaction_system_message(
            ChatMessage(role="system", content=payload)) is True

    def test_auto_sync_keeps_the_breadcrumb(self, mock_agent):
        manager = HookIntegrationManager(mock_agent)
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            self._notice(),
            ChatMessage(role="user", content="Hallo"),
        ]

        manager._auto_sync_session_messages("session-123", messages)

        _, synced = mock_agent._session_tracker.set_compacted_messages.call_args[0]
        kept = [m for m in synced if m.role == "system"]
        assert len(kept) == 1, "the breadcrumb was dropped with the prompt"
        assert json.loads(kept[0].content)["type"] == "pruned_notice"
        assert not any("You are helpful." == m.content for m in synced), \
            "the agent's system prompt must NOT be persisted"
