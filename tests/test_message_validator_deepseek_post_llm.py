"""
Tests for DeepSeek response parsing in post_llm_call hook.
"""
import pytest
from pathlib import Path

from agent_system.hooks import HookContext
from src.plugins.message_validator.hooks import MessageValidatorPlugin


@pytest.fixture
def validator_plugin():
    """Create message validator plugin instance."""
    plugin_dir = Path(__file__).parent.parent / "src" / "plugins" / "message_validator"
    return MessageValidatorPlugin(plugin_dir)


@pytest.mark.asyncio
async def test_parse_deepseek_response_hook(validator_plugin):
    """Test post_llm_call hook parses DeepSeek markers from response."""
    
    # Simulate LLM response with DeepSeek markers
    response_with_markers = (
        "I'll delete that task. "
        "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>todo<｜tool▁sep｜>"
        '{"operation": "delete", "task_id": "task_002"}'
        "<｜tool▁call▁end｜><｜tool▁calls▁end｜>"
    )
    
    context = HookContext(
        hook_type="post_llm_call",
        messages=[],
        request_id="test_req_001",
        session_id="test_session_001",
        metadata={
            "llm_response": {
                "content": response_with_markers,
                "tool_calls": []
            }
        }
    )
    
    # Run the hook
    result = await validator_plugin.parse_deepseek_response(context)
    
    assert result.success
    assert result.modified  # Should be modified
    
    # Check that markers were parsed
    llm_response = result.context.metadata["llm_response"]
    
    # Content should be cleaned (markers removed)
    assert "<｜tool" not in llm_response["content"]
    assert llm_response["content"] == "I'll delete that task."
    
    # Should have tool_calls
    assert len(llm_response["tool_calls"]) == 1
    tool_call = llm_response["tool_calls"][0]
    
    assert tool_call["function"]["name"] == "todo"
    assert '{"operation": "delete", "task_id": "task_002"}' in tool_call["function"]["arguments"]


@pytest.mark.asyncio
async def test_parse_deepseek_response_no_markers(validator_plugin):
    """Test hook passes through normal responses without markers."""
    
    normal_response = "Here is a normal response without any markers."
    
    context = HookContext(
        hook_type="post_llm_call",
        messages=[],
        request_id="test_req_002",
        session_id="test_session_002",
        metadata={
            "llm_response": {
                "content": normal_response,
                "tool_calls": []
            }
        }
    )
    
    result = await validator_plugin.parse_deepseek_response(context)
    
    assert result.success
    assert not result.modified  # Should NOT be modified
    
    # Content unchanged
    llm_response = result.context.metadata["llm_response"]
    assert llm_response["content"] == normal_response
    assert llm_response["tool_calls"] == []


@pytest.mark.asyncio
async def test_parse_deepseek_response_already_has_tool_calls(validator_plugin):
    """Test hook skips if response already has tool_calls."""
    
    context = HookContext(
        hook_type="post_llm_call",
        messages=[],
        request_id="test_req_003",
        session_id="test_session_003",
        metadata={
            "llm_response": {
                "content": "Some content",
                "tool_calls": [{"id": "call_123", "type": "function", "function": {"name": "test"}}]
            }
        }
    )
    
    result = await validator_plugin.parse_deepseek_response(context)
    
    assert result.success
    assert not result.modified  # Should skip


@pytest.mark.asyncio
async def test_parse_deepseek_response_ascii_markers(validator_plugin):
    """Test parsing ASCII variant of DeepSeek markers."""
    
    response_with_ascii = (
        "Processing task. "
        "<|tool_calls_begin|><|tool_call_begin|>todo<|tool_sep|>"
        '{"operation": "update", "task_id": "task_001", "status": "completed"}'
        "<|tool_call_end|><|tool_calls_end|>"
    )
    
    context = HookContext(
        hook_type="post_llm_call",
        messages=[],
        request_id="test_req_004",
        session_id="test_session_004",
        metadata={
            "llm_response": {
                "content": response_with_ascii,
                "tool_calls": []
            }
        }
    )
    
    result = await validator_plugin.parse_deepseek_response(context)
    
    assert result.success
    assert result.modified
    
    llm_response = result.context.metadata["llm_response"]
    assert "<|tool" not in llm_response["content"]
    assert len(llm_response["tool_calls"]) == 1
    assert llm_response["tool_calls"][0]["function"]["name"] == "todo"
