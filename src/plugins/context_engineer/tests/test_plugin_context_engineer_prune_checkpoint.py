"""A prune leaves a checkpoint of what the moved messages established, updated on the next prune, never stacked.

Before, Pre-Layer P left a counter and the retrieval tools: the decisions and the work state of the moved messages
existed only in the archive. With prune_checkpoint_profile a cheap model writes the checkpoint into the one
breadcrumb; a failed or slow call keeps the previous one and the prune goes ahead.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.context_engineer import checkpoint as checkpoint_module
from plugins.context_engineer import compaction as compaction_module
from plugins.context_engineer import hooks as hooks_mod
from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import (
    PRUNE_NOTICE_TYPE, CompactionConfig, LayeredCompactionStrategy, _current_checkpoint, _is_prune_notice)
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.hooks import ContextEngineerPlugin
from plugins.context_engineer.tool_result_store import ToolResultStore

PLUGIN_DIR = Path(hooks_mod.__file__).parent


def _strategy(tmp_path) -> LayeredCompactionStrategy:
    return LayeredCompactionStrategy(
        tool_store=ToolResultStore(tmp_path / "tools.db"),
        core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
        archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
        config=CompactionConfig(layer1_threshold=10**9, layer2_threshold=10**9, layer3_threshold=10**9,
                                max_messages=40, max_messages_prune_to=20, keep_system_messages=True))


def _conversation(start: int, count: int) -> list[dict]:
    return [{"role": "assistant", "content": f"step {i}: decided to keep parser v{i}"} for i in range(start, count)]


def _notices(messages: list[dict]) -> list[dict]:
    return [json.loads(m["content"]) for m in messages if _is_prune_notice(m)]


class Writer:
    """The checkpoint model: answers in turn, records each prompt; an Exception answer is raised."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    async def __call__(self, prompt: str) -> str | None:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _prune(strategy, messages):
    result = await strategy.compact(messages, current_tokens=100)
    assert result.messages_pruned, "fixture: nothing was pruned"
    return result.modified_messages


@pytest.mark.asyncio
async def test_a_prune_writes_a_checkpoint_and_the_next_one_updates_it(tmp_path):
    strategy = _strategy(tmp_path)
    strategy.checkpoint = writer = Writer("## Objective\n- first", "## Objective\n- second")
    messages = [{"role": "user", "content": "ASSIGNMENT-7Q: refactor the parser"}, *_conversation(0, 44), {"role": "user", "content": "go"}]

    messages = await _prune(strategy, messages)
    [first] = _notices(messages)
    assert first["checkpoint"] == "## Objective\n- first" and "checkpoint says" in first["hint"]
    assert "step 0: decided" in writer.prompts[0] and "<existing-checkpoint>" not in writer.prompts[0]
    assert "ASSIGNMENT-7Q" not in writer.prompts[0], "the protected first input is not leaving"

    messages = await _prune(strategy, messages[:-1] + _conversation(44, 70) + [messages[-1]])
    notices = _notices(messages)
    assert len(notices) == 1 and notices[0]["checkpoint"] == "## Objective\n- second"
    assert "<existing-checkpoint>\n## Objective\n- first\n</existing-checkpoint>" in writer.prompts[1]
    assert notices[0]["total_removed"] > first["total_removed"]


@pytest.mark.asyncio
async def test_a_failed_or_slow_checkpoint_keeps_the_previous_one_and_prunes_anyway(tmp_path, monkeypatch):
    strategy = _strategy(tmp_path)
    monkeypatch.setattr(checkpoint_module, "CHECKPOINT_BUDGET_S", 0.2)
    monkeypatch.setattr(compaction_module, "SUMMARY_MIN_CALL_S", 0.05)   # else 0.2 s is no call at all

    writer = Writer("kept", RuntimeError("the cheap model is down"))
    strategy.checkpoint = writer
    messages = await _prune(strategy, [{"role": "user", "content": "ASSIGNMENT-7Q: refactor the parser"}, *_conversation(0, 44),
                                       {"role": "user", "content": "go"}])
    messages = await _prune(strategy, messages[:-1] + _conversation(44, 70) + [messages[-1]])
    assert [n["checkpoint"] for n in _notices(messages)] == ["kept"]

    calls = []

    async def slow(prompt):
        calls.append(prompt)
        await asyncio.sleep(5)

    strategy.checkpoint = slow
    before = len(messages)
    started = time.monotonic()
    messages = await _prune(strategy, messages[:-1] + _conversation(70, 96) + [messages[-1]])
    assert calls, "fixture: the slow call was never started"
    assert time.monotonic() - started < 3, "the budget did not end the slow call"
    assert len(messages) < before + 26 and [n["checkpoint"] for n in _notices(messages)] == ["kept"]


@pytest.mark.asyncio
async def test_a_checkpoint_left_behind_says_so_and_the_next_one_catches_up(tmp_path):
    strategy = _strategy(tmp_path)
    strategy.checkpoint = writer = Writer("## first", RuntimeError("down"), "## caught up")
    messages = await _prune(strategy, [{"role": "user", "content": "ASSIGNMENT-7Q: refactor the parser"},
                                       *_conversation(0, 44), {"role": "user", "content": "go"}])
    messages = await _prune(strategy, messages[:-1] + _conversation(44, 70) + [messages[-1]])
    [behind] = _notices(messages)
    assert behind["checkpoint"] == "## first" and behind["checkpoint_gap"] > 0
    assert f"except for {behind['checkpoint_gap']} of them" in behind["hint"]

    messages = await _prune(strategy, messages[:-1] + _conversation(70, 96) + [messages[-1]])
    [caught_up] = _notices(messages)
    assert caught_up["checkpoint"] == "## caught up" and "checkpoint_gap" not in caught_up
    assert "step 44: decided" in writer.prompts[2], "the batch the failed call missed was not given again"


@pytest.mark.asyncio
async def test_messages_moved_before_the_checkpoint_was_switched_on_are_not_claimed_as_covered(tmp_path):
    """The first checkpoint covers only what it was given: a gap of 0 claimed it summed up all moved messages."""
    strategy = _strategy(tmp_path)
    messages = await _prune(strategy, [{"role": "user", "content": "ASSIGNMENT-7Q: refactor the parser"},
                                       *_conversation(0, 44), {"role": "user", "content": "go"}])
    [before] = _notices(messages)
    assert "checkpoint" not in before and "checkpoint_gap" not in before, "fixture: a checkpoint without a writer"

    strategy.checkpoint = Writer("## from now on")
    messages = await _prune(strategy, messages[:-1] + _conversation(44, 70) + [messages[-1]])
    [after] = _notices(messages)

    assert after["checkpoint"] == "## from now on" and after["checkpoint_gap"] == before["total_removed"], after
    assert f"except for {before['total_removed']} of them" in after["hint"]


@pytest.mark.asyncio
async def test_a_trial_run_calls_no_model_and_one_compaction_shares_one_budget(tmp_path, monkeypatch):
    """Layer 3's yield check runs the layers on a copy: a call there may be thrown away, and three calls of
    25 s each overran the hook's 60 s, which drops the whole compaction."""
    strategy = _strategy(tmp_path)
    removed = [{"role": "assistant", "content": "step: decided"}]
    strategy.checkpoint = writer = Writer("never")
    token = compaction_module._TRIAL.set(True)
    try:
        assert await strategy._write_checkpoint([], removed) == (None, 0) and writer.prompts == []
    finally:
        compaction_module._TRIAL.reset(token)

    monkeypatch.setattr(compaction_module, "SUMMARY_MIN_CALL_S", 0.05)

    async def takes_a_while(prompt):
        await asyncio.sleep(0.6)
        return "## done"

    strategy.checkpoint = takes_a_while
    strategy._checkpoint_budget = 1.0   # what compact() sets for one run
    assert await strategy._write_checkpoint([], removed) == ("## done", 1)
    started = time.monotonic()
    assert await strategy._write_checkpoint([], removed) == (None, 0), "the second call had the whole budget again"
    assert time.monotonic() - started < 0.55


def test_the_checkpoint_carried_is_that_of_the_notice_with_the_highest_count():
    def notice(total, text):
        return {"role": "system", "content": json.dumps(
            {"type": PRUNE_NOTICE_TYPE, "total_removed": total, "hint": "h", "checkpoint": text})}
    assert _current_checkpoint([notice(30, "newer"), notice(10, "older")]) == "newer"
    assert _current_checkpoint([notice(10, "older"), notice(30, "newer")]) == "newer"
    assert _current_checkpoint([{"role": "user", "content": "x"}]) is None


def test_an_answer_too_long_is_cut_at_a_line_break_and_an_empty_one_is_none():
    lines = [f"- bullet {i} " + "z" * 90 for i in range(200)]   # ~20 KB
    whole = "\n".join(lines)
    cut = checkpoint_module.clean(whole)
    assert len(cut) <= checkpoint_module.CHECKPOINT_CHARS + 6 and cut.endswith("\n[...]")
    kept = cut[:-len("\n[...]")]
    assert whole.startswith(kept) and kept.endswith("z"), "not cut at a line break"
    assert checkpoint_module.clean("   ") is None and checkpoint_module.clean(None) is None


@pytest.mark.asyncio
async def test_without_a_writer_the_breadcrumb_only_counts(tmp_path):
    strategy = _strategy(tmp_path)
    messages = await _prune(strategy, [{"role": "user", "content": "ASSIGNMENT-7Q: refactor the parser"}, *_conversation(0, 44),
                                       {"role": "user", "content": "go"}])
    [notice] = _notices(messages)
    assert "checkpoint" not in notice and notice["type"] == PRUNE_NOTICE_TYPE


def test_the_transcript_names_tool_calls_cuts_long_messages_and_keeps_the_newest():
    long = "x" * (checkpoint_module.CHECKPOINT_MESSAGE_CHARS + 500)
    text, shown = checkpoint_module.transcript([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "coder_fs_read_file",
                                                          "arguments": '{"filePath": "src/app.py"}'}}]},
        {"role": "tool", "name": "coder_fs_read_file", "content": long},
    ])
    assert '-> coder_fs_read_file({"filePath": "src/app.py"})' in text
    assert "[tool result coder_fs_read_file" in text and " [...]" in text and long not in text and shown == 2

    # Over the input size, every message is cut shorter before the oldest are dropped.
    many = [{"role": "assistant", "content": f"message {i} " + "y" * 3000} for i in range(60)]
    shorter, shown = checkpoint_module.transcript(many)
    assert len(shorter) <= checkpoint_module.CHECKPOINT_INPUT_CHARS and shown == 60
    assert "message 0 " in shorter and "message 59 " in shorter and not shorter.startswith("[...")

    # Only when even the shortest cut does not fit is the oldest part left out.
    lots = [{"role": "assistant", "content": f"message {i} " + "y" * 3000} for i in range(600)]
    capped, shown = checkpoint_module.transcript(lots)
    assert len(capped) <= checkpoint_module.CHECKPOINT_INPUT_CHARS
    assert "message 599 " in capped and "message 0 " not in capped
    # whole messages: the count says how many of the newest are in, so the checkpoint is not taken to cover the rest
    assert 0 < shown < 600 and f"message {600 - shown} " in capped and f"message {599 - shown} " not in capped
    assert capped.startswith(f"[... {600 - shown} earlier leaving messages omitted]")


def test_the_transcript_masks_credentials():
    text, _ = checkpoint_module.transcript([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "http_get", "arguments": '{"url": "https://api.example.com/v1?key=SECRET123", '
                                                 '"api_key": "SECRET456"}'}}]},
        {"role": "tool", "name": "http_get", "content": "redirected to https://x.example/cb?token=SECRET789"},
    ])
    assert "SECRET" not in text, text
    assert "https://api.example.com/v1?key=" in text, "fixture: the call is not in the transcript"


class FakeLLM:
    """Reports its answer to the hooks it was given, as a real client does (_notify_post_response)."""

    def __init__(self, answer="## Objective\n- written by the profile"):
        self.prompts = []
        self.answer = answer
        self.on_post_response = None

    def set_llm_hooks(self, on_pre_request=None, on_post_response=None):
        self.on_post_response = on_post_response

    async def chat(self, messages, **kwargs):
        self.prompts.append(messages[0].content)
        if self.on_post_response:
            await self.on_post_response({"provider": "fake", "model": "cheap-model",
                                          "usage": {"prompt_tokens": 900, "completion_tokens": 120}})
        return self.answer


#: What llm_openai's chat() returns for a failed call: an error as the answer's text.
LLM_ERROR = json.dumps({"_llm_error": {"error": True, "message": "quota exceeded"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, written", [
    ("## Objective\n- written by the profile", ["## Objective\n- written by the profile"]),
    (LLM_ERROR, [None]),   # taken as text, the error replaced the work state and emptied the pending batch
])
async def test_the_profile_set_for_an_agent_writes_the_checkpoint_through_the_hook(tmp_path, monkeypatch,
                                                                                   answer, written):
    from agent_system.llm import factory, hook_notify
    fake = FakeLLM(answer)
    asked = []
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda *a, **k: asked.append((a, k)) or fake)
    booked = []

    async def record(**fields):
        booked.append(fields)

    monkeypatch.setattr(hook_notify, "notify_response", record)
    plugin = ContextEngineerPlugin(PLUGIN_DIR, stats_history=[])
    plugin._storage_base = tmp_path
    plugin.apply_config({})
    messages = [ChatMessage(role="user", content="the task")]
    messages += [ChatMessage(role="assistant", content=f"step {i}: decided") for i in range(44)]
    messages.append(ChatMessage(role="user", content="go"))
    agent = SimpleNamespace(system_config=SimpleNamespace(network=SimpleNamespace(ssl_verify=True)))
    from agent_system.core.request_context import current_run_session
    run = current_run_session.set("cp")   # the loop sets it around a run; the cached client serves every session
    try:
        result = await plugin.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="req-cp", session_id="cp", messages=messages,
            llm=SimpleNamespace(context_window=1_000_000), agent=agent,
            hook_config={"max_messages": 40, "max_messages_prune_to": 20, "prune_checkpoint_profile": "cheap"}))
        assert result.success, result.error
        notices = [json.loads(m.content) for m in result.context.messages
                   if isinstance(m.content, str) and PRUNE_NOTICE_TYPE in m.content]
        assert [n.get("checkpoint") for n in notices] == written
        assert fake.prompts and "step 0: decided" in fake.prompts[0]
        assert "cheap" in repr(asked), asked
        # no agent wires this client: its spend reached no ledger until it was booked as agent-less
        assert [(b["usage"], b["session_id"]) for b in booked] == [
            ({"prompt_tokens": 900, "completion_tokens": 120}, "cp")], booked
    finally:
        current_run_session.reset(run)
        for sid in list(plugin._session_components):
            plugin.cleanup_session(sid)


@pytest.mark.asyncio
async def test_messages_the_transcript_left_out_are_not_counted_as_covered(tmp_path):
    strategy = _strategy(tmp_path)
    strategy.checkpoint = Writer("## the newest")
    strategy._checkpoint_budget = 25.0
    removed = [{"role": "assistant", "content": f"message {i} " + "y" * 3000} for i in range(600)]

    text, covered = await strategy._write_checkpoint([], removed)

    assert text == "## the newest" and 0 < covered < 600, covered


def test_a_stale_duplicate_notice_does_not_inflate_the_gap(tmp_path):
    """The gap is that of the notice whose count is carried: the largest gap of any said 100 were uncovered."""
    strategy = _strategy(tmp_path)
    stale = {"type": PRUNE_NOTICE_TYPE, "total_removed": 100}
    current = {"type": PRUNE_NOTICE_TYPE, "total_removed": 200, "checkpoint": "## so far"}
    messages = [{"role": "system", "content": json.dumps(stale)}, {"role": "system", "content": json.dumps(current)},
                {"role": "user", "content": "go"}]

    strategy._leave_prune_notice(messages, 10, "## updated", covered=10)

    [notice] = _notices(messages)
    assert notice["total_removed"] == 210 and "checkpoint_gap" not in notice, notice
