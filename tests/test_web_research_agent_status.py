"""Test status events for web_research_agent."""

import pytest
from unittest.mock import AsyncMock, patch

from plugins.web_research_agent.server import WebResearchAgent
from agent_system.mcp.status import PHASE_START, PHASE_END, PHASE_ERROR, PHASE_PROGRESS


class TestWebResearchAgentStatusEvents:
    """Test that web_research_agent publishes proper status events."""

    @pytest.mark.asyncio
    async def test_research_status_events(self):
        """Test that research action publishes proper status events."""
        agent = WebResearchAgent("test_research_agent")
        
        # Mock the underlying _run_with_progress method to avoid actual execution
        agent._run_with_progress = AsyncMock(return_value={"summary": "Research completed"})
        
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
        
        # Mock the underlying _run_with_progress method
        agent._run_with_progress = AsyncMock(return_value={"summary": "Fact check completed"})
        
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
        
        agent._run_with_progress = AsyncMock(return_value={"summary": "Sources compared"})
        
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
        
        agent._run_with_progress = AsyncMock(return_value={"summary": "Research completed"})
        
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

    @pytest.mark.asyncio
    async def test_run_with_progress_events(self):
        """Test that _run_with_progress publishes progress events correctly."""
        agent = WebResearchAgent("progress_tester")
        
        # Mock run_events to yield test events
        async def mock_run_events(task):
            yield {"type": "start", "task": task}
            yield {"type": "mcp_call", "server": "duckduckgo_search", "action": "search", "params": {"query": "test"}}
            yield {"type": "mcp_result", "server": "duckduckgo_search", "action": "search", "result": "search results"}
            yield {"type": "mcp_call", "server": "web_scraper", "action": "scrape", "params": {"url": "https://example.com"}}
            yield {"type": "mcp_result", "server": "web_scraper", "action": "scrape", "result": "scraped content"}
            yield {"type": "final", "summary": "Research completed"}
            yield {"type": "end"}
        
        agent.run_events = mock_run_events
        
        published_events = []
        
        async def mock_publish_status(server, message, request_id=None, phase=None, **kwargs):
            published_events.append({
                "server": server,
                "message": message,
                "request_id": request_id,
                "phase": phase
            })
        
        with patch('plugins.web_research_agent.server.publish_status', mock_publish_status):
            result = await agent._run_with_progress("test research task", "Testing", "test-progress-123")
        
        # Verify progress events were published
        progress_events = [e for e in published_events if e["phase"] == PHASE_PROGRESS]
        assert len(progress_events) >= 4  # Starting analysis, step with tools, processing results, finalizing
        
        # Check specific progress messages
        progress_messages = [e["message"] for e in progress_events]
        assert any("Starting analysis" in msg for msg in progress_messages)
        assert any("Using duckduckgo_search" in msg for msg in progress_messages)
        assert any("Processing results from duckduckgo_search" in msg for msg in progress_messages)
        assert any("Using web_scraper" in msg for msg in progress_messages)
        assert any("Processing results from web_scraper" in msg for msg in progress_messages)
        assert any("Finalizing results" in msg for msg in progress_messages)
        
        # All progress events should have the correct request_id
        for event in progress_events:
            assert event["request_id"] == 'test-progress-123'
        
        # Verify final result
        assert result["summary"] == "Research completed"

    @pytest.mark.asyncio
    async def test_run_with_progress_error_handling(self):
        """Test that _run_with_progress handles errors correctly."""
        agent = WebResearchAgent("error_tester")
        
        # Mock run_events to yield an error
        async def mock_run_events_with_error(task):
            yield {"type": "start", "task": task}
            yield {"type": "error", "message": "Test error occurred"}
        
        agent.run_events = mock_run_events_with_error
        
        published_events = []
        
        async def mock_publish_status(server, message, request_id=None, phase=None, **kwargs):
            published_events.append({
                "server": server,
                "message": message,
                "request_id": request_id,
                "phase": phase
            })
        
        with patch('plugins.web_research_agent.server.publish_status', mock_publish_status):
            with pytest.raises(Exception, match="Test error occurred"):
                await agent._run_with_progress("test task", "Error Test", "error-123")
        
        # Verify error status was published
        error_events = [e for e in published_events if e["phase"] == PHASE_ERROR]
        assert len(error_events) >= 1
        
        error_event = error_events[0]
        assert error_event["request_id"] == 'error-123'
        assert 'Test error occurred' in error_event["message"]

    @pytest.mark.asyncio
    async def test_research_with_progress_integration(self):
        """Test that research method properly integrates with progress tracking."""
        agent = WebResearchAgent("integration_tester")
        
        # Mock _run_with_progress to return realistic results
        async def mock_run_with_progress(task_prompt, operation_name, request_id):
            return {
                "task": task_prompt,
                "calls": [{"server": "duckduckgo_search", "result": "search results"}],
                "summary": "Research completed successfully"
            }
        
        agent._run_with_progress = AsyncMock(side_effect=mock_run_with_progress)
        
        published_events = []
        
        async def mock_publish_status(server, message, request_id=None, phase=None, **kwargs):
            published_events.append({
                "server": server,
                "message": message,
                "request_id": request_id,
                "phase": phase
            })
        
        with patch('plugins.web_research_agent.server.publish_status', mock_publish_status):
            result = await agent.research("machine learning", max_results=3, request_id="integration-test")
        
        # Verify _run_with_progress was called correctly
        agent._run_with_progress.assert_called_once()
        call_args = agent._run_with_progress.call_args
        assert call_args[0][1] == "Researching 'machine learning'"  # operation_name
        assert call_args[0][2] == "integration-test"  # request_id
        
        # Verify START and END events were published
        start_events = [e for e in published_events if e["phase"] == PHASE_START]
        end_events = [e for e in published_events if e["phase"] == PHASE_END]
        assert len(start_events) == 1
        assert len(end_events) == 1
        
        # Verify result format matches expected output
        assert result["status"] == "success"
        assert result["agent"] == "integration_tester"
        assert result["summary"] == "Research completed successfully"
