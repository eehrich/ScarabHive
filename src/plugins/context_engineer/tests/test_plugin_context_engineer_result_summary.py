"""A long tool result comes back as a summary, not as a bare pointer.

Pre-Layer T stored a big arrival and left `{type, ref_id, token_count}` behind:
right for a result the model can page through itself, wrong for a hand-over --
the expensive agent that was handed a sub-agent's answer pays for every token
of it, and a pointer costs it a read call before it can act at all. With a
summary profile a cheap model writes what the result says; the full text stays
in the store and is read back with the read tool.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.context_engineer import hooks as hooks_mod
from plugins.context_engineer.compaction import TOOL_RESULT_REF_TYPE, _prose_of
from plugins.context_engineer.hooks import ContextEngineerPlugin

PLUGIN_DIR = Path(hooks_mod.__file__).parent

WINDOW = 1_000_000          # share 0.25 -> 250k tokens: the share alone never fires here
LONG_PROSE = "the run rewrote the parser and left two open questions " * 400   # ~4k tokens
SUMMARY = "Parser rewritten; two open questions on encoding and on the cache."


class FakeLLM:
    """What create_llm_from_profile returns in these tests."""

    def __init__(self, answer=SUMMARY, fails=False):
        self.answer, self.fails, self.prompts = answer, fails, []

    async def chat(self, messages, **kwargs):
        self.prompts.append(messages[0].content)
        if self.fails:
            raise RuntimeError("the cheap model is down")
        return self.answer


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    impl = ContextEngineerPlugin(PLUGIN_DIR, stats_history=[])
    impl._storage_base = tmp_path
    impl.apply_config({"tool_result_summary_from": 500, "tool_result_summary_profile": "cheap"})
    yield impl
    for sid in list(impl._session_components):
        impl.cleanup_session(sid)


@pytest.fixture
def llm(monkeypatch):
    """The summary client, wherever the hook asks the factory for one."""
    from agent_system.llm import factory
    fake = FakeLLM()
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda *a, **k: fake)
    return fake


def _result(content, name="sub_agent_manager_wait"):
    return ChatMessage(role="tool", tool_call_id="c1", name=name, content=content)


def _messages(content, name="sub_agent_manager_wait"):
    return [ChatMessage(role="system", content="You are the coder."),
            ChatMessage(role="user", content="Hand the sub-agent's answer over."),
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": name, "arguments": "{}"}}]),
            _result(content, name)]


async def _engineer(plugin, session_id, messages, window=WINDOW, overrides=None):
    agent = SimpleNamespace(system_config=SimpleNamespace(network=SimpleNamespace(ssl_verify=True)))
    result = await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id=f"req-{session_id}", session_id=session_id,
        messages=messages, llm=SimpleNamespace(context_window=window), agent=agent,
        hook_config=overrides or {}))
    assert result.success, result.error
    return [m for m in result.context.messages
            if getattr(m, "injected_by", None) != hooks_mod._RESTORATION_MARKER]


def _reference(message):
    reference = json.loads(message.content)
    assert reference.get("type") == TOOL_RESULT_REF_TYPE, message.content[:120]
    return reference


def _stored(plugin, session_id, reference):
    entry = plugin._get_session_components(session_id)["tool_store"].retrieve(reference["ref_id"])
    assert entry is not None
    return entry.content


@pytest.mark.asyncio
async def test_a_long_result_comes_back_as_a_summary_and_keeps_its_full_text(plugin, llm):
    out = await _engineer(plugin, "handover", _messages(LONG_PROSE))

    reference = _reference(out[-1])
    assert reference["summary"] == SUMMARY
    assert _stored(plugin, "handover", reference) == LONG_PROSE
    assert LONG_PROSE[:60] in llm.prompts[0] and "sub_agent_manager_wait" in llm.prompts[0]


@pytest.mark.asyncio
async def test_the_floor_is_a_token_count_not_a_share_of_the_window(plugin, llm):
    """A million-token window would carry the result; the next agent still pays for it."""
    assert plugin.tool_result_max_window_share * WINDOW > 200_000
    reference = _reference((await _engineer(plugin, "floor", _messages(LONG_PROSE)))[-1])
    assert reference["summary"] == SUMMARY


@pytest.mark.asyncio
async def test_a_result_too_big_for_the_window_but_under_the_floor_gets_no_summary(plugin, llm):
    """The share stores it because this window is small; that is no reason to
    pay a model for a summary."""
    plugin.apply_config({"tool_result_summary_from": 50_000, "tool_result_summary_profile": "cheap"})
    # window 4k, share 0.25 -> stored from 1k tokens on; the floor sits at 50k.
    out = await _engineer(plugin, "underfloor", _messages(LONG_PROSE), window=4_000)

    reference = _reference(out[-1])
    assert "summary" not in reference and llm.prompts == []
    assert _stored(plugin, "underfloor", reference) == LONG_PROSE


@pytest.mark.asyncio
async def test_without_a_profile_the_result_stays_where_it_is(plugin, llm):
    plugin.apply_config({"tool_result_summary_from": 500, "tool_result_summary_profile": ""})
    out = await _engineer(plugin, "noprofile", _messages(LONG_PROSE))

    assert out[-1].content == LONG_PROSE and llm.prompts == []


@pytest.mark.asyncio
async def test_a_failed_summary_keeps_the_result_and_the_pointer(plugin, monkeypatch):
    from agent_system.llm import factory
    fake = FakeLLM(fails=True)
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda *a, **k: fake)

    reference = _reference((await _engineer(plugin, "broken", _messages(LONG_PROSE)))[-1])
    assert "summary" not in reference
    assert _stored(plugin, "broken", reference) == LONG_PROSE


@pytest.mark.asyncio
async def test_a_wrapped_answer_is_summarized_by_its_prose(plugin, llm):
    wrapper = json.dumps({"instance_id": "sub-7", "status": "completed", "result": LONG_PROSE})
    reference = _reference((await _engineer(plugin, "wrapped", _messages(wrapper)))[-1])

    assert reference["summary"] == SUMMARY
    assert "instance_id" not in llm.prompts[0], "the wrapper went to the model instead of the answer"


@pytest.mark.asyncio
async def test_a_structured_result_stays_where_it_is(plugin, llm):
    """Nobody summarizes JSON, so taking it out would cost the next agent a
    read call instead of saving it one. Only the window share still takes it."""
    rows = json.dumps({"rows": [{"id": i, "title": f"chapter {i}"} for i in range(900)]})   # ~8k tokens

    out = await _engineer(plugin, "rows", _messages(rows, name="sqlite_query"))
    assert out[-1].content == rows and llm.prompts == []

    # Same result, a window it really is too big for: stored, still no summary.
    out = await _engineer(plugin, "rows_small_window", _messages(rows, name="sqlite_query"), window=4_000)
    reference = _reference(out[-1])
    assert "summary" not in reference and llm.prompts == []
    assert _stored(plugin, "rows_small_window", reference) == rows


WRAPPED = "what it did, at length, " * 100     # the prose is what the wrapper is FOR


@pytest.mark.parametrize("content, expected", [
    ("plain prose about a run", "plain prose about a run"),
    (json.dumps({"instance_id": "s", "status": "completed", "result": WRAPPED}), WRAPPED),
    (json.dumps({"untrusted": True, "content": WRAPPED}), WRAPPED),
    (json.dumps({"result": {"untrusted": True, "content": WRAPPED}}), WRAPPED),
    (json.dumps({"status": "ok", "text": WRAPPED}), WRAPPED),
    (json.dumps({"data": WRAPPED}), WRAPPED),
    (json.dumps({"answer": WRAPPED}), WRAPPED),
    (json.dumps({"output": WRAPPED}), WRAPPED),
    # A result that HAS prose in it, not a wrapper around prose: its own fields
    # carry facts a summary of the log alone would drop.
    (json.dumps({"status": "error", "exit": 1, "error": "build failed",
                 "errors": ["a" * 2_000], "output": "log " * 200}), None),
    ('{"rows": [1, 2, 3]}', None),
    ("[1, 2, 3]", None),
    ('{"broken": ', '{"broken": '),
])
def test_what_counts_as_prose_in_a_tool_result(content, expected):
    assert _prose_of(content) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("floor, summarized", [(0, False), (500, True)])
async def test_the_engine_summarizes_nothing_without_a_floor(floor, summarized):
    """Straight at the engine: the hook builds no summarizer without a floor,
    and the engine does not summarize on its own when one is handed to it."""
    from plugins.context_engineer.compaction import CompactionConfig, LayeredCompactionStrategy

    async def summarize(text, tool_name):
        return SUMMARY

    strategy = LayeredCompactionStrategy(
        tool_store=None, core_memory=None, archival_memory=None,
        config=CompactionConfig(tool_result_summary_from=floor, tool_result_summary_profile="cheap"),
        summarize=summarize)

    written = await strategy._written_summary({"name": "sub_agent_manager_wait"}, LONG_PROSE)
    assert written == (SUMMARY if summarized else None)


@pytest.mark.asyncio
async def test_a_nested_untrusted_wrapper_is_summarized_by_its_prose(plugin, llm):
    """What coding_cli and the sub-agent manager really hand over:
    {"result": {"untrusted": true, "content": "<the answer>"}}."""
    wrapper = json.dumps({"run_id": "a1", "state": "done",
                          "result": {"untrusted": True, "content": LONG_PROSE}})
    reference = _reference((await _engineer(plugin, "nested", _messages(wrapper)))[-1])

    assert reference["summary"] == SUMMARY
    assert llm.prompts[0].count("untrusted") == 0


@pytest.mark.asyncio
async def test_only_the_head_of_a_huge_result_goes_to_the_cheap_model(plugin, llm):
    """The result Pre-Layer T exists for can be a book; the cheap call would
    be the dearest in the run."""
    from plugins.context_engineer.compaction import SUMMARY_INPUT_CHARS
    huge = "a chapter of prose that says something " * 20_000

    await _engineer(plugin, "huge", _messages(huge))

    assert len(llm.prompts[0]) < SUMMARY_INPUT_CHARS + 2_000
    assert "the rest is only in the stored result" in llm.prompts[0]


@pytest.mark.asyncio
async def test_an_echo_is_no_summary(plugin, monkeypatch):
    """A cheap model that returns the text instead of summarizing it would
    write the result back into the conversation, for every turn that follows."""
    from agent_system.llm import factory
    # Long enough that a CAPPED echo (1200 chars) would slip under half of it:
    # the echo has to be judged on what the model wrote, not on what was kept.
    barely_over_the_floor = "the run rewrote the parser and left two open questions " * 55   # ~740 tokens
    echo = FakeLLM(answer=barely_over_the_floor)
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda *a, **k: echo)

    reference = _reference((await _engineer(plugin, "echo", _messages(barely_over_the_floor)))[-1])
    assert "summary" not in reference
    assert _stored(plugin, "echo", reference) == barely_over_the_floor


@pytest.mark.asyncio
async def test_only_an_arrival_is_summarized_never_layer_1(plugin):
    """Layer 1 comes through the same door with the WHOLE history: a model call
    per result there spends the hook's budget, and the compaction it runs in is
    dropped with it."""
    from plugins.context_engineer.compaction import CompactionConfig, LayeredCompactionStrategy

    asked, stored = [], []

    async def summarize(text, tool_name):
        asked.append(tool_name)
        return SUMMARY

    class FakeStore:
        def store_and_reference(self, tool_call_id, tool_name, content, summary=None, inline_summary=None):
            stored.append(inline_summary)
            return "REF"

    strategy = LayeredCompactionStrategy(
        tool_store=FakeStore(), core_memory=None, archival_memory=None,
        config=CompactionConfig(tool_result_summary_from=500, tool_result_summary_profile="cheap"),
        summarize=summarize)

    await strategy._store_tool_result({"name": "layer1"}, LONG_PROSE)
    await strategy._store_tool_result({"name": "arrival"}, LONG_PROSE, on_arrival=True)

    assert asked == ["arrival"] and stored == [None, SUMMARY]


@pytest.mark.asyncio
async def test_the_round_has_one_time_budget_for_all_its_summaries(plugin, monkeypatch):
    """Several results arrive together -- a fan-out to sub-agents is this
    feature's own case. A limit per call would add up past the hook's budget,
    and the hook is dropped WHOLE, with the storing of every other result."""
    from agent_system.llm import factory
    from plugins.context_engineer import compaction as compaction_mod

    class SlowLLM(FakeLLM):
        async def chat(self, messages, **kwargs):
            self.prompts.append(messages[0].content)
            await asyncio.sleep(5)
            return SUMMARY

    slow = SlowLLM()
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda *a, **k: slow)
    monkeypatch.setattr(compaction_mod, "SUMMARY_ROUND_S", 1.5)   # the call itself takes 5 s

    out = await _engineer(plugin, "budget", _messages(LONG_PROSE))

    reference = _reference(out[-1])
    assert slow.prompts, "the summarizer was never asked -- this test measures nothing"
    assert "summary" not in reference, "the summary ran past the round's budget"
    assert _stored(plugin, "budget", reference) == LONG_PROSE


@pytest.mark.asyncio
async def test_the_budget_is_spent_by_the_first_summaries_of_the_round(plugin, monkeypatch):
    """Two results arrive together and share one budget: what is left after the
    first is what the second may take."""
    from agent_system.llm import factory
    from plugins.context_engineer import compaction as compaction_mod

    class TickingLLM(FakeLLM):
        async def chat(self, messages, **kwargs):
            self.prompts.append(messages[0].content)
            await asyncio.sleep(1.2)
            return SUMMARY

    ticking = TickingLLM()
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda *a, **k: ticking)
    monkeypatch.setattr(compaction_mod, "SUMMARY_ROUND_S", 2.0)

    calls = [{"id": f"c{i}", "type": "function",
              "function": {"name": "sub_agent_manager_wait", "arguments": "{}"}} for i in range(3)]
    messages = [ChatMessage(role="system", content="You are the coder."),
                ChatMessage(role="user", content="All at once."),
                ChatMessage(role="assistant", content=None, tool_calls=calls)]
    messages += [ChatMessage(role="tool", tool_call_id=f"c{i}", name="sub_agent_manager_wait",
                             content=LONG_PROSE) for i in range(3)]

    out = await _engineer(plugin, "three", messages)

    first, second, third = (_reference(out[-3]), _reference(out[-2]), _reference(out[-1]))
    assert first["summary"] == SUMMARY, "the first summary did not come through"
    assert "summary" not in second and "summary" not in third
    assert _stored(plugin, "three", third) == LONG_PROSE
    # What is left is not worth a call: the second and third are not even asked.
    assert len(ticking.prompts) == 1, ticking.prompts


@pytest.mark.asyncio
async def test_the_summary_call_carries_the_turns_cancellation_token(plugin, monkeypatch):
    """Stopping the turn must stop the summary too; it runs detached."""
    from agent_system.llm import factory

    class RecordingLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.tokens = []

        async def chat(self, messages, **kwargs):
            self.tokens.append(kwargs.get("cancellation_token"))
            return await super().chat(messages, **kwargs)

    recording = RecordingLLM()
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda *a, **k: recording)
    token = object()
    agent = SimpleNamespace(system_config=SimpleNamespace(network=SimpleNamespace(ssl_verify=True)))
    result = await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id="req-token", session_id="token",
        messages=_messages(LONG_PROSE), llm=SimpleNamespace(context_window=WINDOW),
        agent=agent, cancellation_token=token))

    assert result.success and recording.tokens == [token]


@pytest.mark.asyncio
async def test_a_profile_that_does_not_resolve_leaves_the_results_alone(plugin, monkeypatch):
    """No client, no summary -- and then no reason to take the result out of
    the conversation either. The failure is remembered, not retried per result."""
    from agent_system.llm import factory
    tries = []

    def broken(*args, **kwargs):
        tries.append(args)
        raise ValueError("no such profile")

    monkeypatch.setattr(factory, "create_llm_from_profile", broken)

    for session in ("dead1", "dead2"):
        out = await _engineer(plugin, session, _messages(LONG_PROSE))
        assert out[-1].content == LONG_PROSE

    assert len(tries) == 1, "the profile was rebuilt for every result"


@pytest.mark.asyncio
async def test_a_long_summary_is_cut_and_says_so(plugin, monkeypatch):
    """It is resent on every later turn: a model that writes an essay does not
    get to keep it."""
    from agent_system.llm import factory
    from plugins.context_engineer.compaction import SUMMARY_CHARS
    wordy = FakeLLM(answer="a genuine summary sentence. " * 200)          # ~5600 chars
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda *a, **k: wordy)

    reference = _reference((await _engineer(plugin, "wordy", _messages(LONG_PROSE)))[-1])

    assert len(reference["summary"]) == SUMMARY_CHARS + 1 and reference["summary"].endswith("…")


@pytest.mark.asyncio
async def test_layer_1_stores_the_history_without_asking_the_model(plugin, llm):
    """Layer 1 walks the WHOLE history; one model call per result there would
    spend the hook's budget, and the compaction would be dropped with it."""
    plugin.apply_config({"tool_result_summary_from": 500, "tool_result_summary_profile": "cheap",
                         "layer1_threshold": 2_000, "layer2_threshold": 10_000_000,
                         "layer3_threshold": 10_000_000, "min_tokens_between_compactions": 0,
                         "target_tokens": 1_000})
    older = "an older result of the same kind " * 200                     # ~1.6k tokens each
    messages = [ChatMessage(role="system", content="You are the coder."),
                ChatMessage(role="user", content="Go on.")]
    for i in range(6):
        messages.append(ChatMessage(role="assistant", content=None, tool_calls=[
            {"id": f"old{i}", "type": "function",
             "function": {"name": "sub_agent_manager_wait", "arguments": "{}"}}]))
        messages.append(ChatMessage(role="tool", tool_call_id=f"old{i}",
                                    name="sub_agent_manager_wait", content=older))
    # The model has answered since: every result above is history, none is an
    # arrival, so what happens to them is Layer 1's doing alone.
    messages.append(ChatMessage(role="assistant", content="All six are in."))
    messages.append(ChatMessage(role="user", content="And now?"))

    out = await _engineer(plugin, "layer1", messages)

    assert any(_is_reference(m) for m in out), "Layer 1 stored nothing -- this test measures nothing"
    assert llm.prompts == []


def _is_reference(message):
    try:
        return json.loads(message.content).get("type") == TOOL_RESULT_REF_TYPE
    except (TypeError, ValueError, AttributeError):
        return False


# --- Which tools a summary may replace -------------------------------------
#
# Empty means every tool, and for an agent that only takes hand-overs that is
# right. One that also reads files through the same hook would get a summary
# where it asked for the file -- a turn spent instead of saved.

SUMMARY_TOOLS = "tool_result_summary_tools"


@pytest.mark.asyncio
async def test_a_tool_the_patterns_do_not_name_keeps_its_result_whole(plugin, llm):
    out = await _engineer(plugin, "unnamed", _messages(LONG_PROSE, name="file_ops_read"),
                          overrides={SUMMARY_TOOLS: ["sub_agent_manager_*"]})

    assert out[-1].content == LONG_PROSE and llm.prompts == []


@pytest.mark.asyncio
async def test_a_named_tool_is_summarized_as_before(plugin, llm):
    out = await _engineer(plugin, "named", _messages(LONG_PROSE),
                          overrides={SUMMARY_TOOLS: ["sub_agent_manager_*", "coding_cli_*"]})

    assert _reference(out[-1])["summary"] == SUMMARY


@pytest.mark.asyncio
async def test_no_patterns_means_every_tool(plugin, llm):
    """The default, and what the feature shipped as."""
    out = await _engineer(plugin, "all", _messages(LONG_PROSE, name="file_ops_read"))

    assert _reference(out[-1])["summary"] == SUMMARY


@pytest.mark.asyncio
async def test_one_pattern_written_as_a_string_is_not_read_letter_by_letter(plugin, llm):
    """A bare string in YAML is the likely slip. Iterating it would match its
    CHARACTERS -- and a pattern ending in "*" would still match everything
    through that one character, which is why this one names the tool exactly."""
    out = await _engineer(plugin, "string", _messages(LONG_PROSE),
                          overrides={SUMMARY_TOOLS: "sub_agent_manager_wait"})

    assert _reference(out[-1])["summary"] == SUMMARY


@pytest.mark.asyncio
async def test_one_pattern_as_a_string_still_EXCLUDES_the_tools_it_does_not_name(plugin, llm):
    """The inclusion case alone cannot see the difference: a string that is
    dropped instead of split becomes an empty list, and empty means every tool
    -- the named tool would be summarized either way."""
    out = await _engineer(plugin, "stringout", _messages(LONG_PROSE, name="file_ops_read"),
                          overrides={SUMMARY_TOOLS: "sub_agent_manager_wait"})

    assert out[-1].content == LONG_PROSE and llm.prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("patterns", [["sub_agent_manager_*"], ["*"], 3],
                         ids=["named", "wildcard", "unreadable"])
async def test_a_result_without_a_tool_name_is_never_summarized_under_patterns(
        plugin, llm, patterns, request):
    """A session restored mid tool-turn carries no name (model_dump drops it).
    No pattern an operator could write names that result, so once they name
    tools at all it stays whole -- including under "*", and including under the
    fallback an unreadable value drops to, whose empty pattern would otherwise
    match the empty name exactly."""
    messages = _messages(LONG_PROSE)
    messages[-1].name = None
    out = await _engineer(plugin, f"noname-{request.node.callspec.id}", messages,
                          overrides={SUMMARY_TOOLS: patterns})

    assert out[-1].content == LONG_PROSE and llm.prompts == []


@pytest.mark.asyncio
async def test_patterns_are_matched_case_sensitively_on_every_platform(plugin, llm):
    """fnmatch lowercases both sides on Windows and nowhere else. A camelCase
    tool name would then be filtered one way on a developer's machine and the
    other way on the server -- and a filter that lets a tool through in
    production only is the leak this key exists to prevent."""
    out = await _engineer(plugin, "case", _messages(LONG_PROSE, name="firecrawl_scrapeUrl"),
                          overrides={SUMMARY_TOOLS: ["firecrawl_scrapeurl"]})

    assert out[-1].content == LONG_PROSE and llm.prompts == []


@pytest.mark.asyncio
async def test_the_default_profile_exists_in_the_shipped_llm_config():
    """The plugin names a profile, never a model -- so the name has to be
    there. A default pointing at nothing would fail silently: no client, no
    summarizer, no selection, and every long result simply stays whole."""
    from agent_system.config.settings import load_settings
    from plugins.context_engineer.compaction import CompactionConfig

    name = CompactionConfig().tool_result_summary_profile
    profiles = getattr(load_settings().llm_system, "profiles", None) or {}
    assert name in profiles, (
        f"{name!r} is the shipped default of tool_result_summary_profile "
        f"but no such profile is in config/llm.yaml")


@pytest.mark.asyncio
async def test_a_result_stored_for_the_window_is_still_not_summarized(plugin, llm):
    """The two reasons to store are independent: the share takes a result out
    because it does not fit, and that is no permission to summarize a tool the
    patterns do not name. Selection cannot be the only place that asks --
    a share-picked result reaches the decision without it."""
    out = await _engineer(plugin, "shared", _messages(LONG_PROSE, name="file_ops_read"),
                          window=4_000,   # share 0.25 -> stored from 1k tokens on
                          overrides={SUMMARY_TOOLS: ["sub_agent_manager_*"]})

    reference = _reference(out[-1])       # stored: it did not fit the window
    assert "summary" not in reference and llm.prompts == []
    assert _stored(plugin, "shared", reference) == LONG_PROSE


@pytest.mark.asyncio
async def test_a_pattern_list_that_is_no_list_summarizes_nothing_and_breaks_nothing(plugin, llm):
    """Two things must hold, and they pull in opposite directions.

    A list is ITERATED at the use site: a number there raises inside the hook,
    and a failed hook drops the WHOLE compaction -- every result of that turn
    stays in the context until it outgrows the provider. So it must not crash.

    But it must not fall back to the empty list either: empty means EVERY tool.
    An operator writes this key to keep results away from another provider, and
    a typo would hand them over."""
    out = await _engineer(plugin, "scalar", _messages(LONG_PROSE), overrides={SUMMARY_TOOLS: 3})

    assert out[-1].content == LONG_PROSE and llm.prompts == []


@pytest.mark.asyncio
async def test_a_mapping_of_patterns_is_not_read_as_its_keys(plugin, llm):
    """Iterating a mapping yields its KEYS, so a wrong shape would quietly
    filter by them -- no error anywhere, and a tool the operator never named
    would be summarized because it happens to be a key. Written as a mapping,
    this one names exactly the tool it must NOT let through."""
    out = await _engineer(plugin, "mapping", _messages(LONG_PROSE, name="file_ops_read"),
                          overrides={SUMMARY_TOOLS: {"file_ops_read": True}})

    assert out[-1].content == LONG_PROSE and llm.prompts == []


def test_a_field_with_a_default_factory_falls_back_to_its_factory(monkeypatch, tmp_path):
    """Without the factory the fallback is dataclasses.MISSING -- truthy and
    not iterable. Today only schema.yaml's `default: []` keeps it away, and
    nothing checks that the key is still there."""
    from dataclasses import MISSING, fields
    from plugins.context_engineer.compaction import CompactionConfig

    factory_fields = [f.name for f in fields(CompactionConfig) if f.default_factory is not MISSING]
    assert factory_fields, "this test guards the default_factory path; nothing uses one anymore"

    impl = ContextEngineerPlugin(PLUGIN_DIR, stats_history=[])
    impl._storage_base = tmp_path
    monkeypatch.setattr(impl, "get_config", lambda: {})   # no schema defaults to lean on
    impl.apply_config({})

    for name in factory_fields:
        assert getattr(impl, name) == [], f"{name} fell back to {getattr(impl, name)!r}"
