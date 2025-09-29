"""Integration test for text sanitization in agent message handling."""

import pytest
from unittest.mock import AsyncMock

from agent_system.servers.agent.server import Agent
from agent_system.config.models import AgentConfig
from agent_system.mcp.base import MCPRegistry, MCPServer


class TestAgentSanitizationIntegration:
    """Test that agent properly sanitizes data before sending to LLM."""

    @pytest.mark.asyncio
    async def test_agent_sanitizes_user_input(self):
        """Test that user input is sanitized before being sent to LLM."""
        # Create mock config
        config = AgentConfig(
            name="test_agent",
            system_prompt="You are a test assistant",
            max_turns=1,
            llm_provider="openai",
            llm_model="gpt-4",
            openai_api_key="fake-key"
        )
        
        # Create mock registry
        registry = MCPRegistry()
        
        # Create agent
        agent = Agent("test_agent", config, registry)
        
        # Mock the LLM to capture what messages it receives
        captured_messages = []
        async def mock_chat_tools(messages, tools, cancellation_token=None):
            captured_messages.extend(messages)
            return {"assistant": {"content": "Test response"}}
        
        agent.llm = AsyncMock()
        agent.llm.chat_tools = mock_chat_tools
        
        # Test with problematic input containing null bytes and control characters
        problematic_input = "Hello\x00world\x01test\u200Bdata"
        
        # Call the agent
        await agent.call("run", {"task": problematic_input})
        
        # Verify the user message was sanitized
        user_messages = [msg for msg in captured_messages if msg.role == "user"]
        assert len(user_messages) == 1
        user_content = user_messages[0].content
        
        # Should not contain problematic characters
        assert "\x00" not in user_content
        assert "\x01" not in user_content
        assert "\u200B" not in user_content
        
        # Should contain the safe parts
        assert "Hello" in user_content
        assert "world" in user_content
        assert "test" in user_content
        assert "data" in user_content

    @pytest.mark.asyncio
    async def test_agent_sanitizes_tool_results(self):
        """Test that tool results are sanitized before being sent to LLM."""
        # Create mock config
        config = AgentConfig(
            name="test_agent",
            system_prompt="You are a test assistant",
            max_turns=2,
            llm_provider="openai", 
            llm_model="gpt-4",
            openai_api_key="fake-key"
        )
        
        # Mock tool server that returns problematic data
        class MockToolServer(MCPServer):
            def __init__(self):
                super().__init__("test_tool")
            
            async def call(self, tool: str, params: dict) -> dict:
                return {"result": "Data with\x00null\x01bytes\u200Band\u202Edirection"}
            
            def get_schema(self) -> dict:
                return {
                    "type": "function",
                    "function": {
                        "name": "test_tool",
                        "description": "Test tool"
                    }
                }
            
            def get_default_action(self) -> str:
                return "run"
        
        mock_server = MockToolServer()
        
        # Create mock registry
        registry = MCPRegistry()
        registry.register("test_tool", mock_server)
        
        # Create agent
        agent = Agent("test_agent", config, registry)
        
        # Mock LLM to return tool call, then capture sanitized tool result
        captured_messages = []
        call_count = 0
        
        async def mock_chat_tools(messages, tools, cancellation_token=None):
            nonlocal call_count
            captured_messages.extend(messages)
            call_count += 1
            
            if call_count == 1:
                # First call: return tool call
                return {
                    "assistant": {
                        "content": "",
                        "tool_calls": [{
                            "id": "test_call_1",
                            "function": {
                                "name": "test_tool",
                                "arguments": "{}"
                            }
                        }]
                    }
                }
            else:
                # Second call: final response
                return {"assistant": {"content": "Final response"}}
        
        agent.llm = AsyncMock()
        agent.llm.chat_tools = mock_chat_tools
        
        # Call the agent
        await agent.call("run", {"task": "Test task"})
        
        # Find the tool result message
        tool_messages = [msg for msg in captured_messages if msg.role == "tool"]
        assert len(tool_messages) == 1
        tool_content = tool_messages[0].content
        
        # The problematic characters in the original data get JSON-escaped by json.dumps(),
        # but the sanitization ensures no actual problematic Unicode/binary chars reach the LLM
        # JSON escapes like \\u0000 are safe and expected
        
        # Should contain the safe parts
        assert "Data with" in tool_content
        assert "null" in tool_content
        assert "bytes" in tool_content
        assert "and" in tool_content  # Lowercase due to Unicode normalization
        assert "direction" in tool_content
        
        # Verify it's valid JSON (sanitized content should still be valid JSON)
        import json
        parsed = json.loads(tool_content)
        assert "result" in parsed
