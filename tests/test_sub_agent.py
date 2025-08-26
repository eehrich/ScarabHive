"""
Tests for SubAgent functionality.
"""
import pytest
from unittest.mock import AsyncMock

from agent_system.agent.sub_agent import SubAgent
from agent_system.agent.core import Agent
from agent_system.config.models import AgentConfig, LLMConfig
from agent_system.mcp.base import MCPRegistry


class TestSubAgent:
    """Test SubAgent functionality."""
    
    def create_mock_agent(self):
        """Create a mock agent for testing."""
        mock_agent = AsyncMock(spec=Agent)
        return mock_agent
    
    def test_sub_agent_initialization(self):
        """Test SubAgent initialization."""
        mock_agent = self.create_mock_agent()
        config = {"description": "Test sub-agent"}
        
        sub_agent = SubAgent("test_sub", mock_agent, config)
        
        assert sub_agent.name == "test_sub"
        assert sub_agent.agent is mock_agent
        assert sub_agent.description == "Test sub-agent"
        
    def test_sub_agent_default_description(self):
        """Test SubAgent with default description."""
        mock_agent = self.create_mock_agent()
        
        sub_agent = SubAgent("test_sub", mock_agent)
        
        assert sub_agent.description == "Sub-agent: test_sub"
        
    def test_get_schema(self):
        """Test SubAgent schema generation."""
        mock_agent = self.create_mock_agent()
        sub_agent = SubAgent("test_sub", mock_agent)
        
        schema = sub_agent.get_schema()
        
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "test_sub"
        assert "task" in schema["function"]["parameters"]["properties"]
        assert "action" in schema["function"]["parameters"]["properties"]
        assert "task" in schema["function"]["parameters"]["required"]
        
    def test_get_default_action(self):
        """Test SubAgent default action."""
        mock_agent = self.create_mock_agent()
        sub_agent = SubAgent("test_sub", mock_agent)
        
        assert sub_agent.get_default_action() == "run"
        
    @pytest.mark.asyncio
    async def test_call_success(self):
        """Test successful SubAgent call."""
        mock_agent = self.create_mock_agent()
        mock_agent.run.return_value = {
            "task": "test task",
            "calls": [{"server": "test", "result": "success"}],
            "summary": "Task completed successfully"
        }
        
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = await sub_agent.call("run", {"task": "test task"})
        
        assert result["status"] == "success"
        assert result["sub_agent"] == "test_sub"
        assert result["task"] == "test task"
        assert "result" in result
        assert result["summary"] == "Task completed successfully"
        mock_agent.run.assert_called_once_with("test task")
        
    @pytest.mark.asyncio
    async def test_call_with_different_actions(self):
        """Test SubAgent call with different valid actions."""
        mock_agent = self.create_mock_agent()
        mock_agent.run.return_value = {"task": "test", "summary": "done"}
        
        sub_agent = SubAgent("test_sub", mock_agent)
        
        # Test different valid actions
        for action in ["run", "execute", "ask"]:
            result = await sub_agent.call(action, {"task": "test task"})
            assert result["status"] == "success"
            
    @pytest.mark.asyncio
    async def test_call_invalid_action(self):
        """Test SubAgent call with invalid action."""
        mock_agent = self.create_mock_agent()
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = await sub_agent.call("invalid", {"task": "test task"})
        
        assert result["status"] == "error"
        assert "Unknown action 'invalid'" in result["error"]
        mock_agent.run.assert_not_called()
        
    @pytest.mark.asyncio
    async def test_call_missing_task(self):
        """Test SubAgent call without task parameter."""
        mock_agent = self.create_mock_agent()
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = await sub_agent.call("run", {})
        
        assert result["status"] == "error"
        assert "Missing required parameter" in result["error"]
        mock_agent.run.assert_not_called()
        
    @pytest.mark.asyncio
    async def test_call_with_query_parameter(self):
        """Test SubAgent call with 'query' parameter instead of 'task'."""
        mock_agent = self.create_mock_agent()
        mock_agent.run.return_value = {"summary": "done"}
        
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = await sub_agent.call("run", {"query": "test query"})
        
        assert result["status"] == "success"
        assert result["task"] == "test query"
        mock_agent.run.assert_called_once_with("test query")
        
    @pytest.mark.asyncio
    async def test_call_with_prompt_parameter(self):
        """Test SubAgent call with 'prompt' parameter instead of 'task'."""
        mock_agent = self.create_mock_agent()
        mock_agent.run.return_value = {"summary": "done"}
        
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = await sub_agent.call("run", {"prompt": "test prompt"})
        
        assert result["status"] == "success"
        assert result["task"] == "test prompt"
        mock_agent.run.assert_called_once_with("test prompt")
        
    @pytest.mark.asyncio
    async def test_call_agent_exception(self):
        """Test SubAgent call when wrapped agent raises exception."""
        mock_agent = self.create_mock_agent()
        mock_agent.run.side_effect = RuntimeError("Agent failed")
        
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = await sub_agent.call("run", {"task": "test task"})
        
        assert result["status"] == "error"
        assert result["sub_agent"] == "test_sub"
        assert result["task"] == "test task"
        assert "Agent failed" in result["error"]
        
    def test_extract_summary_with_summary(self):
        """Test summary extraction when result has summary."""
        mock_agent = self.create_mock_agent()
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = {"summary": "Test summary", "other": "data"}
        summary = sub_agent._extract_summary(result)
        
        assert summary == "Test summary"
        
    def test_extract_summary_with_successful_calls(self):
        """Test summary extraction with successful tool calls."""
        mock_agent = self.create_mock_agent()
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = {
            "calls": [
                {"server": "test1", "result": "success"},
                {"server": "test2", "result": "also success"}
            ]
        }
        summary = sub_agent._extract_summary(result)
        
        assert summary == "Executed 2 tool(s) successfully"
        
    def test_extract_summary_with_errors(self):
        """Test summary extraction with errors.""" 
        mock_agent = self.create_mock_agent()
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = {
            "errors": ["First error", "Second error"]
        }
        summary = sub_agent._extract_summary(result)
        
        assert summary == "Failed with 2 error(s): First error"
        
    def test_extract_summary_default(self):
        """Test summary extraction fallback."""
        mock_agent = self.create_mock_agent()
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = {"some": "data"}
        summary = sub_agent._extract_summary(result)
        
        assert summary == "Task completed"
        
    def test_extract_summary_string_fallback(self):
        """Test summary extraction with non-dict result."""
        mock_agent = self.create_mock_agent()
        sub_agent = SubAgent("test_sub", mock_agent)
        
        result = "Simple string result"
        summary = sub_agent._extract_summary(result)
        
        assert summary == "Simple string result"
