"""An archived message keeps the stamps a session read back finds its sub-agents by.

A run's first message carries the run's id, a message with tool calls the id
each call's tool ran under (ChatMessage.request_id, .tool_request_ids); a run a
tool started carries its tool's id as prefix. Layer 2 swaps an archived message
for a placeholder built field by field: a stamp it does not carry over is gone
from the session, and with it the sub-agent's run from the chat.
"""

from __future__ import annotations

import json

import pytest

from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import CompactionConfig, LayeredCompactionStrategy
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.tool_result_store import ToolResultStore


def _archived(message: dict) -> bool:
    try:
        return json.loads(message.get("content") or "").get("type") == "archived_ref"
    except (TypeError, ValueError, AttributeError):
        return False


@pytest.mark.asyncio
async def test_an_archived_run_opener_and_call_keep_their_stamps(tmp_path):
    strategy = LayeredCompactionStrategy(
        tool_store=ToolResultStore(tmp_path / "tools.db", session_id="t"),
        core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
        archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
        config=CompactionConfig(layer1_threshold=10**9, layer2_threshold=1, layer3_threshold=10**9,
                                target_tokens=10**7, max_messages=0, deduplicate_media=False,
                                archive_after_turns=1),
    )
    messages = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "The earlier question", "request_id": "run0000001"},
        {"role": "assistant", "content": None, "tool_request_ids": {"call_1": "run0000001_001"}, "step": 1,
         "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "result"},
        {"role": "assistant", "content": "The earlier answer"},
        {"role": "user", "content": "The current request", "request_id": "run0000002"},
        {"role": "assistant", "content": "Working on it"},
    ]

    result = await strategy.compact(messages, current_tokens=10, force=True)

    view = result.modified_messages
    opener = next(m for m in view if m["role"] == "user" and _archived(m))
    call = next(m for m in view if m.get("tool_calls"))
    assert _archived(call), "fixture: the earlier turn's call must be archived"
    assert opener.get("request_id") == "run0000001"
    assert call.get("tool_request_ids") == {"call_1": "run0000001_001"}
    assert call.get("step") == 1
