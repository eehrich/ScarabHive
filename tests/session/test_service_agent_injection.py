"""
Tests for agent_injection service.

Tests the centralized session_service injection functionality used by
CLI, API, and agent_run entry points.
"""

import pytest
from unittest.mock import MagicMock

from agent_system.services.agent_injection import (
    inject_session_service_into_agents,
    inject_dependencies_into_agents,
)


@pytest.fixture
def mock_registry():
    """Create mock MCPRegistry."""
    registry = MagicMock()
    registry.list.return_value = []
    return registry


@pytest.fixture
def mock_agent():
    """Create mock Agent instance."""
    from agent_system.servers.agent.server import Agent
    
    agent = MagicMock(spec=Agent)
    agent.name = "test_agent"
    agent._session_service = None
    return agent


@pytest.fixture
def mock_session_service():
    """Create mock SessionService."""
    service = MagicMock()
    service.session_manager = MagicMock()
    return service


class TestInjectSessionServiceIntoAgents:
    """Test inject_session_service_into_agents function."""
    
    def test_inject_into_empty_registry(self, mock_registry, mock_session_service):
        """Test injection when registry is empty."""
        mock_registry.list.return_value = []
        
        count = inject_session_service_into_agents(mock_registry, mock_session_service)
        
        assert count == 0
        mock_registry.list.assert_called_once()
    
    def test_inject_into_single_agent(self, mock_registry, mock_agent, mock_session_service):
        """Test injection into a single agent."""
        mock_registry.list.return_value = ["test_agent"]
        mock_registry.get.return_value = mock_agent
        
        count = inject_session_service_into_agents(mock_registry, mock_session_service)
        
        assert count == 1
        assert mock_agent._session_service == mock_session_service
        mock_registry.get.assert_called_once_with("test_agent")
    
    def test_inject_into_multiple_agents(self, mock_registry, mock_session_service):
        """Test injection into multiple agents."""
        from agent_system.servers.agent.server import Agent
        
        agent1 = MagicMock(spec=Agent)
        agent1.name = "agent1"
        agent1._session_service = None
        
        agent2 = MagicMock(spec=Agent)
        agent2.name = "agent2"
        agent2._session_service = None
        
        mock_registry.list.return_value = ["agent1", "agent2"]
        mock_registry.get.side_effect = lambda name: agent1 if name == "agent1" else agent2
        
        count = inject_session_service_into_agents(mock_registry, mock_session_service)
        
        assert count == 2
        assert agent1._session_service == mock_session_service
        assert agent2._session_service == mock_session_service
    
    def test_skip_non_agent_servers(self, mock_registry, mock_session_service):
        """Test that non-Agent MCP servers are skipped."""
        from agent_system.servers.agent.server import Agent
        
        agent = MagicMock(spec=Agent)
        agent.name = "real_agent"
        agent._session_service = None
        
        # Create a non-Agent MCP server (no spec, different type)
        mcp_server = MagicMock()
        mcp_server.name = "web_scraper"
        # Make isinstance check fail by setting __class__ explicitly
        type(mcp_server).__name__ = "MCPServer"
        
        mock_registry.list.return_value = ["real_agent", "web_scraper"]
        mock_registry.get.side_effect = lambda name: agent if name == "real_agent" else mcp_server
        
        count = inject_session_service_into_agents(mock_registry, mock_session_service)
        
        # Only the Agent should be counted as injected
        assert count == 1
        assert agent._session_service == mock_session_service
        # MagicMock allows attribute setting, so we can't test hasattr
        # Instead verify the count is correct (only 1 agent processed)
    
    def test_handle_registry_get_exception(self, mock_registry, mock_session_service):
        """Test handling of exceptions during registry.get()."""
        mock_registry.list.return_value = ["broken_agent", "good_agent"]
        
        from agent_system.servers.agent.server import Agent
        good_agent = MagicMock(spec=Agent)
        good_agent._session_service = None
        
        def get_side_effect(name):
            if name == "broken_agent":
                raise KeyError("Agent not found")
            return good_agent
        
        mock_registry.get.side_effect = get_side_effect
        
        # Should not raise, should continue with good_agent
        count = inject_session_service_into_agents(mock_registry, mock_session_service)
        
        assert count == 1
        assert good_agent._session_service == mock_session_service
    
    def test_overwrite_existing_session_service(self, mock_registry, mock_agent, mock_session_service):
        """Test that existing session_service is overwritten."""
        old_service = MagicMock()
        mock_agent._session_service = old_service
        
        mock_registry.list.return_value = ["test_agent"]
        mock_registry.get.return_value = mock_agent
        
        count = inject_session_service_into_agents(mock_registry, mock_session_service)
        
        assert count == 1
        assert mock_agent._session_service == mock_session_service
        assert mock_agent._session_service != old_service


class TestInjectDependenciesIntoAgents:
    """Test inject_dependencies_into_agents function."""
    
    def test_inject_session_service_only(self, mock_registry, mock_agent, mock_session_service):
        """Test injecting only session_service."""
        mock_registry.list.return_value = ["test_agent"]
        mock_registry.get.return_value = mock_agent
        
        results = inject_dependencies_into_agents(
            mock_registry,
            session_service=mock_session_service
        )
        
        assert results['_session_service'] == 1
        assert mock_agent._session_service == mock_session_service
    
    def test_inject_multiple_dependencies(self, mock_registry, mock_agent):
        """Test injecting multiple dependencies."""
        custom_service = MagicMock()
        session_service = MagicMock()
        
        mock_registry.list.return_value = ["test_agent"]
        mock_registry.get.return_value = mock_agent
        
        results = inject_dependencies_into_agents(
            mock_registry,
            session_service=session_service,
            custom_service=custom_service
        )
        
        assert results['_session_service'] == 1
        assert results['_custom_service'] == 1
        assert mock_agent._session_service == session_service
        assert mock_agent._custom_service == custom_service
    
    def test_auto_add_underscore_prefix(self, mock_registry, mock_agent):
        """Test that dependencies without underscore prefix get it added."""
        service = MagicMock()
        
        mock_registry.list.return_value = ["test_agent"]
        mock_registry.get.return_value = mock_agent
        
        results = inject_dependencies_into_agents(
            mock_registry,
            my_service=service  # No underscore prefix
        )
        
        assert '_my_service' in results
        assert results['_my_service'] == 1
        assert mock_agent._my_service == service
    
    def test_preserve_existing_underscore_prefix(self, mock_registry, mock_agent):
        """Test that dependencies with underscore prefix are preserved."""
        service = MagicMock()
        
        mock_registry.list.return_value = ["test_agent"]
        mock_registry.get.return_value = mock_agent
        
        results = inject_dependencies_into_agents(
            mock_registry,
            _custom_service=service  # Already has underscore
        )
        
        assert '_custom_service' in results
        assert results['_custom_service'] == 1
        assert mock_agent._custom_service == service
    
    def test_skip_none_session_service(self, mock_registry, mock_agent):
        """Test that None session_service is skipped."""
        custom_service = MagicMock()
        
        mock_registry.list.return_value = ["test_agent"]
        mock_registry.get.return_value = mock_agent
        
        # Start with no _session_service attribute
        if hasattr(mock_agent, '_session_service'):
            delattr(mock_agent, '_session_service')
        
        results = inject_dependencies_into_agents(
            mock_registry,
            session_service=None,
            custom_service=custom_service
        )
        
        assert '_session_service' not in results
        assert '_custom_service' in results
        # MagicMock allows attribute setting, so check results instead
        assert results['_custom_service'] == 1
    
    def test_empty_dependencies(self, mock_registry):
        """Test with no dependencies to inject."""
        mock_registry.list.return_value = []
        
        results = inject_dependencies_into_agents(mock_registry)
        
        assert results == {}
