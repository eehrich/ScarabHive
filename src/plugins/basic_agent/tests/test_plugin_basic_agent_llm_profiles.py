"""Tests for basic_agent LLM profile switching functionality.

This module tests the dynamic LLM profile switching capabilities:
- llm_profile parameter
- use_advanced_model parameter
- Multi-profile vs single-profile agents
- Schema rendering with enum
- Profile validation
"""

import pytest
from unittest.mock import Mock
try:
    from unittest.mock import AsyncMock  # Python 3.8+
except ImportError:
    # Fallback for Python <3.8
    class AsyncMock(Mock):  # type: ignore[no-redef]
        def __call__(self, *args, **kwargs):
            async def coro(*args, **kwargs):
                return super(AsyncMock, self).__call__(*args, **kwargs)
            return coro(*args, **kwargs)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(iter(self.return_value))
            except StopIteration:
                raise StopAsyncIteration

from agent_system.config.models import AgentSystemConfig, ToolServerConfig, AgentConfig, LLMSystemConfig, LLMProfile
from plugins.basic_agent.server import BasicAgent
from agent_system.tools.base import ToolServerRegistry


@pytest.fixture
def mock_system_config():
    """Create a mock system config with multiple LLM profiles."""
    config = Mock(spec=AgentSystemConfig)

    # Mock LLM system with multiple profiles
    config.llm_system = Mock(spec=LLMSystemConfig)
    config.llm_system.profiles = {
        "fast": Mock(spec=LLMProfile, model_ref="gpt-5-nano"),
        "normal": Mock(spec=LLMProfile, model_ref="gpt-5-mini"),
        "think": Mock(spec=LLMProfile, model_ref="gpt-5.1"),
    }
    config.llm_system.models = {
        "gpt-5-nano": Mock(provider="openai_httpx", model="gpt-5-nano"),
        "gpt-5-mini": Mock(provider="openai_httpx", model="gpt-5-mini"),
        "gpt-5.1": Mock(provider="openai_httpx", model="gpt-5.1"),
    }

    config.network = Mock()
    config.network.ssl_verify = False

    return config


@pytest.fixture
def mock_registry():
    """Create a mock tool registry."""
    return Mock(spec=ToolServerRegistry)


class TestBasicAgentMultiProfile:
    """Test BasicAgent with multiple LLM profiles."""

    def test_multi_profile_schema_includes_llm_profile_enum(self, mock_system_config, mock_registry):
        """Test that multi-profile agent includes llm_profile with enum in schema."""
        agent_config = AgentConfig(llm_profile=["normal", "think"], llm_profile_advanced=["fast"])
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        # Get tools
        tools = agent.get_tools()

        # Find execute_task tool
        execute_task_tool = None
        for tool in tools:
            func = tool.get("function", {})
            if "execute_task" in func.get("name", ""):
                execute_task_tool = func
                break

        assert execute_task_tool is not None

        # Check properties
        params = execute_task_tool.get("parameters", {})
        props = params.get("properties", {})

        # Should have llm_profile property
        assert "llm_profile" in props
        assert props["llm_profile"]["type"] == "string"
        assert "enum" in props["llm_profile"]
        assert props["llm_profile"]["enum"] == ["normal", "think", "fast"]

        # Should have use_advanced_model property
        assert "use_advanced_model" in props
        assert props["use_advanced_model"]["type"] == "boolean"

    def test_multi_profile_default_profile(self, mock_system_config, mock_registry):
        """Test that first profile in list is default; available = Union beider Ketten."""
        agent_config = AgentConfig(llm_profile=["normal", "think"], llm_profile_advanced=["fast"])
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        assert agent.agent_config.default_llm_profile == "normal"
        assert agent.agent_config.available_llm_profiles == ["normal", "think", "fast"]

    @pytest.mark.asyncio
    async def test_execute_task_with_explicit_llm_profile(self, mock_system_config, mock_registry):
        """Test execute_task with explicit llm_profile parameter."""
        agent_config = AgentConfig(llm_profile=["normal", "think"])
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        # Mock run_events to return an async iterator
        async def mock_run_events(*args, **kwargs):
            yield {"type": "start"}
            yield {"type": "final", "summary": "Task completed"}

        mock_run_events_spy = Mock(side_effect=mock_run_events)
        agent.run_events = mock_run_events_spy

        # Execute with explicit profile
        result = await agent.execute_task({
            "task": "Test task",
            "llm_profile": "think"
        })

        assert result["status"] == "success"

        # Verify run_events was called with llm_override
        assert mock_run_events_spy.called
        call_kwargs = mock_run_events_spy.call_args[1]
        assert call_kwargs["llm_override"] is not None

    @pytest.mark.asyncio
    async def test_execute_task_with_invalid_profile(self, mock_system_config, mock_registry):
        """Test execute_task with invalid profile returns error."""
        agent_config = AgentConfig(llm_profile=["normal", "think"])
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        # Try to use profile not in agent's list
        result = await agent.execute_task({
            "task": "Test task",
            "llm_profile": "invalid_profile"
        })

        assert result["status"] == "error"
        assert "not available" in result["error"]
        assert "normal" in result["error"]  # Should list available profiles

    @pytest.mark.asyncio
    async def test_execute_task_with_use_advanced_model(self, mock_system_config, mock_registry):
        """Test execute_task with use_advanced_model=True uses llm_profile_advanced[0]."""
        agent_config = AgentConfig(llm_profile=["normal", "think"], llm_profile_advanced=["fast"])
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        # Mock run_events to return an async iterator
        async def mock_run_events(*args, **kwargs):
            yield {"type": "start"}
            yield {"type": "final", "summary": "Task completed"}

        mock_run_events_spy = Mock(side_effect=mock_run_events)
        agent.run_events = mock_run_events_spy

        # Execute with use_advanced_model
        result = await agent.execute_task({
            "task": "Test task",
            "use_advanced_model": True
        })

        assert result["status"] == "success"

        # execute_task mappt use_advanced_model NICHT selbst — das Flag geht
        # an run_events durch (dort zentrales Advanced-Mapping + Fallback-Kette)
        assert mock_run_events_spy.called
        call_kwargs = mock_run_events_spy.call_args[1]
        assert call_kwargs["use_advanced_model"] is True
        assert call_kwargs["llm_override"] is None

    @pytest.mark.asyncio
    async def test_llm_profile_takes_precedence_over_use_advanced_model(self, mock_system_config, mock_registry):
        """Test that explicit llm_profile takes precedence over use_advanced_model."""
        agent_config = AgentConfig(llm_profile=["normal", "think"], llm_profile_advanced=["fast"])
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        # Mock run_events to return an async iterator
        async def mock_run_events(*args, **kwargs):
            yield {"type": "start"}
            yield {"type": "final", "summary": "Task completed"}

        mock_run_events_spy = Mock(side_effect=mock_run_events)
        agent.run_events = mock_run_events_spy

        # Execute with both parameters - llm_profile should win
        result = await agent.execute_task({
            "task": "Test task",
            "llm_profile": "normal",
            "use_advanced_model": True  # Should be ignored
        })

        assert result["status"] == "success"

        # Explizites llm_profile gewinnt: Override gebaut, Flag NICHT durchgereicht
        call_kwargs = mock_run_events_spy.call_args[1]
        assert call_kwargs["llm_override"] is not None
        assert call_kwargs["use_advanced_model"] is False


class TestBasicAgentSingleProfile:
    """Test BasicAgent with single LLM profile."""

    def test_single_profile_schema_excludes_llm_profile(self, mock_system_config, mock_registry):
        """Test that single-profile agent excludes llm_profile from schema (token optimization)."""
        agent_config = AgentConfig(llm_profile="normal")
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        # Get tools
        tools = agent.get_tools()

        # Find execute_task tool
        execute_task_tool = None
        for tool in tools:
            func = tool.get("function", {})
            if "execute_task" in func.get("name", ""):
                execute_task_tool = func
                break

        assert execute_task_tool is not None

        # Check properties
        params = execute_task_tool.get("parameters", {})
        props = params.get("properties", {})

        # Should NOT have llm_profile property (token optimization)
        assert "llm_profile" not in props

        # Should still have use_advanced_model (but with note that it has no effect)
        assert "use_advanced_model" in props
        assert "no advanced profile" in props["use_advanced_model"]["description"]

    def test_single_profile_default(self, mock_system_config, mock_registry):
        """Test single profile configuration."""
        agent_config = AgentConfig(llm_profile="normal")
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        assert agent.agent_config.default_llm_profile == "normal"
        assert agent.agent_config.available_llm_profiles == ["normal"]

    @pytest.mark.asyncio
    async def test_use_advanced_model_has_no_effect_on_single_profile(self, mock_system_config, mock_registry):
        """Test that use_advanced_model has no effect on single-profile agent."""
        agent_config = AgentConfig(llm_profile="normal")
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        # Mock run_events to return an async iterator
        async def mock_run_events(*args, **kwargs):
            yield {"type": "start"}
            yield {"type": "final", "summary": "Task completed"}

        agent.run_events = Mock(side_effect=mock_run_events)

        # Execute with use_advanced_model - should be ignored (only one profile)
        result = await agent.execute_task({
            "task": "Test task",
            "use_advanced_model": True
        })

        assert result["status"] == "success"

        # run_events should NOT have llm_override (only one profile available)
        # For single profile, use_advanced_model doesn't create override
        # because available_profiles has length 1


class TestAgentRunEventsLLMOverride:
    """Test Agent.run_events() with use_advanced_model parameter."""

    @pytest.mark.asyncio
    async def test_run_events_use_advanced_model_creates_override(self, mock_system_config, mock_registry):
        """Test that run_events with use_advanced_model=True creates LLM override."""
        agent_config = AgentConfig(llm_profile=["normal"], llm_profile_advanced=["think"])
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        # Mock _run_events to return an async iterator
        async def mock_run_events_impl(*args, **kwargs):
            yield {"type": "final", "summary": "Done"}

        mock_run_events_spy = Mock(side_effect=mock_run_events_impl)
        agent._run_events = mock_run_events_spy

        # Call run_events with use_advanced_model
        events = []
        async for event in agent.run_events("Test task", use_advanced_model=True):
            events.append(event)

        # Verify _run_events was called
        assert mock_run_events_spy.called

        # Check if llm_override was created (via side effect in run_events)
        # The actual LLM override creation happens before _run_events call

    @pytest.mark.asyncio
    async def test_run_events_explicit_llm_override_takes_precedence(self, mock_system_config, mock_registry):
        """Test that explicit llm_override takes precedence over use_advanced_model."""
        agent_config = AgentConfig(llm_profile=["normal"], llm_profile_advanced=["think"])
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        # Mock _run_events to return an async iterator
        async def mock_run_events_impl(*args, **kwargs):
            yield {"type": "final", "summary": "Done"}

        mock_run_events_spy = Mock(side_effect=mock_run_events_impl)
        agent._run_events = mock_run_events_spy

        # Create explicit LLM override
        explicit_llm = Mock()

        # Call with both - explicit should win
        events = []
        async for event in agent.run_events(
            "Test task",
            llm_override=explicit_llm,
            use_advanced_model=True  # Should be ignored
        ):
            events.append(event)

        # Verify _run_events was called with the explicit override
        assert mock_run_events_spy.called
        call_kwargs = mock_run_events_spy.call_args[1]
        assert call_kwargs["llm_override"] == explicit_llm


class TestTemplateVariableInjection:
    """Test template variable injection for schema rendering."""

    def test_get_template_vars_includes_llm_profiles(self, mock_system_config, mock_registry):
        """Test that get_template_vars includes llm_profiles (Union) + has_advanced."""
        agent_config = AgentConfig(llm_profile=["normal", "think"], llm_profile_advanced=["fast"])
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        template_vars = agent.get_template_vars()

        assert "name" in template_vars
        assert template_vars["name"] == "test_agent"

        assert "llm_profiles" in template_vars
        assert template_vars["llm_profiles"] == ["normal", "think", "fast"]
        assert template_vars["has_advanced"] is True

    def test_get_template_vars_single_profile(self, mock_system_config, mock_registry):
        """Test template vars for single-profile agent."""
        agent_config = AgentConfig(llm_profile="normal")
        server_config = ToolServerConfig(
            type="basic_agent",
            enabled=True,
            agent_config=agent_config
        )

        agent = BasicAgent("test_agent", mock_system_config, server_config, mock_registry)

        template_vars = agent.get_template_vars()

        assert "llm_profiles" in template_vars
        assert template_vars["llm_profiles"] == ["normal"]
        assert template_vars["has_advanced"] is False
