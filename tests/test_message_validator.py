"""Tests for message_validator plugin."""

import pytest
from agent_system.llm.models import ChatMessage
from agent_system.hooks import HookContext, HookType
from plugins.message_validator.hooks import MessageValidatorPlugin
from pathlib import Path


@pytest.fixture
def validator():
    """Create a message validator plugin instance."""
    plugin_dir = Path(__file__).parent.parent / 'src' / 'plugins' / 'message_validator'
    return MessageValidatorPlugin(plugin_dir)


def create_context(messages):
    """Helper to create HookContext for testing."""
    return HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test",
        session_id="test_session",
        agent=None,
        agent_name="test_agent",
        messages=messages
    )


@pytest.mark.asyncio
async def test_validate_structure_with_chat_messages(validator):
    """Test that structure validation works with ChatMessage objects."""
    messages = [
        ChatMessage(role='system', content='You are a helpful assistant'),
        ChatMessage(role='user', content='Hello'),
        ChatMessage(role='assistant', content='Hi there!'),
    ]
    
    context = create_context(messages)
    result = await validator.validate_structure(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['structure_errors'] == []


@pytest.mark.asyncio
async def test_validate_structure_with_dicts(validator):
    """Test that structure validation works with dict messages."""
    messages = [
        {'role': 'system', 'content': 'You are a helpful assistant'},
        {'role': 'user', 'content': 'Hello'},
        {'role': 'assistant', 'content': 'Hi there!'},
    ]
    
    context = create_context(messages)
    result = await validator.validate_structure(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['structure_errors'] == []


@pytest.mark.asyncio
async def test_validate_structure_missing_fields(validator):
    """Test structure validation with missing fields."""
    messages = [
        {'content': 'Missing role'},  # Missing 'role'
        {'role': 'user'},  # Missing 'content'
    ]
    
    context = create_context(messages)
    result = await validator.validate_structure(context)
    
    # Should succeed in non-strict mode but log warnings
    assert result.success is True
    assert len(result.metadata['structure_errors']) > 0


@pytest.mark.asyncio
async def test_validate_messages_with_chat_messages(validator):
    """Test message validation with ChatMessage objects."""
    messages = [
        ChatMessage(role='user', content='Hello'),
        ChatMessage(role='assistant', content='Hi there!'),
        ChatMessage(role='user', content='How are you?'),
    ]
    
    context = create_context(messages)
    result = await validator.validate_messages(context)
    
    assert result.success is True
    # Should have 3 validated messages
    assert len(result.context.messages) == 3


@pytest.mark.asyncio
async def test_validate_messages_sanitization(validator):
    """Test that content sanitization works."""
    messages = [
        ChatMessage(
            role='user',
            content='<script>alert("xss")</script>Normal text'
        ),
    ]
    
    context = create_context(messages)
    result = await validator.validate_messages(context)
    
    assert result.success is True
    # Script tag should be removed
    sanitized_content = result.context.messages[0].content
    assert '<script>' not in sanitized_content
    assert 'Normal text' in sanitized_content


@pytest.mark.asyncio
async def test_validate_messages_role_validation(validator):
    """Test that invalid roles are caught."""
    messages = [
        ChatMessage(role='invalid_role', content='Test'),
    ]
    
    context = create_context(messages)
    result = await validator.validate_messages(context)
    
    # Should succeed in non-strict mode but log errors
    assert result.success is True
    assert len(result.metadata['validation_errors']) > 0


@pytest.mark.asyncio
async def test_validate_messages_length_limit(validator):
    """Test that long messages are truncated."""
    # Create a very long message
    long_content = 'x' * 150000  # Exceeds default 100k limit
    
    messages = [
        ChatMessage(role='user', content=long_content),
    ]
    
    context = create_context(messages)
    result = await validator.validate_messages(context)
    
    assert result.success is True
    # Should be truncated
    assert len(result.context.messages[0].content) < len(long_content)
    assert '[truncated]' in result.context.messages[0].content


@pytest.mark.asyncio
async def test_validate_messages_mixed_types(validator):
    """Test validation with mixed ChatMessage and dict messages."""
    messages = [
        ChatMessage(role='system', content='System prompt'),
        {'role': 'user', 'content': 'Dict message'},
        ChatMessage(role='assistant', content='Response'),
    ]
    
    context = create_context(messages)
    result = await validator.validate_messages(context)
    
    assert result.success is True
    assert len(result.context.messages) == 3


@pytest.mark.asyncio
async def test_alternating_pattern_enforcement(validator):
    """Test that alternating user/assistant pattern is enforced."""
    messages = [
        ChatMessage(role='user', content='First'),
        ChatMessage(role='user', content='Second user (duplicate)'),
        ChatMessage(role='assistant', content='Response'),
    ]
    
    context = create_context(messages)
    result = await validator.validate_messages(context)
    
    assert result.success is True
    # Duplicate user message should be filtered out
    # Should have: user, assistant (2 messages)
    assert len(result.context.messages) == 2
    assert result.context.messages[0].role == 'user'
    assert result.context.messages[1].role == 'assistant'
