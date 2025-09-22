"""Test status events for web_research_agent using the new StatusScope system."""

import pytest
from unittest.mock import AsyncMock

from plugins.web_research_agent.server import WebResearchAgent


class TestWebResearchAgentStatusEvents:
    """Test that web_research_agent uses StatusScope system properly."""

    @pytest.mark.asyncio
    async def test_research_status_events(self):
        """Test that research action uses status object correctly."""
        agent = WebResearchAgent("test_research_agent")
        
        # Create mock status object to track calls
        mock_status = AsyncMock()
        
        # Mock the underlying _run_with_progress method to simulate calling status methods
        async def mock_run_with_progress(prompt, request_id, status):
            # Simulate the actual method calling status methods
            await status.progress("Starting analysis...")
            await status.progress("Processing...")
            return {"summary": "Research completed"}
        
        agent._run_with_progress = mock_run_with_progress
        
        # Test with status object injected
        result = await agent.call("research", {
            "topic": "artificial intelligence",
            "request_id": "test-123",
            "_status": mock_status
        })
        
        # Verify the call succeeded
        assert "summary" in result
        
        # Verify status methods were called
        mock_status.progress.assert_called()
        # Note: StatusScope is not used as context manager in agent methods,
        # it's passed as a parameter after already being entered
