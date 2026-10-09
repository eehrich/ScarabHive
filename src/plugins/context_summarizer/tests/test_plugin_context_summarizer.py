"""Tests for context_summarizer plugin."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from agent_system.hooks import HookContext, HookType
from plugins.context_summarizer.plugin import PLUGIN_FACTORY


@pytest.fixture
def summarizer_plugin():
    """Create context summarizer plugin instance."""
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    
    # Create minimal configs
    system_config = AgentSystemConfig()
    server_config = ToolServerConfig()
    
    # PLUGIN_FACTORY returns ContextSummarizerHybridPlugin
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, server_config)
    return plugin.server._hooks_impl  # Return the hooks implementation from server


@pytest.fixture
def mock_agent():
    """Create mock agent with proper config structure."""
    from unittest.mock import Mock
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
    
    # Mock next_internal_tool_request_id to generate unique suffixes
    call_counter = 0
    async def mock_next_request_id(base_id: str) -> str:
        nonlocal call_counter
        call_counter += 1
        return f"{base_id}_{call_counter:03d}"
    
    agent.next_internal_tool_request_id = mock_next_request_id
    
    return agent


@pytest.fixture
def mock_llm():
    """Create mock LLM for testing."""
    llm = AsyncMock()
    llm.context_window = 100000  # Match the model config context_window
    llm.generate = AsyncMock(return_value={
        'content': 'This is a concise summary of the conversation discussing project plans and technical details.'
    })
    return llm


def create_test_messages(count: int) -> list:
    """Create test ChatMessage objects for summarization."""
    from agent_system.llm.models import ChatMessage
    messages = []
    for i in range(count):
        messages.append(ChatMessage(
            role='user' if i % 2 == 0 else 'assistant',
            content=f'Test message {i} with some content to make it longer ' * 10
        ))
    return messages


@pytest.mark.asyncio
async def test_plugin_initialization(summarizer_plugin):
    """Test that plugin initializes correctly."""
    assert summarizer_plugin.name == 'context_summarizer'
    assert summarizer_plugin.trigger_percentage == 0.60  # Changed from trigger_tokens
    assert summarizer_plugin.chunk_size == 10
    assert summarizer_plugin.preserve_recent == 10
    # schema.yaml declares 'turbo' (enum: turbo/normal/chat/think). The test
    # used to say 'fast' -- the second, wrong default in server.py, which does
    # not exist in config/llm.yaml. The invalid profile made client creation
    # fail and the summarizer silently fell back to the agent LLM.
    assert summarizer_plugin.llm_profile == 'turbo'


@pytest.mark.asyncio
async def test_below_threshold_no_summarization(summarizer_plugin, mock_agent, mock_llm):
    """Test that messages below threshold are not summarized."""
    messages = create_test_messages(5)  # Only 5 messages = low token count
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] in ['below_threshold', 'insufficient_old_messages', 'no_context_window']
    mock_llm.generate.assert_not_called()


@pytest.mark.asyncio
async def test_empty_messages(summarizer_plugin, mock_agent, mock_llm):
    """Test handling of empty message list."""
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        agent=mock_agent,
        messages=[],
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] == 'no_messages'


@pytest.mark.asyncio
async def test_chunked_summarization(summarizer_plugin, mock_agent, mock_llm):
    """Test that large message sets are summarized in chunks."""
    messages = create_test_messages(50)
    summarizer_plugin.trigger_percentage = 0.001  # Very low trigger for testing
    summarizer_plugin.chunk_size = 10
    summarizer_plugin.preserve_recent = 5
    
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
        summary_stats = result.metadata['summarization']
        assert summary_stats['total_chunks'] > 1
        assert summary_stats['summary_count'] > 0


@pytest.mark.asyncio
async def test_status_messages_published(summarizer_plugin, mock_agent, mock_llm, monkeypatch):
    """Test that status messages are published during summarization."""
    from agent_system.tools.status import get_status_bus
    
    published_statuses = []
    
    async def mock_publish(event):
        published_statuses.append({
            'server': event.server,
            'message': event.message,
            'phase': event.phase,
            'request_id': event.request_id
        })
    
    status_bus = get_status_bus()
    monkeypatch.setattr(status_bus, 'publish', mock_publish)
    
    try:
        messages = create_test_messages(300)
        summarizer_plugin.trigger_percentage = 0.01
        
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id='test-status-123',
            session_id='session-1',
            agent=mock_agent,
            messages=messages,
            llm=mock_llm
        )
        
        result = await summarizer_plugin.summarize_context(context)

        assert result.modified, \
            "summarization never ran — the status assertions below would be vacuous"
        if result.modified:
            # StatusScope only sends START and END (no manual PROGRESS anymore)
            assert len(published_statuses) >= 2, "Should have at least START and END status messages"
            
            # Check for START message
            start_messages = [s for s in published_statuses if s['phase'].value == 'start']
            assert len(start_messages) > 0, "Should have START status message"
            assert start_messages[0]['server'] == 'context_summarizer'
            assert 'Summarizing' in start_messages[0]['message']
            assert 'older messages' in start_messages[0]['message']
            
            end_messages = [s for s in published_statuses if s['phase'].value == 'end']
            assert len(end_messages) > 0, "Should have END status message"
            assert end_messages[0]['server'] == 'context_summarizer'
            # The end line must carry the outcome, not a generic 'completed'.
            msg = end_messages[0]['message']
            assert msg.startswith('Summarized ') and 'tokens' in msg, msg
            
            # CRITICAL: Verify unique request_id with suffix (like tool calls)
            # All summarizer status messages should use test-status-123_001 instead of test-status-123
            summarizer_events = [s for s in published_statuses if s['server'] == 'context_summarizer']
            for event in summarizer_events:
                assert event['request_id'] == 'test-status-123_001', \
                    f"Expected unique request_id 'test-status-123_001', got '{event['request_id']}'"
    
    finally:
        pass


@pytest.mark.asyncio
async def test_rejected_summarization_status_says_not_applied(
        summarizer_plugin, mock_agent, mock_llm, monkeypatch):
    """A run whose reduction was refused must not read as 'completed'.

    That was the shipped behaviour: the scope's static end message fired for
    the rejected branch too, so a no-op looked like a success in the UI.
    """
    from agent_system.tools.status import get_status_bus

    published = []

    async def mock_publish(event):
        published.append(event)

    monkeypatch.setattr(get_status_bus(), 'publish', mock_publish)

    messages = create_test_messages(300)
    summarizer_plugin.trigger_percentage = 0.01
    # The POTENTIAL reduction (~0.97) passes the pre-check outside the scope;
    # bloated summaries make the ACHIEVED reduction negative — the in-scope
    # rejected branch. NB: the chunk path calls summarizer_llm.chat(), not
    # generate() — mocking generate would leave str(AsyncMock()) as summary.
    mock_llm.chat = AsyncMock(return_value='padding words ' * 4000)

    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-rejected-1',
        session_id='session-rej',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm,
    )

    result = await summarizer_plugin.summarize_context(context)

    assert result.modified is False
    assert result.metadata.get('reason') == 'insufficient_reduction', result.metadata
    ends = [e for e in published
            if e.server == 'context_summarizer' and e.phase.value == 'end']
    assert len(ends) == 1, [(e.server, e.phase.value, e.message) for e in published]
    assert ends[0].message.startswith('Not applied:'), ends[0].message


@pytest.mark.asyncio
async def test_max_messages_trigger(summarizer_plugin, mock_agent, mock_llm):
    """Test that summarization triggers when message count exceeds max_messages."""
    messages = create_test_messages(30)
    summarizer_plugin.max_messages = 20  # Trigger when > 20 messages
    summarizer_plugin.trigger_percentage = 0.99  # Token trigger should NOT fire (very high)
    summarizer_plugin.preserve_recent = 5

    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-maxmsg-1',
        session_id='session-maxmsg',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )

    result = await summarizer_plugin.summarize_context(context)

    # Should have triggered due to message count (30 > 20)
    # Either modified (summarization done) or skipped for other reasons,
    # but NOT 'below_threshold'
    if not result.modified:
        assert result.metadata.get('reason') != 'below_threshold', \
            f"Should not be below_threshold with {len(messages)} messages > max_messages=20"


@pytest.mark.asyncio
async def test_max_messages_below_threshold(summarizer_plugin, mock_agent, mock_llm):
    """Test that max_messages does NOT trigger when message count is below limit."""
    messages = create_test_messages(10)
    summarizer_plugin.max_messages = 50  # Well above message count
    summarizer_plugin.trigger_percentage = 0.99  # Token trigger also won't fire

    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-maxmsg-2',
        session_id='session-maxmsg-2',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )

    result = await summarizer_plugin.summarize_context(context)

    assert result.success is True
    assert result.modified is False
    # Should be below threshold (both token and message count)
    assert result.metadata.get('reason') in ['below_threshold', 'no_context_window', 'insufficient_old_messages']


@pytest.mark.asyncio
async def test_max_messages_disabled_by_default(summarizer_plugin, mock_agent, mock_llm):
    """Test that max_messages=0 (default) disables message-count trigger."""
    messages = create_test_messages(10)
    summarizer_plugin.max_messages = 0  # Disabled
    summarizer_plugin.trigger_percentage = 0.99  # Token trigger won't fire either

    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-maxmsg-3',
        session_id='session-maxmsg-3',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )

    result = await summarizer_plugin.summarize_context(context)

    assert result.success is True
    assert result.modified is False


@pytest.mark.asyncio
async def test_max_messages_per_agent_override(summarizer_plugin, mock_agent, mock_llm):
    """Test that max_messages can be overridden per agent via hook_config."""
    messages = create_test_messages(15)
    summarizer_plugin.max_messages = 0  # Globally disabled
    summarizer_plugin.trigger_percentage = 0.99  # Token trigger won't fire

    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-maxmsg-4',
        session_id='session-maxmsg-4',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm,
        hook_config={'max_messages': 10},  # Agent-level override: trigger > 10
    )

    result = await summarizer_plugin.summarize_context(context)

    # 15 messages > 10 max_messages override → should trigger
    if not result.modified:
        assert result.metadata.get('reason') != 'below_threshold', \
            "Should not be below_threshold with 15 messages > hook_config max_messages=10"
