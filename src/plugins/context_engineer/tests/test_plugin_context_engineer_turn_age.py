"""How old a message is, in turns — for Layers 2 and 3.

A turn is a user message nobody injected. The agent loop and the hooks add
marked user messages of their own (step budget note, loop intervention,
follow-ups, debate posts); counted as turns, each of them aged the request it
belongs to, until Layer 3 dropped the task still being worked on and Layer 2
archived the request a follow-up asked to check again.
"""

from __future__ import annotations

import pytest

from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import CompactionConfig, LayeredCompactionStrategy
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.tool_result_store import ToolResultStore

#: The markers production writes on user messages.
MARKERS = ("agent.step_budget", "agent.loop_intervention", "agent_continuation.followup",
           "agent.empty_response", "agent.max_steps", "debate_forum_posts")


def _strategy(tmp_path, **config):
    defaults = dict(layer1_threshold=10**9, layer2_threshold=10**9, layer3_threshold=10**9,
                    target_tokens=10**7, max_messages=0, deduplicate_media=False)
    defaults.update(config)
    return LayeredCompactionStrategy(
        tool_store=ToolResultStore(tmp_path / "tools.db", session_id="t"),
        core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
        archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
        config=CompactionConfig(**defaults),
    )


def _step(n: int) -> list[dict]:
    call_id = f"call_{n}"
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "name": "read", "content": f"result {n}"},
    ]


def _inline(messages: list[dict], text: str) -> bool:
    """Whether a message with this exact content is still in the view, unarchived."""
    return any(msg.get("content") == text for msg in messages)


def _unmarked(messages: list[dict]) -> list[dict]:
    return [{k: v for k, v in msg.items() if k != "injected_by"} for msg in messages]


class TestLayerThree:

    @staticmethod
    def _history() -> list[dict]:
        messages = [{"role": "system", "content": "You are an agent."},
                    {"role": "user", "content": "The task"}]
        for n in range(35):
            messages += _step(n)
            messages.append({"role": "user", "content": f"note {n}",
                             "injected_by": MARKERS[n % len(MARKERS)]})
        return messages

    @pytest.mark.asyncio
    async def test_injected_user_messages_do_not_age_the_task(self, tmp_path):
        config = dict(layer3_threshold=1, drop_after_turns=30)

        unmarked = await _strategy(tmp_path / "a", **config).compact(
            _unmarked(self._history()), current_tokens=10, force=True)
        assert 3 in unmarked.layers_applied
        assert not _inline(unmarked.modified_messages, "The task"), (
            "fixture: counted as person turns, the notes must age the task out")

        result = await _strategy(tmp_path / "b", **config).compact(
            self._history(), current_tokens=10, force=True)

        assert 3 in result.layers_applied
        assert result.messages_dropped == 0
        assert _inline(result.modified_messages, "The task")


    @pytest.mark.asyncio
    async def test_a_wake_opens_a_turn_like_a_typed_line(self, tmp_path):
        """A woken run's task is a `developer` message, nobody injected
        (cli_utils/agent_runner.wake_message). Read as "no turn here", a
        session woken again and again without anybody typing keeps every age
        frozen at the last human turn -- the age layers stop firing at all --
        and the wake itself, the only instruction that run has, stands outside
        the protected set."""
        config = dict(layer3_threshold=1, drop_after_turns=30)
        messages = [{"role": "system", "content": "You are an agent."},
                    {"role": "user", "content": "The task"}]
        for n in range(35):
            messages += _step(n)
            messages.append({"role": "developer", "content": f"woken {n}"})

        result = await _strategy(tmp_path, **config).compact(
            messages, current_tokens=10, force=True)

        assert 3 in result.layers_applied
        assert not _inline(result.modified_messages, "The task"), (
            "a wake is a turn: a task thirty-five of them back is old")
        assert _inline(result.modified_messages, "woken 34"), (
            "the woken run's own instruction, dropped out from under it")


class TestLayerTwo:

    @pytest.mark.asyncio
    async def test_step_notes_do_not_archive_the_current_request(self, tmp_path):
        messages = [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "The earlier question"},
            {"role": "assistant", "content": "The earlier answer"},
            {"role": "user", "content": "The current request"},
            *_step(1), *_step(2),
            {"role": "user", "content": "Step 4 of 5", "injected_by": "agent.step_budget"},
            *_step(3),
            {"role": "user", "content": "Step 5 of 5", "injected_by": "agent.step_budget"},
        ]
        result = await _strategy(tmp_path, layer2_threshold=1, archive_after_turns=1).compact(
            messages, current_tokens=10, force=True)

        assert 2 in result.layers_applied
        assert not _inline(result.modified_messages, "The earlier question"), (
            "fixture: a real turn older than archive_after_turns must be archived")
        assert _inline(result.modified_messages, "The current request")

    @pytest.mark.asyncio
    async def test_a_follow_up_does_not_archive_the_request_it_checks(self, tmp_path):
        messages = [
            {"role": "user", "content": "The current request"},
            {"role": "assistant", "content": "Done."},
            {"role": "user", "content": "Check your answer again.",
             "injected_by": "agent_continuation.followup"},
        ]
        result = await _strategy(tmp_path, layer2_threshold=1, archive_after_turns=1).compact(
            messages, current_tokens=10, force=True)

        assert 2 in result.layers_applied
        assert _inline(result.modified_messages, "The current request")
