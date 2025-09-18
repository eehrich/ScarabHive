import pytest
import asyncio
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch
from agent_system.agent.interface_api import build_app

class TestDebugEndpoints:
    @pytest.fixture
    def client(self):
        app = build_app()
        return TestClient(app)

    @pytest.fixture
    def app(self):
        return build_app()

    def test_debug_messages_endpoint(self, client):
        """Test the debug messages endpoint."""
        # Mock the conversation manager
        mock_conversation_manager = Mock()
        mock_conversation_manager.get_messages.return_value = [
            {"role": "user", "content": "test message"},
            {"role": "assistant", "content": "test response"}
        ]

        mock_context_manager = Mock()
        mock_context_manager.get_usage_stats.return_value = {
            'actual_usage': {'total_tokens': 1000},
            'context_window': 10000,
            'prediction_threshold': 0.90
        }

        with patch.object(app.state, 'conversation_manager', mock_conversation_manager), \
             patch.object(app.state, 'context_manager', mock_context_manager):

            response = client.get("/api/debug/messages")

            assert response.status_code == 200
            data = response.json()
            assert 'messages' in data
            assert 'usage_stats' in data
            assert 'message_count' in data
            assert data['message_count'] == 2

    def test_context_stats_endpoint(self, client):
        """Test the context stats endpoint."""
        mock_context_manager = Mock()
        mock_stats = {
            'actual_usage': {'total_tokens': 5000},
            'context_window': 10000,
            'prediction_threshold': 0.90,
            'summarization_threshold': 8000
        }
        mock_context_manager.get_usage_stats.return_value = mock_stats

        with patch.object(app.state, 'context_manager', mock_context_manager):
            response = client.get("/api/debug/context-stats")

            assert response.status_code == 200
            data = response.json()
            assert data == mock_stats

    def test_debug_endpoints_error_handling(self, client):
        """Test error handling in debug endpoints."""
        with patch.object(app.state, 'conversation_manager', None):
            response = client.get("/api/debug/messages")
            assert response.status_code == 200  # Should handle gracefully with empty data
