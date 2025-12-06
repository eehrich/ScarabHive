"""Tests for context_usage_tracker plugin."""

import pytest
from unittest.mock import Mock

from agent_system.hooks.plugin_hook import HookContext, HookType
from plugins.context_usage_tracker.plugin import ContextUsageTrackerPlugin, ContextUsageTrackerHooks
from plugins.context_usage_tracker.tracker import UsageTracker


@pytest.fixture
def plugin(tmp_path):
    """Create a context usage tracker plugin instance with temporary storage."""
    storage_path = tmp_path / "test_context_usage.json"
    tracker = UsageTracker(storage_path=storage_path)
    
    plugin = ContextUsageTrackerPlugin(
        name="context_usage_tracker",
        system_config={},
        mcp_config={}
    )
    plugin.tracker = tracker
    plugin.hooks_plugin.tracker = tracker
    
    return plugin


@pytest.fixture
def mock_hook_context():
    """Create a mock hook context for testing."""
    from agent_system.config.models import AgentSystemConfig, AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile
    
    context = Mock(spec=HookContext)
    context.hook_type = HookType.POST_LLM_CALL
    context.agent = Mock()
    context.agent.agent_id = "test-agent-123"
    
    system_config = AgentSystemConfig()
    system_config.llm_system = LLMSystemConfig(
        models={
            'gpt-4': LLMModelConfig(
                provider='openai',
                model='gpt-4',
                context_window=8000
            )
        },
        profiles={
            'normal': LLMProfile(model_ref='gpt-4')
        }
    )
    agent_config = AgentConfig(llm_profile='normal')
    
    context.agent.system_config = system_config
    context.agent.agent_config = agent_config
    context.agent_name = "test_agent"
    context.session_id = "session-456"
    context.messages = [{"role": "user", "content": "Hello"}]
    
    # Mock LLM with context_window attribute (handles llm_override)
    context.llm = Mock()
    context.llm.context_window = 8000
    
    context.llm_response = {
        "assistant": {"role": "assistant", "content": "Hi"},
        "usage": {
            "total_tokens": 150,
            "prompt_tokens": 100,
            "completion_tokens": 50,
        }
    }
    return context


def test_plugin_initialization(plugin):
    """Test that plugin initializes correctly."""
    assert plugin.name == "context_usage_tracker"
    assert isinstance(plugin.tracker, UsageTracker)
    assert isinstance(plugin.hooks_plugin, ContextUsageTrackerHooks)
    assert plugin.web_factory is not None


def test_get_hooks(plugin):
    """Test that plugin returns correct hooks from schema."""
    hooks = plugin.get_hooks()
    assert len(hooks) >= 1
    # Schema-based hooks are loaded from schema.yaml
    hook_names = [h.get('name') for h in hooks]
    assert 'track_usage' in hook_names


def test_get_web_router(plugin):
    """Test that plugin provides web router."""
    router = plugin.get_web_router()
    assert router is not None
    assert router.prefix == "/plugins/context_usage_tracker"


@pytest.mark.asyncio
async def test_track_llm_usage(plugin, mock_hook_context):
    """Test that LLM usage is tracked correctly."""
    # Execute via hooks_plugin's track_usage method directly
    result = await plugin.hooks_plugin.track_usage(mock_hook_context)
    
    assert result.success is True
    assert result.modified is False
    
    # Check that usage was recorded
    latest = plugin.tracker.get_latest()
    assert latest is not None
    assert latest["agent_id"] == "test-agent-123"
    assert latest["agent_name"] == "test_agent"
    assert latest["session_id"] == "session-456"
    assert latest["total_tokens"] == 150
    assert latest["prompt_tokens"] == 100
    assert latest["completion_tokens"] == 50
    assert latest["message_count"] == 1
    assert latest["context_window"] == 8000


@pytest.mark.asyncio
async def test_track_llm_usage_with_cached_tokens(plugin, mock_hook_context):
    """Test that LLM usage tracking extracts cached tokens from prompt_tokens_details."""
    # Add cached_tokens to the mock response (OpenAI format)
    mock_hook_context.llm_response["usage"]["prompt_tokens_details"] = {
        "cached_tokens": 75  # 75 of 100 prompt tokens were cached
    }
    
    result = await plugin.hooks_plugin.track_usage(mock_hook_context)
    
    assert result.success is True
    
    latest = plugin.tracker.get_latest()
    assert latest is not None
    assert latest["cached_tokens"] == 75
    assert latest["prompt_tokens"] == 100  # Total prompt tokens still 100


@pytest.mark.asyncio
async def test_track_llm_usage_no_response(plugin):
    """Test that hook handles missing LLM response gracefully."""
    context = Mock(spec=HookContext)
    context.llm_response = None
    
    result = await plugin.hooks_plugin.track_usage(context)
    assert result.success is True
    assert result.modified is False


@pytest.mark.asyncio
async def test_track_llm_usage_no_usage_data(plugin):
    """Test that hook handles missing usage data gracefully."""
    context = Mock(spec=HookContext)
    context.llm_response = {"assistant": {"role": "assistant", "content": "Hi"}}
    
    result = await plugin.hooks_plugin.track_usage(context)
    assert result.success is True
    assert result.modified is False


def test_tracker_records_agent_stats(plugin, mock_hook_context):
    """Test that tracker maintains per-agent statistics."""
    # Record multiple usages
    for i in range(3):
        mock_hook_context.llm_response["usage"]["total_tokens"] = 100 + i * 50
        plugin.tracker.record_usage(
            agent_id="test-agent-123",
            agent_name="test_agent",
            session_id="session-456",
            total_tokens=100 + i * 50,
            prompt_tokens=70,
            completion_tokens=30 + i * 50,
            message_count=i + 1,
            context_window=8000,
        )
    
    # Check agent stats
    agent_stats = plugin.tracker.get_agent_stats()
    assert "test-agent-123" in agent_stats
    stats = agent_stats["test-agent-123"]
    assert stats["total_calls"] == 3
    assert stats["total_tokens"] == 100 + 150 + 200  # Sum of all calls
    assert stats["peak_tokens"] == 200  # Max tokens
    assert stats["message_count"] == 3  # Last message count


def test_tracker_history(plugin):
    """Test that tracker maintains usage history."""
    # Record some usage
    for i in range(5):
        plugin.tracker.record_usage(
            agent_id=f"agent-{i}",
            agent_name=f"Agent {i}",
            session_id=f"session-{i}",
            total_tokens=100 * (i + 1),
            prompt_tokens=60,
            completion_tokens=40,
            message_count=1,
            context_window=8000,
        )
    
    # Get history
    history = plugin.tracker.get_history()
    assert len(history) == 5
    
    # Get last 3
    history_last_3 = plugin.tracker.get_history(last_n=3)
    assert len(history_last_3) == 3
    assert history_last_3[-1]["agent_id"] == "agent-4"


def test_tracker_persistence(tmp_path):
    """Test that tracker persists and loads history correctly."""
    storage_path = tmp_path / "tracker_persist.json"
    
    # Create tracker and record data
    tracker1 = UsageTracker(max_history=10, storage_path=storage_path)
    tracker1.record_usage(
        agent_id="agent-1",
        agent_name="Agent 1",
        session_id="session-1",
        total_tokens=500,
        prompt_tokens=300,
        completion_tokens=200,
        message_count=3,
        context_window=8000,
    )
    tracker1.record_usage(
        agent_id="agent-1",
        agent_name="Agent 1",
        session_id="session-1",
        total_tokens=800,
        prompt_tokens=500,
        completion_tokens=300,
        message_count=5,
        context_window=8000,
    )
    
    # Verify data in first tracker
    assert len(tracker1.get_history()) == 2
    latest1 = tracker1.get_latest()
    assert latest1["total_tokens"] == 800
    agent_stats1 = tracker1.get_agent_stats()
    assert agent_stats1["agent-1"]["total_calls"] == 2
    assert agent_stats1["agent-1"]["total_tokens"] == 1300
    
    # Create new tracker instance (simulates restart)
    tracker2 = UsageTracker(max_history=10, storage_path=storage_path)
    
    # Verify data was loaded
    history2 = tracker2.get_history()
    assert len(history2) == 2
    assert history2[0]["total_tokens"] == 500
    assert history2[1]["total_tokens"] == 800
    
    latest2 = tracker2.get_latest()
    assert latest2 is not None
    assert latest2["total_tokens"] == 800
    assert latest2["agent_id"] == "agent-1"
    
    agent_stats2 = tracker2.get_agent_stats()
    assert "agent-1" in agent_stats2
    assert agent_stats2["agent-1"]["total_calls"] == 2
    assert agent_stats2["agent-1"]["total_tokens"] == 1300
    assert agent_stats2["agent-1"]["peak_tokens"] == 800


def test_tracker_clear_history(plugin):
    """Test that tracker can clear history."""
    # Record some usage
    plugin.tracker.record_usage(
        agent_id="test-agent",
        agent_name="Test Agent",
        session_id="session-1",
        total_tokens=100,
        prompt_tokens=60,
        completion_tokens=40,
        message_count=1,
        context_window=8000,
    )
    
    assert len(plugin.tracker.get_history()) == 1
    
    # Clear
    plugin.tracker.clear_history()
    
    assert len(plugin.tracker.get_history()) == 0
    assert plugin.tracker.get_latest() is None
    assert len(plugin.tracker.get_agent_stats()) == 0


@pytest.mark.asyncio
async def test_track_llm_usage_respects_llm_override(plugin):
    """Test that context_window is taken from llm object (handles llm_override)."""
    from agent_system.config.models import AgentSystemConfig, AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile
    
    # Create context with agent configured for 100k context window
    context = Mock(spec=HookContext)
    context.hook_type = HookType.POST_LLM_CALL
    context.agent = Mock()
    context.agent.agent_id = "web_research_agent"
    
    # Agent has normal profile with 100k context
    system_config = AgentSystemConfig()
    system_config.llm_system = LLMSystemConfig(
        models={
            'gpt-4': LLMModelConfig(
                provider='openai',
                model='gpt-4',
                context_window=100000  # Agent's default: 100k
            ),
            'gpt-4-turbo': LLMModelConfig(
                provider='openai',
                model='gpt-4-turbo',
                context_window=20000  # Override: 20k
            )
        },
        profiles={
            'normal': LLMProfile(model_ref='gpt-4'),
            'small': LLMProfile(model_ref='gpt-4-turbo')
        }
    )
    agent_config = AgentConfig(llm_profile='normal')
    
    context.agent.system_config = system_config
    context.agent.agent_config = agent_config
    context.agent_name = "web_research_agent"
    context.session_id = "session-test"
    context.messages = [{"role": "user", "content": "Test"}]
    
    # But actual LLM used is the override with 20k context (small profile)
    context.llm = Mock()
    context.llm.context_window = 20000  # This should be used!
    
    context.llm_response = {
        "assistant": {"role": "assistant", "content": "Response"},
        "usage": {
            "total_tokens": 1096,
            "prompt_tokens": 997,
            "completion_tokens": 99,
        }
    }
    
    # Execute hook
    result = await plugin.hooks_plugin.track_usage(context)
    
    assert result.success is True
    
    # Verify that tracker used the override context_window (20k), not agent's default (100k)
    latest = plugin.tracker.get_latest()
    assert latest is not None
    assert latest["context_window"] == 20000  # Should use llm.context_window
    assert latest["total_tokens"] == 1096
    # Verify usage percentage is calculated correctly with 20k, not 100k
    expected_percentage = (1096 / 20000) * 100  # ~5.48%
    assert abs(latest["usage_percentage"] - expected_percentage) < 0.01


def test_tracker_cached_tokens(plugin):
    """Test that tracker properly records and accumulates cached_tokens."""
    # Record usage with cached tokens
    plugin.tracker.record_usage(
        agent_id="test-agent",
        agent_name="Test Agent",
        session_id="session-1",
        total_tokens=500,
        prompt_tokens=300,
        completion_tokens=200,
        message_count=3,
        context_window=8000,
        cached_tokens=150,  # 150 of the 300 prompt tokens were cached
    )
    plugin.tracker.record_usage(
        agent_id="test-agent",
        agent_name="Test Agent",
        session_id="session-1",
        total_tokens=800,
        prompt_tokens=500,
        completion_tokens=300,
        message_count=5,
        context_window=8000,
        cached_tokens=200,  # 200 of the 500 prompt tokens were cached
    )

    # Check latest snapshot has cached_tokens
    latest = plugin.tracker.get_latest()
    assert latest is not None
    assert latest["cached_tokens"] == 200

    # Check agent stats accumulates cached_tokens
    agent_stats = plugin.tracker.get_agent_stats()
    assert "test-agent" in agent_stats
    stats = agent_stats["test-agent"]
    assert stats["total_cached_tokens"] == 350  # 150 + 200

    # Check history contains cached_tokens
    history = plugin.tracker.get_history()
    assert len(history) == 2
    assert history[0]["cached_tokens"] == 150
    assert history[1]["cached_tokens"] == 200


def test_tracker_cached_tokens_persistence(tmp_path):
    """Test that cached_tokens persists and loads correctly."""
    storage_path = tmp_path / "tracker_cached.json"

    # Create tracker and record data with cached tokens
    tracker1 = UsageTracker(max_history=10, storage_path=storage_path)
    tracker1.record_usage(
        agent_id="agent-1",
        agent_name="Agent 1",
        session_id="session-1",
        total_tokens=500,
        prompt_tokens=300,
        completion_tokens=200,
        message_count=3,
        context_window=8000,
        cached_tokens=100,
    )

    # Verify data in first tracker
    assert tracker1.get_latest()["cached_tokens"] == 100
    assert tracker1.get_agent_stats()["agent-1"]["total_cached_tokens"] == 100

    # Create new tracker instance (simulates restart)
    tracker2 = UsageTracker(max_history=10, storage_path=storage_path)

    # Verify cached_tokens was loaded
    latest2 = tracker2.get_latest()
    assert latest2["cached_tokens"] == 100
    
    agent_stats2 = tracker2.get_agent_stats()
    assert agent_stats2["agent-1"]["total_cached_tokens"] == 100

