"""Tests for Context Summarizer plugin web UI."""
import inspect
import pytest
from plugins.context_summarizer.plugin import PLUGIN_FACTORY
from plugins.context_summarizer.web_endpoints import ContextSummarizerWebFactory


def test_factory_is_callable():
    """Test that PLUGIN_FACTORY is a callable that creates plugin instances."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    # PLUGIN_FACTORY is a function, not a class
    assert callable(PLUGIN_FACTORY), "PLUGIN_FACTORY should be callable"
    
    # Create minimal configs
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    
    # Instantiate the plugin
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    # Verify plugin is a hybrid plugin
    assert hasattr(plugin, "server"), "Plugin should have server attribute"
    assert hasattr(plugin, "web_factory"), "Plugin should have web_factory attribute"
    assert hasattr(plugin, "list_tools"), "Plugin should have list_tools method"
    assert hasattr(plugin, "on_pre_llm_call"), "Plugin should have on_pre_llm_call method"


def test_web_factory_type():
    """Test that plugin has web_factory and server attributes."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    # Plugin is hybrid with server and web_factory
    assert hasattr(plugin, "server"), "Plugin should have server"
    assert hasattr(plugin, "web_factory"), "Plugin should have web_factory"
    assert plugin.web_factory is not None


def test_shared_history_list():
    """Test that server and web factory share the same history list."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    # Server hooks and web_factory share summarization_history
    assert plugin.server._hooks_impl.summarization_history is plugin.web_factory.summarization_history


@pytest.mark.asyncio
async def test_get_history_empty():
    """Test getting history when empty."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    from unittest.mock import MagicMock
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    # Web endpoints require request with query_params
    mock_request = MagicMock()
    mock_request.query_params = {}
    result = await plugin.web_factory.get_history(mock_request)
    assert result['success'] is True
    assert result['events'] == []


@pytest.mark.asyncio
async def test_get_stats_empty():
    """Test getting stats when empty."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    from unittest.mock import Mock
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    # Web endpoints require request parameter
    mock_request = Mock()
    result = await plugin.web_factory.get_stats(mock_request)
    assert result['success'] is True
    assert result['total_events'] == 0


@pytest.mark.asyncio
async def test_render_panel():
    """Test rendering the panel endpoint."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    from fastapi import Request
    from unittest.mock import MagicMock
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    # Create mock request
    request = MagicMock(spec=Request)
    
    # Render panel via web_factory
    response = await plugin.web_factory.render_panel(request)
    
    # Verify response
    assert response.status_code == 200
    assert "Context Summarization History" in response.body.decode('utf-8')


def test_router_has_panel_endpoint():
    """Test that plugin has web router."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    # Plugin provides get_web_router() method
    router = plugin.get_web_router()
    assert router is not None, "Plugin should provide web router"

