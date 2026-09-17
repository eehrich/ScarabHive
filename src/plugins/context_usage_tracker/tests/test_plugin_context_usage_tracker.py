"""Tests for context_usage_tracker plugin."""

import json

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

    # The path goes into the CONSTRUCTOR, not onto the finished object:
    # building the plugin opens a database and migrates any legacy file at that
    # path, so a default here would reach into the real data/ directory.
    plugin = ContextUsageTrackerPlugin(
        name="context_usage_tracker",
        system_config={},
        server_config={"storage_path": str(tmp_path / "plugin_usage.json")}
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


def test_tracker_invalidate_session(tmp_path):
    """Test that invalidate_session marks usage data as stale.
    
    This feature prevents over-optimization when multiple context optimization
    hooks run in sequence (e.g., context_engineer then context_summarizer).
    """
    storage_path = tmp_path / "tracker_invalidate.json"
    tracker = UsageTracker(max_history=10, storage_path=storage_path)
    
    # Record usage for a session
    tracker.record_usage(
        agent_id="agent-1",
        agent_name="Agent 1",
        session_id="session-abc",
        total_tokens=80000,
        prompt_tokens=75000,
        completion_tokens=5000,
        message_count=50,
        context_window=100000,
    )
    
    # Get latest - should NOT have is_stale flag
    latest = tracker.get_latest(session_id="session-abc")
    assert latest is not None
    assert latest["prompt_tokens"] == 75000
    assert "is_stale" not in latest
    
    # Invalidate the session (simulates context optimization)
    tracker.invalidate_session("session-abc", "test_context_optimization")
    
    # Get latest again - should now have is_stale=True
    latest_after_invalidate = tracker.get_latest(session_id="session-abc")
    assert latest_after_invalidate is not None
    assert latest_after_invalidate["prompt_tokens"] == 75000  # Data still there
    assert latest_after_invalidate.get("is_stale") is True   # But marked stale
    
    # Record new usage - should clear stale flag
    tracker.record_usage(
        agent_id="agent-1",
        agent_name="Agent 1",
        session_id="session-abc",
        total_tokens=40000,  # Lower after optimization
        prompt_tokens=35000,
        completion_tokens=5000,
        message_count=25,
        context_window=100000,
    )
    
    # Get latest - should be fresh (no is_stale flag)
    latest_fresh = tracker.get_latest(session_id="session-abc")
    assert latest_fresh is not None
    assert latest_fresh["prompt_tokens"] == 35000
    assert "is_stale" not in latest_fresh


def test_tracker_invalidate_session_isolation(tmp_path):
    """Test that invalidate_session only affects the target session."""
    storage_path = tmp_path / "tracker_invalidate_isolation.json"
    tracker = UsageTracker(max_history=10, storage_path=storage_path)
    
    # Record usage for two sessions
    tracker.record_usage(
        agent_id="agent-1", agent_name="Agent 1", session_id="session-1",
        total_tokens=50000, prompt_tokens=45000, completion_tokens=5000,
        message_count=30, context_window=100000,
    )
    tracker.record_usage(
        agent_id="agent-1", agent_name="Agent 1", session_id="session-2",
        total_tokens=60000, prompt_tokens=55000, completion_tokens=5000,
        message_count=40, context_window=100000,
    )
    
    # Invalidate only session-1
    tracker.invalidate_session("session-1", "context_engineer_compaction")
    
    # session-1 should be stale
    latest_1 = tracker.get_latest(session_id="session-1")
    assert latest_1 is not None
    assert latest_1.get("is_stale") is True
    
    # session-2 should NOT be stale
    latest_2 = tracker.get_latest(session_id="session-2")
    assert latest_2 is not None
    assert "is_stale" not in latest_2


def test_tracker_clear_history_clears_invalidations(tmp_path):
    """Test that clear_history also clears invalidation tracking."""
    storage_path = tmp_path / "tracker_clear_invalidate.json"
    tracker = UsageTracker(max_history=10, storage_path=storage_path)
    
    # Record usage and invalidate
    tracker.record_usage(
        agent_id="agent-1", agent_name="Agent 1", session_id="session-1",
        total_tokens=50000, prompt_tokens=45000, completion_tokens=5000,
        message_count=30, context_window=100000,
    )
    tracker.invalidate_session("session-1", "test")
    
    # Clear history
    tracker.clear_history()
    
    # Record new usage - should NOT be stale (invalidation was cleared)
    tracker.record_usage(
        agent_id="agent-1", agent_name="Agent 1", session_id="session-1",
        total_tokens=50000, prompt_tokens=45000, completion_tokens=5000,
        message_count=30, context_window=100000,
    )
    
    latest = tracker.get_latest(session_id="session-1")
    assert latest is not None
    assert "is_stale" not in latest



# ---------------------------------------------------------------------------
# v2.0.0: per-call cost / model / request_id / cache_write tracking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_track_llm_usage_with_cost_and_model(plugin, mock_hook_context):
    """The hook extracts cost, cache writes, model and request_id per call."""
    mock_hook_context.llm_response["usage"]["cost"] = 0.3942
    mock_hook_context.llm_response["usage"]["prompt_tokens_details"] = {
        "cached_tokens": 60,
        "cache_write_tokens": 40,
    }
    mock_hook_context.llm.model = "anthropic/claude-sonnet-4.5"
    mock_hook_context.request_id = "req-789"

    result = await plugin.hooks_plugin.track_usage(mock_hook_context)
    assert result.success is True

    latest = plugin.tracker.get_latest()
    assert latest is not None
    assert latest["cost"] == pytest.approx(0.3942)
    assert latest["cached_tokens"] == 60
    assert latest["cache_write_tokens"] == 40
    assert latest["model"] == "anthropic/claude-sonnet-4.5"
    assert latest["request_id"] == "req-789"


@pytest.mark.asyncio
async def test_track_llm_usage_cache_creation_tokens_fallback(plugin, mock_hook_context):
    """Native Anthropic reports cache writes as cache_creation_tokens."""
    mock_hook_context.llm_response["usage"]["prompt_tokens_details"] = {
        "cached_tokens": 10,
        "cache_creation_tokens": 555,
    }
    await plugin.hooks_plugin.track_usage(mock_hook_context)
    latest = plugin.tracker.get_latest()
    assert latest["cache_write_tokens"] == 555


@pytest.mark.asyncio
async def test_track_llm_usage_without_cost_stays_none(plugin, mock_hook_context):
    """Providers without billing info leave cost as None (panel shows a dash)."""
    await plugin.hooks_plugin.track_usage(mock_hook_context)
    latest = plugin.tracker.get_latest()
    assert latest["cost"] is None


def test_agent_stats_cost_rollup(tmp_path):
    """Per-agent rollup accumulates cost, prompt/completion and cache writes."""
    tracker = UsageTracker(storage_path=tmp_path / "t.json")
    tracker.record_usage(
        agent_id="a1", agent_name="A1", session_id="s1",
        total_tokens=100, prompt_tokens=80, completion_tokens=20,
        cached_tokens=40, cache_write_tokens=10, cost=0.5, model="m1",
    )
    tracker.record_usage(
        agent_id="a1", agent_name="A1", session_id="s1",
        total_tokens=200, prompt_tokens=150, completion_tokens=50,
        cached_tokens=100, cache_write_tokens=0, cost=None, model="m1",
    )
    stats = tracker.get_agent_stats()["a1"]
    assert stats["total_cost"] == pytest.approx(0.5)
    assert stats["cost_known_calls"] == 1
    assert stats["total_prompt_tokens"] == 230
    assert stats["total_completion_tokens"] == 70
    assert stats["total_cache_write_tokens"] == 10

    # Session-filtered variant carries the same fields
    s_stats = tracker.get_agent_stats(session_id="s1")["a1"]
    assert s_stats["total_cost"] == pytest.approx(0.5)
    assert s_stats["cost_known_calls"] == 1


def test_statistics_totals_block(tmp_path):
    """get_statistics exposes the cost/cache aggregates the panel cards use."""
    tracker = UsageTracker(storage_path=tmp_path / "t.json")
    tracker.record_usage(
        agent_id="a1", agent_name="A1", session_id="s1",
        total_tokens=100, prompt_tokens=80, completion_tokens=20,
        cached_tokens=40, cost=0.25,
    )
    tracker.record_usage(
        agent_id="a2", agent_name="A2", session_id="s1",
        total_tokens=100, prompt_tokens=20, completion_tokens=80,
        cached_tokens=10, cost=None,
    )
    totals = tracker.get_statistics()["totals"]
    assert totals["cost"] == pytest.approx(0.25)
    assert totals["cost_known_calls"] == 1
    assert totals["prompt_tokens"] == 100
    assert totals["completion_tokens"] == 100
    assert totals["cached_tokens"] == 50
    assert totals["cache_hit_rate"] == pytest.approx(50.0)


def test_history_agent_filter(tmp_path):
    tracker = UsageTracker(storage_path=tmp_path / "t.json")
    tracker.record_usage(agent_id="a1", agent_name="A1", session_id="s1", total_tokens=1)
    tracker.record_usage(agent_id="a2", agent_name="A2", session_id="s1", total_tokens=2)
    tracker.record_usage(agent_id="a1", agent_name="A1", session_id="s2", total_tokens=3)
    assert len(tracker.get_history(agent_id="a1")) == 2
    assert len(tracker.get_history(agent_id="a1", session_id="s2")) == 1


def test_load_old_format_without_cost_fields(tmp_path):
    """A pre-2.0 JSON file (no cost/model/... fields) loads with defaults."""
    import json as _json
    storage = tmp_path / "old.json"
    storage.write_text(_json.dumps({
        "agents": {"a1": {
            "agent_id": "a1", "agent_name": "A1", "total_calls": 3,
            "total_tokens": 300, "peak_tokens": 150, "message_count": 5,
            "last_activity": 123.0, "total_cached_tokens": 50,
        }},
        "history": [{
            "timestamp": 123.0, "agent_id": "a1", "agent_name": "A1",
            "session_id": "s1", "total_tokens": 100, "prompt_tokens": 80,
            "completion_tokens": 20, "message_count": 2,
            "context_window": 1000, "usage_percentage": 10.0,
        }],
        "latest": None,
    }), encoding="utf-8")

    tracker = UsageTracker(storage_path=storage)
    hist = tracker.get_history()
    assert len(hist) == 1
    assert hist[0]["cost"] is None
    assert hist[0]["cache_write_tokens"] == 0
    assert hist[0]["model"] == ""
    stats = tracker.get_agent_stats()["a1"]
    assert stats["total_cost"] == 0.0
    assert stats["total_calls"] == 3


def test_session_filter_includes_sub_and_sub_sub_agents(tmp_path):
    """Session scope pulls in sub-agent calls (own sub-sessions) via the
    hierarchical request-id tree — including sub-sub-agents; other sessions
    stay excluded."""
    tracker = UsageTracker(storage_path=tmp_path / "t.json")
    # main conversation call in the root session
    tracker.record_usage(agent_id="main", agent_name="Main", session_id="root-sess",
                         total_tokens=10, prompt_tokens=8, completion_tokens=2,
                         cost=0.1, request_id="req1")
    # sub-agent call: own sub-session, request prefixed by the parent request
    tracker.record_usage(agent_id="subA", agent_name="SubA", session_id="sub-sess-1",
                         total_tokens=20, prompt_tokens=15, completion_tokens=5,
                         cost=0.2, request_id="req1_sub_abc123")
    # sub-sub-agent call (transitively prefixed)
    tracker.record_usage(agent_id="subB", agent_name="SubB", session_id="sub-sub-sess",
                         total_tokens=30, prompt_tokens=25, completion_tokens=5,
                         cost=0.3, request_id="req1_sub_abc123_sub_def456")
    # unrelated session must NOT leak in
    tracker.record_usage(agent_id="other", agent_name="Other", session_id="other-sess",
                         total_tokens=99, request_id="reqX")

    hist = tracker.get_history(session_id="root-sess")
    assert [h["agent_id"] for h in hist] == ["main", "subA", "subB"]

    stats = tracker.get_agent_stats(session_id="root-sess")
    assert set(stats.keys()) == {"main", "subA", "subB"}
    assert stats["subB"]["total_cost"] == pytest.approx(0.3)

    totals = tracker.get_statistics(session_id="root-sess")["totals"]
    assert totals["cost"] == pytest.approx(0.6)

    # agent filter composes with the tree expansion
    assert len(tracker.get_history(session_id="root-sess", agent_id="subA")) == 1

    # get_latest stays exact-session (Context Now = main conversation)
    assert tracker.get_latest(session_id="root-sess")["agent_id"] == "main"


def test_session_filter_pre20_snapshots_match_exact_only(tmp_path):
    """Old snapshots without request_id still match their own session."""
    tracker = UsageTracker(storage_path=tmp_path / "t.json")
    tracker.record_usage(agent_id="main", agent_name="Main", session_id="root-sess",
                         total_tokens=10)  # request_id defaults to ""
    hist = tracker.get_history(session_id="root-sess")
    assert len(hist) == 1


# ---------------------------------------------------------------------------
# v2.1.0: pricing-table fallback when the provider sends no billed cost
# ---------------------------------------------------------------------------

@pytest.fixture
def pricing_file(tmp_path, monkeypatch):
    """A minimal pricing table + chdir so the default relative path resolves."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "llm_pricing.yaml").write_text(
        "deepseek-chat:\n  input: 0.28\n  output: 0.42\n  cached_input: 0.028\n",
        encoding="utf-8",
    )
    # Point the module at the temp table directly. chdir used to work because
    # the default path was CWD-relative -- that was the bug, not the mechanism.
    from agent_system.llm import pricing as pricing_mod
    monkeypatch.setattr(pricing_mod, "DEFAULT_PRICING_PATH", cfg / "llm_pricing.yaml")
    pricing_mod._cache.update(path=None, mtime=None, table={})
    return cfg / "llm_pricing.yaml"


def test_estimate_cost_formula(pricing_file):
    from agent_system.llm.pricing import estimate_cost
    # 1M uncached input + 1M output
    assert estimate_cost("deepseek-chat", 1_000_000, 1_000_000) == pytest.approx(0.28 + 0.42)
    # cached portion billed at cached_input rate
    est = estimate_cost("deepseek-chat", 1_000_000, 0, cached_tokens=1_000_000)
    assert est == pytest.approx(0.028)
    # unknown model -> None (caller keeps cost as dash)
    assert estimate_cost("unknown/model", 1000, 1000) is None


@pytest.mark.asyncio
async def test_track_usage_pricing_fallback(plugin, mock_hook_context, pricing_file):
    """No billed cost + model in the table -> estimated cost, flagged."""
    mock_hook_context.llm.model = "deepseek-chat"
    result = await plugin.hooks_plugin.track_usage(mock_hook_context)
    assert result.success is True
    latest = plugin.tracker.get_latest()
    # 100 prompt + 50 completion at deepseek rates
    assert latest["cost"] == pytest.approx((100 * 0.28 + 50 * 0.42) / 1_000_000)
    assert latest["cost_is_estimate"] is True


@pytest.mark.asyncio
async def test_track_usage_billed_cost_not_overridden(plugin, mock_hook_context, pricing_file):
    """A billed provider cost wins over the table and is NOT flagged."""
    mock_hook_context.llm.model = "deepseek-chat"
    mock_hook_context.llm_response["usage"]["cost"] = 0.777
    await plugin.hooks_plugin.track_usage(mock_hook_context)
    latest = plugin.tracker.get_latest()
    assert latest["cost"] == pytest.approx(0.777)
    assert latest["cost_is_estimate"] is False


@pytest.mark.asyncio
async def test_track_usage_unknown_model_keeps_none(plugin, mock_hook_context, pricing_file):
    mock_hook_context.llm.model = "some/unpriced-model"
    await plugin.hooks_plugin.track_usage(mock_hook_context)
    latest = plugin.tracker.get_latest()
    assert latest["cost"] is None
    assert latest["cost_is_estimate"] is False


@pytest.mark.asyncio
async def test_track_usage_batch_discount_applied(plugin, mock_hook_context, tmp_path, monkeypatch):
    """Batch clients (BatchLLMClient.batch_provider) get the table's
    batch_discount — otherwise estimates would be ~2x the real price."""
    cfg = tmp_path / "config"; cfg.mkdir(exist_ok=True)
    (cfg / "llm_pricing.yaml").write_text(
        "batchy-model:\n  input: 1.0\n  output: 2.0\n  batch_discount: 0.5\n",
        encoding="utf-8")
    from agent_system.llm import pricing as pricing_mod
    monkeypatch.setattr(pricing_mod, "DEFAULT_PRICING_PATH", cfg / "llm_pricing.yaml")
    pricing_mod._cache.update(path=None, mtime=None, table={})

    mock_hook_context.llm.model = "batchy-model"
    mock_hook_context.llm.batch_provider = "anthropic"  # str -> batch
    await plugin.hooks_plugin.track_usage(mock_hook_context)
    latest = plugin.tracker.get_latest()
    full = (100 * 1.0 + 50 * 2.0) / 1_000_000
    assert latest["cost"] == pytest.approx(full * 0.5)
    assert latest["cost_is_estimate"] is True


def test_estimated_calls_tracked_in_rollups(tmp_path):
    """Rollups and statistics separate billed from estimated costs."""
    tracker = UsageTracker(storage_path=tmp_path / "t.json")
    tracker.record_usage(agent_id="a1", agent_name="A1", session_id="s1",
                         total_tokens=10, cost=0.5, cost_is_estimate=False)
    tracker.record_usage(agent_id="a1", agent_name="A1", session_id="s1",
                         total_tokens=10, cost=0.2, cost_is_estimate=True)
    stats = tracker.get_agent_stats()["a1"]
    assert stats["cost_known_calls"] == 2
    assert stats["cost_estimated_calls"] == 1
    totals = tracker.get_statistics()["totals"]
    assert totals["cost_estimated_calls"] == 1
    s_stats = tracker.get_agent_stats(session_id="s1")["a1"]
    assert s_stats["cost_estimated_calls"] == 1


class TestCacheRateStaysBelowOneHundred:
    """Measured in production: 10 of 56 agents showed a cache rate above 100%,
    one at 799%. Per call the numbers were always sound — the aggregate was not.

    total_cached_tokens and total_prompt_tokens were introduced in different
    releases and keep accumulating across upgrades, so an agent active before
    the newer counter existed carries cache reads with no matching prompt
    tokens. The ratio then compares a long history against a short one.
    """

    def _record(self, tracker, *, prompt, cached, n=1):
        for _ in range(n):
            tracker.record_usage(
                agent_id="a", agent_name="A", session_id="s",
                total_tokens=prompt + 10, prompt_tokens=prompt,
                completion_tokens=10, message_count=1, context_window=8000,
                cached_tokens=cached)

    def test_pair_moves_in_lockstep(self, plugin):
        self._record(plugin.tracker, prompt=1000, cached=900, n=3)
        s = plugin.tracker.get_agent_stats()["a"]
        assert s["cache_rate_prompt_tokens"] == 3000
        assert s["cache_rate_cached_tokens"] == 2700
        assert s["cache_rate_cached_tokens"] <= s["cache_rate_prompt_tokens"]

    def test_legacy_stats_do_not_produce_a_rate(self, plugin, tmp_path):
        """The real shape on disk: cached tokens accumulated, the paired base
        absent. The rate must be unavailable, not wrong."""
        import json
        from plugins.context_usage_tracker.tracker import UsageTracker

        path = tmp_path / "usage.json"
        path.write_text(json.dumps({"agents": {"old": {
            "agent_id": "old", "agent_name": "v4_request_analyzer",
            "total_calls": 127, "total_tokens": 320639,
            "total_cached_tokens": 179968,     # accumulated since the old release
            "total_prompt_tokens": 22531,      # only since the newer one
            "total_completion_tokens": 5587,
        }}, "history": []}), encoding="utf-8")

        stats = UsageTracker(storage_path=path).get_agent_stats()["old"]
        # what the panel used to divide -> 799%
        assert stats["total_cached_tokens"] / stats["total_prompt_tokens"] > 7
        # what it divides now -> no base, so no number is claimed
        assert stats["cache_rate_prompt_tokens"] == 0

    def test_a_later_call_makes_the_rate_available_again(self, plugin, tmp_path):
        """Self-healing: the next call for that agent establishes the pair."""
        self._record(plugin.tracker, prompt=500, cached=100)
        s = plugin.tracker.get_agent_stats()["a"]
        rate = s["cache_rate_cached_tokens"] / s["cache_rate_prompt_tokens"] * 100
        assert 0 < rate <= 100

    def test_legacy_stats_are_seeded_from_retained_history(self, tmp_path):
        """Otherwise every agent shows '–' until it next runs. The snapshots
        still on disk carry matched per-call numbers, so the window can start
        from measured data instead of empty."""
        import json
        from plugins.context_usage_tracker.tracker import UsageTracker

        history = [{
            "timestamp": 1.0 + i, "agent_id": "old", "agent_name": "A",
            "session_id": "s", "total_tokens": 1010, "prompt_tokens": 1000,
            "completion_tokens": 10, "message_count": 1, "context_window": 8000,
            "usage_percentage": 12.6, "cached_tokens": 800,
        } for i in range(5)]
        path = tmp_path / "usage.json"
        path.write_text(json.dumps({"agents": {"old": {
            "agent_id": "old", "agent_name": "A",
            "total_cached_tokens": 179968,   # long history
            "total_prompt_tokens": 22531,    # short history -> 799% before
        }}, "history": history}), encoding="utf-8")

        s = UsageTracker(storage_path=path).get_agent_stats()["old"]
        assert s["cache_rate_prompt_tokens"] == 5000
        assert s["cache_rate_cached_tokens"] == 4000
        assert s["cache_rate_cached_tokens"] / s["cache_rate_prompt_tokens"] == 0.8

    def test_seeding_does_not_double_count_on_migration(self, tmp_path):
        """A legacy file whose pair is ALREADY filled must not be seeded again.

        Those snapshots were counted at record time by the version that wrote
        the file; adding them once more would double the window it is divided
        by. Only an empty pair may be seeded.
        """
        from plugins.context_usage_tracker.tracker import UsageTracker

        history = [{
            "timestamp": 1000.0 + i, "agent_id": "a", "agent_name": "A",
            "session_id": "s", "total_tokens": 1010, "prompt_tokens": 1000,
            "completion_tokens": 10, "message_count": 1, "context_window": 8000,
            "usage_percentage": 12.6, "cached_tokens": 800,
        } for i in range(4)]
        path = tmp_path / "usage.json"
        path.write_text(json.dumps({"agents": {"a": {
            "agent_id": "a", "agent_name": "A",
            # Deliberately LARGER than the four retained snapshots would seed:
            # the pair covers every call since it was introduced, the history
            # only the tail. A fixture where both numbers coincide cannot tell
            # a correct guard from a missing one — seeding SETS the value.
            "cache_rate_prompt_tokens": 25_000,
            "cache_rate_cached_tokens": 20_000,
        }}, "history": history}), encoding="utf-8")

        stats = UsageTracker(storage_path=path).get_agent_stats()["a"]
        assert stats["cache_rate_prompt_tokens"] == 25_000, (
            "the pair was overwritten by a re-seed from the retained tail")
        assert stats["cache_rate_cached_tokens"] == 20_000

    def test_legacy_file_is_migrated_exactly_once(self, tmp_path):
        """Repeated starts on the same file must not double the all-time totals.

        A marker row in the database is what makes this idempotent — the file
        is deliberately left untouched, because moving it would be a
        destructive side effect of merely reading usage data, and during a
        rollout an old-code process is still writing that very file.
        """
        from plugins.context_usage_tracker.tracker import UsageTracker

        history = [{
            "timestamp": 1000.0 + i, "agent_id": "a", "agent_name": "A",
            "session_id": "s", "total_tokens": 100, "prompt_tokens": 90,
            "completion_tokens": 10, "message_count": 1, "context_window": 1000,
            "usage_percentage": 10.0,
        } for i in range(5)]
        path = tmp_path / "usage.json"
        path.write_text(json.dumps({"agents": {"a": {
            "agent_id": "a", "agent_name": "A",
            "total_calls": 7, "total_tokens": 700,
        }}, "history": history}), encoding="utf-8")

        tracker = UsageTracker(storage_path=path)
        assert tracker.get_agent_stats()["a"]["total_calls"] == 7
        assert tracker.db.count() == 5
        assert path.exists(), "the legacy file was moved or deleted"

        for round_no in range(3):
            again = UsageTracker(storage_path=path)
            assert again.get_agent_stats()["a"]["total_calls"] == 7, (
                f"the totals were re-imported on start {round_no}")
            # The snapshots are what a re-import actually duplicates: the agent
            # totals go in with INSERT OR REPLACE and would look unchanged.
            assert again.db.count() == 5, (
                f"the history was imported again on start {round_no} "
                f"({again.db.count()} rows instead of 5)")

    def test_construction_touches_nothing_on_disk(self, tmp_path):
        """Building the plugin must not create a store or read the legacy file.

        Several tests boot the real config purely to obtain an Agent, which
        constructs every plugin. With eager initialisation that created a
        database in the production data directory — from a test that never
        recorded anything.
        """
        from plugins.context_usage_tracker.tracker import UsageTracker

        path = tmp_path / "usage.json"
        tracker = UsageTracker(storage_path=path)

        assert list(tmp_path.iterdir()) == [], (
            f"construction wrote to disk: {[p.name for p in tmp_path.iterdir()]}")

        # ...and the store still opens correctly on first real use.
        tracker.record_usage(agent_id="a", agent_name="A", session_id="s",
                             total_tokens=10, context_window=100)
        assert tracker.db_path.exists()


class TestCrossProcessStorage:
    """Two processes share one store — the reason this moved off JSON.

    The file version was read ONCE at construction and rewritten in full on
    every change, so `agent-cli` and `agent-api` each kept a private copy and
    whichever wrote last destroyed the other's runs. Reproduced before the
    change: the CLI's agent was simply absent from the file afterwards.
    """

    @staticmethod
    def _two_trackers(tmp_path):
        from plugins.context_usage_tracker.tracker import UsageTracker
        path = tmp_path / "usage.json"
        return UsageTracker(storage_path=path), UsageTracker(storage_path=path)

    @staticmethod
    def _record(tracker, agent_id, **kw):
        tracker.record_usage(
            agent_id=agent_id, agent_name=kw.pop("agent_name", agent_id),
            session_id=kw.pop("session_id", "s"),
            total_tokens=kw.pop("total_tokens", 100),
            prompt_tokens=kw.pop("prompt_tokens", 90),
            completion_tokens=kw.pop("completion_tokens", 10),
            context_window=kw.pop("context_window", 100000),
            request_id=kw.pop("request_id", "r"), **kw)

    def test_a_second_process_sees_the_first_immediately(self, tmp_path):
        """No restart, no reload: the panel process reads what the CLI wrote."""
        api, cli = self._two_trackers(tmp_path)

        self._record(cli, "cli_agent")

        assert "cli_agent" in api.get_agent_stats(), (
            "the API process cannot see a run recorded by the CLI process")
        assert api.get_latest() is not None

    def test_neither_process_overwrites_the_other(self, tmp_path):
        """The exact failure that was measured: last writer wins, data gone."""
        api, cli = self._two_trackers(tmp_path)

        self._record(cli, "cli_agent")
        self._record(api, "api_agent")

        assert sorted(api.get_agent_stats()) == ["api_agent", "cli_agent"], (
            "one process's runs were destroyed by the other's write")

    def test_totals_of_one_agent_add_up_across_processes(self, tmp_path):
        """Accumulation happens in SQL, not in either process's memory."""
        api, cli = self._two_trackers(tmp_path)

        for i in range(6):
            self._record(cli if i % 2 else api, "shared",
                         total_tokens=100, prompt_tokens=90, cached_tokens=45)

        stats = api.get_agent_stats()["shared"]
        assert stats["total_calls"] == 6
        assert stats["total_tokens"] == 600
        # The lockstep pair must stay divisible — it is why it exists.
        assert stats["cache_rate_prompt_tokens"] == 540
        assert stats["cache_rate_cached_tokens"] == 270

    def test_stale_flag_crosses_the_process_boundary(self, tmp_path):
        """The compaction runs in the worker, the panel renders in the API.

        A flag only the writer can see marks nothing.
        """
        api, worker = self._two_trackers(tmp_path)
        self._record(worker, "a", session_id="s1")

        worker.invalidate_session("s1")

        assert api.get_latest(session_id="s1").get("is_stale") is True
        # ...and a fresh call clears it, in both processes.
        self._record(worker, "a", session_id="s1")
        assert "is_stale" not in api.get_latest(session_id="s1")

    def test_history_is_a_window_not_a_cap(self, tmp_path):
        """max_history bounds the QUERY, no longer the storage.

        The deque silently dropped the oldest entry forever; the database keeps
        it for retention and later analysis.
        """
        from plugins.context_usage_tracker.tracker import UsageTracker
        tracker = UsageTracker(max_history=5, storage_path=tmp_path / "u.json")

        for i in range(12):
            self._record(tracker, "a", request_id=f"r{i}")

        assert len(tracker.get_history()) == 5, "the query window is not applied"
        assert tracker.db.count() == 12, (
            "the database dropped rows — max_history is capping storage again")

    def test_retention_prunes_by_age_and_keeps_the_totals(self, tmp_path):
        """Pruning snapshots must not touch the all-time accumulators.

        They live in their own table exactly so that a retention pass does not
        rewrite an agent's history.
        """
        import time as _time
        from plugins.context_usage_tracker.database import UsageDatabase

        db = UsageDatabase(tmp_path / "u.db", retention_days=1)
        old = _time.time() - 5 * 86400
        for i in range(3):
            db.record({"timestamp": old + i, "agent_id": "a", "agent_name": "A",
                       "session_id": "s", "request_id": f"r{i}",
                       "total_tokens": 100, "prompt_tokens": 90,
                       "completion_tokens": 10, "context_window": 1000})
        db.record({"timestamp": _time.time(), "agent_id": "a", "agent_name": "A",
                   "session_id": "s", "request_id": "new", "total_tokens": 100,
                   "prompt_tokens": 90, "completion_tokens": 10,
                   "context_window": 1000})

        db._apply_retention()

        assert db.count() == 1, "old snapshots were not pruned"
        assert db.agent_stats()["a"]["total_calls"] == 4, (
            "retention reset the all-time totals along with the snapshots")


class TestSharedStoreRegressions:
    """Properties that only became reachable once the store was shared.

    Each of these was found by review or measurement after the SQLite move, and
    each one is a way the old per-process model quietly protected the code.
    """

    @staticmethod
    def _record(tracker, **kw):
        tracker.record_usage(
            agent_id=kw.pop("agent_id", "a"), agent_name=kw.pop("agent_name", "A"),
            session_id=kw.pop("session_id", "s"),
            total_tokens=kw.pop("total_tokens", 100),
            prompt_tokens=kw.pop("prompt_tokens", 90),
            completion_tokens=kw.pop("completion_tokens", 10),
            context_window=kw.pop("context_window", 1000),
            request_id=kw.pop("request_id", "r"), **kw)

    def test_a_quiet_session_is_not_pushed_out_by_a_busy_one(self, tmp_path):
        """The window must narrow BEFORE the limit, not after.

        The deque was per-process, so "last N" meant "last N of mine". Against a
        shared store, taking the newest N globally and filtering afterwards
        makes a quiet session vanish as soon as another process — or a
        coordinator fanning out to sub-agents — fills the window. The rows are
        still there; they just fall outside it.
        """
        from plugins.context_usage_tracker.tracker import UsageTracker
        tracker = UsageTracker(max_history=10, storage_path=tmp_path / "u.json")

        self._record(tracker, session_id="quiet", request_id="q1")
        for i in range(40):                       # the busy neighbour
            self._record(tracker, session_id="busy", request_id=f"b{i}")

        history = tracker.get_history(session_id="quiet")
        assert len(history) == 1, (
            "the quiet session fell out of a globally-taken window")
        assert history[0]["request_id"] == "q1"
        stats = tracker.get_statistics(session_id="quiet")
        assert stats.get("timespan", {}).get("sample_count") == 1

    def test_sub_agent_calls_still_count_towards_the_session(self, tmp_path):
        """The tree rule survived the move into SQL.

        Sub-agents run in their own sessions; their request ids carry the
        parent's as a prefix. Underscores are LIKE wildcards, and these
        prefixes are full of them — the query uses substr for that reason.
        """
        from plugins.context_usage_tracker.tracker import UsageTracker
        tracker = UsageTracker(max_history=100, storage_path=tmp_path / "u.json")

        self._record(tracker, session_id="parent", request_id="req_1")
        self._record(tracker, session_id="sub_a", request_id="req_1_sub_x")
        self._record(tracker, session_id="sub_b", request_id="req_1_sub_x_sub_y")
        self._record(tracker, session_id="stranger", request_id="other_1")

        got = {s["request_id"] for s in tracker.get_history(session_id="parent")}
        assert got == {"req_1", "req_1_sub_x", "req_1_sub_x_sub_y"}, got

    def test_legacy_import_survives_simultaneous_starts(self, tmp_path):
        """agent-api and agent-writer-worker are restarted together.

        A check-then-act over separate transactions let both import: measured
        with three simultaneous starts, the history landed twice. The marker is
        claimed atomically, so exactly one caller may proceed.
        """
        from plugins.context_usage_tracker.database import UsageDatabase

        db = UsageDatabase(tmp_path / "u.db")
        winners = [db.claim_once("k") for _ in range(5)]

        assert winners.count(True) == 1, f"{winners.count(True)} callers claimed it"
        # ...and a failed import gives the claim back so a later start retries.
        db.release_claim("k")
        assert db.claim_once("k") is True

    def test_import_defaults_do_not_invent_a_price(self, tmp_path):
        """A snapshot without a `cost` key is unpriced, not a $0.00 call.

        get_statistics counts a call as priced when `cost is not None`, so a 0
        default would inflate cost_known_calls with calls that never had one.
        The same for latency_ms, and a timestamp of 0 would be deleted by the
        first retention pass.
        """
        import time as _time
        from plugins.context_usage_tracker.database import UsageDatabase

        db = UsageDatabase(tmp_path / "u.db")
        db.import_legacy([{"agent_id": "a", "session_id": "s"}], {})

        row = db.recent_snapshots(10)[0]
        assert row["cost"] is None, "an absent cost became a known 0.00 call"
        assert row["latency_ms"] is None
        assert row["timestamp"] > _time.time() - 60, (
            "an absent timestamp defaulted to 0 and retention would delete it")

    def test_import_keys_agents_by_their_dict_key(self, tmp_path):
        """A legacy entry without an inner agent_id must not collapse.

        Every such row would land on the primary key "" and overwrite the
        previous one — 179 agents become 1.
        """
        from plugins.context_usage_tracker.database import UsageDatabase

        db = UsageDatabase(tmp_path / "u.db")
        db.import_legacy([], {"first": {"agent_name": "A", "total_calls": 3},
                              "second": {"agent_name": "B", "total_calls": 4}})

        stats = db.agent_stats()
        assert sorted(stats) == ["first", "second"], stats
        assert stats["first"]["total_calls"] == 3
        assert stats["second"]["total_calls"] == 4

    def test_retention_delete_uses_the_timestamp_index(self, tmp_path):
        """Retention runs inline in a write transaction — it must not scan.

        The index shipped on `id`, which IS the rowid, so SQLite never used it
        while the age DELETE had nothing to walk.
        """
        from plugins.context_usage_tracker.database import UsageDatabase

        db = UsageDatabase(tmp_path / "u.db")
        plan = " ".join(str(r[-1]) for r in db._get_conn().execute(
            "EXPLAIN QUERY PLAN DELETE FROM usage_snapshots WHERE timestamp < 1"
        ).fetchall())
        assert "idx_usage_timestamp" in plan, f"retention still scans: {plan}"
