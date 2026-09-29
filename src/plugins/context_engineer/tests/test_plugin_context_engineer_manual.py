"""/compact below the automatic thresholds.

A person typing /compact was told "saved 0 tokens" whenever the session sat
below layer1_threshold: the manual flag got compact() past its entry check,
but Layer 1 then asked the token threshold again. Manual now runs Layer 1 —
the reversible one — whatever the token count. Layers 2 and 3 take messages
out of the conversation; they keep their thresholds, manual or not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import CompactionConfig, LayeredCompactionStrategy
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.tool_result_store import ToolResultStore

BIG = "a large tool result line with enough words to count " * 60


def _strategy(tmp_path):
    # Production-like thresholds: far above what these sessions carry.
    return LayeredCompactionStrategy(
        tool_store=ToolResultStore(tmp_path / "tools.db"),
        core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
        archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
        config=CompactionConfig(
            layer1_threshold=80_000, layer2_threshold=100_000, layer3_threshold=120_000,
            target_tokens=60_000, tool_result_min_size=100, tool_result_keep_last=1,
            archive_after_turns=3, drop_after_turns=6, max_messages=0,
            keep_system_messages=True,
        ),
    )


def _session(turns: int) -> list[dict]:
    """Each turn: a question, one tool call with a large result, an answer."""
    msgs: list[dict] = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"question {i}"})
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {"name": "grep", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "name": "grep", "tool_call_id": f"call_{i}", "content": BIG})
        msgs.append({"role": "assistant", "content": f"answer {i}"})
    return msgs


class TestManualIgnoresTheThresholds:

    @pytest.mark.asyncio
    async def test_manual_runs_the_reversible_layer_below_every_threshold(self, tmp_path):
        messages = _session(8)
        result = await _strategy(tmp_path).compact(messages, current_tokens=5_000, manual=True)

        assert result.tool_results_stored > 0, "old tool results stayed inline"
        assert result.layers_applied == [1]

    @pytest.mark.asyncio
    async def test_below_the_thresholds_no_message_leaves_the_conversation(self, tmp_path):
        """The session is old enough for Layer 2 (3 turns) and Layer 3 (6 turns)."""
        messages = _session(8)
        result = await _strategy(tmp_path).compact(messages, current_tokens=5_000, manual=True)

        assert result.messages_archived == 0, "a manual compaction archived turns below the threshold"
        assert result.messages_dropped == 0, "a manual compaction dropped turns below the threshold"
        assert len(result.modified_messages) == len(messages)

    @pytest.mark.asyncio
    async def test_at_the_limit_manual_goes_on_to_layer_3(self, tmp_path):
        strategy = _strategy(tmp_path)
        strategy.config.layer2_threshold = 1
        strategy.config.layer3_threshold = 1
        strategy.config.target_tokens = 0
        result = await strategy.compact(_session(8), current_tokens=5_000, manual=True)

        assert result.layers_applied == [1, 2, 3]
        assert result.messages_dropped > 0

    @pytest.mark.asyncio
    async def test_keep_last_still_decides_what_can_go(self, tmp_path):
        """Two turns: one result protected by keep_last."""
        messages = _session(2)
        result = await _strategy(tmp_path).compact(messages, current_tokens=5_000, manual=True)

        assert result.tool_results_stored == 1, "keep_last=1 keeps the newest result inline"
        assert result.modified_messages[-2]["content"] == BIG

    @pytest.mark.asyncio
    async def test_force_without_manual_still_respects_the_thresholds(self, tmp_path):
        """force is also set by media events and the byte limit — those must not
        start archiving a small session."""
        result = await _strategy(tmp_path).compact(_session(8), current_tokens=5_000, force=True)

        assert result.tool_results_stored == 0
        assert result.messages_archived == 0 and result.messages_dropped == 0


class TestTheToolPathIsManual:

    @pytest.mark.asyncio
    async def test_engineer_context_passes_the_manual_trigger_on(self, tmp_path, monkeypatch):
        from agent_system.hooks import HookContext, HookType
        from agent_system.llm.models import ChatMessage
        from plugins.context_engineer.hooks import ContextEngineerPlugin

        hooks = ContextEngineerPlugin(Path("src/plugins/context_engineer"))
        hooks._storage_base = tmp_path
        strategy = hooks._get_session_components("s-manual")["strategy"]
        seen = {}
        original = strategy.compact

        async def spy(*args, **kwargs):
            seen.update(kwargs)
            return await original(*args, **kwargs)

        monkeypatch.setattr(strategy, "compact", spy)

        await hooks.engineer_context(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="s-manual", session_id="s-manual",
            messages=[ChatMessage(role="user", content="hi"),
                      ChatMessage(role="assistant", content="hello")],
            metadata={"manual_trigger": True},
        ))

        assert seen.get("manual") is True
