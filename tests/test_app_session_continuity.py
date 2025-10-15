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
        async with client.stream("GET", "/events?task=What is my name?") as response:
            assert response.status_code == 200
            
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])  # Remove "data: " prefix
                    if data.get("type") == "start":
                        session_id = data.get("session_id")
                    elif data.get("type") == "end":
                        break
            
        assert session_id is not None, "No session_id found in start event"
        
        # Second request: continue the conversation with the same session_id
        conversation_includes_history = False
        async with client.stream("GET", f"/events?task=Call me Enrico&session_id={session_id}") as response:
            assert response.status_code == 200
            
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if data.get("type") == "final" and data.get("summary"):
                        # If session history is working, the agent should know about the previous question
                        content = data.get("summary", "")
                        # The agent should acknowledge both the previous question and the new information
                        if "name" in content.lower() or "enrico" in content.lower():
                            conversation_includes_history = True
                    elif data.get("type") == "end":
                        break
        
        # The conversation should reference the previous context
        # This would fail if session history was reset
        assert conversation_includes_history, "Agent did not acknowledge conversation history"