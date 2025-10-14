"""Tests for context_optimizer web UI."""

import pytest
from pathlib import Path
from unittest.mock import Mock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from agent_system.config.models import AgentSystemConfig, MCPConfig


@pytest.fixture
def sample_history():
    """Sample summarization history for testing."""
    return [
        {
            'timestamp': '2025-01-14T10:00:00',
            'session_id': 'test-session-1',
            'request_id': 'req-001',
            'strategy': 'truncate',
            'original_message_count': 50,
            'summarized_message_count': 30,
            'messages_summarized': 20,
            'original_tokens': 10000,
            'new_tokens': 6000,
            'tokens_saved': 4000,
            'reduction_ratio': 0.4,
            'before_messages': [{'role': 'user', 'content': 'test'}],
            'after_messages': [{'role': 'user', 'content': 'test'}]
        },
        {
            'timestamp': '2025-01-14T11:00:00',
            'session_id': 'test-session-2',
            'request_id': 'req-002',
            'strategy': 'truncate',
            'original_message_count': 40,
            'summarized_message_count': 25,
            'messages_summarized': 15,
            'original_tokens': 8000,
            'new_tokens': 5000,
            'tokens_saved': 3000,
            'reduction_ratio': 0.375,
            'before_messages': [{'role': 'user', 'content': 'test2'}],
            'after_messages': [{'role': 'user', 'content': 'test2'}]
        }
    ]


@pytest.fixture
def web_endpoints(sample_history):
    """Create web endpoints instance with sample history."""
    from src.plugins.context_optimizer.web_endpoints import ContextOptimizerWebEndpoints
    
    system_config = Mock(spec=AgentSystemConfig)
    mcp_config = Mock(spec=MCPConfig)
    
    endpoints = ContextOptimizerWebEndpoints(
        name='context_optimizer',
        system_config=system_config,
        mcp_config=mcp_config,
        summarization_history=sample_history
    )
    
    return endpoints


@pytest.fixture
def test_app(web_endpoints):
    """Create test FastAPI app with registered routes."""
    from fastapi import APIRouter
    
    app = FastAPI()
    router = APIRouter()
    web_endpoints.register_routes(router, prefix='/context-optimizer')
    app.include_router(router)
    
    return TestClient(app)


def test_get_panel(test_app):
    """Test panel HTML endpoint."""
    response = test_app.get('/context-optimizer/panel')
    
    assert response.status_code == 200
    assert 'text/html' in response.headers['content-type']
    assert 'Context Summarization History' in response.text


def test_get_history_all(test_app, sample_history):
    """Test getting all history."""
    response = test_app.get('/context-optimizer/history')
    
    assert response.status_code == 200
    data = response.json()
    
    assert data['success'] is True
    assert data['count'] == 2
    assert len(data['history']) == 2
    
    # Check order (most recent first)
    assert data['history'][0]['session_id'] == 'test-session-2'
    assert data['history'][1]['session_id'] == 'test-session-1'


def test_get_history_filtered_by_session(test_app):
    """Test filtering history by session."""
    response = test_app.get('/context-optimizer/history?session_id=test-session-1')
    
    assert response.status_code == 200
    data = response.json()
    
    assert data['success'] is True
    assert data['count'] == 1
    assert data['history'][0]['session_id'] == 'test-session-1'


def test_get_history_with_limit(test_app):
    """Test history limit parameter."""
    response = test_app.get('/context-optimizer/history?limit=1')
    
    assert response.status_code == 200
    data = response.json()
    
    assert data['success'] is True
    assert data['count'] == 1


def test_get_stats(test_app):
    """Test getting aggregate statistics."""
    response = test_app.get('/context-optimizer/stats')
    
    assert response.status_code == 200
    data = response.json()
    
    assert data['success'] is True
    assert data['total_summarizations'] == 2
    assert data['total_messages_summarized'] == 35  # 20 + 15
    assert data['total_tokens_saved'] == 7000  # 4000 + 3000
    assert len(data['sessions']) == 2


def test_get_stats_empty(test_app):
    """Test stats with empty history."""
    # Clear history
    test_app.delete('/context-optimizer/history')
    
    response = test_app.get('/context-optimizer/stats')
    
    assert response.status_code == 200
    data = response.json()
    
    assert data['success'] is True
    assert data['total_summarizations'] == 0
    assert data['total_messages_summarized'] == 0
    assert data['total_tokens_saved'] == 0
    assert len(data['sessions']) == 0


def test_clear_history(test_app, sample_history):
    """Test clearing history."""
    response = test_app.delete('/context-optimizer/history')
    
    assert response.status_code == 200
    data = response.json()
    
    assert data['success'] is True
    assert data['cleared_count'] == 2
    
    # Verify history is empty
    response = test_app.get('/context-optimizer/history')
    data = response.json()
    assert data['count'] == 0


def test_history_tracking_in_hook():
    """Test that hooks properly track summarization events."""
    from src.plugins.context_optimizer.hooks import ContextOptimizerPlugin
    from agent_system.hooks import HookContext, HookType
    
    plugin_dir = Path(__file__).parent.parent / 'src' / 'plugins' / 'context_optimizer'
    history = []
    
    plugin = ContextOptimizerPlugin(plugin_dir, summarization_history=history)
    
    # Create test context with many messages (to trigger optimization)
    messages = [
        {'role': 'system', 'content': 'You are a helpful assistant'},
        *[{'role': 'user', 'content': f'Message {i}' * 1000} for i in range(100)]  # Lots of messages
    ]
    
    # Mock LLM with small context window
    mock_llm = Mock()
    mock_llm.context_window = 1000  # Very small to force optimization
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-req',
        session_id='test-session',
        agent=None,
        agent_name='test-agent',
        messages=messages,
        llm=mock_llm
    )
    
    # Run optimization (async)
    import asyncio
    result = asyncio.run(plugin.optimize_context(context))
    
    # Should have modified messages
    assert result.modified is True
    
    # Should have recorded event in history
    assert len(history) == 1
    event = history[0]
    
    assert event['session_id'] == 'test-session'
    assert event['strategy'] == 'truncate'
    assert event['original_message_count'] > event['summarized_message_count']
    assert event['tokens_saved'] > 0
    assert 'before_messages' in event
    assert 'after_messages' in event


def test_plugin_factory():
    """Test that plugin factory returns hybrid plugin tuple."""
    from src.plugins.context_optimizer.plugin import PLUGIN_FACTORY
    
    result = PLUGIN_FACTORY()
    
    # Should return tuple for hybrid plugin
    assert isinstance(result, tuple)
    assert len(result) == 2
    
    hooks_plugin, web_factory = result
    
    # First element should be hooks plugin
    assert hasattr(hooks_plugin, 'optimize_context')
    
    # Second element should be web factory function
    assert callable(web_factory)
    
    # Test web factory
    system_config = Mock(spec=AgentSystemConfig)
    mcp_config = Mock(spec=MCPConfig)
    web_plugin = web_factory('context_optimizer', system_config, mcp_config)
    
    assert hasattr(web_plugin, 'register_routes')
