"""
Test tool filtering patterns with servername/toolname format.

Tests that patterns like "web_scraper/*" work correctly with fnmatch.
"""

import pytest
from agent_system.servers.agent.tool_discovery import ToolDiscoveryService
from agent_system.config.models import AgentConfig, ToolConfig
from unittest.mock import Mock, AsyncMock


class TestToolFilteringPatterns:
    """Test tool name pattern matching."""
    
    @pytest.fixture
    def mock_tool_integration_manager(self):
        """Create mock tool integration manager."""
        manager = Mock()
        manager.tool_integration = None
        manager.get_available_tools = AsyncMock(return_value=[])
        return manager
    
    def create_discovery_service(self, allowed_patterns, tool_manager):
        """Helper to create tool discovery service."""
        config = AgentConfig(
            llm_profile="test",
            tools=ToolConfig(allowed=allowed_patterns, blocked=[])
        )
        return ToolDiscoveryService(
            agent_name="test_agent",
            agent_config=config,
            tool_integration_manager=tool_manager,
            registry=None
        )
    
    def test_server_wildcard_pattern(self, mock_tool_integration_manager):
        """Test that web_scraper/* matches web_scraper/web_scraper."""
        service = self.create_discovery_service(
            ["web_scraper/*"],
            mock_tool_integration_manager
        )
        
        # Test the pattern matching
        assert service._matches_any_pattern("web_scraper/web_scraper", ["web_scraper/*"])
        assert service._matches_any_pattern("web_scraper/extract_text", ["web_scraper/*"])
        assert not service._matches_any_pattern("duckduckgo_search/search", ["web_scraper/*"])
    
    def test_complex_wildcard_pattern(self, mock_tool_integration_manager):
        """Test complex pattern like web_sc*/*scrape*."""
        service = self.create_discovery_service(
            ["web_sc*/*scrape*"],
            mock_tool_integration_manager
        )
        
        assert service._matches_any_pattern("web_scraper/web_scraper", ["web_sc*/*scrape*"])
        assert service._matches_any_pattern("web_scraper/scrape_content", ["web_sc*/*scrape*"])
        assert not service._matches_any_pattern("web_scraper/extract_text", ["web_sc*/*scrape*"])
    
    def test_exact_match_pattern(self, mock_tool_integration_manager):
        """Test exact tool name matching."""
        service = self.create_discovery_service(
            ["web_scraper/web_scraper"],
            mock_tool_integration_manager
        )
        
        assert service._matches_any_pattern("web_scraper/web_scraper", ["web_scraper/web_scraper"])
        assert not service._matches_any_pattern("web_scraper/extract_text", ["web_scraper/web_scraper"])
    
    def test_multiple_patterns(self, mock_tool_integration_manager):
        """Test multiple patterns."""
        service = self.create_discovery_service(
            ["web_scraper/*", "duckduckgo_search/*"],
            mock_tool_integration_manager
        )

        assert service._matches_any_pattern("web_scraper/web_scraper", ["web_scraper/*", "duckduckgo_search/*"])
        assert service._matches_any_pattern("duckduckgo_search/search", ["web_scraper/*", "duckduckgo_search/*"])
        assert not service._matches_any_pattern("weather/get_forecast", ["web_scraper/*", "duckduckgo_search/*"])

    def test_dotted_external_requires_dot_form(self, mock_tool_integration_manager):
        """Discovery gate STRICT: dotted external tool names ('server.tool')
        pass ONLY via the dot form ('server.*' / exact) -- not via
        'server/*' or the bare server name. Regression test against a
        loosening of the security gate when the matcher changes."""
        service = self.create_discovery_service(
            ["weather.*"], mock_tool_integration_manager
        )
        # The slash form and the bare name do NOT cover dotted externals
        assert not service._matches_any_pattern("weather.get_forecast", ["weather/*"])
        assert not service._matches_any_pattern("weather.get_forecast", ["weather"])
        # The dot form and an exact match cover them
        assert service._matches_any_pattern("weather.get_forecast", ["weather.*"])
        assert service._matches_any_pattern("weather.get_forecast", ["weather.get_forecast"])
        # The server itself still passes (pass-through to tool filtering)
        assert service._matches_any_pattern("weather", ["weather/*"])
        assert service._matches_any_pattern("weather", ["weather"])
        assert service._matches_any_pattern("weather", ["weather/get_forecast"])

    def test_question_mark_and_seq_globs(self, mock_tool_integration_manager):
        """'?' and '[seq]' globs match (fnmatch runs for all glob metacharacters,
        not just '*')."""
        service = self.create_discovery_service(
            ["ssh_control_v?"], mock_tool_integration_manager
        )
        assert service._matches_any_pattern("ssh_control_v2", ["ssh_control_v?"])
        assert not service._matches_any_pattern("ssh_control_v22", ["ssh_control_v?"])
        assert service._matches_any_pattern("worker_3", ["worker_[0-9]"])
    
    def test_global_wildcard(self, mock_tool_integration_manager):
        """Test global wildcard matches everything."""
        service = self.create_discovery_service(
            ["*"],
            mock_tool_integration_manager
        )
        
        # Global wildcard should match anything
        tools = ["web_scraper/scrape", "duckduckgo/search", "anything"]
        filtered = service._filter_tools_by_patterns(tools, ["*"])
        assert filtered == tools
    
    def test_server_name_still_matches(self, mock_tool_integration_manager):
        """Test that bare server names can still be matched."""
        service = self.create_discovery_service(
            ["web_scraper"],
            mock_tool_integration_manager
        )
        
        # Exact server name match
        assert service._matches_any_pattern("web_scraper", ["web_scraper"])
        # But not its tools (they need explicit pattern)
        assert not service._matches_any_pattern("web_scraper/web_scraper", ["web_scraper"])
