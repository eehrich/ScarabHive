"""Spend that belongs to no agent still has to be counted somewhere.

The decisions client calls a provider without an agent around it, so the
agent-level ``post_llm_call`` hook never sees it. Until this hook existed, the
only trace of such a call was a message-debugger row -- pruned by a retention
setting and switchable by a config flag. For chat there were two ledgers, for
that call there was one.

The danger of closing that gap is the reason for half the tests here:
``post_llm_response`` fires for chat calls TOO, with the agent attached, so a
hook that is not careful books every chat call a second time.

The other half exists because the first version of this file got it wrong in
the opposite direction: every test built its own context and filled in the
field it wanted to read, so the suite was green while the hook recorded
nothing at all for TTS -- whose notification carries no usage. A fixture that
invents the input proves the hook works on the input the author imagined.
``TestTheRealDispatchers`` runs the producers instead.
"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_system.hooks.plugin_hook import HookContext, HookType
from plugins.context_usage_tracker.plugin import ContextUsageTrackerHooks
from plugins.context_usage_tracker.tracker import UsageTracker

DECISIONS_USAGE = {"input_tokens": 120, "output_tokens": 4, "cost": 1.5e-05}


@pytest.fixture
def hooks(tmp_path):
    tracker = UsageTracker(storage_path=tmp_path / "usage.json")
    return ContextUsageTrackerHooks(
        Path("src/plugins/context_usage_tracker"), tracker)


def a_response(*, agent=None, usage=DECISIONS_USAGE, error=None, **kwargs):
    """A post_llm_response context, as a client dispatches it."""
    context = Mock(spec=HookContext)
    context.hook_type = HookType.POST_LLM_RESPONSE
    context.agent = agent
    context.agent_name = kwargs.get("agent_name", "openrouter_decisions")
    context.session_id = kwargs.get("session_id", "session_7")
    context.request_id = kwargs.get("request_id", "req_42")
    context.llm_usage = usage
    context.llm_error = error
    context.llm_model = kwargs.get("model", "~typesafe/jev-latest")
    context.llm_provider = "openrouter_decisions"
    context.llm_duration_ms = 812.0
    return context


def snapshots(hooks):
    """The rows the panel and every live total read."""
    return hooks.tracker.get_history()


class TestWhatItCounts:

    @pytest.mark.asyncio
    async def test_a_decisions_call_reaches_the_live_totals(self, hooks):
        result = await hooks.track_non_agent_usage(a_response())

        assert result.success is True
        recorded = snapshots(hooks)
        assert len(recorded) == 1, "the call was not counted at all"
        assert recorded[0]["cost"] == pytest.approx(1.5e-05)
        assert recorded[0]["session_id"] == "session_7"
        assert recorded[0]["request_id"] == "req_42", (
            "without the request id the run it belongs to cannot be summed")

    @pytest.mark.asyncio
    async def test_the_token_counts_arrive_the_way_round_they_were_sent(self, hooks):
        """The tokens are the plugin's main product; the block is a copy of the
        agent path, where swapping the two names is the classic slip."""
        await hooks.track_non_agent_usage(a_response())

        recorded = snapshots(hooks)[0]
        assert recorded["prompt_tokens"] == 120
        assert recorded["completion_tokens"] == 4
        assert recorded["total_tokens"] == 124, "no total was derived from the two"
        assert recorded["latency_ms"] == pytest.approx(812.0)

    @pytest.mark.asyncio
    async def test_a_cached_prompt_is_carried_over_too(self, hooks):
        """Every dialect lands in one shape; dropping a field silently makes
        the row's cache rate 0."""
        anthropic_shaped = {"input_tokens": 300, "output_tokens": 10,
                            "cache_read_input_tokens": 250,
                            "cache_creation_input_tokens": 40, "cost": 2e-05}

        await hooks.track_non_agent_usage(a_response(usage=anthropic_shaped))

        recorded = snapshots(hooks)[0]
        assert recorded["cached_tokens"] == 250
        assert recorded["cache_write_tokens"] == 40

    @pytest.mark.asyncio
    async def test_the_provider_is_who_spent_it(self, hooks):
        """Not the same string twice: the fixture used to set agent_name and
        provider to the same value, so the fallback chain was never tested."""
        await hooks.track_non_agent_usage(
            a_response(agent_name="", ))

        assert snapshots(hooks)[0]["agent_name"] == "openrouter_decisions", (
            "with no agent name the provider has to answer for the spend")

    @pytest.mark.asyncio
    async def test_the_billed_figure_wins_over_any_estimate(self, hooks):
        """A decisions model has no per-token price -- it reports its cost."""
        await hooks.track_non_agent_usage(a_response())

        recorded = snapshots(hooks)[0]
        assert recorded["cost_is_estimate"] is False
        assert recorded["cost"] == pytest.approx(DECISIONS_USAGE["cost"])

    @pytest.mark.asyncio
    async def test_a_call_that_carries_no_conversation_has_no_context_window(self, hooks):
        """0, not a guess: a percentage of a window it does not live in is noise."""
        await hooks.track_non_agent_usage(a_response())

        recorded = snapshots(hooks)[0]
        assert recorded["context_window"] == 0
        assert recorded["usage_percentage"] == 0


class TestWhatItMustNotCount:

    @pytest.mark.asyncio
    async def test_a_chat_call_is_left_to_the_other_hook(self, hooks):
        """The whole point of the guard.

        post_llm_response fires for chat calls as well -- the agent server
        wires it with the agent attached -- and post_llm_call already counts
        those. Without the guard every chat call in the system is booked twice.
        """
        result = await hooks.track_non_agent_usage(a_response(agent=Mock()))

        assert result.success is True
        assert snapshots(hooks) == [], "a chat call was counted a second time"

    @pytest.mark.asyncio
    async def test_a_failed_call_is_not_spend(self, hooks):
        """Clients report retries and failures through the same hook."""
        await hooks.track_non_agent_usage(
            a_response(error="[RETRY 1/3] ReadTimeout: timed out"))

        assert snapshots(hooks) == []

    @pytest.mark.asyncio
    async def test_a_response_without_usage_is_not_a_row(self, hooks):
        await hooks.track_non_agent_usage(a_response(usage=None))

        assert snapshots(hooks) == []

    @pytest.mark.asyncio
    async def test_bookkeeping_never_fails_the_call(self, hooks):
        """The call is already paid for and the answer is in hand.

        A ledger that cannot write -- a locked database, a full disk -- must
        not turn that into a failed decision. The row is lost, the call is not.
        """
        hooks.tracker = Mock()
        hooks.tracker.record_usage.side_effect = OSError("database is locked")

        result = await hooks.track_non_agent_usage(a_response())

        assert result.success is True
        assert hooks.tracker.record_usage.called, "fixture: the write was never attempted"


class TestItIsActuallyRegistered:
    """Every test above calls the method directly -- that proves nothing about
    whether the hook ever runs. What connects the two is schema.yaml."""

    @staticmethod
    def _declared():
        import yaml
        schema = yaml.safe_load(
            (Path("src/plugins/context_usage_tracker/schema.yaml")).read_text(encoding="utf-8"))
        return {h["name"]: h for h in schema["hooks"]}

    def test_the_hook_is_declared_with_the_type_that_carries_agent_less_calls(self):
        hook = self._declared().get("track_non_agent_usage")

        assert hook, "the hook is not in schema.yaml, so it never runs"
        assert hook["type"] == "post_llm_response", (
            "post_llm_call is the agent-level hook -- on that type this hook "
            "would see exactly the calls it must not count")
        assert hook.get("enabled") is True

    def test_every_declared_hook_has_its_method(self):
        for name in self._declared():
            assert callable(getattr(ContextUsageTrackerHooks, name, None)), (
                f"schema.yaml declares {name!r}, the class has no such method")


class TestTheRealDispatchers:
    """What the clients actually send -- not what a fixture imagines."""

    @staticmethod
    async def _with_hook_registered(hooks, fire):
        from agent_system.hooks import HookType, get_hook_registry

        # The name is not decoration: the registry passes its last segment on
        # as `target_hook_name`, and SchemaBasedPluginHook dispatches the
        # schema entry with exactly that name. Registered under anything else,
        # the hook is called and does nothing -- which is how a green suite can
        # cover a hook that never runs.
        name = "context_usage_tracker.track_non_agent_usage"
        registry = get_hook_registry()
        await registry.register_hook(HookType.POST_LLM_RESPONSE, name, hooks)
        try:
            await fire()
        finally:
            await registry.unregister_hook(HookType.POST_LLM_RESPONSE, name)

    @pytest.mark.asyncio
    async def test_a_decisions_call_arrives_through_its_own_client(self, hooks):
        """End to end: the client's notification, the registry, this hook."""
        from plugins.llm_decisions import openrouter

        await self._with_hook_registered(hooks, lambda: openrouter._notify_response(
            model="~typesafe/jev-latest", url=openrouter.DECISIONS_URL,
            session_id="session_7", duration_ms=812.0,
            usage={"input_tokens": 120, "output_tokens": 4, "cost": 1.5e-05},
            data={"answers": {"is_final": True}}))

        recorded = snapshots(hooks)
        assert len(recorded) == 1, "the real client's call was not counted"
        assert recorded[0]["cost"] == pytest.approx(1.5e-05)
        assert recorded[0]["prompt_tokens"] == 120

    @pytest.mark.asyncio
    async def test_a_tts_call_is_knowingly_not_counted(self, hooks):
        """The gap this hook does NOT close, pinned so nobody claims it does.

        `notify_tts_response` reports audio seconds and bytes and takes no
        usage at all, so there is nothing here to book. Wiring it up needs a
        unit this table does not have -- a synthesised minute is not prompt and
        completion tokens. If TTS ever does report a usage, this test turns red
        and whoever changed it has to decide what the row means.
        """
        from agent_system.llm import tts

        await self._with_hook_registered(hooks, lambda: tts.notify_tts_response(
            provider="gemini_tts", model="gemini-2.5-flash-preview-tts",
            url="https://generativelanguage.googleapis.com/v1beta/models",
            duration_ms=4200.0, audio_seconds=37.5, audio_bytes_len=1_200_000))

        assert snapshots(hooks) == [], (
            "a TTS call was booked -- with what token counts?")


class TestItDoesNotDiluteTheStatistics:
    """A row without a window must not drag down how full the window ran."""

    @staticmethod
    def _an_agent_call(hooks):
        hooks.tracker.record_usage(
            agent_id="a1", agent_name="coder", session_id="session_7",
            total_tokens=100_000, prompt_tokens=99_000, completion_tokens=1_000,
            context_window=200_000, cost=0.4, model="sonnet")

    @pytest.mark.asyncio
    async def test_a_windowless_call_is_left_out_of_the_percentages(self, hooks):
        self._an_agent_call(hooks)
        await hooks.track_non_agent_usage(a_response())

        stats = hooks.tracker.get_statistics()

        assert stats["usage_percentage"]["avg"] == pytest.approx(50.0), (
            "the decisions row halved the figure for how full the window ran")
        assert stats["usage_percentage"]["min"] == pytest.approx(50.0), (
            "min would sit at 0 % forever")
        assert stats["tokens"]["max"] == 100_000
        assert stats["timespan"]["sample_count"] == 2, (
            "the call itself still counts -- only its percentage does not")

    @pytest.mark.asyncio
    async def test_nothing_but_windowless_calls_is_zero_not_a_crash(self, hooks):
        await hooks.track_non_agent_usage(a_response())

        stats = hooks.tracker.get_statistics()

        assert stats["usage_percentage"]["avg"] == 0.0
        assert stats["tokens"]["avg"] == 124

