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
from plugins.context_engineer.compaction import (
    CompactionConfig,
    LayeredCompactionStrategy,
    _request_user_indices,
)
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
    async def test_injected_user_messages_do_not_age_the_request(self, tmp_path):
        config = dict(layer3_threshold=1, drop_after_turns=30)

        unmarked = await _strategy(tmp_path / "a", **config).compact(
            _unmarked(self._history()), current_tokens=10, force=True)
        assert 3 in unmarked.layers_applied
        assert not _inline(unmarked.modified_messages, "result 0"), (
            "fixture: counted as person turns, the notes must age the first steps out")

        result = await _strategy(tmp_path / "b", **config).compact(
            self._history(), current_tokens=10, force=True)

        assert 3 in result.layers_applied
        assert result.messages_dropped == 0
        assert _inline(result.modified_messages, "result 0")


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
        assert not _inline(result.modified_messages, "result 0"), (
            "a wake is a turn: a step thirty-five of them back is old")
        assert _inline(result.modified_messages, "woken 34"), (
            "the woken run's own instruction, dropped out from under it")


class TestLayerThreeKeepsWhatItsCutKeeps:
    """Layer 3 has two passes, and only the second -- the cut to a target --
    asked what must stay. The age pass runs first and asked only what a message
    IS, so it dropped what the cut, two lines later, protects by where it
    STANDS: the task, a woken run's last human message. And the cut itself
    missed the round the model has not seen yet, which Pre-Layer P protects."""

    @staticmethod
    def _aged(tmp_path, **config):
        return _strategy(tmp_path, **dict(dict(layer3_threshold=1, drop_after_turns=2), **config))

    @pytest.mark.asyncio
    async def test_the_age_pass_keeps_the_task(self, tmp_path):
        messages = [{"role": "system", "content": "You are an agent."},
                    {"role": "user", "content": "The task"}]
        for n in range(5):
            messages += [{"role": "assistant", "content": f"answer {n}"},
                         {"role": "user", "content": f"question {n}"}]

        result = await self._aged(tmp_path).compact(messages, current_tokens=10, force=True)

        assert not _inline(result.modified_messages, "answer 0"), "fixture: nothing aged out"
        assert _inline(result.modified_messages, "The task")

    @pytest.mark.asyncio
    async def test_the_age_pass_keeps_a_woken_runs_last_human_message(self, tmp_path):
        """Woken again and again, the session ages with every wake; what a
        person last sent -- and the media Layer 1 keeps for it -- went with the
        turns around it."""
        messages = [{"role": "system", "content": "You are an agent."},
                    {"role": "user", "content": "The task"},
                    {"role": "assistant", "content": "on it"},
                    {"role": "user", "content": "here is the screenshot"}]
        for n in range(4):
            messages += [{"role": "assistant", "content": f"answer {n}"},
                         {"role": "developer", "content": f"woken {n}"}]

        result = await self._aged(tmp_path).compact(messages, current_tokens=10, force=True)

        assert not _inline(result.modified_messages, "on it"), "fixture: nothing aged out"
        assert _inline(result.modified_messages, "here is the screenshot")

    @pytest.mark.asyncio
    async def test_the_age_pass_does_not_take_the_unseen_round_along_with_its_call(self, tmp_path):
        """A person writes while the tools run: the call is a turn old now, and
        its unit took the results the model has not seen yet with it."""
        messages = [{"role": "system", "content": "You are an agent."},
                    {"role": "user", "content": "The task"},
                    *_step(0),
                    {"role": "user", "content": "how far are you?"}]

        result = await self._aged(tmp_path, drop_after_turns=1).compact(
            messages, current_tokens=10, force=True)

        assert _inline(result.modified_messages, "result 0")

    @pytest.mark.asyncio
    async def test_the_cut_keeps_the_round_the_model_has_not_seen(self, tmp_path):
        """tool_preload answers a new user message with pairs of its own, in the
        same pass (tool_preload/hooks.py). Counted as the working tail, they
        filled it, and the first preloaded result -- one the model has not seen
        yet -- lay in front of it, in reach of the cut."""
        messages = [{"role": "system", "content": "You are an agent."},
                    {"role": "user", "content": "The task"}]
        for n in range(6):
            messages += _step(n)
        messages += [{"role": "assistant", "content": "Done."},
                     {"role": "user", "content": "And now the next one."}]
        for n in range(3):
            call, reply = _step(100 + n)
            messages += [dict(call, injected_by="tool_preload"), reply]

        result = await self._aged(tmp_path, drop_after_turns=10**9, target_tokens=1,
                                  tool_result_keep_last=2).compact(
            messages, current_tokens=10**6, force=True)

        assert not _inline(result.modified_messages, "result 0"), "fixture: the cut took nothing"
        assert _inline(result.modified_messages, "result 100")


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


#: What a woken run is handed, verbatim (cli_utils/agent_runner.wake_message).
WAKE = "You were woken because input is waiting for this session. Read it and act on it."


class TestAWokenRunsOnlyInstruction:
    """A run woken at a fresh prompt has no `user` message at all -- the wake
    IS its task, and it is a `developer` message. Three places read the bare
    role and none of them could reach it: both protected-set builders gate on
    there being a user message, and the sequence guard deletes whatever stands
    first and is not one."""

    @staticmethod
    def _history(steps: int) -> list[dict]:
        messages = [{"role": "system", "content": "You are an agent."},
                    {"role": "developer", "content": WAKE}]
        for n in range(steps):
            messages += _step(n)
        return messages

    @staticmethod
    async def _pruned(tmp_path, messages):
        """Pre-Layer P, the layer that actually fires in production."""
        return await _strategy(tmp_path, max_messages=20, max_messages_prune_to=8).compact(
            messages, current_tokens=10, force=True)

    @pytest.mark.asyncio
    async def test_the_wake_survives_the_prune(self, tmp_path):
        """It is the oldest candidate and the cheapest to drop, so it went
        first -- the run lost the only thing it had been told."""
        result = await self._pruned(tmp_path, self._history(40))

        assert result.messages_pruned > 0, "fixture: nothing was pruned, so nothing is proven"
        assert _inline(result.modified_messages, WAKE)

    @pytest.mark.asyncio
    async def test_no_stand_in_task_is_invented_for_it(self, tmp_path):
        """With no `user` message left the guard writes one: "Continue with the
        task." Which then IS the task, for the model and for every later
        compaction that protects the first user message as the thing everything
        refers to."""
        result = await self._pruned(tmp_path, self._history(40))

        assert not _inline(result.modified_messages, "Continue with the task.")
        first = next(msg for msg in result.modified_messages if msg.get("role") != "system")
        assert first.get("content") == WAKE, "the conversation opens on something else"

    @pytest.mark.asyncio
    async def test_the_wake_is_protected_when_layer_three_cuts_to_a_target(self, tmp_path):
        """Layer 3 builds a protected set of its own before it selects, and it
        was gated on a `user` message the same way Pre-Layer P is. Here the
        target leaves no room, so everything unprotected is fair game."""
        result = await _strategy(tmp_path, layer3_threshold=1, target_tokens=1,
                                 drop_after_turns=10**9).compact(
            self._history(40), current_tokens=10**6, force=True)

        assert result.messages_dropped > 0, "fixture: nothing was selected, so nothing is proven"
        assert _inline(result.modified_messages, WAKE)

    def test_the_repair_clears_the_way_to_the_wake_instead_of_inventing_a_task(self, tmp_path):
        """The shape the counting branch decides: nothing a person sent is left
        -- archived away, and Layer 2 protects nothing -- but the wake is still
        there, behind an assistant turn.

        Counting only `user` messages, this reads as "no input at all" and a
        stand-in is written in front of the head: "Continue with the task."
        That sentence then IS the task, for the model and for every later
        compaction, which protects the first user message as the thing
        everything else refers to. Counting inputs, the repair does its job --
        it clears down to the wake and stops there."""
        messages = [
            {"role": "system", "content": "You are an agent."},
            {"role": "assistant", "content": "what is left of an archived turn"},
            {"role": "developer", "content": WAKE},
        ]

        removed = _strategy(tmp_path)._ensure_valid_message_sequence(messages, "test")

        assert removed == 1, "it kept deleting past the wake, or stopped before the head"
        assert not _inline(messages, "Continue with the task.")
        assert [msg.get("role") for msg in messages] == ["system", "developer"]


class TestWhatTheRequestStandsOn:
    """`_request_user_indices` answers three claims with one set, and they came
    apart when the wake arrived: the API needs the last input, Layer 1 needs
    the last message a PERSON sent (its media is evicted otherwise), and the
    protected sets need the head of the turn. Letting the head take the
    person's slot spends it on a message that never carries media."""

    @staticmethod
    def _conversation(*, woken: bool) -> list[dict]:
        messages = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "an answer"},
            {"role": "user", "content": "here is the screenshot"},
            {"role": "assistant", "content": "another answer"},
            {"role": "user", "content": "Check your answer again.",
             "injected_by": "agent_continuation.followup"},
            {"role": "assistant", "content": "checked"},
        ]
        if woken:
            messages.append({"role": "developer", "content": WAKE})
        return messages

    def test_a_wake_does_not_take_the_persons_slot(self):
        """Index 2 is the last thing a person sent -- the screenshot. With the
        wake answering for "person" as well, it falls out of the set and its
        media is evicted on this very call."""
        assert _request_user_indices(self._conversation(woken=True)) == {2, 6}

    def test_without_a_wake_nothing_moved(self):
        """The counter-proof: the same conversation one message shorter must
        answer exactly as it always did."""
        assert _request_user_indices(self._conversation(woken=False)) == {2, 4}

    def test_the_head_keeps_its_slot_behind_a_later_delivery(self):
        """A direct message delivered after the wake becomes the last input.
        Without a slot of its own the wake -- that run's whole task -- then
        falls out of the set while its own turn is still being worked on."""
        messages = self._conversation(woken=True) + [
            {"role": "user", "content": "v6 fragt: wie weit bist du?",
             "injected_by": "debate_forum_direct"}]

        assert _request_user_indices(messages) == {2, 6, 7}
