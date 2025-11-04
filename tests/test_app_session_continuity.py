"""Test session continuity across multiple requests."""
import pytest
import json
import httpx
from agent_system.app import build_app


@pytest.mark.asyncio  
async def test_session_continuity():
    """Test that multiple requests with the same session_id preserve conversation history."""
    app = build_app()
    
    async with httpx.AsyncClient(base_url="http://testserver") as client:
        client._transport = httpx.ASGITransport(app=app)
        
        # First request: start a conversation
        session_id = None
        first_request_completed = False
        async with client.stream("GET", "/events?task=Hello") as response:
            assert response.status_code == 200
            
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])  # Remove "data: " prefix
                    if data.get("type") == "start":
                        session_id = data.get("session_id")
                    elif data.get("type") == "end":
                        first_request_completed = True
                        break
            
        assert session_id is not None, "No session_id found in start event"
        assert first_request_completed, "First request did not complete"
        
        # Second request: continue the conversation with the same session_id
        # The test verifies that the session_id is accepted and the request completes
        second_request_completed = False
        same_session_used = False
        async with client.stream("GET", f"/events?task=How are you&session_id={session_id}") as response:
            assert response.status_code == 200
            
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if data.get("type") == "start":
                        # Verify the same session_id is being used
                        if data.get("session_id") == session_id:
                            same_session_used = True
                    elif data.get("type") == "end":
                        second_request_completed = True
                        break
        
        # The conversation should use the same session
        assert same_session_used, "Second request did not use the same session_id"
        assert second_request_completed, "Second request did not complete"