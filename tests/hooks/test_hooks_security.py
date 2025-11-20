"""
Tests for hook security features (Task 9312).

Tests validation, audit logging, and security safeguards.
"""

import logging
import pytest

from agent_system.hooks import (
    HookRegistry,
    HookContext,
    HookResult,
    HookType,
    PluginHook,
)


@pytest.fixture
def registry():
    """Create a fresh hook registry for each test."""
    return HookRegistry(default_timeout=5.0)


class InvalidResultHook(PluginHook):
    """Hook that returns invalid result (wrong type)."""
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Return a string instead of HookResult."""
        return "invalid"  # type: ignore


class InvalidModifiedHook(PluginHook):
    """Hook that claims modification but doesn't provide context."""
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Return modified=True but no context."""
        return HookResult(success=True, modified=True, context=None)


class InvalidContextTypeHook(PluginHook):
    """Hook that returns wrong context type."""
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Return dict instead of HookContext."""
        return HookResult(success=True, modified=True, context={})  # type: ignore


class ValidModifyingHook(PluginHook):
    """Hook that properly modifies context."""
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Add a message to context."""
        messages = list(context.messages) if context.messages else []
        messages.append({"role": "system", "content": "Added by hook"})
        
        modified_context = HookContext(
            hook_type=context.hook_type,
            request_id=context.request_id,
            session_id=context.session_id,
            agent=context.agent,
            agent_name=context.agent_name,
            messages=messages,
            llm_response=context.llm_response,
            tool_call=context.tool_call,
            tool_result=context.tool_result,
            output=context.output,
            metadata=context.metadata,
            step=context.step,
            llm=context.llm
        )
        
        return HookResult(
            success=True,
            modified=True,
            context=modified_context,
            metadata={"added_message": True}
        )


@pytest.mark.asyncio
async def test_invalid_result_type(registry):
    """Test that invalid result type is rejected."""
    hook = InvalidResultHook("invalid_result", {})
    await registry.register_hook(HookType.PRE_LLM_CALL, "invalid_result", hook)
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test",
        session_id="test",
        agent=None,
        agent_name="test",
        messages=[{"role": "user", "content": "test"}]
    )
    
    # Should log error but not crash
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, context)
    
    # Context should be unchanged because hook result was invalid
    assert result_context.messages == context.messages


@pytest.mark.asyncio
async def test_invalid_modified_without_context(registry):
    """Test that modified=True without context is rejected."""
    hook = InvalidModifiedHook("invalid_modified", {})
    await registry.register_hook(HookType.PRE_LLM_CALL, "invalid_modified", hook)
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test",
        session_id="test",
        agent=None,
        agent_name="test",
        messages=[{"role": "user", "content": "test"}]
    )
    
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, context)
    
    # Context should be unchanged
    assert result_context.messages == context.messages


@pytest.mark.asyncio
async def test_invalid_context_type(registry):
    """Test that wrong context type is rejected."""
    hook = InvalidContextTypeHook("invalid_context", {})
    await registry.register_hook(HookType.PRE_LLM_CALL, "invalid_context", hook)
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test",
        session_id="test",
        agent=None,
        agent_name="test",
        messages=[{"role": "user", "content": "test"}]
    )
    
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, context)
    
    # Context should be unchanged
    assert result_context.messages == context.messages


@pytest.mark.asyncio
async def test_audit_logging_on_modification(registry, caplog):
    """Test that modifications are audit logged."""
    hook = ValidModifyingHook("modifying_hook", {})
    await registry.register_hook(HookType.PRE_LLM_CALL, "modifying_hook", hook)
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_request",
        session_id="test_session",
        agent=None,
        agent_name="test",
        messages=[{"role": "user", "content": "original"}]
    )
    
    with caplog.at_level(logging.INFO):
        result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, context)
    
    # Check modification was applied
    assert len(result_context.messages) == 2
    assert result_context.messages[1]["content"] == "Added by hook"
    
    # Check audit log was created
    audit_logs = [r for r in caplog.records if "Hook modification audit" in r.message]
    assert len(audit_logs) > 0
    
    audit_log = audit_logs[0]
    assert audit_log.levelname == "INFO"
    assert "modifying_hook" in audit_log.message
    assert hasattr(audit_log, "request_id")
    assert audit_log.request_id == "test_request"
    assert hasattr(audit_log, "session_id")
    assert audit_log.session_id == "test_session"


@pytest.mark.asyncio
async def test_context_isolation(registry):
    """Test that hook receives deep copy, not original context."""
    class ContextMutatingHook(PluginHook):
        """Hook that tries to mutate original context."""
        
        async def on_pre_llm_call(self, context: HookContext) -> HookResult:
            # Try to mutate messages in place
            if context.messages:
                context.messages.append({"role": "system", "content": "mutation"})
            return HookResult(success=True, modified=False, context=context)
    
    hook = ContextMutatingHook("mutating", {})
    await registry.register_hook(HookType.PRE_LLM_CALL, "mutating", hook)
    
    original_messages = [{"role": "user", "content": "test"}]
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test",
        session_id="test",
        agent=None,
        agent_name="test",
        messages=original_messages.copy()
    )
    
    result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, context)
    
    # Original messages should not be mutated
    # Because hook said modified=False, result should use original context
    assert len(result_context.messages) == 1
    assert result_context.messages[0]["content"] == "test"


@pytest.mark.asyncio
async def test_validation_statistics(registry):
    """Test that validation failures are tracked in statistics."""
    hook = InvalidResultHook("invalid_stats", {})
    await registry.register_hook(HookType.PRE_LLM_CALL, "invalid_stats", hook)
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test",
        session_id="test",
        agent=None,
        agent_name="test",
        messages=[{"role": "user", "content": "test"}]
    )
    
    await registry.execute_hooks(HookType.PRE_LLM_CALL, context)
    
    # Check stats
    stats = registry.get_stats("invalid_stats")
    assert stats["executions"] > 0
    assert stats["failures"] > 0  # Validation failure should count as failure


@pytest.mark.asyncio
async def test_multiple_modifications_audit(registry, caplog):
    """Test audit logging with multiple modifying hooks."""
    hook1 = ValidModifyingHook("hook1", {})
    hook2 = ValidModifyingHook("hook2", {})
    
    await registry.register_hook(HookType.PRE_LLM_CALL, "hook1", hook1, order_spec=None, priority=10)
    await registry.register_hook(HookType.PRE_LLM_CALL, "hook2", hook2, order_spec=None, priority=20)
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="multi_test",
        session_id="test",
        agent=None,
        agent_name="test",
        messages=[{"role": "user", "content": "original"}]
    )
    
    with caplog.at_level(logging.INFO):
        result_context = await registry.execute_hooks(HookType.PRE_LLM_CALL, context)
    
    # Should have 3 messages total (original + 2 added)
    assert len(result_context.messages) == 3
    
    # Should have 2 audit logs (one per modifying hook)
    audit_logs = [r for r in caplog.records if "Hook modification audit" in r.message]
    assert len(audit_logs) == 2
