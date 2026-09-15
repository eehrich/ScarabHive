"""The hook around the compaction: what it removes, reports and configures.

One test per defect found in the review of 2026-09-13, each reproduced first.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.context_engineer import hooks as hooks_mod
from plugins.context_engineer.compaction import LayeredCompactionStrategy
from plugins.context_engineer.hooks import ContextEngineerPlugin

PLUGIN_DIR = Path(hooks_mod.__file__).parent


@pytest.fixture
def plugin(tmp_path):
    impl = ContextEngineerPlugin(PLUGIN_DIR, stats_history=[])
    impl._storage_base = tmp_path
    yield impl
    for sid in list(impl._session_components):
        impl.cleanup_session(sid)


def _context(session_id, messages, **fields):
    return HookContext(hook_type=HookType.PRE_LLM_CALL, request_id=f"req-{session_id}",
                       session_id=session_id, messages=messages,
                       metadata={"manual_trigger": True}, **fields)


class TestTheRestorationBlock:

    @pytest.mark.asyncio
    async def test_only_system_copies_are_removed_by_their_header(self, plugin):
        header = hooks_mod._RESTORATION_HEADER
        messages = [
            ChatMessage(role="system", content="You are a coder."),
            ChatMessage(role="system", content=f"{header}\nold copy"),
            ChatMessage(role="user", content=f"why does hooks.py contain '{header}'?"),
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]),
            ChatMessage(role="tool", tool_call_id="c1", name="read_file",
                        content=f'_RESTORATION_HEADER = "{header}"'),
        ]
        result = await plugin.engineer_context(_context("restore", messages))

        kept = result.context.messages if result.context else messages
        roles_with_header = [m.role for m in kept
                             if isinstance(m.content, str) and header in m.content
                             and getattr(m, "injected_by", None) != hooks_mod._RESTORATION_MARKER]
        assert "tool" in roles_with_header, "a tool result that quotes the header was deleted"
        assert "user" in roles_with_header, "a user message that quotes the header was deleted"
        assert "system" not in roles_with_header, "the legacy system copy stayed"


class TestPerAgentOverrides:

    def test_overrides_arriving_after_a_bare_caller_still_apply(self, plugin):
        components = plugin._get_session_components("ov")  # e.g. the WebUI stats poll
        default = components["strategy"].config.tool_result_keep_last

        plugin._get_session_components("ov", overrides={"tool_result_keep_last": default + 7})
        assert components["strategy"].config.tool_result_keep_last == default + 7

        plugin._get_session_components("ov")  # a bare caller again keeps them
        assert components["strategy"].config.tool_result_keep_last == default + 7

    @pytest.mark.asyncio
    async def test_the_compact_tool_brings_the_agents_overrides(self, plugin):
        from agent_system.config.models import MCPConfig
        from unittest.mock import MagicMock

        from plugins.context_engineer.server import ContextEngineerServer

        srv = ContextEngineerServer("context_engineer", MagicMock(),
                                    MCPConfig(type="context_engineer", enabled=True))
        srv._hooks_impl = plugin
        seen = {}

        async def spy(context):
            seen["hook_config"] = context.hook_config
            return SimpleNamespace(success=True, modified=False, context=None, metadata={}, error=None)

        plugin.engineer_context = spy
        agent = SimpleNamespace(
            name="a", llm=None,
            agent_config=SimpleNamespace(hooks=SimpleNamespace(overrides={
                "context_engineer.engineer_context": {"enabled": True, "tool_result_keep_last": 9}})),
            get_live_messages=lambda sid: [ChatMessage(role="user", content="hi")],
        )
        await srv.compact({"_session_id": "s", "_agent": agent})

        assert seen["hook_config"] == {"tool_result_keep_last": 9}


class TestTheCompactTool:

    @staticmethod
    def _tool(plugin, metadata):
        from agent_system.config.models import MCPConfig
        from unittest.mock import MagicMock

        from plugins.context_engineer.server import ContextEngineerServer

        srv = ContextEngineerServer("context_engineer", MagicMock(),
                                    MCPConfig(type="context_engineer", enabled=True))
        srv._hooks_impl = plugin
        seen = {}

        async def spy(context):
            seen["manual"] = context.metadata["manual_trigger"]
            seen["partial_view"] = context.metadata.get("partial_view")
            return SimpleNamespace(
                success=True, modified=True, error=None, metadata=metadata,
                context=SimpleNamespace(messages=[ChatMessage(role="user", content="hi")]))

        plugin.engineer_context = spy
        staged = []
        agent = SimpleNamespace(
            name="a", llm=None, agent_config=None,
            get_live_messages=lambda sid: [ChatMessage(role="user", content="hi")],
            _session_tracker=SimpleNamespace(
                set_compacted_messages=lambda sid, msgs: staged.append(msgs)),
        )
        return srv, seen, agent, staged

    @pytest.mark.asyncio
    @pytest.mark.parametrize("request_id, by_person", [(None, True), ("req-7_003", False)])
    async def test_only_a_person_bypasses_the_automatic_rules(self, plugin, request_id, by_person):
        """A /compact typed at the prompt dispatches without a request id; the
        model calling the tool mid-run always has one."""
        srv, seen, agent, _ = self._tool(plugin, {"compacted": True})
        params = {"_session_id": "s", "_agent": agent}
        if request_id:
            params["_request_id"] = request_id
        await srv.compact(params)

        assert seen["manual"] is by_person
        assert seen["partial_view"] is True, (
            "the tool reads without the system prompt and did not say so")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("by_person", [True, False])
    async def test_only_a_run_that_changed_something_is_staged(self, plugin, by_person):
        """Through the real hook: it stages on its own, not only the tool.

        The model's call below the thresholds changes nothing; a person's
        /compact runs Layer 1 and stores the old results.
        """
        from agent_system.config.models import MCPConfig
        from unittest.mock import MagicMock

        from plugins.context_engineer.server import ContextEngineerServer

        srv = ContextEngineerServer("context_engineer", MagicMock(),
                                    MCPConfig(type="context_engineer", enabled=True))
        srv._hooks_impl = plugin
        staged = []
        overrides = {"context_engineer.engineer_context": {
            "enabled": True, "always_compact_media_keep_last": 2, "tool_result_keep_last": 2,
            "tool_result_min_size": 100}}
        agent = SimpleNamespace(
            name="a", llm=None,
            agent_config=SimpleNamespace(hooks=SimpleNamespace(overrides=overrides)),
            get_live_messages=lambda sid: TestHysteresis()._run_messages(6),
            _session_tracker=SimpleNamespace(
                set_compacted_messages=lambda sid, msgs: staged.append(msgs)),
        )
        params = {"_session_id": "s", "_agent": agent}
        if not by_person:
            params["_request_id"] = "req-7_003"
        answer = await srv.compact(params)

        assert answer["status"] == "success", answer
        assert bool(staged) is by_person, (
            "a run that changed nothing was staged — the agent rebuilds its list from it "
            "and drops leading system messages" if not by_person
            else "fixture: the person's compaction was not staged")


class TestHysteresis:
    """After a rewriting run the next waits for min_tokens_between_compactions of growth."""

    BIG = "a large tool result line with enough words to count " * 60
    CONFIG = {"layer1_threshold": 100_000, "layer2_threshold": 10**9, "layer3_threshold": 10**9,
              "min_tokens_between_compactions": 20_000, "always_compact_media_keep_last": 2,
              "tool_result_keep_last": 5}

    def _run_messages(self, results: int):
        return [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                for i in range(results)]),
            *[ChatMessage(role="tool", tool_call_id=f"c{i}", name="t", content=self.BIG)
              for i in range(results)],
        ]

    async def _call(self, plugin, session, tokens, monkeypatch, results=8, messages=None, **config):
        monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (tokens, True))
        return await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id=session,
            messages=messages if messages is not None else self._run_messages(results),
            hook_config={**self.CONFIG, **config}))

    @pytest.fixture
    def rewrites(self, monkeypatch):
        """rewrite_layers of every compact() call, in order."""
        seen = []
        original = LayeredCompactionStrategy.compact

        async def recording(self, *args, **kwargs):
            seen.append(kwargs.get("rewrite_layers", True))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(LayeredCompactionStrategy, "compact", recording)
        return seen

    @pytest.mark.asyncio
    async def test_a_run_that_stays_above_the_threshold_waits_for_growth(
            self, plugin, monkeypatch, rewrites):
        first = await self._call(plugin, "hy", 150_000, monkeypatch)   # first run: layers
        held = await self._call(plugin, "hy", 155_000, monkeypatch)    # the base: held
        await self._call(plugin, "hy", 175_000, monkeypatch)           # +20k over the base

        assert rewrites[0] is True and 1 in first.metadata["layers_applied"]
        assert rewrites[1] is False, "the layers ran again right after a compaction"
        assert 1 not in (held.metadata or {}).get("layers_applied", []), (
            "held, and Layer 1 rewrote the messages anyway")
        assert rewrites[2] is True, "the layers stayed held after enough growth"

    @pytest.mark.asyncio
    async def test_the_base_is_the_next_reading_not_the_runs_own_estimate(
            self, plugin, monkeypatch, rewrites):
        """The run reports original minus ESTIMATED savings; the provider counts
        30 % more. Measured against that, the next call had grown by 30 % of the
        context without a single new token and compacted again."""
        await self._call(plugin, "scale", 150_000, monkeypatch)
        await self._call(plugin, "scale", 190_000, monkeypatch)   # same messages, provider scale

        assert rewrites == [True, False], "a reading on another scale released the hold"

    @pytest.mark.asyncio
    async def test_a_context_that_shrank_lowers_the_base(self, plugin, monkeypatch, rewrites):
        await self._call(plugin, "shrink", 150_000, monkeypatch)
        await self._call(plugin, "shrink", 152_000, monkeypatch)   # base
        await self._call(plugin, "shrink", 60_000, monkeypatch)    # context_summarizer ran
        await self._call(plugin, "shrink", 110_000, monkeypatch)   # +50k since, above L1

        assert rewrites[3] is True, "held against a base the context had long left"

    @pytest.mark.asyncio
    async def test_the_base_follows_the_calls_the_threshold_gate_turns_away(
            self, plugin, monkeypatch, rewrites):
        """Without always-media the gate returns before the hysteresis."""
        plain = {"always_compact_media_keep_last": 0, "compact_media_after_user_message": False}
        await self._call(plugin, "gate", 150_000, monkeypatch, **plain)
        below = await self._call(plugin, "gate", 60_000, monkeypatch, **plain)
        await self._call(plugin, "gate", 105_000, monkeypatch, **plain)

        assert below.metadata["reason"] == "below_threshold", "fixture: the gate did not turn it away"
        assert rewrites == [True, True], (
            "the first reading over the threshold became the base and held its own compaction")

    @pytest.mark.asyncio
    async def test_a_deeper_layer_passes_the_hold(self, plugin, monkeypatch, rewrites):
        deeper = {"layer2_threshold": 160_000}
        await self._call(plugin, "deep", 150_000, monkeypatch, **deeper)
        await self._call(plugin, "deep", 152_000, monkeypatch, **deeper)   # base
        await self._call(plugin, "deep", 158_000, monkeypatch, **deeper)   # still layer 1
        await self._call(plugin, "deep", 165_000, monkeypatch, **deeper)   # layer 2 due now

        assert rewrites[2] is False, "fixture: the hold did not hold below layer 2"
        assert rewrites[3] is True, "layer 2 became due and was held back anyway"

    @pytest.mark.asyncio
    async def test_media_passes_alone_set_no_mark(self, plugin, monkeypatch, tmp_path):
        # Below the threshold only the media passes run: no mark, nothing held later.
        messages = [ChatMessage(role="user", content="go")]
        for i in range(4):
            image = tmp_path / f"shot{i}.png"
            image.write_bytes(b"x" * 20_000)
            messages += [
                ChatMessage(role="assistant", content=None, tool_calls=[
                    {"id": f"m{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]),
                ChatMessage(role="tool", tool_call_id=f"m{i}", name="t", content="ok",
                            multimodal_content=[{"type": "image", "path": str(image),
                                                 "mime_type": "image/png"}]),
            ]
        result = await self._call(plugin, "media", 50_000, monkeypatch, messages=messages)

        assert "M" in result.metadata["layers_applied"], "fixture: no media pass did anything"
        assert "media" not in plugin._hysteresis_marks

    @pytest.mark.asyncio
    async def test_marks_outlive_the_session_components_but_not_the_cap(
            self, plugin, monkeypatch):
        monkeypatch.setattr(hooks_mod, "_MAX_HYSTERESIS_MARKS", 2)
        for session in ("a", "b", "c"):
            await self._call(plugin, session, 150_000, monkeypatch)
        assert list(plugin._hysteresis_marks) == ["b", "c"], "the oldest mark was not the one dropped"

        plugin.cleanup_session("c")   # idle TTL or LRU eviction of the stores
        assert "c" in plugin._hysteresis_marks, (
            "an evicted session lost its mark and compacts again on its next call")

    @pytest.mark.asyncio
    async def test_a_reading_of_part_of_the_picture_moves_no_base(self, plugin, monkeypatch, rewrites):
        """Stale provider counts, and the compact tool's reading without the system prompt."""
        await self._call(plugin, "part", 150_000, monkeypatch)
        await self._call(plugin, "part", 152_000, monkeypatch)                  # base
        monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (120_000, False))
        await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="part",
            messages=self._run_messages(8), hook_config=self.CONFIG))          # stale
        await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="part",
            messages=self._run_messages(8), hook_config=self.CONFIG,
            metadata={"partial_view": True}))                                  # the tool's
        monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (130_000, True))
        await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="part",
            messages=self._run_messages(8), hook_config=self.CONFIG,
            metadata={"partial_view": True}))
        await self._call(plugin, "part", 160_000, monkeypatch)                  # +8k

        assert plugin._hysteresis_marks["part"].tokens == 152_000, "a partial reading moved the base"
        assert rewrites[-1] is False, "released by the gap between two scales, not by growth"

    @staticmethod
    def _tracked(*snapshots):
        """An agent whose usage tracker hands out these snapshots, one per reading."""
        latest = iter(snapshots)
        tracker = SimpleNamespace(get_latest=lambda session_id: next(latest))
        registry = SimpleNamespace(get_server=lambda name: SimpleNamespace(tracker=tracker))
        return SimpleNamespace(system_config=SimpleNamespace(mcp_registry=registry))

    def test_a_stale_provider_count_is_not_a_whole_reading(self, plugin):
        strategy = plugin._get_session_components("read")["strategy"]
        agent = self._tracked({"prompt_tokens": 150_000},
                              {"prompt_tokens": 150_000, "is_stale": True})
        context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r",
                              session_id="read", messages=[], agent=agent)
        messages = [{"role": "user", "content": "hi"}]

        fresh = plugin._read_tokens(context, messages, strategy)
        stale = plugin._read_tokens(context, messages, strategy)

        assert fresh == (150_000, True)
        assert stale[1] is False and stale[0] < 150_000, stale

    @pytest.mark.asyncio
    async def test_a_tracker_that_stopped_recording_does_not_hold_for_good(
            self, plugin, monkeypatch, rewrites):
        deep = {**self.CONFIG, "layer3_threshold": 200_000}
        await self._call(plugin, "silent", 210_000, monkeypatch, layer3_threshold=200_000)

        async def stale(tokens):
            monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (tokens, False))
            await plugin.engineer_context(HookContext(
                hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="silent",
                messages=self._run_messages(8), hook_config=deep))

        await stale(215_000)
        assert plugin._hysteresis_marks["silent"].tokens is None, (
            "one stale reading — normal after a compaction mid-step — set the base")
        await stale(215_000)
        assert plugin._hysteresis_marks["silent"].tokens == 215_000, (
            "no base after readings that could never be fresh again")
        await stale(240_000)

        assert rewrites[-1] is True, "a level-3 mark held every layer for good"

    @pytest.mark.asyncio
    async def test_a_base_from_stale_readings_gives_way_to_the_next_whole_one(
            self, plugin, monkeypatch, rewrites):
        """The stale base is on the estimate's scale; the provider counts 30 % more."""
        await self._call(plugin, "back", 150_000, monkeypatch)
        monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (150_000, False))
        for _ in range(2):
            await plugin.engineer_context(HookContext(
                hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="back",
                messages=self._run_messages(8), hook_config=self.CONFIG))
        assert plugin._hysteresis_marks["back"].tokens == 150_000, "fixture: no stale base"

        await self._call(plugin, "back", 195_000, monkeypatch)   # usage recorded again

        assert rewrites[-1] is False, "the gap between the two scales released the hold"
        assert plugin._hysteresis_marks["back"].tokens == 195_000

    @pytest.mark.asyncio
    async def test_no_base_yet_means_no_growth(self, plugin, monkeypatch, rewrites):
        """Right after a run, a partial reading may not set the base — and must not release."""
        await self._call(plugin, "fresh", 150_000, monkeypatch)
        await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="fresh",
            messages=self._run_messages(8), hook_config=self.CONFIG,
            metadata={"partial_view": True}))

        assert plugin._hysteresis_marks["fresh"].tokens is None, "fixture: the base was set"
        assert rewrites == [True, False], "compacted again right after a compaction"

    @pytest.mark.asyncio
    async def test_manual_is_never_held(self, plugin, monkeypatch):
        await self._call(plugin, "man", 150_000, monkeypatch)
        monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (151_000, True))
        seen = {}
        original = LayeredCompactionStrategy.compact

        async def recording(self, *args, **kwargs):
            seen["rewrite_layers"] = kwargs.get("rewrite_layers", True)
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(LayeredCompactionStrategy, "compact", recording)
        await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="man",
            messages=self._run_messages(8), metadata={"manual_trigger": True},
            hook_config={"layer1_threshold": 100_000, "min_tokens_between_compactions": 20_000}))
        assert seen["rewrite_layers"] is True


class TestTheBlockBehindTheSystemPrompt:
    """It changes only when the prompt's front is rewritten anyway."""

    CONFIG = TestHysteresis.CONFIG

    @staticmethod
    def _block(result):
        return next((m.content for m in result.context.messages
                     if getattr(m, "injected_by", None) == hooks_mod._RESTORATION_MARKER), "")

    async def _call(self, plugin, tokens, monkeypatch, messages, **config):
        monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (tokens, True))
        return await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="facts",
            messages=messages, hook_config={**self.CONFIG, **config}))

    @staticmethod
    def _chat(turns):
        messages = [ChatMessage(role="user", content="the task")]
        for t in range(turns):
            messages += [ChatMessage(role="assistant", content=f"answer {t}"),
                         ChatMessage(role="user", content=f"go on {t}")]
        return messages

    @pytest.mark.asyncio
    async def test_a_stored_fact_waits_for_a_rewrite_of_the_front(self, plugin, monkeypatch):
        run = TestHysteresis()._run_messages(8)
        await plugin._handle_store_fact("the deploy target is api1", session_id="facts")
        await self._call(plugin, 150_000, monkeypatch, run)
        await plugin._handle_store_fact("the database is books.db", session_id="facts")
        held = await self._call(plugin, 155_000, monkeypatch, run)
        layer_one = await self._call(plugin, 180_000, monkeypatch, run)   # stores, front untouched

        assert "api1" in self._block(held), "fixture: the block does not list the facts"
        assert 1 in layer_one.metadata["layers_applied"], "fixture: Layer 1 did not run"
        assert "books.db" not in self._block(held) + self._block(layer_one), (
            "a stored fact rewrote the front of the prompt without a rewrite there")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rewrite", ["shorter", "new_head"])
    async def test_a_conversation_rewritten_elsewhere_shows_the_fact(
            self, plugin, monkeypatch, rewrite):
        """context_summarizer runs after this hook and may take the store_fact call with it."""
        await self._call(plugin, 50_000, monkeypatch, self._chat(20))
        await plugin._handle_store_fact("the database is books.db", session_id="facts")
        same = await self._call(plugin, 50_000, monkeypatch, self._chat(21))
        summarized = (self._chat(3) if rewrite == "shorter" else
                      [ChatMessage(role="user", content="summary of the first half"),
                       *self._chat(21)[1:]])
        after = await self._call(plugin, 50_000, monkeypatch, summarized)

        assert "books.db" not in self._block(same), "fixture: the block changed without a rewrite"
        assert "books.db" in self._block(after), (
            "the store_fact call left the conversation and the block still hides the fact")

    @pytest.mark.asyncio
    async def test_messages_leaving_the_view_show_the_fact(self, plugin, monkeypatch):
        await self._call(plugin, 50_000, monkeypatch, self._chat(20))
        await plugin._handle_store_fact("the database is books.db", session_id="facts")
        pruned = await self._call(plugin, 150_000, monkeypatch, self._chat(21),
                                  max_messages=20, max_messages_headroom=0)

        assert pruned.metadata["messages_pruned"] > 0, "fixture: nothing was pruned"
        assert "books.db" in self._block(pruned)

    @pytest.mark.asyncio
    async def test_it_keeps_no_copy_of_the_first_message(self, plugin, monkeypatch):
        """An upload session's first message is megabytes of base64, for up to 10,000 sessions."""
        messages = [ChatMessage(role="user", content="x" * 200_000), *self._chat(3)[1:]]
        await self._call(plugin, 50_000, monkeypatch, messages)

        assert len(plugin._shown_blocks["facts"].head) <= 64

    @pytest.mark.asyncio
    async def test_the_first_stored_results_bring_their_explanation(self, plugin, monkeypatch):
        await plugin._handle_store_fact("the deploy target is api1", session_id="facts")
        run = TestHysteresis()._run_messages(8)
        await self._call(plugin, 50_000, monkeypatch, run)
        stored = await self._call(plugin, 150_000, monkeypatch, run)

        assert stored.metadata["tool_results_stored"] > 0, "fixture: nothing was stored"
        assert "## Tool Results" in self._block(stored), (
            "references in the conversation, and no word on how to read them")


class TestTheGateReadsTheSessionConfig:

    @pytest.mark.asyncio
    async def test_an_agents_lower_threshold_opens_the_gate(self, plugin, monkeypatch):
        """Plugin threshold 140k, agent 100k, context 120k: the agent's value decides."""
        plugin.layer1_threshold = 140_000
        plugin.always_compact_media_keep_last = 0
        plugin.compact_media_after_user_message = False
        monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (120_000, True))
        result = await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="gate",
            messages=[ChatMessage(role="user", content="go"),
                      ChatMessage(role="assistant", content="ok")],
            hook_config={"layer1_threshold": 100_000}))
        assert (result.metadata or {}).get("reason") != "below_threshold", (
            "the plugin's threshold decided, not the agent's")


class TestBookkeeping:

    @pytest.mark.asyncio
    async def test_a_refused_fact_is_reported_as_refused(self, plugin):
        assert (await plugin._handle_store_fact("the deploy target is api1", importance=0.9,
                                                session_id="full"))["success"]
        memory = plugin._get_session_components("full")["core_memory"]
        memory.max_tokens = memory._current_tokens  # full

        answer = await plugin._handle_store_fact("the user likes green", importance=0.5,
                                                 session_id="full")

        assert answer["success"] is False, "a fact that was not stored was reported as stored"
        assert [f.content for f in memory.facts] == ["the deploy target is api1"]

    @pytest.mark.asyncio
    async def test_a_fact_that_cannot_fit_is_called_too_large(self, plugin):
        """Measured as the budget is: the formatted fact, not the bare text."""
        from agent_system.llm.token_utils import estimate_content_tokens

        # A fact whose bare text fits the budget exactly and whose formatted line does not.
        fact = next(text for text in (" ".join(["word"] * k) for k in range(10, 80))
                    if estimate_content_tokens(f"- {text}") > estimate_content_tokens(text))
        memory = plugin._get_session_components("tight")["core_memory"]
        memory.max_tokens = estimate_content_tokens(fact)

        answer = await plugin._handle_store_fact(fact, session_id="tight")

        assert answer["success"] is False, "fixture: the fact was stored"
        assert "larger than core memory" in answer["error"], (
            "an empty memory told the agent to raise the importance of a fact that can never fit")

    @pytest.mark.asyncio
    async def test_the_tool_answers_a_refused_fact_with_an_error(self, plugin):
        from agent_system.config.models import MCPConfig
        from unittest.mock import AsyncMock, MagicMock

        from plugins.context_engineer.server import ContextEngineerServer

        srv = ContextEngineerServer("context_engineer", MagicMock(),
                                    MCPConfig(type="context_engineer", enabled=True))
        srv._hooks_impl = plugin
        memory = plugin._get_session_components("full")["core_memory"]
        memory.max_tokens = 5
        status = SimpleNamespace(progress=AsyncMock(), end=AsyncMock(), error=AsyncMock())

        answer = await srv.store_fact({"fact": "the user likes green buttons a lot",
                                       "_session_id": "full", "_status": status})

        assert answer["status"] == "error", answer
        assert "larger than core memory" in answer["error"], (
            "a fact that can never fit was told to raise its importance")
        status.error.assert_awaited()
        status.end.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_status_goes_to_a_child_of_the_request(self, plugin, monkeypatch, tmp_path):
        ids = []

        class _Scope:
            def __init__(self, bus, source, request_id, start_msg=""):
                ids.append(request_id)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def end(self, msg):
                pass

        monkeypatch.setattr(hooks_mod, "StatusScope", _Scope)

        async def child(base):
            return f"{base}_001"

        agent = SimpleNamespace(next_internal_tool_request_id=child)
        big = "a large tool result line with enough words to count " * 60
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                for i in range(8)]),
            *[ChatMessage(role="tool", tool_call_id=f"c{i}", name="t", content=big) for i in range(8)],
        ]
        await plugin.engineer_context(_context("status", messages, agent=agent))

        assert ids == ["req-status_001"], f"status went to {ids}"
        # The same run writes the history entry the panel reads.
        assert "media_always_compacted" in plugin.stats_history[-1]

    @pytest.mark.asyncio
    async def test_the_request_schema_reaches_the_next_hook(self, plugin):
        schema = [{"type": "function", "function": {"name": "only_this_request", "parameters": {}}}]
        messages = [ChatMessage(role="user", content="go"), ChatMessage(role="assistant", content="ok")]
        result = await plugin.engineer_context(_context("schema", messages, tools_schema=schema))

        assert result.context is not None, "fixture: the hook returned no context to check"
        assert result.context.tools_schema is schema

    def test_the_estimate_counts_the_request_schema_not_the_shared_one(self, plugin):
        schema = [{"type": "function", "function": {"name": "t" * 400, "parameters": {}}}]
        strategy = plugin._get_session_components("est")["strategy"]
        messages = [{"role": "user", "content": "hi"}]
        agent = SimpleNamespace(_current_tools_schema=[])
        with_schema = plugin._get_actual_or_estimated_tokens(
            _context("est", [], agent=agent, tools_schema=schema), messages, strategy)
        without = plugin._get_actual_or_estimated_tokens(
            _context("est", [], agent=agent), messages, strategy)
        assert with_schema > without

    @pytest.mark.asyncio
    async def test_the_history_keeps_only_the_newest_events(self, plugin):
        import threading

        saved_on = []
        plugin.history_callback = lambda: saved_on.append(threading.get_ident())
        plugin.stats_history.extend({"session_id": "old", "index": i} for i in range(hooks_mod.HISTORY_LIMIT))
        big = "a large tool result line with enough words to count " * 60
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                for i in range(8)]),
            *[ChatMessage(role="tool", tool_call_id=f"c{i}", name="t", content=big) for i in range(8)],
        ]
        await plugin.engineer_context(_context("capped", messages))

        assert plugin.stats_history[-1].get("session_id") == "capped", "fixture: the run recorded no compaction"
        assert len(plugin.stats_history) == hooks_mod.HISTORY_LIMIT
        assert plugin.stats_history[0]["index"] == 1, "not the oldest event was dropped"
        # the save may wait out a reader: never on the loop every agent runs on
        assert saved_on and saved_on != [threading.get_ident()], "the history was saved on the event loop"

    def test_a_history_save_that_fails_midway_leaves_the_saved_history(self, tmp_path, monkeypatch):
        import json

        from plugins.context_engineer import atomic_json
        from plugins.context_engineer.server import ContextEngineerServer

        server = ContextEngineerServer.__new__(ContextEngineerServer)  # its __init__ reads data/context_engineer
        server._history_file = tmp_path / "history.json"
        server.stats_history = [{"session_id": "s", "index": 0}]
        server._save_history_sync()
        server.stats_history.append({"session_id": "s", "index": 1})

        def dump_half(data, file, **kwargs):
            file.write('{"events": [')
            file.flush()
            raise OSError("killed mid-write")

        monkeypatch.setattr(atomic_json.json, "dump", dump_half)
        server._save_history_sync()  # logged, not raised
        monkeypatch.undo()

        assert server._load_history_sync() == [{"session_id": "s", "index": 0}]
        assert [path.name for path in tmp_path.iterdir()] == ["history.json"], "a temporary file stayed"
        assert json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))["events"][0]["index"] == 0
