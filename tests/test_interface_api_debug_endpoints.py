"""Tests for API debug endpoints that expose context usage and agent statistics."""

import pytest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from agent_system.agent.interface_api import build_app
from agent_system.context.manager import ContextManager


class TestAPIDebugEndpoints:
    """Test the API debug endpoints for context visibility."""

    @pytest.fixture
    def app(self):
        """Create test app."""
        return build_app()

    @pytest.fixture
    def client(self, app):
        """Create test client."""
        return TestClient(app)

    def test_debug_messages_endpoint_no_agent(self, client):
        """Test /api/debug/messages endpoint when no agent is available."""
        with patch('api.endpoints.get_agent', return_value=None):
            response = client.get("/api/debug/messages")
            assert response.status_code == 200
            data = response.json()
            assert data["messages"] == []
            assert data["usage_stats"] == {}
            assert data["message_count"] == 0

    def test_debug_messages_endpoint_with_agent(self, client):
        """Test /api/debug/messages endpoint with agent data."""
        # Mock agent with conversation and context manager
        mock_agent = Mock()
        mock_agent.conversation = [
            Mock(role="user", content="Hello", tool_calls=None, tool_call_id=None),
            Mock(role="assistant", content="Hi there!", tool_calls=None, tool_call_id=None)
        ]

        # Mock context manager with usage stats and estimate_token_count method
        mock_context_manager = Mock(spec=ContextManager)
        mock_context_manager.get_usage_stats.return_value = {
            "context_window": 4096,
            "prediction_threshold": 3686,
            "usage_percentage": 50.0
        }
        mock_context_manager.estimate_token_count.return_value = 150
        mock_agent.context_manager = mock_context_manager

        with patch('api.endpoints.get_agent', return_value=mock_agent):
            response = client.get("/api/debug/messages")
            assert response.status_code == 200
            data = response.json()

            assert len(data["messages"]) == 2
            assert data["messages"][0]["role"] == "user"
            assert data["messages"][0]["content"] == "Hello"
            assert data["messages"][1]["role"] == "assistant"
            assert data["messages"][1]["content"] == "Hi there!"

            assert data["usage_stats"]["context_window"] == 4096
            assert data["usage_stats"]["predicted_tokens"] == 150
            assert data["message_count"] == 2

    def test_context_stats_endpoint_no_agent(self, client):
        """Test /api/debug/context-stats endpoint when no agent is available."""
        with patch('api.endpoints.get_agent', return_value=None):
            response = client.get("/api/debug/context-stats")
            assert response.status_code == 200
            data = response.json()
            assert data["context_window"] is None
            assert data["prediction_threshold"] is None

    def test_context_stats_endpoint_with_agent(self, client):
        """Test /api/debug/context-stats endpoint with agent data."""
        mock_agent = Mock()
        mock_context_manager = Mock(spec=ContextManager)
        mock_context_manager.get_usage_stats.return_value = {
            "context_window": 8192,
            "prediction_threshold": 7372,
            "summarization_threshold": 6144,
            "actual_usage": 2048,
            "warning_levels": ["low"]
        }
        mock_agent.context_manager = mock_context_manager

        with patch('api.endpoints.get_agent', return_value=mock_agent):
            response = client.get("/api/debug/context-stats")
            assert response.status_code == 200
            data = response.json()

            assert data["context_window"] == 8192
            assert data["prediction_threshold"] == 7372
            assert data["summarization_threshold"] == 6144
            assert data["actual_usage"] == 2048
            assert data["warning_levels"] == ["low"]

    def test_agent_stats_endpoint(self, client):
        """Test /api/agents/stats endpoint for per-agent context tracking."""
        # Mock the agent tracker with some test data
        mock_stats = {
            "agent_1": {
                "agent_id": "agent_1",
                "agent_name": "Test Agent 1",
                "context_window": 4096,
                "current_tokens": 1024,
                "predicted_tokens": 1024,
                "message_count": 5,
                "summarization_count": 0,
                "peak_tokens": 1200,
                "total_llm_calls": 3,
                "total_tokens_processed": 2048
            },
            "agent_2": {
                "agent_id": "agent_2",
                "agent_name": "Test Agent 2",
                "context_window": 8192,
                "current_tokens": 2048,
                "predicted_tokens": 2100,
                "message_count": 8,
                "summarization_count": 1,
                "peak_tokens": 2500,
                "total_llm_calls": 5,
                "total_tokens_processed": 4096
            }
        }

        with patch('agent_system.context.agent_tracker.get_all_agent_stats', return_value=mock_stats):
            response = client.get("/api/agents/stats")
            assert response.status_code == 200
            data = response.json()

            assert data["agent_count"] == 2
            assert "agents" in data
            assert "agent_1" in data["agents"]
            assert "agent_2" in data["agents"]

            agent1_stats = data["agents"]["agent_1"]
            assert agent1_stats["agent_name"] == "Test Agent 1"
            assert agent1_stats["context_window"] == 4096
            assert agent1_stats["current_tokens"] == 1024
            assert agent1_stats["peak_tokens"] == 1200

            agent2_stats = data["agents"]["agent_2"]
            assert agent2_stats["agent_name"] == "Test Agent 2"
            assert agent2_stats["summarization_count"] == 1

    def test_agent_stats_endpoint_error_handling(self, client):
        """Test /api/agents/stats endpoint error handling."""
        with patch('agent_system.context.agent_tracker.get_all_agent_stats', side_effect=Exception("Test error")):
            response = client.get("/api/agents/stats")
            assert response.status_code == 500
            assert "Test error" in response.json()["detail"]

    def test_health_endpoint(self, client):
        """Test /api/health endpoint still works."""
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "agent-system-api"