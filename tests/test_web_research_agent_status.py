"""Test status events for web_research_agent."""

import pytest
from unittest.mock import AsyncMock, patch

from plugins.web_research_agent.server import WebResearchAgent
from agent_system.mcp.status import PHASE_START, PHASE_END, PHASE_ERROR


class TestWebResearchAgentStatusEvents:
    """Test that web_research_agent publishes proper status events."""

    @pytest.mark.asyncio
    async def test_research_status_events(self):
        """Test that research action publishes proper status events."""
        agent = WebResearchAgent("test_research_agent")
        
        # Mock the underlying run method to avoid actual execution
        agent.run = AsyncMock(return_value={"summary": "Research completed"})
        
        published_events = []
        
        # Mock publish_status to capture events
        async def mock_publish_status(server, message, request_id=None, phase=None, **kwargs):
            published_events.append({
                "server": server,
                "message": message,
                "request_id": request_id,
                "phase": phase
            })
        
        with patch('plugins.web_research_agent.server.publish_status', mock_publish_status):
            result = await agent.call("research", {
                "topic": "artificial intelligence",
                "request_id": "test-123"
            })
        
        # Verify the call succeeded
        assert "summary" in result
        
        # Verify status events were published
        assert len(published_events) == 2
        
        # Check START event
        start_event = published_events[0]
        assert start_event["server"] == "test_research_agent"
        assert "Research started: artificial intelligence" in start_event["message"]
        assert start_event["request_id"] == "test-123"
        assert start_event["phase"] == PHASE_START
        
        # Check END event
        end_event = published_events[1]
        assert end_event["server"] == "test_research_agent"
        assert "Research completed: artificial intelligence" in end_event["message"]
        assert end_event["request_id"] == "test-123"
        assert end_event["phase"] == PHASE_END

    @pytest.mark.asyncio
    async def test_fact_check_status_events(self):
        """Test that fact_check action publishes proper status events."""
        agent = WebResearchAgent("test_fact_checker")
        
        # Mock the underlying run method
        agent.run = AsyncMock(return_value={"summary": "Fact check completed"})
        
        published_events = []
        
        async def mock_publish_status(server, message, request_id=None, phase=None, **kwargs):
            published_events.append({
                "server": server,
                "message": message,
                "request_id": request_id,
                "phase": phase
            })
        
        with patch('plugins.web_research_agent.server.publish_status', mock_publish_status):
            result = await agent.call("fact_check", {
                "claim": "The Earth is flat",
                "request_id": "fact-456"
            })
        
        # Verify the call succeeded
        assert "summary" in result
        
        # Verify status events
        assert len(published_events) == 2
        
        start_event = published_events[0]
        assert start_event["server"] == "test_fact_checker"
        assert "Fact-check started: The Earth is flat" in start_event["message"]
        assert start_event["request_id"] == "fact-456"
        assert start_event["phase"] == PHASE_START

    @pytest.mark.asyncio
    async def test_compare_sources_status_events(self):
        """Test that compare_sources action publishes proper status events."""
        agent = WebResearchAgent("test_comparer")
        
        agent.run = AsyncMock(return_value={"summary": "Sources compared"})
        
        published_events = []
        
        async def mock_publish_status(server, message, request_id=None, phase=None, **kwargs):
            published_events.append({
                "server": server,
                "message": message,
                "request_id": request_id,
                "phase": phase
            })
        
        with patch('plugins.web_research_agent.server.publish_status', mock_publish_status):
            result = await agent.call("compare_sources", {
                "topic": "climate change",
                "source_urls": ["https://site1.com", "https://site2.com"],
                "request_id": "compare-789"
            })
        
        # Verify status events
        assert len(published_events) == 2
        
        start_event = published_events[0]
        assert start_event["server"] == "test_comparer"
        assert "Compare sources started: climate change" in start_event["message"]
        assert start_event["request_id"] == "compare-789"
        assert start_event["phase"] == PHASE_START

    @pytest.mark.asyncio
    async def test_status_events_without_request_id(self):
        """Test that status events work even without request_id."""
        agent = WebResearchAgent("test_no_request_id")
        
        agent.run = AsyncMock(return_value={"summary": "Research completed"})
        
        published_events = []
        
        async def mock_publish_status(server, message, request_id=None, phase=None, **kwargs):
            published_events.append({
                "server": server,
                "message": message,
                "request_id": request_id,
                "phase": phase
            })
        
        with patch('plugins.web_research_agent.server.publish_status', mock_publish_status):
            result = await agent.call("research", {"topic": "test without request_id"})
        
        # Verify events are still published with None request_id
        assert len(published_events) == 2
        assert published_events[0]["request_id"] is None
        assert published_events[1]["request_id"] is None
