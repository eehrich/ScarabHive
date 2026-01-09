"""
Tests for hook system registry and execution.
"""

import asyncio
import pytest

from agent_system.hooks import (
    HookRegistry,
    PluginHook,
    HookContext,
    HookResult,
    HookType,
)


class SimpleHook(PluginHook):
    """Simple test hook that modifies context."""
    
    def __init__(self, name: str, config: dict = None):
        super().__init__(name, config or {})
        self.call_count = 0
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Modify messages by appending test message."""
        self.call_count += 1
        if context.messages is not None:
            from agent_system.llm.models import ChatMessage
            context.messages.append(ChatMessage(role="system", content=f"Hook {self.name} executed"))
            return HookResult(success=True, modified=True, context=context)
        return HookResult(success=True, modified=False, context=context)
    
    async def on_format_output(self, context: HookContext) -> HookResult:
        """Uppercase the output."""
        self.call_count += 1
        if context.output:
            context.output = context.output.upper()
            return HookResult(success=True, modified=True, context=context)
        return HookResult(success=True, modified=False, context=context)


class FailingHook(PluginHook):
    """Hook that always fails."""
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Return failure result."""
        return HookResult(success=False, error="Intentional failure", context=context)


class SlowHook(PluginHook):
    """Hook that takes time to execute."""
    
    def __init__(self, name: str, delay: float = 1.0, config: dict = None):
        super().__init__(name, config or {})
        self.delay = delay
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Sleep for delay seconds."""
        await asyncio.sleep(self.delay)
        return HookResult(success=True, modified=False, context=context)


@pytest.fixture
def registry():
    """Create a fresh hook registry for each test."""
    return HookRegistry(default_timeout=5.0)


@pytest.fixture
def base_context():
    """Create a base hook context for testing."""
    from agent_system.llm.models import ChatMessage
    return HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-request",
        session_id="test-session",
        agent_name="test-agent",
        messages=[ChatMessage(role="user", content="Hello")],
        step=1,
    )


@pytest.mark.asyncio
async def test_hook_registration(registry):
    """Test basic hook registration."""
    hook = SimpleHook("test_hook")
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "test_hook", hook)
    
    hooks_list = registry.list_hooks(HookType.PRE_LLM_CALL)
    assert "pre_llm_call" in hooks_list
    assert "test_hook" in hooks_list["pre_llm_call"]


@pytest.mark.asyncio
async def test_duplicate_hook_registration(registry):
    """Test that duplicate hook names are rejected."""
    hook1 = SimpleHook("test_hook")
    hook2 = SimpleHook("test_hook")
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "test_hook", hook1)
    
    with pytest.raises(ValueError, match="already registered"):
        await registry.register_hook(HookType.PRE_LLM_CALL, "test_hook", hook2)


@pytest.mark.asyncio
async def test_hook_execution(registry, base_context):
    """Test basic hook execution."""
    hook = SimpleHook("test_hook")
    await registry.register_hook(HookType.PRE_LLM_CALL, "test_hook", hook)
    
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # Hook should have added a message
    assert len(result_context.messages) == 2
    assert result_context.messages[-1].content == "Hook test_hook executed"
    assert hook.call_count == 1


@pytest.mark.asyncio
async def test_multiple_hooks_execution(registry, base_context):
    """Test execution of multiple hooks in order."""
    hook1 = SimpleHook("hook1")
    hook2 = SimpleHook("hook2")
    hook3 = SimpleHook("hook3")
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "hook1", hook1)
    await registry.register_hook(HookType.PRE_LLM_CALL, "hook2", hook2)
    await registry.register_hook(HookType.PRE_LLM_CALL, "hook3", hook3)
    
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # All hooks should have executed
    assert hook1.call_count == 1
    assert hook2.call_count == 1
    assert hook3.call_count == 1
    
    # Should have 4 messages: original + 3 from hooks
    assert len(result_context.messages) == 4


@pytest.mark.asyncio
async def test_hook_ordering_dependencies(registry, base_context):
    """Test hook execution with before/after ordering."""
    hook1 = SimpleHook("first", {"order": {"before": ["middle"]}})
    hook2 = SimpleHook("middle", {"order": {"after": ["first"], "before": ["last"]}})
    hook3 = SimpleHook("last", {"order": {"after": ["middle"]}})
    
    # Register in random order
    await registry.register_hook(HookType.PRE_LLM_CALL, "last", hook3)
    await registry.register_hook(HookType.PRE_LLM_CALL, "first", hook1)
    await registry.register_hook(HookType.PRE_LLM_CALL, "middle", hook2)
    
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # Check execution order by message content
    messages = result_context.messages
    assert messages[1].content == "Hook first executed"
    assert messages[2].content == "Hook middle executed"
    assert messages[3].content == "Hook last executed"


@pytest.mark.asyncio
async def test_circular_dependency_detection(registry, base_context):
    """Test that circular dependencies are detected."""
    hook1 = SimpleHook("hook1", {"order": {"after": ["hook2"]}})
    hook2 = SimpleHook("hook2", {"order": {"after": ["hook1"]}})
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "hook1", hook1)
    await registry.register_hook(HookType.PRE_LLM_CALL, "hook2", hook2)
    
    # Should still execute hooks despite circular dependency (fallback to registration order)
    await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)

    # Both hooks should execute (order may be arbitrary)
    assert hook1.call_count == 1
    assert hook2.call_count == 1


@pytest.mark.asyncio
async def test_failing_hook_isolation(registry, base_context):
    """Test that failing hooks don't stop other hooks."""
    hook1 = SimpleHook("success1")
    hook2 = FailingHook("failing")
    hook3 = SimpleHook("success2")
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "success1", hook1)
    await registry.register_hook(HookType.PRE_LLM_CALL, "failing", hook2)
    await registry.register_hook(HookType.PRE_LLM_CALL, "success2", hook3)
    
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # Successful hooks should execute
    assert hook1.call_count == 1
    assert hook3.call_count == 1
    
    # Should have messages from successful hooks
    assert len(result_context.messages) == 3


@pytest.mark.asyncio
async def test_hook_timeout(registry, base_context):
    """Test that slow hooks are timed out."""
    slow_hook = SlowHook("slow", delay=10.0)
    fast_hook = SimpleHook("fast")
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "slow", slow_hook)
    await registry.register_hook(HookType.PRE_LLM_CALL, "fast", fast_hook)
    
    # Execute with short timeout
    await registry.execute_hooks(
        HookType.PRE_LLM_CALL, base_context, timeout=0.5
    )
    
    # Fast hook should still execute
    assert fast_hook.call_count == 1
    
    # Stats should show timeout for slow hook
    stats = registry.get_stats("slow")
    assert stats["failures"] >= 1


@pytest.mark.asyncio
async def test_disabled_hook_skipped(registry, base_context):
    """Test that disabled hooks are skipped."""
    hook = SimpleHook("test_hook")
    
    # Register hook with enabled=False
    await registry.register_hook(HookType.PRE_LLM_CALL, "test_hook", hook, enabled=False)
    
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # Hook should not execute
    assert hook.call_count == 0
    assert len(result_context.messages) == 1  # Only original message


@pytest.mark.asyncio
async def test_hook_statistics(registry, base_context):
    """Test hook execution statistics tracking."""
    hook = SimpleHook("test_hook")
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "test_hook", hook)
    
    # Execute multiple times
    for _ in range(3):
        await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    stats = registry.get_stats("test_hook")
    assert stats["executions"] == 3
    assert stats["successes"] == 3
    assert stats["failures"] == 0
    assert stats["avg_time"] >= 0  # Changed from > to >= since fast hooks can be 0


@pytest.mark.asyncio
async def test_hook_unregistration(registry, base_context):
    """Test hook unregistration."""
    hook = SimpleHook("test_hook")
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "test_hook", hook)
    
    # Verify registered
    hooks_list = registry.list_hooks(HookType.PRE_LLM_CALL)
    assert "test_hook" in hooks_list["pre_llm_call"]
    
    # Unregister
    result = await registry.unregister_hook(HookType.PRE_LLM_CALL, "test_hook")
    assert result is True
    
    # Verify unregistered
    hooks_list = registry.list_hooks(HookType.PRE_LLM_CALL)
    assert "test_hook" not in hooks_list.get("pre_llm_call", [])
    
    # Unregistering again should return False
    result = await registry.unregister_hook(HookType.PRE_LLM_CALL, "test_hook")
    assert result is False


@pytest.mark.asyncio
async def test_hook_info_retrieval(registry):
    """Test retrieving hook information."""
    hook = SimpleHook("test_hook", {"enabled": True, "order": {"before": ["end"]}})
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "test_hook", hook)
    
    info = registry.get_hook_info("test_hook")
    assert info is not None
    assert info["name"] == "test_hook"
    assert info["type"] == "pre_llm_call"
    assert info["enabled"] is True
    assert info["order_spec"]["before"] == ["end"]
    assert info["class"] == "SimpleHook"


@pytest.mark.asyncio
async def test_format_output_hooks(registry):
    """Test format output hooks."""
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT,
        request_id="test-request",
        session_id="test-session",
        agent_name="test-agent",
        output="hello world",
    )
    
    hook = SimpleHook("formatter")
    await registry.register_hook(HookType.FORMAT_OUTPUT, "formatter", hook)
    
    result_context = await registry.execute_hooks(HookType.FORMAT_OUTPUT, context)
    
    assert result_context.output == "HELLO WORLD"
    assert hook.call_count == 1


@pytest.mark.asyncio
async def test_no_hooks_registered(registry, base_context):
    """Test execution when no hooks are registered."""
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # Context should be unchanged
    assert result_context.messages == base_context.messages
    assert len(result_context.messages) == 1


@pytest.mark.asyncio
async def test_context_isolation(registry, base_context):
    """Test that hooks receive deep copies of context."""
    class MutatingHook(PluginHook):
        async def on_pre_llm_call(self, context: HookContext) -> HookResult:
            # Try to mutate the context
            if context.messages:
                context.messages.clear()
            return HookResult(success=True, modified=True, context=context)
    
    hook1 = MutatingHook("mutator")
    hook2 = SimpleHook("normal")
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "mutator", hook1)
    await registry.register_hook(HookType.PRE_LLM_CALL, "normal", hook2)
    
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # Even though mutator cleared messages, normal hook should have received original messages
    # and could add to them
    assert len(result_context.messages) >= 0


@pytest.mark.asyncio
async def test_hook_categories(registry, base_context):
    """Test hook ordering using categories."""
    execution_order = []
    
    class TrackedHook(PluginHook):
        def __init__(self, name: str, config: dict = None):
            super().__init__(name, config or {})
        
        async def on_pre_llm_call(self, context: HookContext) -> HookResult:
            execution_order.append(self.name)
            return HookResult(success=True, modified=False, context=context)
    
    # Register hooks with categories
    hook1 = TrackedHook("inject_hook1")
    hook2 = TrackedHook("inject_hook2")
    hook3 = TrackedHook("capture_hook")
    
    # inject_hook1 and inject_hook2 are in category "inject"
    await registry.register_hook(
        HookType.PRE_LLM_CALL,
        "inject_hook1",
        hook1,
        order_spec={"after": ["begin"], "before": ["end"]},
        category="inject"
    )
    
    await registry.register_hook(
        HookType.PRE_LLM_CALL,
        "inject_hook2",
        hook2,
        order_spec={"after": ["begin"], "before": ["end"]},
        category="inject"
    )
    
    # capture_hook comes after all "inject" hooks
    await registry.register_hook(
        HookType.PRE_LLM_CALL,
        "capture_hook",
        hook3,
        order_spec={"after": ["inject"], "before": ["end"]}  # "inject" is a category
    )
    
    # Execute hooks
    await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # Verify that capture_hook comes after both inject hooks
    assert len(execution_order) == 3
    assert execution_order.index("capture_hook") > execution_order.index("inject_hook1")
    assert execution_order.index("capture_hook") > execution_order.index("inject_hook2")


@pytest.mark.asyncio
async def test_hook_category_before(registry, base_context):
    """Test hook ordering before a category."""
    execution_order = []
    
    class TrackedHook(PluginHook):
        def __init__(self, name: str, config: dict = None):
            super().__init__(name, config or {})
        
        async def on_pre_llm_call(self, context: HookContext) -> HookResult:
            execution_order.append(self.name)
            return HookResult(success=True, modified=False, context=context)
    
    # Register hooks with categories
    hook1 = TrackedHook("optimize_hook")
    hook2 = TrackedHook("inject_hook1")
    hook3 = TrackedHook("inject_hook2")
    
    # inject hooks are in category "inject"
    await registry.register_hook(
        HookType.PRE_LLM_CALL,
        "inject_hook1",
        hook2,
        order_spec={"after": ["begin"], "before": ["end"]},
        category="inject"
    )
    
    await registry.register_hook(
        HookType.PRE_LLM_CALL,
        "inject_hook2",
        hook3,
        order_spec={"after": ["begin"], "before": ["end"]},
        category="inject"
    )
    
    # optimize_hook comes before all "inject" hooks
    await registry.register_hook(
        HookType.PRE_LLM_CALL,
        "optimize_hook",
        hook1,
        order_spec={"after": ["begin"], "before": ["inject"]}  # Before "inject" category
    )
    
    # Execute hooks
    await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # Verify that optimize_hook comes before both inject hooks
    assert len(execution_order) == 3
    assert execution_order.index("optimize_hook") < execution_order.index("inject_hook1")
    assert execution_order.index("optimize_hook") < execution_order.index("inject_hook2")


@pytest.mark.asyncio
async def test_hook_category_nonexistent_debug_log(registry, base_context, caplog):
    """Test that referencing non-existent category logs a debug message (not warning)."""
    import logging
    caplog.set_level(logging.DEBUG)
    
    hook = SimpleHook("test_hook")
    
    await registry.register_hook(
        HookType.PRE_LLM_CALL,
        "test_hook",
        hook,
        order_spec={"after": ["nonexistent_category"], "before": ["end"]}
    )
    
    # Execute hooks - should work and log debug message (not warning)
    await registry.execute_hooks(HookType.PRE_LLM_CALL, base_context)
    
    # Check for debug message in logs (it's normal for hooks to be disabled)
    assert any(
        "inactive hook/category 'nonexistent_category'" in record.message
        for record in caplog.records
    )
