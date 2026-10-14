"""A compaction that does not pay for the cache break it causes is not made.

Measured on a long agent run (Gemini): Layer 1
ran every ~20k tokens of growth and took 2-5 % of the context. Each run rewrote
old messages, so the next call found ~18k of ~130k tokens in the provider's
cache and cost about ten times a cache hit -- for a saving of a tiny
fraction of that per call. A run of layers 1-3 is therefore tried on a copy first and
kept only when it takes ``min_reduction`` of the context.

The other half: the token thresholds assumed a 200k-1M model; on a 128k one
layer1_threshold 140000 lay beyond the window. They are bound to the window of
the model a call goes to.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.context_engineer import hooks as hooks_mod
from plugins.context_engineer.compaction import CompactionConfig, LayeredCompactionStrategy
from plugins.context_engineer.hooks import ContextEngineerPlugin

PLUGIN_DIR = Path(hooks_mod.__file__).parent
BIG = "a large tool result line with enough words to count " * 60   # ~945 tokens
RESULTS = 10

#: Layer 1 alone, due at once; what it may take is decided by tool_result_keep_last.
LAYER_ONE = {"layer1_threshold": 1000, "layer2_threshold": 10**9, "layer3_threshold": 10**9,
             "target_tokens": 500, "always_compact_media_keep_last": 0}


@pytest.fixture
def plugin(tmp_path):
    impl = ContextEngineerPlugin(PLUGIN_DIR, stats_history=[])
    impl._storage_base = tmp_path
    yield impl
    for sid in list(impl._session_components):
        impl.cleanup_session(sid)


def _run_messages(results: int = RESULTS, newest: str | None = None, task: str = "go") -> list[dict]:
    """One agent run of ``results`` steps, a tool call and its result each.

    Every result but the newest has gone out in a request (an assistant message
    follows it); the newest is the round no request carried yet.
    """
    messages = [{"role": "user", "content": task}]
    for i in range(results):
        content = newest if newest is not None and i == results - 1 else f"{i} {BIG}"
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "t", "content": content},
        ]
    return messages


def _session(plugin, session_id: str, **config):
    components = plugin._get_session_components(session_id, overrides={**LAYER_ONE, **config})
    return components["strategy"], components


def _rows(store, table: str) -> int:
    return store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _placeholders(messages: list[dict]) -> list[dict]:
    found = []
    for message in messages:
        try:
            data = json.loads(message.get("content") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("type") in ("tool_result_ref", "archived_ref"):
            found.append(data)
    return found


class TestAShallowRunIsThrownAway:

    @pytest.mark.asyncio
    async def test_the_context_goes_out_unchanged_and_nothing_is_stored(self, plugin):
        """One of ten results is old enough to go: a tenth of the context."""
        strategy, components = _session(plugin, "shallow", tool_result_keep_last=RESULTS - 1)
        messages = _run_messages()
        before = copy.deepcopy(messages)
        tokens = strategy._estimate_messages_tokens(messages)

        result = await strategy.compact(messages, tokens, min_reduction=0.2)

        assert result.discarded_saving and 0 < result.discarded_saving < 0.2 * tokens, (
            f"fixture: the run was not a shallow one ({result.discarded_saving} of {tokens})")
        assert result.modified_messages == before and messages == before
        assert result.tool_results_stored == 0 and 1 not in result.layers_applied
        assert _rows(components["tool_store"], "tool_results") == 0, (
            "a run that was thrown away left its results in the store")

    @pytest.mark.asyncio
    async def test_without_the_gate_the_same_run_is_kept(self, plugin):
        strategy, components = _session(plugin, "ungated", tool_result_keep_last=RESULTS - 1)
        messages = _run_messages()

        result = await strategy.compact(messages, strategy._estimate_messages_tokens(messages))

        assert result.tool_results_stored == 1 and result.discarded_saving is None
        assert _rows(components["tool_store"], "tool_results") == 1

    @pytest.mark.asyncio
    async def test_an_archiving_run_thrown_away_archives_nothing(self, plugin):
        """Layer 2 on old, short turns behind one long message: nothing worth a break."""
        strategy, components = _session(plugin, "archive", layer1_threshold=10**9, layer2_threshold=1,
                                        archive_after_turns=1)
        messages = []
        for turn in range(6):
            messages += [{"role": "user", "content": f"question {turn} " + "word " * 40},
                         {"role": "assistant", "content": f"answer {turn} " + "word " * 40}]
        messages += [{"role": "user", "content": BIG * 20}]
        before = copy.deepcopy(messages)
        tokens = strategy._estimate_messages_tokens(messages)

        result = await strategy.compact(messages, tokens, min_reduction=0.2)

        assert result.discarded_saving is not None, "fixture: Layer 2 was not tried"
        assert result.modified_messages == before and result.messages_archived == 0
        assert _rows(components["archival_memory"], "archived_messages") == 0


    @pytest.mark.asyncio
    @pytest.mark.parametrize("gate", [0.2, 0.0])
    async def test_inline_media_of_a_run_thrown_away_is_not_written_to_disk(self, plugin, tmp_path, gate):
        """An old upload Layer 1 would evict, among results it must keep: little to take."""
        strategy, _ = _session(plugin, f"media-{gate}", tool_result_keep_last=RESULTS)
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "iVBORw0KGgo" * 40}}
        messages = [{"role": "user", "content": [{"type": "text", "text": "look"}, image]},
                    {"role": "assistant", "content": "seen"}, *_run_messages()]
        tokens = strategy._estimate_messages_tokens(messages)

        result = await strategy.compact(messages, tokens, min_reduction=gate)

        stored = [p for p in (tmp_path / f"media-{gate}" / "media").rglob("*") if p.is_file()]
        assert (result.discarded_saving is not None) is bool(gate), "fixture: not the run this is about"
        assert bool(stored) is not bool(gate), stored


class TestADeepRunIsKept:

    @pytest.mark.asyncio
    async def test_the_results_are_stored_and_can_be_read_back(self, plugin):
        """Eight of ten results go. What stands in the conversation must lead to
        the stored text -- not to the stand-ins the trial run on the copy wrote."""
        strategy, components = _session(plugin, "deep", tool_result_keep_last=2)
        messages = _run_messages()
        tokens = strategy._estimate_messages_tokens(messages)

        result = await strategy.compact(messages, tokens, min_reduction=0.2)

        placeholders = _placeholders(result.modified_messages)
        assert result.discarded_saving is None and result.tool_results_stored == RESULTS - 2
        assert len(placeholders) == RESULTS - 2 and result.tokens_saved >= 0.2 * tokens
        store = components["tool_store"]
        assert _rows(store, "tool_results") == RESULTS - 2
        read_back = sorted(store.retrieve(p["ref_id"]).content for p in placeholders)
        assert read_back == sorted(f"{i} {BIG}" for i in range(RESULTS - 2))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", ["manual", "window", "window_off"])
    async def test_a_shallow_run_someone_needs_is_kept(self, plugin, how):
        """/compact is a person's request; past force_above_window_share the next call would not fit."""
        strategy, _ = _session(plugin, f"must-{how}", tool_result_keep_last=RESULTS - 1,
                               force_above_window_share=0 if how == "window_off" else 0.85)
        messages = _run_messages()
        tokens = strategy._estimate_messages_tokens(messages)

        result = await strategy.compact(
            messages, tokens, min_reduction=0.2, manual=how == "manual",
            # window: the context fills 90 % of it
            context_window=None if how == "manual" else int(tokens / 0.9))

        assert (result.tool_results_stored == 1) is (how != "window_off"), how

    @pytest.mark.asyncio
    async def test_a_shallow_run_for_the_byte_limit_is_kept(self, plugin):
        """Over max_request_bytes the provider refuses the request: nothing to weigh."""
        strategy, _ = _session(plugin, "must-bytes", tool_result_keep_last=RESULTS - 1,
                               max_request_bytes=1000, target_request_bytes=500)
        messages = _run_messages()

        result = await strategy.compact(messages, strategy._estimate_messages_tokens(messages),
                                        min_reduction=0.2)

        assert result.discarded_saving is None and result.tool_results_stored >= 1


class TestWhatCostsNoCache:
    """A rewrite behind the point where this call has lost the cache anyway is free."""

    @pytest.mark.asyncio
    async def test_a_new_result_too_large_to_stay_inline_is_stored_at_once(self, plugin):
        """Sent inline once, it is part of the cached prefix, and taking it out later is a break."""
        strategy, _ = _session(plugin, "arrival", tool_result_keep_last=RESULTS,
                               tool_result_max_inline_size=1500)
        messages = _run_messages(newest=BIG * 2, task=BIG * 20)
        tokens = strategy._estimate_messages_tokens(messages)

        result = await strategy.compact(messages, tokens, min_reduction=0.2)

        assert result.tokens_saved < 0.2 * tokens, "fixture: the run passes on its share alone"
        assert result.tool_results_stored == 1 and result.discarded_saving is None
        assert _placeholders(result.modified_messages[-1:]), "the new result went out inline"

    @pytest.mark.asyncio
    async def test_of_a_run_thrown_away_the_part_that_costs_nothing_is_taken(self, plugin):
        """One old result (a break, and not worth it) and the oversized new one (free)."""
        strategy, components = _session(plugin, "part", tool_result_keep_last=RESULTS - 1,
                                        tool_result_max_inline_size=1500)
        messages = _run_messages(newest=BIG * 2, task=BIG * 20)
        sent = copy.deepcopy(messages[:-1])

        result = await strategy.compact(messages, strategy._estimate_messages_tokens(messages),
                                        min_reduction=0.2)

        assert result.discarded_saving is not None, "fixture: the run was kept whole"
        assert result.modified_messages[:-1] == sent, "a message a request had carried was rewritten"
        assert _placeholders(result.modified_messages[-1:]) and result.tool_results_stored == 1
        assert _rows(components["tool_store"], "tool_results") == 1 and 1 in result.layers_applied

    @pytest.mark.asyncio
    async def test_layer_one_rides_along_with_a_prune_that_rewrote_the_front(self, plugin):
        """Pre-Layer P cut the front in this call: the cache is gone from message 0 on."""
        strategy, _ = _session(plugin, "prune", tool_result_keep_last=3, max_messages=12,
                               max_messages_prune_to=10, tool_result_max_inline_size=10**6)
        messages = _run_messages(12, newest=BIG * 30)
        tokens = strategy._estimate_messages_tokens(messages)

        result = await strategy.compact(messages, tokens, min_reduction=0.2)

        assert "P" in result.layers_applied, "fixture: nothing was pruned"
        assert result.tool_results_stored >= 1 and result.discarded_saving is None


class TestTheHook:
    """Through the real hook: its gates, its hold, the window of the model."""

    CONFIG = {"layer1_threshold": 100_000, "layer2_threshold": 10**9, "layer3_threshold": 10**9,
              "min_tokens_between_compactions": 20_000, "always_compact_media_keep_last": 0,
              "tool_result_keep_last": 5}
    #: No token threshold of its own: the plugin's, bound to the model's window.
    PLUGIN_THRESHOLDS = {"min_tokens_between_compactions": 20_000, "always_compact_media_keep_last": 0,
                         "tool_result_keep_last": 5}

    async def _call(self, plugin, session, tokens, monkeypatch, llm=None, base=None, **config):
        monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (tokens, True))
        return await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id=session, llm=llm,
            messages=[ChatMessage(**m) for m in _run_messages(8)],
            hook_config={**(self.CONFIG if base is None else base), **config}))

    @pytest.fixture
    def compactions(self, monkeypatch):
        """rewrite_layers of every compact() call, in order."""
        seen = []
        original = LayeredCompactionStrategy.compact

        async def recording(self, *args, **kwargs):
            seen.append(kwargs.get("rewrite_layers", True))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(LayeredCompactionStrategy, "compact", recording)
        return seen

    @pytest.mark.asyncio
    async def test_a_run_thrown_away_is_not_tried_again_on_the_next_call(
            self, plugin, monkeypatch, compactions):
        """150k read, three results of ~950 tokens to take: thrown away. The same
        attempt on every following call would cost the copy and the estimates each time."""
        first = await self._call(plugin, "hold", 150_000, monkeypatch)
        held = await self._call(plugin, "hold", 155_000, monkeypatch)
        await self._call(plugin, "hold", 156_000, monkeypatch)
        await self._call(plugin, "hold", 176_000, monkeypatch)   # +20k over the base

        results = [m for m in first.context.messages if m.role == "tool"]
        assert len(results) == 8 and all(BIG in m.content for m in results), (
            "the run was thrown away and the conversation changed anyway")
        assert (held.metadata or {}).get("reason") == "hysteresis"
        assert compactions == [True, True], "tried on every call, or never again"
        assert first.metadata.get("reason") == "not_worth_it" and first.metadata["discarded_saving"] > 0, (
            "the caller of the compact tool reads no word on why nothing happened")

    @pytest.mark.asyncio
    async def test_a_run_thrown_away_does_not_hold_the_one_the_window_forces(
            self, plugin, monkeypatch, compactions):
        """128k model: thrown away at 100k, forced from 108.8k on. Held by the first
        run's mark, the context stood at 99 % of the window before anything ran."""
        llm = SimpleNamespace(context_window=128_000)
        plugin.layer1_threshold, plugin.layer2_threshold, plugin.layer3_threshold = 140_000, 10**9, 10**9

        async def call(tokens):
            return await self._call(plugin, "tight", tokens, monkeypatch, llm=llm, base=self.PLUGIN_THRESHOLDS)

        thrown_away = await call(100_000)
        forced = await call(110_000)
        again = await call(112_000)

        assert thrown_away.metadata.get("reason") == "not_worth_it", "fixture: the first run was kept"
        assert forced.metadata["tool_results_stored"] > 0, "the forced run was held back"
        assert (again.metadata or {}).get("reason") == "hysteresis", (
            "past the share every call runs the layers again")
        assert compactions == [True, True]

    @pytest.mark.asyncio
    async def test_a_run_that_is_worth_it_goes_through_the_hook(self, plugin, monkeypatch):
        """The reading fits the messages (~8k): six of eight results are most of it."""
        result = await self._call(plugin, "kept", 9_000, monkeypatch, layer1_threshold=1_000,
                                  target_tokens=500, tool_result_keep_last=2)

        assert result.metadata.get("reason") is None and result.metadata["tool_results_stored"] == 6
        assert len(_placeholders([m.model_dump() for m in result.context.messages])) == 6

    @pytest.mark.asyncio
    async def test_a_forced_run_that_left_the_window_tight_is_tried_again_before_its_end(
            self, plugin, monkeypatch, compactions):
        """128k model, forced at 110k, still at 112k after it: 16k of window left and
        a hold of 20000 -- the next forced run came past the end."""
        llm = SimpleNamespace(context_window=128_000)
        plugin.layer1_threshold, plugin.layer2_threshold, plugin.layer3_threshold = 140_000, 10**9, 10**9

        async def call(tokens):
            return await self._call(plugin, "tight-again", tokens, monkeypatch, llm=llm,
                                    base=self.PLUGIN_THRESHOLDS)

        await call(110_000)
        held = await call(112_000)
        still_held = await call(119_000)
        again = await call(120_000)       # half of the 16k left above the reading after the run

        assert (held.metadata or {}).get("reason") == "hysteresis"
        assert (still_held.metadata or {}).get("reason") == "hysteresis", "every call past the share runs again"
        assert (again.metadata or {}).get("reason") != "hysteresis", f"held to the window's end: {again.metadata}"
        assert compactions == [True, True]

    @pytest.mark.asyncio
    async def test_the_hold_between_two_runs_fits_a_small_window(self, plugin, monkeypatch, compactions):
        """32k model: a run at 17.6k, the reading back at 10k, at 18.5k again. Held
        for 20000 tokens of growth, the next run came at 100 % of the window."""
        llm = SimpleNamespace(context_window=32_000)
        plugin.layer1_threshold, plugin.layer2_threshold, plugin.layer3_threshold = 140_000, 170_000, 200_000
        plugin.target_tokens = 70_000

        async def call(tokens):
            return await self._call(plugin, "small", tokens, monkeypatch, llm=llm,
                                    base=self.PLUGIN_THRESHOLDS, min_reduction=0)

        first = await call(17_600)
        at_once = await call(17_900)
        await call(10_000)
        again = await call(18_500)

        assert first.metadata["tool_results_stored"] > 0, "fixture: the first run did not run"
        assert (at_once.metadata or {}).get("reason") == "hysteresis", "there is no hold left at all"
        assert again.metadata.get("tool_results_stored", 0) > 0, (
            f"8500 tokens above the last reading and still held: {again.metadata}")
        assert compactions == [True, True]

    @pytest.mark.asyncio
    async def test_the_binding_is_the_calls_not_the_sessions(self, plugin, monkeypatch):
        """A fallback model with a small window answered one step; the next goes to the large one."""
        plugin.layer1_threshold = 140_000

        async def reason(window):
            result = await self._call(plugin, "fallback", 80_000, monkeypatch, base=self.PLUGIN_THRESHOLDS,
                                      llm=SimpleNamespace(context_window=window), min_reduction=0)
            return (result.metadata or {}).get("reason")

        assert [await reason(1_048_576), await reason(128_000) != "below_threshold",
                await reason(1_048_576)] == ["below_threshold", True, "below_threshold"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("window, opens", [(128_000, True), (1_048_576, False), (None, False)])
    async def test_the_thresholds_follow_the_window_of_the_model(
            self, plugin, monkeypatch, window, opens):
        """80k tokens: over 55 % of a 128k window, far below 140k."""
        plugin.layer1_threshold = 140_000
        llm = SimpleNamespace(context_window=window)

        result = await self._call(plugin, f"window-{window}", 80_000, monkeypatch, llm=llm,
                                  layer1_threshold=None)

        assert ((result.metadata or {}).get("reason") != "below_threshold") is opens

    @pytest.mark.asyncio
    @pytest.mark.parametrize("own_share, opens", [(None, False), (0.5, True)])
    async def test_an_agents_own_token_threshold_is_not_bound_by_an_inherited_share(
            self, plugin, monkeypatch, own_share, opens):
        """9,000,000 is an agent's "never"; the plugin's 55 % of a 128k window must not undo it."""
        llm = SimpleNamespace(context_window=128_000)

        result = await self._call(plugin, f"own-{own_share}", 100_000, monkeypatch, llm=llm,
                                  layer1_threshold=9_000_000, layer1_window_share=own_share)

        assert ((result.metadata or {}).get("reason") != "below_threshold") is opens


@pytest.mark.parametrize("window, expected", [
    (None, (140_000, 170_000, 200_000, 70_000)),
    (128_000, (70_400, 83_200, 96_000, 38_400)),
    (200_000, (110_000, 130_000, 150_000, 60_000)),
    (1_048_576, (140_000, 170_000, 200_000, 70_000)),   # the token values are the ceiling
])
def test_a_share_lowers_a_threshold_and_never_raises_it(window, expected):
    config = CompactionConfig(layer1_threshold=140_000, layer2_threshold=170_000, layer3_threshold=200_000,
                              target_tokens=70_000, layer1_window_share=0.55, layer2_window_share=0.65,
                              layer3_window_share=0.75, target_window_share=0.30)

    bound = config.for_window(window)

    assert (bound.layer1_threshold, bound.layer2_threshold, bound.layer3_threshold,
            bound.target_tokens) == expected
    assert config.layer1_threshold == 140_000, "the session's config itself was changed"


@pytest.mark.parametrize("window, hold", [
    (32_000, 8_000),        # 17.6k - 9.6k: the way from the target back up to Layer 1
    (64_000, 16_000),
    (128_000, 20_000),      # 32k of way: the configured hold is the shorter one
    (1_048_576, 20_000),
])
def test_the_hold_comes_down_with_the_thresholds(window, hold):
    config = CompactionConfig(layer1_threshold=140_000, target_tokens=70_000, layer1_window_share=0.55,
                              target_window_share=0.30, min_tokens_between_compactions=20_000)

    assert config.for_window(window).min_tokens_between_compactions == hold
