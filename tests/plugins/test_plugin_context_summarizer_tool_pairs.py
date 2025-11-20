"""Test that context_summarizer keeps tool_calls/tool response pairs together."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.context_summarizer.plugin import PLUGIN_FACTORY


@pytest.fixture
def summarizer_plugin():
    """Create context summarizer plugin instance."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    return plugin.server._hooks_impl  # Return hooks implementation from server


@pytest.fixture
def mock_agent():
    """Create mock agent."""
    from agent_system.config.models import AgentSystemConfig, AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile
    
    agent = Mock()
    
    system_config = AgentSystemConfig()
    system_config.llm_system = LLMSystemConfig(
        models={
            'gpt-4': LLMModelConfig(
                provider='openai',
                model='gpt-4',
                context_window=100000
            )
        },
        profiles={
            'normal': LLMProfile(model_ref='gpt-4')
        }
    )
    
    agent_config = AgentConfig(llm_profile='normal')
    
    agent.system_config = system_config
    agent.agent_config = agent_config
    agent.agent_id = 'test-agent'
    
    # Mock next_internal_tool_request_id
    call_counter = 0
    async def mock_next_request_id(base_id: str) -> str:
        nonlocal call_counter
        call_counter += 1
        return f"{base_id}_{call_counter:03d}"
    
    agent.next_internal_tool_request_id = mock_next_request_id
    
    return agent


@pytest.fixture
def mock_llm():
    """Create mock LLM."""
    llm = AsyncMock()
    llm.chat = AsyncMock(return_value='Summary of conversation')
    llm.context_window = 100000
    return llm


def create_messages_with_tool_calls():
    """Create message sequence with tool calls and responses."""
    messages = [
        # System message
        ChatMessage(role='system', content='You are a helpful assistant'),
        
        # Old messages (should be summarized)
        ChatMessage(role='user', content='What is the weather?'),
        ChatMessage(role='assistant', content='', tool_calls=[
            {'id': 'call_1', 'type': 'function', 'function': {'name': 'get_weather', 'arguments': '{}'}}
        ]),
        ChatMessage(role='tool', content='{"temp": 20}', tool_call_id='call_1', name='get_weather'),
        ChatMessage(role='assistant', content='The temperature is 20°C'),
        
        ChatMessage(role='user', content='What about tomorrow?'),
        ChatMessage(role='assistant', content='', tool_calls=[
            {'id': 'call_2', 'type': 'function', 'function': {'name': 'get_forecast', 'arguments': '{}'}}
        ]),
        ChatMessage(role='tool', content='{"temp": 22}', tool_call_id='call_2', name='get_forecast'),
        ChatMessage(role='assistant', content='Tomorrow will be 22°C'),
        
        # Recent messages (should be preserved) - last 5
        ChatMessage(role='user', content='Tell me about Paris'),
        ChatMessage(role='assistant', content='', tool_calls=[
            {'id': 'call_3', 'type': 'function', 'function': {'name': 'search', 'arguments': '{"q":"Paris"}'}}
        ]),
        ChatMessage(role='tool', content='{"result": "Capital of France"}', tool_call_id='call_3', name='search'),
        ChatMessage(role='assistant', content='Paris is the capital of France'),
        ChatMessage(role='user', content='What is the population?'),
    ]
    return messages


@pytest.mark.asyncio
async def test_tool_pairs_stay_together(summarizer_plugin, mock_agent, mock_llm):
    """Test that tool_calls and their responses stay in the same category."""
    messages = create_messages_with_tool_calls()
    
    # Set preserve_recent to 5 to test the boundary
    summarizer_plugin.preserve_recent = 5
    summarizer_plugin.trigger_percentage = 0.01  # Low threshold to trigger summarization
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    # Convert ChatMessages to dicts for categorization
    messages_as_dicts = [msg.model_dump(exclude_none=True) for msg in messages]
    
    system_msgs, recent_msgs, old_msgs = summarizer_plugin._categorize_messages(messages_as_dicts)
    
    # Verify no orphaned tool responses
    # Check that every tool message has its corresponding assistant with tool_calls
    for msg in recent_msgs + old_msgs:
        if msg.get('role') == 'tool':
            tool_call_id = msg.get('tool_call_id')
            assert tool_call_id, "Tool message missing tool_call_id"
            
            # Find the assistant message with this tool_call
            found_tool_call = False
            for candidate in recent_msgs + old_msgs + system_msgs:
                if candidate.get('role') == 'assistant' and candidate.get('tool_calls'):
                    for tc in candidate['tool_calls']:
                        if isinstance(tc, dict) and tc.get('id') == tool_call_id:
                            found_tool_call = True
                            break
                if found_tool_call:
                    break
            
            assert found_tool_call, f"Tool response with id '{tool_call_id}' has no matching assistant tool call"
    
    # Verify that tool pairs are in the same category
    def find_category(msg_dict):
        """Find which category contains this message."""
        if msg_dict in system_msgs:
            return 'system'
        elif msg_dict in recent_msgs:
            return 'recent'
        elif msg_dict in old_msgs:
            return 'old'
        return None
    
    for msg in recent_msgs + old_msgs + system_msgs:
        if msg.get('role') == 'assistant' and msg.get('tool_calls'):
            assistant_category = find_category(msg)
            
            for tc in msg['tool_calls']:
                if isinstance(tc, dict):
                    tool_call_id = tc.get('id')
                    
                    # Find the corresponding tool response
                    for tool_msg in recent_msgs + old_msgs + system_msgs:
                        if tool_msg.get('role') == 'tool' and tool_msg.get('tool_call_id') == tool_call_id:
                            tool_category = find_category(tool_msg)
                            assert assistant_category == tool_category, \
                                f"Tool pair split: assistant in '{assistant_category}', tool in '{tool_category}'"


@pytest.mark.asyncio
async def test_summarization_preserves_tool_structure(summarizer_plugin, mock_agent, mock_llm):
    """Test that full summarization doesn't create orphaned tool responses."""
    messages = create_messages_with_tool_calls()
    
    summarizer_plugin.preserve_recent = 5
    summarizer_plugin.trigger_percentage = 0.01
    summarizer_plugin.min_reduction = 0.1  # Low threshold to allow summarization
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    if result.modified:
        new_messages = result.context.messages
        
        # Validate no orphaned tool responses
        pending_tool_calls = {}
        for i, msg in enumerate(new_messages):
            if msg.role == 'assistant' and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.id if hasattr(tc, 'id') else tc.get('id')
                    if tc_id:
                        pending_tool_calls[tc_id] = i
            
            elif msg.role == 'tool':
                tool_call_id = msg.tool_call_id
                assert tool_call_id, f"Tool message at index {i} missing tool_call_id"
                assert tool_call_id in pending_tool_calls, \
                    f"Orphaned tool response at index {i} with id '{tool_call_id}'"
                del pending_tool_calls[tool_call_id]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
