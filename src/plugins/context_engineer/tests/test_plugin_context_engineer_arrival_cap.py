"""Pre-Layer T: a new tool result too big for the window is stored on arrival.

Layer 1 archived large results only from layer1_threshold on, so a result that
took a third of a 128k window went out with every call — or sank it. The cap
must not cost the prompt cache: it touches the round no request has carried yet
and nothing before it, and a run that came in for it alone does nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.context_engineer import hooks as hooks_mod
from plugins.context_engineer.compaction import RETRIEVAL_MARKER, TOOL_RESULT_REF_TYPE
from plugins.context_engineer.hooks import ContextEngineerPlugin

PLUGIN_DIR = Path(hooks_mod.__file__).parent

WINDOW = 40_000            # share 0.25 -> a result may take 10k tokens
LARGE = "chapter text " * 4_000   # ~15.7k tokens: over the share
SMALL = "chapter text " * 1_500   # ~5.9k tokens: within it


@pytest.fixture
def plugin(tmp_path):
    impl = ContextEngineerPlugin(PLUGIN_DIR, stats_history=[])
    impl._storage_base = tmp_path
    yield impl
    for sid in list(impl._session_components):
        impl.cleanup_session(sid)


def _call(call_id, reasoning=None):
    return ChatMessage(role="assistant", content=None, reasoning_details=reasoning, tool_calls=[
        {"id": call_id, "type": "function", "function": {"name": "writer_content_chapter", "arguments": "{}"}}])


def _result(call_id, content):
    return ChatMessage(role="tool", tool_call_id=call_id, name="writer_content_chapter", content=content)


async def _engineer(plugin, session_id, messages):
    result = await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id=f"req-{session_id}", session_id=session_id,
        messages=messages, llm=SimpleNamespace(context_window=WINDOW)))
    assert result.success, result.error
    return [m for m in result.context.messages
            if getattr(m, "injected_by", None) != hooks_mod._RESTORATION_MARKER]


def _is_ref(message):
    try:
        return json.loads(message.content).get("type") == TOOL_RESULT_REF_TYPE
    except (TypeError, ValueError, AttributeError):
        return False


@pytest.mark.asyncio
async def test_a_new_result_over_the_share_is_stored_below_every_threshold(plugin):
    messages = [ChatMessage(role="system", content="You are a writer."),
                ChatMessage(role="user", content="Read chapter 3."),
                _call("c1"), _result("c1", LARGE)]

    out = await _engineer(plugin, "arrive", messages)

    assert _is_ref(out[-1]), out[-1].content[:80]
    ref = json.loads(out[-1].content)["ref_id"]
    stored = plugin._get_session_components("arrive")["tool_store"].retrieve(ref)
    assert stored is not None and stored.content == LARGE, "the full result was not kept"


@pytest.mark.asyncio
async def test_a_result_within_the_share_stays_inline(plugin):
    messages = [ChatMessage(role="user", content="Read chapter 3."),
                _call("c1"), _result("c1", SMALL)]

    out = await _engineer(plugin, "inline", messages)

    assert out[-1].content == SMALL


@pytest.mark.asyncio
async def test_the_cached_prefix_is_left_as_it_was_sent(plugin):
    """An older result over the share was already sent: storing it now would
    rewrite the prefix. The reasoning of the turns before stays verifiable."""
    reasoning = [{"type": "reasoning.encrypted", "data": "opaque"}]
    messages = [ChatMessage(role="user", content="Read chapters 2 and 3."),
                _call("c0", reasoning), _result("c0", LARGE),
                _call("c1", reasoning), _result("c1", LARGE)]
    sent = [m.model_dump() for m in messages[:-1]]

    out = await _engineer(plugin, "prefix", messages)

    assert _is_ref(out[-1])
    assert [m.model_dump() for m in out[:-1]] == sent


@pytest.mark.asyncio
async def test_a_retrieval_answer_stays_inline(plugin):
    """Storing what the agent just pulled out of storage is a loop without exit."""
    answer = json.dumps({"status": "success", "content": LARGE, RETRIEVAL_MARKER: True})
    messages = [ChatMessage(role="user", content="Read it back."),
                _call("c1"), _result("c1", answer)]

    out = await _engineer(plugin, "retrieval", messages)

    assert out[-1].content == answer


@pytest.mark.asyncio
async def test_preloaded_pairs_belong_to_the_unsent_round(plugin):
    """tool_preload appends assistant/tool pairs in the same pass; only the last
    result followed the last assistant message, the first big one was missed."""
    messages = [ChatMessage(role="user", content="Hello."),
                ChatMessage(role="assistant", content="Hi, what shall I read?"),
                ChatMessage(role="user", content="Chapters 2 and 3."),
                _call("preload_1").model_copy(update={"injected_by": "tool_preload"}),
                _result("preload_1", LARGE),
                _call("preload_2").model_copy(update={"injected_by": "tool_preload"}),
                _result("preload_2", SMALL)]

    out = await _engineer(plugin, "preload", messages)

    assert _is_ref(out[4]), out[4].content[:80]
    assert out[1].content == "Hi, what shall I read?"


@pytest.mark.asyncio
async def test_a_run_for_the_cap_alone_leaves_no_hysteresis_mark(plugin):
    """The mark spaces out rewrites of old messages; T rewrote none."""
    messages = [ChatMessage(role="user", content="Read chapter 3."),
                _call("c1"), _result("c1", LARGE)]

    await _engineer(plugin, "unmarked", messages)

    assert "unmarked" not in plugin._hysteresis_marks


@pytest.mark.asyncio
async def test_a_run_for_the_cap_alone_runs_no_other_pass(plugin):
    """Below the gate the hook used to return; coming in for T must not let
    Pre-Layer P prune the older messages."""
    messages = [ChatMessage(role="user", content="Read chapter 2."),
                _call("c0"), _result("c0", "short"),
                ChatMessage(role="user", content="Now chapter 3."),
                _call("c1"), _result("c1", LARGE)]
    sent = [m.model_dump() for m in messages[:-1]]

    result = await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id="req-p", session_id="noprune",
        messages=messages, llm=SimpleNamespace(context_window=WINDOW),
        hook_config={"max_messages": 3}))
    out = [m for m in result.context.messages
           if getattr(m, "injected_by", None) != hooks_mod._RESTORATION_MARKER]

    assert _is_ref(out[-1])
    assert [m.model_dump() for m in out[:-1]] == sent


@pytest.mark.asyncio
async def test_after_the_cap_layer_2_reads_the_lowered_count(plugin):
    """T took the context back under Layer 1's threshold; Layer 2's gate still
    read the count from before and archived old turns nobody needed to."""
    strategy = plugin._get_session_components("l2", overrides={
        "layer1_threshold": 140_000, "layer2_threshold": 170_000,
        "layer3_threshold": 200_000, "target_tokens": 70_000})["strategy"]
    big = "chapter text " * 14_000          # ~55k tokens, over 200k * 0.25
    messages = [{"role": "user", "content": "Read the book."},
                _call("c1").model_dump(exclude_none=True),
                _result("c1", big).model_dump(exclude_none=True)]

    result = await strategy.compact(messages, current_tokens=175_000,
                                    session_id="l2", context_window=200_000)

    assert "T" in result.layers_applied
    assert 2 not in result.layers_applied, result.layers_applied


@pytest.mark.asyncio
async def test_the_hysteresis_level_is_what_was_due_after_the_cap(plugin):
    """Read before T, a 55k arrival made Layer 3 look due; the mark then held
    Layers 1-3 back although only Layer 1 was."""
    messages = [ChatMessage(role="user", content="Read the whole book.")]
    for n in range(15):                          # ~150k tokens of older results
        messages += [_call(f"old{n}"), _result(f"old{n}", "old text " * 3_700)]
    messages += [_call("big"), _result("big", "chapter text " * 14_000)]

    result = await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id="req-lvl", session_id="level",
        messages=messages, llm=SimpleNamespace(context_window=200_000),
        hook_config={"layer1_threshold": 140_000, "layer2_threshold": 170_000,
                     "layer3_threshold": 200_000, "target_tokens": 70_000}))

    assert result.success, result.error
    assert "T" in result.metadata["layers_applied"] and 1 in result.metadata["layers_applied"]
    assert plugin._hysteresis_marks["level"].level == 1


@pytest.mark.asyncio
async def test_the_gate_reads_the_call_without_what_the_cap_stores(plugin):
    """Over Layer 1's threshold only because of the arrival: the hook came in as
    if a layer were due, and Pre-Layer P pruned older messages on the way."""
    messages = [ChatMessage(role="user", content="Read chapter 2."),
                _call("c0"), _result("c0", "short"),
                ChatMessage(role="user", content="Now chapter 3."),
                _call("c1"), _result("c1", LARGE)]
    sent = [m.model_dump() for m in messages[:-1]]

    result = await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id="req-g", session_id="gate",
        messages=messages, llm=SimpleNamespace(context_window=WINDOW),
        hook_config={"layer1_threshold": 12_000, "max_messages": 3}))
    out = [m for m in result.context.messages
           if getattr(m, "injected_by", None) != hooks_mod._RESTORATION_MARKER]

    assert _is_ref(out[-1])
    assert [m.model_dump() for m in out[:-1]] == sent


@pytest.mark.asyncio
async def test_the_byte_limit_reads_the_call_without_what_the_cap_stores(plugin):
    """Over the byte limit only because of the arrival: the hook forced a full
    run, and Pre-Layer P pruned older messages although T alone got it under."""
    messages = [ChatMessage(role="user", content="Read chapter 2."),
                _call("c0"), _result("c0", "short"),
                ChatMessage(role="user", content="Now chapter 3."),
                _call("c1"), _result("c1", LARGE)]
    sent = [m.model_dump() for m in messages[:-1]]

    result = await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id="req-b", session_id="bytes",
        messages=messages, llm=SimpleNamespace(context_window=WINDOW),
        hook_config={"max_request_bytes": 40_000, "max_messages": 3}))
    out = [m for m in result.context.messages
           if getattr(m, "injected_by", None) != hooks_mod._RESTORATION_MARKER]

    assert _is_ref(out[-1])
    assert [m.model_dump() for m in out[:-1]] == sent


@pytest.mark.asyncio
async def test_the_hysteresis_holds_on_the_call_without_what_the_cap_stores(plugin):
    """Read with the arrival, Layer 2 looked due and released the hold: Layer 1
    rewrote the older results although nothing but the arrival had grown."""
    plugin._hysteresis_marks["hold"] = hooks_mod._HysteresisMark(level=1)
    messages = [ChatMessage(role="user", content="Read the chapters.")]
    for n in range(4):                           # ~11k tokens: Layer 1 is due, no more
        messages += [_call(f"old{n}"), _result(f"old{n}", "old text " * 1_000)]
    messages += [_call("big"), _result("big", LARGE)]
    sent = [m.model_dump() for m in messages[:-1]]

    result = await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id="req-h", session_id="hold",
        messages=messages, llm=SimpleNamespace(context_window=WINDOW),
        hook_config={"layer1_threshold": 10_000, "layer2_threshold": 20_000,
                     "layer3_threshold": 60_000, "target_tokens": 5_000,
                     "min_tokens_between_compactions": 50_000, "tool_result_keep_last": 0}))
    out = [m for m in result.context.messages
           if getattr(m, "injected_by", None) != hooks_mod._RESTORATION_MARKER]

    assert _is_ref(out[-1])
    assert [m.model_dump() for m in out[:-1]] == sent


@pytest.mark.asyncio
async def test_a_provider_count_from_before_the_arrival_is_not_undercut(plugin, monkeypatch):
    """The previous call's prompt_tokens never held the arrival; subtracting its
    estimate from max(count, estimate) put a due Layer 1 under its threshold."""
    real = plugin._read_tokens
    monkeypatch.setattr(plugin, "_read_tokens",
                        lambda ctx, msgs, strategy: (max(110_000, real(ctx, msgs, strategy)[0]), True))
    big = "chapter text " * 8_900                # ~35k tokens, over 128k * 0.25
    messages = [ChatMessage(role="user", content="Read the book."),
                _call("c1"), _result("c1", big)]

    result = await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id="req-a", session_id="actual",
        messages=messages, llm=SimpleNamespace(context_window=128_000),
        hook_config={"layer1_threshold": 100_000, "layer2_threshold": 150_000,
                     "layer3_threshold": 200_000, "target_tokens": 50_000}))

    layers = result.metadata["layers_applied"]
    assert "T" in layers and 1 in layers, layers


@pytest.mark.asyncio
async def test_pre_layer_p_keeps_the_round_the_model_has_not_seen(plugin):
    """With a small max_messages the candidate window spans the whole list, and
    the pointer T just left ranked cheapest of all."""
    strategy = plugin._get_session_components("prune", overrides={"max_messages": 6})["strategy"]
    messages = [{"role": "user", "content": "Read everything."}]
    for n in range(6):
        messages += [_call(f"old{n}").model_dump(exclude_none=True),
                     _result(f"old{n}", "old text").model_dump(exclude_none=True)]
    messages += [_call("big").model_dump(exclude_none=True),
                 _result("big", LARGE).model_dump(exclude_none=True)]

    result = await strategy.compact(messages, session_id="prune", context_window=WINDOW)

    assert result.messages_pruned > 0, "fixture: P did not run"
    tail = result.modified_messages[-2:]
    assert tail[0].get("tool_calls", [{}])[0].get("id") == "big"
    assert json.loads(tail[1]["content"])["type"] == TOOL_RESULT_REF_TYPE


def test_the_window_falls_back_to_the_agents_model(monkeypatch):
    import agent_system.llm.factory as factory
    monkeypatch.setattr(factory, "resolve_llm_config_for_agent",
                        lambda system, agent: SimpleNamespace(spec=SimpleNamespace(context_window=64_000)))
    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="s",
                          llm=None, agent=SimpleNamespace(agent_config=object(), system_config=object()))

    assert hooks_mod._context_window(context) == 64_000


@pytest.mark.asyncio
async def test_the_hysteresis_does_not_hold_it(plugin):
    """The hold exists to space out rewrites of old messages; this is none."""
    plugin._hysteresis_marks["held"] = hooks_mod._HysteresisMark(level=1)
    messages = [ChatMessage(role="user", content="Read chapter 3."),
                _call("c1"), _result("c1", LARGE)]

    out = await _engineer(plugin, "held", messages)

    assert _is_ref(out[-1]), "held back by the hysteresis"
