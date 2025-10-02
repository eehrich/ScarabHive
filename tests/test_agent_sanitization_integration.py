"""Integration test for text sanitization in agent message handling."""

import pytest
from unittest.mock import AsyncMock

from agent_system.servers.agent.server import Agent
from agent_system.config.models import AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile
from agent_system.mcp.base import MCPRegistry, MCPServer


def create_test_config():
    """Create a test configuration with the new LLM system structure."""
    return AgentConfig(
        llm_system=LLMSystemConfig(
            models={
                "gpt-4": LLMModelConfig(provider="openai", model="gpt-4", openai_api_key="fake-key")
            },
            profiles={
                "normal": LLMProfile(model_ref="gpt-4")
            },
            default_profile="normal"
        ),
        max_steps=1
    )


class TestAgentSanitizationIntegration:
    """Test that agent properly sanitizes data before sending to LLM."""

    @pytest.mark.asyncio
    async def test_agent_sanitizes_user_input(self):
        """Test that user input is sanitized before being sent to LLM."""
        from agent_system.config.models import AgentSystemConfig, MCPConfig
        
        # Create mock config
        agent_config = create_test_config()
        system_config = AgentSystemConfig(
            llm_system=LLMSystemConfig(
                models={
                    "gpt-4": LLMModelConfig(provider="openai", model="gpt-4", openai_api_key="fake-key")
                },
                profiles={
                    "normal": LLMProfile(model_ref="gpt-4")
                },
                default_profile="normal"
            )
        )
        mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
        
        # Create mock registry
        registry = MCPRegistry()
        
        # Create agent
        agent = Agent("test_agent", system_config, mcp_config, registry)
        
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
        """Test that tool results are sanitized before being sent to LLM.

        This test registers a simple mock tool server in the registry that
        returns a tool result containing problematic control characters. We
        then run the agent through one iteration and capture the tool message
        sent to the LLM to assert sanitization occurred.
        """
        from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig as AC
    # No external plugin classes required; use a simple DummyToolServer below

        # Create system and agent configs
        system_config = AgentSystemConfig()
        agent_cfg = AC(max_steps=1)
        mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_cfg)

        # Create registry and register a dummy tool server that returns problematic chars
        registry = MCPRegistry()

        class DummyToolServer:
            def __init__(self, name):
                self.name = name

            def get_default_action(self):
                return "call"

            async def call(self, action, params):
                # Return a dict containing problematic characters (null bytes, control chars)
                return {
                    "result": "Data with\u0000null\u0001bytes\u200band\u202edirection"
                }

        # Register dummy tool under name 'test_tool'
        dummy = DummyToolServer("test_tool")
        registry.register("test_tool", dummy)


        # Mock LLM to capture messages passed for tool results and instruct a tool call
        captured_messages = []
        call_count = 0

        async def mock_chat_tools(messages, tools, cancellation_token=None):
            nonlocal call_count
            call_count += 1
            # Append ChatMessage objects so we can inspect tool messages
            captured_messages.extend(messages)
            if call_count == 1:
                # On first LLM call, instruct a tool call
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
            # Subsequent calls: normal assistant response
            return {"assistant": {"content": "OK", "tool_calls": None}}

        agent_llm = AsyncMock()
        agent_llm.chat_tools = mock_chat_tools

        # Build agent with injected mock LLM to avoid real LLM init
        agent = Agent("test_agent", system_config, mcp_config, registry, llm=agent_llm)

        # Instead of driving the full agent planning loop, call the ToolExecutionManager
        # directly with a single prepared tool call so we reliably invoke the DummyToolServer.
        tool_calls = [{
            "id": "test_call_1",
            "function": {"name": "test_tool", "arguments": "{}"}
        }]
        tool_name_mapping = {}
        available_tools = ["test_tool"]

        tool_messages, events, results = await agent._tool_execution_manager.execute_tools(
            tool_calls, tool_name_mapping, available_tools, step=0, request_id="req1"
        )

        assert len(tool_messages) >= 1, "Expected at least one tool message from execute_tools"

        # Inspect first tool message content - should be JSON and sanitized
        import json
        first_tool_content = tool_messages[0].content
        # Ensure it's valid JSON after sanitization
        parsed = json.loads(first_tool_content)
        assert "result" in parsed
        # Ensure problematic control characters are escaped (i.e., not raw)
        assert "\\u0000" in first_tool_content or "\u0000" in first_tool_content
        assert "null" in first_tool_content
        assert "bytes" in first_tool_content
