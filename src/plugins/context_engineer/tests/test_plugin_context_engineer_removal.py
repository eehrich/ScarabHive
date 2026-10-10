"""What happens when messages leave the conversation.

Two questions that used to be answered differently in different places and
now run through one path (``_archive_then_remove``):

1. Is the content still reachable afterwards? Pre-Layer P used to archive,
   Layer 3 deleted raw -- the same operation, two answers.
2. How often does it happen? Every removal rewrites the start of the
   conversation and thereby invalidates the provider prompt cache for
   everything behind it.
"""

from __future__ import annotations

import json

import pytest

from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import (
    CompactionConfig,
    LayeredCompactionStrategy,
)
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.tool_result_store import ToolResultStore


def _turns(n: int) -> list[dict]:
    msgs: list[dict] = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"Frage {i}"})
        msgs.append({"role": "assistant", "content": f"unersetzliche Antwort {i}"})
    return msgs


class TestPruneHysteresis:
    """A prune must buy quiet steps, not trigger again on the next one.

    Trimming to exactly ``max_messages`` put the next step over the limit
    again. A session at the limit therefore paid the cache break on EVERY
    step for a single saved message -- the reason ``max_messages`` was
    switched off in production ("breaks cache").
    """

    @staticmethod
    def _strategy(tmp_path, prune_to: int, max_messages: int = 40):
        return LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9,
                max_messages=max_messages, max_messages_prune_to=prune_to,
                keep_system_messages=True,
            ),
        )

    @staticmethod
    def _over_the_limit() -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": "die Aufgabe"}]
        msgs += [{"role": "assistant", "content": f"Antwort {i}"} for i in range(44)]
        msgs += [{"role": "user", "content": "letzte Frage"}]
        return msgs

    @pytest.mark.asyncio
    async def test_a_prune_goes_below_the_limit_not_to_it(self, tmp_path):
        strat = self._strategy(tmp_path, prune_to=20)

        result = await strat.compact(self._over_the_limit(), current_tokens=100)

        kept = len(result.modified_messages)
        assert kept <= 40 - 20 + 1, (
            f"trimmed only to {kept}; a prune that lands ON the limit "
            f"triggers again at the very next message")

    @pytest.mark.asyncio
    async def test_the_next_steps_are_quiet(self, tmp_path):
        """The gain: one cache break instead of one per step."""
        strat = self._strategy(tmp_path, prune_to=20)
        messages = self._over_the_limit()

        pruning_rounds = 0
        for step in range(15):
            result = await strat.compact(messages, current_tokens=100)
            if result.messages_pruned:
                pruning_rounds += 1
            # compact() works on a copy; the conversation continues with the
            # result, just as the hook writes it back.
            messages = result.modified_messages
            messages.append({"role": "assistant", "content": f"neu {step}"})
            assert len(messages) <= 41, (
                f"step {step}: {len(messages)} messages are over the "
                f"limit -- the hysteresis must not let the list grow")

        assert pruning_rounds == 1, (
            f"{pruning_rounds} of 15 steps pruned. Each rewrites the front of "
            f"the conversation and invalidates the prompt cache behind it; that "
            f"is what max_messages_prune_to is for")

    @pytest.mark.asyncio
    async def test_a_deep_cut_buys_as_many_quiet_messages(self, tmp_path):
        """200 / 30: one prune, then 170 messages without a cache break.

        The half-limit cap kept every prune shallow; the cut goes as deep as
        configured, and the next prune waits for the whole distance.
        """
        strat = self._strategy(tmp_path, prune_to=30, max_messages=200)
        messages = [{"role": "user", "content": "die Aufgabe"}]
        messages += [{"role": "assistant", "content": f"Antwort {i}"} for i in range(200)]
        messages += [{"role": "user", "content": "letzte Frage"}]

        prunes = []
        for step in range(172):
            result = await strat.compact(messages, current_tokens=100)
            if result.messages_pruned:
                prunes.append((step, len(result.modified_messages)))
            messages = result.modified_messages
            messages.append({"role": "assistant", "content": f"neu {step}"})

        assert prunes[0] == (0, 30), f"the first prune did not cut to 30: {prunes}"
        assert [s for s, _ in prunes] == [0, 171], (
            f"prunes at steps {[s for s, _ in prunes]}: 30 kept, the next is due "
            f"once 171 more arrived (over 200), not before")

    @pytest.mark.asyncio
    async def test_prune_to_the_limit_still_trims_to_the_limit(self, tmp_path):
        """The old behaviour stays reachable: prune whenever the list is over the limit."""
        strat = self._strategy(tmp_path, prune_to=40)

        result = await strat.compact(self._over_the_limit(), current_tokens=100)
        assert 39 <= len(result.modified_messages) <= 40


class TestLayer3DoesNotDestroy:
    """Layer 3 archives before deleting, like Pre-Layer P right next to it.

    It was the last place that removed messages without a copy. The
    docstring called that "irreversible", and in the last 1000 production
    compactions the layer fired zero times -- so the gap was latent, not
    observed. It remains a gap: the layer runs as soon as the token
    thresholds say so.
    """

    @staticmethod
    def _strategy(tmp_path, keep_system: bool = True):
        archival = ArchivalMemory(tmp_path / "archive.db", session_id="t")
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=archival,
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=1, target_tokens=0,
                drop_after_turns=2, max_messages=0,
                keep_system_messages=keep_system,
            ),
        )
        return strat, archival

    @pytest.mark.asyncio
    async def test_dropped_messages_reach_the_archive(self, tmp_path):
        strat, archival = self._strategy(tmp_path)
        messages = _turns(8)

        result = await strat.compact(messages, current_tokens=10_000, force=True)

        assert result.messages_dropped > 0, (
            "Layer 3 dropped nothing -- this fixture does not test the layer")
        stored = [r[0] for r in archival._db.execute(
            "SELECT content FROM archived_messages").fetchall()]
        live = {str(m.get("content")) for m in result.modified_messages}
        for i in range(3):
            text = f"unersetzliche Antwort {i}"
            assert any(text in s for s in stored) or text in live, (
                f"'{text}' left the conversation without reaching the archive")

    @pytest.mark.asyncio
    async def test_a_failed_archive_write_keeps_the_messages(self, tmp_path, monkeypatch):
        """Not deleting beats deleting without a copy.

        The same trade-off as in Pre-Layer P: a too-long context costs money,
        destroyed content cannot be restored. The write is all-or-nothing, so
        a failure affects the whole batch.
        """
        strat, archival = self._strategy(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(archival, "store_many", boom)

        messages = _turns(8)
        before = [str(m.get("content")) for m in messages]

        result = await strat.compact(messages, current_tokens=10_000, force=True)

        assert result.messages_dropped == 0
        after = [str(m.get("content")) for m in result.modified_messages]
        assert before == after, "messages were destroyed although nothing was stored"

    @pytest.mark.asyncio
    async def test_the_breadcrumb_survives_a_drop(self, tmp_path):
        """The notice is protected by its TYPE, not by its role.

        Layer 3 only checked the role. With ``keep_system_messages=False`` it
        threw out the one message that tells the agent that anything was
        archived at all -- and the running counter in it restarts at zero on
        the next prune.
        """
        strat, _ = self._strategy(tmp_path, keep_system=False)
        notice = json.dumps({"type": "pruned_notice", "total_removed": 137,
                             "hint": "aeltere Nachrichten wurden ausgelagert"})
        messages = [{"role": "system", "content": notice}] + _turns(8)

        result = await strat.compact(messages, current_tokens=10_000, force=True)

        kept = [m for m in result.modified_messages
                if "pruned_notice" in str(m.get("content"))]
        assert kept, "the notice was dropped -- the agent cannot know there is something to fetch"
        assert json.loads(kept[0]["content"])["total_removed"] >= 137, (
            "the running counter was reset")


class TestLayer1ResultsAreFindable:
    """A stored result the agent cannot LIST is only half stored.

    Layer 1 called store_and_reference without a session_id, so every result
    landed under "default" while list/search query the real id. Measured: one
    compaction, then list(section='tool_results') answered "0 of 0" - and the
    system prompt tells the model to look exactly there. read(ref=...) still
    worked, which is why nothing noticed: the catalogue was blind, not the
    content. The tests that covered list() seeded the store by hand with the
    right id and so never exercised the path production uses.
    """

    @pytest.mark.asyncio
    async def test_a_compacted_result_shows_up_in_the_catalogue(self, tmp_path):
        from pathlib import Path

        from plugins.context_engineer.hooks import ContextEngineerPlugin

        hooks = ContextEngineerPlugin(Path("src/plugins/context_engineer"))
        hooks._storage_base = tmp_path
        sid = "a-real-session"
        strat = hooks._get_session_components(sid)["strategy"]
        strat.config.layer1_threshold = 1
        strat.config.tool_result_min_size = 20
        strat.config.tool_result_keep_last = 0

        messages = [
            {"role": "user", "content": "mach was"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "name": "t", "tool_call_id": "call_1",
             "content": "sehr grosses Werkzeug-Ergebnis " * 80},
        ]
        result = await strat.compact(
            messages, current_tokens=10**6, force=True, session_id=sid)
        assert result.tool_results_stored == 1, "fixture stored nothing"

        listed = await hooks._handle_context_list(
            section="tool_results", session_id=sid)
        assert listed["total"] == 1, (
            "the compacted result is not in the catalogue - list() is blind "
            "while the system prompt points the model at it")
        assert "Werkzeug-Ergebnis" in listed["entries"][0]["summary"], (
            "the preview never reaches the only place that shows it")

        found = await hooks._handle_context_list(
            filter="Werkzeug", session_id=sid)
        assert found["count"] >= 1, "search cannot reach it either"


class TestAnInheritedMarkNeverCollapsesTheConversation:
    """A mark that does not fit the limit must trim, not empty.

    A distance below the limit (the old max_messages_headroom, default 50)
    went to 1 for ANY limit below it: an agent configured with
    max_messages=40 lost its whole conversation on the first prune instead
    of 20 messages. An absolute mark above the agent's own limit, inherited
    from the plugin config, prunes to half the limit.
    """

    @pytest.mark.parametrize("max_messages", [200, 40, 30, 10, 2])
    @pytest.mark.asyncio
    async def test_a_prune_keeps_a_workable_conversation(self, tmp_path, caplog, max_messages):
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9,
                max_messages=max_messages, max_messages_prune_to=100,
                keep_system_messages=True,
            ),
        )
        messages = [{"role": "user", "content": "die Aufgabe"}]
        messages += [{"role": "assistant", "content": f"Antwort {i}"}
                     for i in range(max_messages * 2)]
        messages += [{"role": "user", "content": "letzte Frage"}]

        result = await strat.compact(messages, current_tokens=100)
        kept = len(result.modified_messages)

        # The floor is 3 and cannot be crossed: the first user message (the
        # task), the last one (or the request is invalid) and the breadcrumb
        # are all protected. A limit below that is a nonsense config, and
        # Pre-Layer P already logs a warning when it cannot reach it.
        assert kept <= max(max_messages, 3), f"still over the limit: {kept}"
        # 100 fits only the limit of 200 (and is its half); every other limit
        # falls back to half, down to the protected floor of 3.
        assert kept == max(max_messages // 2, 3), (
            f"max_messages={max_messages} kept {kept} messages, not half the "
            f"limit - the inherited mark was not replaced")
        assert ("does not fit" in caplog.text) == (max_messages < 100), (
            "a mark that does not fit the limit must say so")


class TestADeepPruneKeepsTheWorkingPointers:
    """Placeholders go first, but only among the old messages.

    The candidate window was twice the need, at least 20. A deep cut needs most
    of the list, and a small list is shorter than 20, so the window spanned all
    of it, and the cheapest-first order took the newest tool-result pointers —
    the ones the agent is working with — before old real content.
    """

    @staticmethod
    def _strategy(tmp_path, **limits):
        return LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9, keep_system_messages=True, **limits,
            ),
        )

    @staticmethod
    def _ref(k):
        return json.dumps({"type": "tool_result_ref", "ref_id": f"TR_{k}",
                           "summary": f"stored {k}"})

    @classmethod
    def _pairs(cls, count, pointers):
        messages = []
        for k in range(count):
            messages += [
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"c{k}", "type": "function",
                     "function": {"name": "t", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": f"c{k}", "name": "t",
                 "content": cls._ref(k) if k in pointers else f"result {k}"},
            ]
        return messages

    @pytest.mark.asyncio
    async def test_the_newest_placeholders_survive(self, tmp_path):
        strat = self._strategy(tmp_path, max_messages=40, max_messages_prune_to=10)
        messages = ([{"role": "user", "content": "the task"}]
                    + self._pairs(20, pointers={17, 18, 19})
                    + [{"role": "user", "content": "last question"}])

        result = await strat.compact(messages, current_tokens=100)
        kept = [m.get("content") for m in result.modified_messages]

        assert result.messages_pruned > 0, "fixture: nothing was pruned"
        assert len(kept) <= 12, f"the cut did not go deep: {len(kept)} kept"
        assert self._ref(18) in kept, (
            "the prune took a pointer from the newest messages - the window "
            "reached into what stays")
        # Inside the window cheap still goes first: the older pointer leaves,
        # the real result before it stays.
        assert self._ref(17) not in kept and "result 16" in kept, (
            "among the old messages the placeholder no longer goes before real content")

    @pytest.mark.asyncio
    async def test_a_small_limit_keeps_them_too(self, tmp_path):
        """Limit 24, the repo's own prune test agent.

        The need is 15 of 23 candidates: twice that (30) and the floor of 20
        both reach past the 19 before the newer half of what stays.
        """
        strat = self._strategy(tmp_path, max_messages=24)
        messages = ([{"role": "system", "content": "the prompt"},
                     {"role": "user", "content": "the task"}]
                    + self._pairs(12, pointers={9}))

        result = await strat.compact(messages, current_tokens=100)
        kept = [m.get("content") for m in result.modified_messages]

        assert result.messages_pruned > 0, "fixture: nothing was pruned"
        assert self._ref(9) in kept, (
            "the window reached into the newer half of what stays and "
            "took the pointer")
