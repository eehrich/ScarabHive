"""
HookIntegrationManager.execute_pre_llm_hooks with hooks that rewrite the messages.

What the hooks return is handed back to the step loop, which makes it the run's
``messages`` -- and that list is what the run persists at its end
(tests/agent/test_agent_hook_modified_messages_persist.py). The manager stages
nothing in the session tracker itself.
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


class TestPreLlmHooksModifyMessages:
    """What pre_llm hooks change comes back from execute_pre_llm_hooks."""
    
    @pytest.mark.asyncio
    async def test_modified_messages_are_returned(self, mock_agent, registry, sample_messages):
        """A hook that rewrites the list hands the new list back -- and stages nothing."""
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
        
        assert result_messages is not sample_messages
        mock_agent._session_tracker.set_compacted_messages.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_unmodified_messages_pass_through(self, mock_agent, registry, sample_messages):
        """A hook that only observes leaves the very list the loop handed in."""
        # Register an observer hook that doesn't modify
        observer_hook = MessageObserverHook("observer")
        await registry.register_hook(HookType.PRE_LLM_CALL, "observer", observer_hook)
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        # Use a short message list that won't trigger modification
        short_messages = sample_messages[:3]
        
        result = await manager.execute_pre_llm_hooks(
            messages=short_messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        # Hook executed but didn't modify
        assert observer_hook.call_count == 1
        assert result is short_messages
    
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
    async def test_archived_ref_comes_back(self, mock_agent, registry):
        """The archived_ref system message a hook adds is in the returned list."""
        # Hook that creates archived_ref
        archive_hook = ArchivedRefPreservingHook("archiver")
        await registry.register_hook(HookType.PRE_LLM_CALL, "archiver", archive_hook)
        
        manager = HookIntegrationManager(mock_agent)
        manager.registry = registry
        
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hello"),
        ]
        
        result = await manager.execute_pre_llm_hooks(
            messages=messages,
            step=1,
            request_id="req-123",
            session_id="session-123"
        )
        
        archived = [m for m in result if m.role == 'system' and 'archived_ref' in (m.content or '')]
        assert len(archived) == 1
        assert json.loads(archived[0].content)["type"] == "archived_ref"


class TestContextOptimizationHooks:
    """Hooks shaped like the context optimization plugins."""
    
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
        
        archived = [m for m in result if m.role == 'system' and 'archived_ref' in (m.content or '')]
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
    async def test_hooks_disabled_pass_the_messages_through(self, mock_agent, registry, sample_messages):
        """Disabled hooks hand back the list the loop handed in."""
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


class TestCompactionSystemMessages:
    """The rule for "which system messages ARE conversation" lives once.

    It used to be written out by hand at four places, and they disagreed: the
    prune breadcrumb was kept by one and dropped by the others, so its running
    total reset depending on which path ran last.
    """

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
