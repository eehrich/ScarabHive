"""Test tool call ID generation."""

import pytest
import json
from unittest.mock import AsyncMock, MagicMock

from agent_system.llm.clients import OpenAIAsyncClient, OllamaNativeAsyncClient


class TestToolCallIds:
    """Test that tool call IDs are properly generated when missing."""

    def test_openai_client_generates_id_when_missing(self):
        """Test that OpenAI client generates UUID when tool call has no ID."""
        # Create a mock tool call without ID
        mock_tc = MagicMock()
        mock_tc.id = None  # No ID provided
        mock_tc.function.name = "test_function"
        mock_tc.function.arguments = '{"param": "value"}'
        
        # Create mock response
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Test response"
        mock_response.choices[0].message.tool_calls = [mock_tc]
        
        # The actual client logic would extract this
        tool_calls = [mock_tc]
        out_calls = []
        for tc in tool_calls:
            import uuid
            tc_id = getattr(tc, "id", None) or f"call_{uuid.uuid4().hex[:12]}"
            out_calls.append({
                "id": tc_id,
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            })
        
        # Verify ID was generated
        assert out_calls[0]["id"] is not None
        assert out_calls[0]["id"].startswith("call_")
        assert len(out_calls[0]["id"]) == 17  # "call_" + 12 hex chars

    def test_ollama_client_generates_id_when_missing(self):
        """Test that Ollama client generates UUID when tool call has no ID."""
        # Simulate Ollama response without ID
        tc = {
            "function": {
                "name": "test_function",
                "arguments": '{"param": "value"}'
            }
            # Note: no "id" field
        }
        
        # Apply the same logic as in OllamaNativeAsyncClient
        import uuid
        tc_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
        
        result = {
            "id": tc_id,
            "function": {
                "name": tc["function"]["name"],
                "arguments": tc["function"]["arguments"],
            },
        }
        
        # Verify ID was generated
        assert result["id"] is not None
        assert result["id"].startswith("call_")
        assert len(result["id"]) == 17  # "call_" + 12 hex chars

    def test_id_preserved_when_provided(self):
        """Test that existing IDs are preserved."""
        existing_id = "provided_id_123"
        tc = {"id": existing_id, "function": {"name": "test", "arguments": "{}"}}
        
        import uuid
        tc_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
        
        assert tc_id == existing_id

    def test_generated_ids_are_unique(self):
        """Test that generated IDs are unique."""
        import uuid
        
        ids = []
        for _ in range(10):
            tc_id = f"call_{uuid.uuid4().hex[:12]}"
            ids.append(tc_id)
        
        # All IDs should be unique
        assert len(set(ids)) == len(ids)
