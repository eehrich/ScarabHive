"""Tests for Context Summarizer plugin web UI."""
import inspect
import pytest
from plugins.context_summarizer.plugin import PLUGIN_FACTORY
from plugins.context_summarizer.web_endpoints import ContextSummarizerWebFactory


def test_factory_is_class():
    """Test that PLUGIN_FACTORY is a class that can be instantiated."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    # Verify PLUGIN_FACTORY is a class
    assert inspect.isclass(PLUGIN_FACTORY), "PLUGIN_FACTORY should be a class"
    
    # Create minimal configs
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    
    # Instantiate the plugin
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    # Verify plugin has hook capabilities
    assert hasattr(plugin, "get_hooks"), "Plugin should have get_hooks method"
    assert hasattr(plugin, "execute_hook"), "Plugin should have execute_hook method"
    
    # Verify plugin has web capabilities
    assert hasattr(plugin, "get_web_router"), "Plugin should have get_web_router method"
    assert hasattr(plugin, "get_panels"), "Plugin should have get_panels method"
    assert hasattr(plugin, "get_static_assets"), "Plugin should have get_static_assets method"


def test_web_factory_type():
    """Test that web_factory is ContextSummarizerWebFactory."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    assert isinstance(plugin.web_factory, ContextSummarizerWebFactory)


def test_shared_history_list():
    """Test that hooks plugin and web factory share the same history list."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    assert plugin.hooks_plugin.summarization_history is plugin.web_factory.summarization_history


@pytest.mark.asyncio
async def test_get_history_empty():
    """Test getting history when empty."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    result = await plugin.web_factory.get_history()
    assert result['success'] is True
    assert result['events'] == []


@pytest.mark.asyncio
async def test_get_stats_empty():
    """Test getting stats when empty."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    result = await plugin.web_factory.get_stats()
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
    
    # Render panel
    response = await plugin.web_factory.render_panel(request)
    
    # Verify response
    assert response.status_code == 200
    assert "Context Summarization History" in response.body.decode('utf-8')


def test_router_has_panel_endpoint():
    """Test that router includes the panel endpoint."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    
    router = plugin.get_web_router()
    
    # Check routes - FastAPI combines prefix with path
    route_paths = [route.path for route in router.routes]
    
    assert "/plugins/context_summarizer/history" in route_paths
    assert "/plugins/context_summarizer/stats" in route_paths
    assert "/plugins/context_summarizer/panel" in route_paths
    
    # Verify prefix is set
    assert router.prefix == "/plugins/context_summarizer"

