"""What a compaction may not lose, break or misreport.

One test per defect found in the review of 2026-09-13, each reproduced before
it was fixed. The shapes are the ones production produces: ChatMessage dumps,
text_file parts from attachments, Gemini batch ids that repeat across turns.
"""

from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import (
    RETRIEVAL_MARKER,
    CompactionConfig,
    LayeredCompactionStrategy,
)
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.media_store import MediaStore
from plugins.context_engineer.tool_result_store import ToolResultStore

BIG = "a large tool result line with enough words to count " * 60


def _strategy(tmp_path, **config):
    defaults = dict(layer1_threshold=10**9, layer2_threshold=10**9, layer3_threshold=10**9,
                    target_tokens=0, max_messages=0, keep_system_messages=True,
                    tool_result_min_size=100, tool_result_keep_last=1)
    defaults.update(config)
    archival = ArchivalMemory(tmp_path / "archive.db", session_id="t")
    strategy = LayeredCompactionStrategy(
        tool_store=ToolResultStore(tmp_path / "tools.db", session_id="t"),
        core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
        archival_memory=archival,
        config=CompactionConfig(**defaults),
        media_store=MediaStore(tmp_path / "media"),
    )
    return strategy, archival


def _call(call_id, name="read", arguments="{}"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _archived_text(archival) -> str:
    return "\n".join(row[0] for row in archival._db.execute(
        "SELECT content FROM archived_messages").fetchall())


class TestTheArchiveKeepsWhatLeaves:

    def test_attached_files_and_tool_arguments_are_stored(self, tmp_path):
        archival = ArchivalMemory(tmp_path / "archive.db", session_id="t")
        archival.store({"role": "user", "content": [
            {"type": "text", "text": "please review"},
            {"type": "text_file", "name": "secret.py", "content": "SECRET_BODY = 42"},
        ]})
        archival.store_many([{"role": "assistant", "content": None, "tool_calls": [
            _call("c1", "write_file", '{"path": "a.txt", "content": "WRITTEN_BODY"}')]}])

        stored = _archived_text(archival)
        assert "SECRET_BODY = 42" in stored, "the attached file did not reach the archive"
        assert "WRITTEN_BODY" in stored, "the tool call arguments did not reach the archive"

    @pytest.mark.asyncio
    async def test_inline_media_is_saved_before_its_message_leaves(self, tmp_path):
        strategy, archival = _strategy(tmp_path, layer3_threshold=1, drop_after_turns=1)
        payload = base64.b64encode(b"PNGDATA" * 4000).decode()
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "old picture"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": payload}},
            ]},
            {"role": "assistant", "content": "seen"},
            {"role": "user", "content": "new question"},
        ]
        result = await strategy.compact(messages, current_tokens=10_000, force=True)

        assert result.messages_dropped > 0, "fixture: Layer 3 removed nothing"
        stored = _archived_text(archival)
        assert 'read(ref="' in stored, "the archived message names no place to get the image back"
        path = stored.split('read(ref="')[1].split('"')[0]
        assert (tmp_path / "media").exists() and path and open(path, "rb").read() == b"PNGDATA" * 4000


    @pytest.mark.asyncio
    async def test_media_of_messages_the_sequence_fix_removes_is_saved_too(self, tmp_path):
        """The sequence fix deletes leading non-user messages on its own account."""
        from plugins.context_engineer.compaction import CompactionResult

        strategy, archival = _strategy(tmp_path)
        payload = base64.b64encode(b"GIFDATA" * 4000).decode()
        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "the first question"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "here is the chart"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/gif", "data": payload}},
            ]},
            {"role": "user", "content": "next"},
        ]
        result = CompactionResult(original_tokens=0, final_tokens=0, tokens_saved=0,
                                  modified_messages=messages)
        strategy._current_session_id = "t"
        removed = await strategy._archive_then_remove(result, {1}, "test")

        assert removed == 2, "fixture: the sequence fix did not remove the leading assistant"
        stored = _archived_text(archival)
        assert "here is the chart" in stored and 'read(ref="' in stored, (
            "the image of a message the sequence fix removed went nowhere")


class TestCollidingToolCallIds:
    """The Gemini batch client mints call_{name}_{index}, repeated every turn."""

    @pytest.mark.asyncio
    async def test_layer3_never_leaves_a_result_without_its_call(self, tmp_path):
        strategy, _ = _strategy(tmp_path, layer3_threshold=1, drop_after_turns=2)
        messages = [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": None, "tool_calls": [_call("call_read_0")]},
            {"role": "tool", "tool_call_id": "call_read_0", "name": "read", "content": BIG},
            {"role": "user", "content": "turn 2"},
            {"role": "user", "content": "turn 3"},
            {"role": "assistant", "content": None, "tool_calls": [
                _call("call_read_0"), _call("call_write_1", "write")]},
            {"role": "tool", "tool_call_id": "call_read_0", "name": "read", "content": "young"},
            {"role": "tool", "tool_call_id": "call_write_1", "name": "write", "content": "done"},
        ]
        result = await strategy.compact(messages, current_tokens=10_000, force=True)

        kept = result.modified_messages
        issued = {tc["id"] for m in kept if m.get("role") == "assistant"
                  for tc in m.get("tool_calls") or []}
        orphans = [m for m in kept if m.get("role") == "tool" and m["tool_call_id"] not in issued]
        assert not orphans, f"tool results without their call: {orphans}"
        assert any(m.get("content") == "young" for m in kept), "the young turn was dragged out"
        # Layer 3 books no savings itself; they come from the baseline estimate.
        assert result.messages_dropped > 0 and result.tokens_saved > 0

    def test_two_bodies_under_one_id_stay_two(self, tmp_path):
        store = ToolResultStore(tmp_path / "tools.db", session_id="t")
        first = json.loads(store.store_and_reference("call_read_0", "read", "ALPHA " * 100))
        second = json.loads(store.store_and_reference("call_read_0", "read", "BETA " * 100))

        assert first["ref_id"] != second["ref_id"]
        assert store.retrieve(first["ref_id"]).content.startswith("ALPHA")
        assert store.retrieve(second["ref_id"]).content.startswith("BETA")


class TestLayerOneMedia:

    @staticmethod
    def _media_result(call_id, path, content='{"status": "success"}'):
        return {"role": "tool", "tool_call_id": call_id, "name": "load", "content": content,
                "multimodal_content": [{"type": "audio", "path": str(path), "mime_type": "audio/wav"}]}

    @pytest.mark.asyncio
    async def test_the_newest_tool_results_keep_their_media(self, tmp_path):
        strategy, _ = _strategy(tmp_path, layer1_threshold=1)
        old, new = tmp_path / "old.wav", tmp_path / "new.wav"
        old.write_bytes(b"x" * 100_000)
        new.write_bytes(b"x" * 100_000)
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [_call("a"), _call("b")]},
            self._media_result("a", old),
            self._media_result("b", new),
        ]
        result = await strategy.compact(messages, current_tokens=10_000, force=True)

        assert result.modified_messages[2]["multimodal_content"] == [], "the old media stayed"
        assert result.modified_messages[3]["multimodal_content"], (
            "the newest media was evicted before the model could see it")

    @pytest.mark.asyncio
    async def test_a_restored_file_stays_restored(self, tmp_path):
        strategy, _ = _strategy(tmp_path, layer1_threshold=1, tool_result_keep_last=0)
        media = tmp_path / "restored.wav"
        media.write_bytes(b"x" * 100_000)
        answer = json.dumps({RETRIEVAL_MARKER: True, "status": "success"})
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [_call("r")]},
            self._media_result("r", media, content=answer),
        ]
        result = await strategy.compact(messages, current_tokens=10_000, force=True)

        assert result.modified_messages[2]["multimodal_content"], (
            "a file the agent just restored with read() was evicted again")

    @pytest.mark.asyncio
    async def test_storing_the_text_does_not_bring_the_media_back(self, tmp_path):
        strategy, _ = _strategy(tmp_path, layer1_threshold=1, tool_result_keep_last=0)
        media = tmp_path / "shot.wav"
        media.write_bytes(b"x" * 100_000)
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [_call("s")]},
            self._media_result("s", media, content=BIG),
        ]
        result = await strategy.compact(messages, current_tokens=10_000, force=True)

        tool = result.modified_messages[2]
        assert result.tool_results_stored == 1, "fixture: the text was not stored"
        assert tool.get("multimodal_content") == [], (
            "the media counted as evicted is still attached to the request")


class TestTheNumbersTellTheTruth:

    @pytest.mark.asyncio
    async def test_a_layer_that_changed_nothing_saved_nothing(self, tmp_path):
        """current_tokens includes tool definitions the messages never contain."""
        strategy, _ = _strategy(tmp_path, layer1_threshold=1)
        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        result = await strategy.compact(messages, current_tokens=150_000, force=True)

        assert result.layers_applied == [1]
        assert result.tokens_saved == 0, f"reported {result.tokens_saved} tokens saved for no change"
        assert result.final_tokens == result.original_tokens


class TestLayerTwo:

    @pytest.mark.asyncio
    async def test_archives_in_conversation_order_and_leaves_retrieval_answers(self, tmp_path):
        strategy, archival = _strategy(tmp_path, layer2_threshold=1, archive_after_turns=1)
        messages = []
        for i in range(40):
            messages.append({"role": "user", "content": f"question {i:02d}"})
            messages.append({"role": "assistant", "content": f"answer {i:02d}"})
        answer = json.dumps({RETRIEVAL_MARKER: True, "content": "fetched"})
        messages[2:2] = [
            {"role": "assistant", "content": None, "tool_calls": [_call("r")]},
            {"role": "tool", "tool_call_id": "r", "name": "context_engineer_read", "content": answer},
        ]
        messages.append({"role": "user", "content": "now"})
        await strategy.compact(messages, current_tokens=10_000, force=True)

        rows = archival._db.execute(
            "SELECT content FROM archived_messages ORDER BY timestamp").fetchall()
        questions = [r[0] for r in rows if r[0].startswith("question")]
        assert questions == sorted(questions), "the archive order is not the conversation order"
        assert not any("fetched" in r[0] for r in rows), "a retrieval answer went back into the archive"


class TestCoreMemoryEviction:

    @pytest.mark.asyncio
    async def test_a_minor_fact_never_pushes_out_a_major_one(self, tmp_path):
        memory = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=500)
        assert await memory.add_fact("the deploy target is the api1 host", importance=0.95)
        assert await memory.add_fact("the database is books.db on disk", importance=0.95)
        memory.max_tokens = memory._current_tokens  # full

        added = await memory.add_fact("the user likes green buttons a lot", importance=0.05)

        contents = [f.content for f in memory.facts]
        assert "the deploy target is the api1 host" in contents
        assert "the database is books.db on disk" in contents
        assert added is False

    @pytest.mark.asyncio
    async def test_an_oversized_fact_evicts_nothing(self, tmp_path):
        memory = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=60)
        await memory.add_fact("keep me", importance=0.2)

        assert await memory.add_fact("word " * 400, importance=1.0) is False
        assert [f.content for f in memory.facts] == ["keep me"]

    @pytest.mark.asyncio
    async def test_a_more_important_fact_does_make_room(self, tmp_path):
        memory = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=500)
        await memory.add_fact("the user likes green buttons a lot", importance=0.1)
        await memory.add_fact("the database is books.db on disk", importance=0.5)
        memory.max_tokens = memory._current_tokens  # full

        assert await memory.add_fact("the deploy target is the api1 host", importance=0.9)
        assert "the user likes green buttons a lot" not in [f.content for f in memory.facts]

    @pytest.mark.asyncio
    async def test_a_raised_importance_is_saved(self, tmp_path):
        memory = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=500)
        await memory.add_fact("same fact", importance=0.2)
        await memory.add_fact("same fact", importance=0.9)

        reloaded = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=500)
        assert reloaded.facts[0].importance == 0.9

    @pytest.mark.asyncio
    async def test_a_file_over_budget_loads_trimmed_in_eviction_order(self, tmp_path):
        """Saved under a larger budget (or counted by an older estimator), it loaded
        over budget, and the first new fact then evicted a whole batch."""
        memory = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=500)
        for content, importance in [("the deploy target is the api1 host", 0.9),
                                    ("old minor fact about the colours", 0.2),
                                    ("new minor fact about the colours", 0.2),
                                    ("the database is books.db on disk", 0.9)]:
            await memory.add_fact(content, importance=importance)
        budget = memory._current_tokens - memory._fact_tokens(memory.facts[1])

        reloaded = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=budget)

        assert [f.content for f in reloaded.facts] == [
            "the deploy target is the api1 host", "new minor fact about the colours",
            "the database is books.db on disk"]
        assert reloaded._current_tokens <= budget
        again = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=500)
        assert len(again.facts) == 3, "the trim was not saved"

    @pytest.mark.asyncio
    async def test_a_failed_save_of_the_trim_keeps_the_loaded_facts(self, tmp_path, monkeypatch):
        memory = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=500)
        for i in range(4):
            await memory.add_fact(f"fact number {i} about the project", importance=0.5)
        budget = memory._current_tokens - memory._fact_tokens(memory.facts[0])

        def locked(self):
            raise PermissionError("locked by a virus scanner")
        monkeypatch.setattr(CoreMemory, "_save_sync", locked)
        reloaded = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=budget)

        assert len(reloaded.facts) == 3

    @pytest.mark.asyncio
    async def test_the_total_is_the_sum_of_the_facts_and_stays_in_budget(self, tmp_path):
        """Lines were added as prose, the total recounted as the whole section: two scales."""
        memory = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=300)
        for i in range(40):
            await memory.add_fact(f"fact number {i} says something about the project here",
                                  importance=0.5)
            assert memory._current_tokens == sum(memory._fact_tokens(f) for f in memory.facts)
            assert memory._current_tokens <= memory.max_tokens, (
                f"after fact {i}: {memory._current_tokens} of {memory.max_tokens}")
        assert len(memory.facts) > 3, "fixture: nothing was stored"

    @pytest.mark.asyncio
    async def test_one_small_fact_evicts_only_what_it_needs(self, tmp_path):
        """Scored as one text, the section changed type when the last "from "/"class " fact
        left, and a short note evicted 51 facts where 2 were enough."""
        memory = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=5000)
        await memory.add_fact("config is loaded from yaml", importance=0.1)
        await memory.add_fact("the class names use pascal case", importance=0.1)
        i = 0
        while True:
            note = f"plain note number {i} about the project"
            probe = memory.facts[0].__class__(content=note, category="facts", importance=0.5)
            if memory._current_tokens + memory._fact_tokens(probe) > memory.max_tokens:
                break
            await memory.add_fact(note, importance=0.5)
            i += 1
        before = len(memory.facts)

        assert await memory.add_fact("a new short plain note", importance=0.5)
        assert before + 1 - len(memory.facts) <= 2, (
            f"{before + 1 - len(memory.facts)} facts evicted for one short note")

    @pytest.mark.asyncio
    async def test_facts_of_equal_importance_rotate_oldest_first(self, tmp_path):
        """The tool stores at 0.5 by default: a full memory must still take one."""
        memory = CoreMemory(storage_path=tmp_path / "m.json", max_tokens=500)
        await memory.add_fact("the first fact of the run", importance=0.5)
        await memory.add_fact("the second fact of the run", importance=0.5)
        memory.max_tokens = memory._current_tokens  # full

        assert await memory.add_fact("the third fact of the run", importance=0.5)
        assert [f.content for f in memory.facts] == [
            "the second fact of the run", "the third fact of the run"]


class TestHeldRunsRewriteNothing:

    @pytest.mark.asyncio
    async def test_pre_layer_p_waits_too(self, tmp_path):
        strategy, _ = _strategy(tmp_path, max_messages=6, max_messages_headroom=0)
        messages = [{"role": "user", "content": "go"}]
        for i in range(10):
            messages += [{"role": "assistant", "content": f"step {i}"},
                         {"role": "user", "content": f"more {i}"}]

        held = await strategy.compact(list(messages), current_tokens=1_000, force=True,
                                      rewrite_layers=False)
        free = await strategy.compact(list(messages), current_tokens=1_000, force=True)

        assert free.messages_pruned > 0, "fixture: Pre-Layer P had nothing to prune"
        assert held.messages_pruned == 0 and "P" not in held.layers_applied, (
            "a held run pruned messages")


class TestLayerTwoOrder:

    @pytest.mark.asyncio
    async def test_archives_in_conversation_order_when_the_indices_are_sparse(self, tmp_path):
        """A set of small dense indices iterates in order by accident; sparse ones do not."""
        strategy, archival = _strategy(tmp_path, layer2_threshold=1, archive_after_turns=1)
        ref = json.dumps({"type": "archived_ref", "ref_id": "old", "summary": "long gone"})
        messages = [{"role": "assistant", "content": ref} for _ in range(260)]
        real = {7: "question a", 9: "question b", 258: "question c"}
        for i, text in real.items():
            messages[i] = {"role": "user", "content": text}
        messages.append({"role": "user", "content": "now"})
        assert list(set(real)) != sorted(real), "fixture: this set iterates in order"

        await strategy.compact(messages, current_tokens=10_000, force=True)

        rows = archival._db.execute(
            "SELECT content FROM archived_messages ORDER BY timestamp, rowid").fetchall()
        assert [r[0] for r in rows if r[0].startswith("question")] == [
            "question a", "question b", "question c"]


class TestTheArchiveWriteFails:

    @pytest.mark.asyncio
    async def test_the_messages_that_stay_keep_their_media(self, tmp_path, monkeypatch):
        strategy, archival = _strategy(tmp_path, layer3_threshold=1, drop_after_turns=1)
        payload = base64.b64encode(b"PNGDATA" * 4000).decode()
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": payload}}
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "old picture"}, dict(image)]},
            {"role": "assistant", "content": "seen"},
            {"role": "user", "content": "new question"},
        ]

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(archival, "store_many", locked)
        result = await strategy.compact(messages, current_tokens=10_000, force=True)

        assert result.messages_dropped == 0, "fixture: the failed write did not stop the removal"
        assert result.modified_messages[0]["content"][1] == image, (
            "the image of a message that stayed in the conversation was evicted")


class TestTheByteLimit:

    @staticmethod
    def _media_result(call_id, path, kind):
        return {"role": "tool", "tool_call_id": call_id, "name": "load", "content": "ok",
                "multimodal_content": [{"type": kind, "path": str(path), "mime_type": f"{kind}/x"}]}

    @pytest.mark.asyncio
    async def test_the_newest_media_goes_when_nothing_else_can_make_room(self, tmp_path):
        strategy, _ = _strategy(tmp_path, max_request_bytes=2_000_000,
                                target_request_bytes=1_000_000)
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x" * 3_000_000)
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [_call("v")]},
            self._media_result("v", clip, "video"),
        ]
        result = await strategy.compact(messages, current_tokens=1_000)

        assert result.modified_messages[2]["multimodal_content"] == [], (
            "every request goes out over the byte limit")

    @pytest.mark.asyncio
    async def test_the_newest_media_stays_when_the_byte_pass_made_room(self, tmp_path):
        strategy, _ = _strategy(tmp_path, max_request_bytes=2_000_000,
                                target_request_bytes=1_000_000)
        clip, shot = tmp_path / "clip.mp4", tmp_path / "shot.png"
        clip.write_bytes(b"x" * 3_000_000)
        shot.write_bytes(b"x" * 50_000)
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [_call("v")]},
            self._media_result("v", clip, "video"),
            {"role": "assistant", "content": None, "tool_calls": [_call("s")]},
            self._media_result("s", shot, "image"),
        ]
        result = await strategy.compact(messages, current_tokens=1_000)

        assert result.modified_messages[2]["multimodal_content"] == [], (
            "fixture: the byte pass did not take the old clip")
        assert result.modified_messages[4]["multimodal_content"], (
            "the image the agent just loaded was evicted although the request was already small")


class TestLayerThreeCutsDeep:
    """docs/prompt_cache_design.md par. 3.5: rarely and deep, down to the target."""

    @pytest.mark.asyncio
    async def test_an_agent_run_is_cut_down_to_target(self, tmp_path):
        # A target below what the working tail alone takes: the tail must hold anyway.
        strategy, archival = _strategy(tmp_path, layer3_threshold=1, target_tokens=1_000,
                                       tool_result_keep_last=3)
        messages = [{"role": "system", "content": "prompt"},
                    {"role": "user", "content": "write the chapter"}]
        for i in range(40):
            messages += [
                {"role": "assistant", "content": f"draft {i} " + "prose " * 600,
                 "tool_calls": [_call(f"s{i}", "save")]},
                {"role": "tool", "tool_call_id": f"s{i}", "name": "save", "content": "saved"},
            ]
        before = strategy._estimate_messages_tokens(messages)
        result = await strategy.compact(messages, current_tokens=before, force=True)

        kept = result.modified_messages
        text = " ".join(str(m.get("content")) for m in kept)
        assert before > 20_000, "fixture: nothing to cut"
        assert strategy._estimate_messages_tokens(kept) <= 5_500, (
            "one user turn: Layer 3 left the run far above its target")
        assert "write the chapter" in text, "the task left the conversation"
        assert all(f"draft {i} " in text for i in (37, 38, 39)), "the working tail was cut"
        assert "draft 0 " in _archived_text(archival), "what left was not archived"
        issued = {tc["id"] for m in kept if m.get("role") == "assistant"
                  for tc in m.get("tool_calls") or []}
        assert all(m["tool_call_id"] in issued for m in kept if m.get("role") == "tool")

    @pytest.mark.asyncio
    async def test_a_chat_is_cut_down_to_target_not_by_age(self, tmp_path):
        strategy, _ = _strategy(tmp_path, layer3_threshold=1, target_tokens=5_000,
                                drop_after_turns=30)
        messages = [{"role": "system", "content": "prompt"}]
        for t in range(40):
            messages += [{"role": "user", "content": f"question {t}"},
                         {"role": "assistant", "content": f"answer {t} " + "text " * 400}]
        result = await strategy.compact(messages, current_tokens=100_000, force=True)

        kept = result.modified_messages
        text = " ".join(str(m.get("content")) for m in kept)
        assert strategy._estimate_messages_tokens(kept) <= 5_500, (
            "only the turns older than drop_after_turns left: the next run cuts again")
        assert "question 39" in text and "answer 39 " in text, "the current turn was cut"

    @staticmethod
    def _chat(turns, words=100):
        messages = [{"role": "system", "content": "prompt"}]
        for t in range(turns):
            messages += [{"role": "user", "content": f"question {t}"},
                         {"role": "assistant", "content": f"answer {t} " + "text " * words}]
        return messages

    @pytest.mark.asyncio
    async def test_an_uploaded_image_does_not_empty_the_chat(self, tmp_path):
        """Counted at 0.25 tokens per base64 character it weighed 256k tokens."""
        strategy, _ = _strategy(tmp_path, layer3_threshold=1, target_tokens=20_000,
                                drop_after_turns=100)
        messages = self._chat(40)
        payload = base64.b64encode(b"x" * 750_000).decode()
        messages.append({"role": "user", "content": [
            {"type": "text", "text": "what is on this screenshot?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}}]})
        assert strategy._estimate_messages_tokens(messages) < 20_000, (
            "a 750 KB image is estimated by its base64 length, not as the picture it is")

        result = await strategy.compact(messages, current_tokens=300_000, force=True)

        assert 3 in result.layers_applied, "fixture: Layer 3 did not run"
        assert result.messages_dropped == 0, "one image took a chat far below target with it"

    @pytest.mark.asyncio
    async def test_the_threshold_is_read_on_the_callers_scale(self, tmp_path):
        """current_tokens carries tool definitions and the provider's own count."""
        strategy, _ = _strategy(tmp_path, layer1_threshold=1, target_tokens=2_000,
                                drop_after_turns=100)
        messages = self._chat(40)
        estimate = strategy._estimate_messages_tokens(messages)
        strategy.config.layer3_threshold = estimate + 1_000

        result = await strategy.compact(messages, current_tokens=estimate + 5_000, force=True)

        assert 3 in result.layers_applied and result.messages_dropped > 0, (
            "the caller measured above Layer 3, the messages alone below — and Layer 3 waited")

    @pytest.mark.asyncio
    async def test_media_removed_before_the_baseline_is_not_part_of_the_offset(self, tmp_path):
        """The media passes book their savings before Layer 1 takes its estimate."""
        import copy as copy_module

        def conversation():
            messages = self._chat(20)
            old = base64.b64encode(b"x" * 3_000_000).decode()   # 9 tiles, ~2,300 tokens
            new = base64.b64encode(b"y" * 5_000).decode()
            messages.insert(1, {"role": "user", "content": [
                {"type": "text", "text": "an old picture"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{old}"}}]})
            messages.append({"role": "user", "content": [
                {"type": "text", "text": "a new picture"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{new}"}}]})
            return messages

        strategy, _ = _strategy(tmp_path, always_compact_media_keep_last=1, layer1_threshold=1,
                                target_tokens=1_000, drop_after_turns=100)
        start = strategy._estimate_messages_tokens(conversation())
        tools_gap = 1_000
        dry = await strategy.compact(conversation(), current_tokens=start + tools_gap, force=True)
        assert dry.media_always_compacted == 1, "fixture: the old picture was not evicted"
        after_media = strategy._estimate_messages_tokens(dry.modified_messages)
        assert start - after_media > tools_gap // 2, "fixture: the evicted picture is too cheap to matter"

        # Between the real gap and the gap plus the evicted picture.
        strategy.config.layer3_threshold = after_media + tools_gap + (start - after_media) // 2
        above = await strategy.compact(conversation(), current_tokens=start + tools_gap, force=True)
        # Below the real gap: the tool definitions alone reach it.
        strategy.config.layer3_threshold = after_media + tools_gap // 2
        below = await strategy.compact(conversation(), current_tokens=start + tools_gap, force=True)

        assert 3 not in above.layers_applied, (
            "the picture the media pass had removed counted as tool definitions, and Layer 3 ran")
        assert 3 in below.layers_applied, (
            "the media savings and the estimate disagree in scale and ate the real gap")

    @pytest.mark.asyncio
    async def test_after_pre_layer_p_layer_one_reads_the_callers_scale(self, tmp_path):
        def conversation():
            messages = [{"role": "user", "content": "go"}]
            for i in range(15):
                messages += [
                    {"role": "assistant", "content": None, "tool_calls": [_call(f"p{i}")]},
                    {"role": "tool", "tool_call_id": f"p{i}", "name": "read", "content": BIG},
                ]
            return messages

        strategy, _ = _strategy(tmp_path, max_messages=20, max_messages_headroom=0,
                                tool_result_min_size=10)
        tools_gap = 5_000
        start = strategy._estimate_messages_tokens(conversation())
        dry = await strategy.compact(conversation(), current_tokens=start + tools_gap, force=True)
        assert dry.messages_pruned > 0 and 1 not in dry.layers_applied, "fixture: P did not prune"
        after_prune = strategy._estimate_messages_tokens(dry.modified_messages)

        strategy.config.layer1_threshold = after_prune + tools_gap // 2
        result = await strategy.compact(conversation(), current_tokens=start + tools_gap, force=True)

        assert 1 in result.layers_applied, (
            "after the prune Layer 1 compared the messages alone against its threshold")

    @pytest.mark.asyncio
    async def test_the_task_of_a_continued_session_stays(self, tmp_path):
        strategy, _ = _strategy(tmp_path, layer3_threshold=1, target_tokens=1_000,
                                drop_after_turns=100)
        messages = [{"role": "system", "content": "prompt"},
                    {"role": "user", "content": "the task everything refers to"}]
        for t in range(10):
            messages += [{"role": "assistant", "content": f"answer {t} " + "text " * 400},
                         {"role": "user", "content": f"go on {t}"}]
        result = await strategy.compact(messages, current_tokens=100_000, force=True)

        text = " ".join(str(m.get("content")) for m in result.modified_messages)
        assert result.messages_dropped > 0, "fixture: nothing was cut"
        assert "the task everything refers to" in text, "the first user message was cut"


class TestMediaDedup:

    @staticmethod
    def _twice(image):
        def result(call_id):
            return {"role": "tool", "tool_call_id": call_id, "name": "load", "content": "ok",
                    "multimodal_content": [{"type": "image", "path": str(image),
                                            "mime_type": "image/png"}]}
        return [
            {"role": "user", "content": "compare"},
            {"role": "assistant", "content": None, "tool_calls": [_call("a")]},
            result("a"),
            {"role": "assistant", "content": None, "tool_calls": [_call("b")]},
            result("b"),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("always_media", [0, 4])
    async def test_the_newest_copy_stays(self, tmp_path, always_media):
        """Keeping the older copy lost the image: Layer 1 takes older media first."""
        strategy, _ = _strategy(tmp_path, always_compact_media_keep_last=always_media,
                                deduplicate_media=True)
        image = tmp_path / "a.png"
        image.write_bytes(b"x" * 20_000)
        result = await strategy.compact(self._twice(image), current_tokens=1_000, force=True)

        out = result.modified_messages
        assert result.media_deduplicated == 1, "fixture: no duplicate found"
        assert out[2]["multimodal_content"] == []
        assert out[4]["multimodal_content"], "the newest copy was evicted"
