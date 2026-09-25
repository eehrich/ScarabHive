"""Layer 2 archives its pass as one batch, not one row at a time.

Row by row, every store() committed on its own. When a later one failed, the
rows before it were already in the archive, the compaction failed as a whole,
and the messages stayed in the conversation -- so the next compaction archived
them AGAIN. Each retry left a second copy in the archive and a second hit in
every search. One transaction makes the failure clean: nothing or everything.

A pass past the embedding chunk also goes the way Pre-Layer P's does: rows now,
embedding in the background, instead of seconds of embedding inside the hook.
"""

from __future__ import annotations

import json

import pytest

from plugins.context_engineer import compaction as compaction_mod
from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import CompactionConfig, LayeredCompactionStrategy
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.tool_result_store import ToolResultStore


def _strategy(tmp_path):
    archival = ArchivalMemory(tmp_path / "archive.db", session_id="t")
    strategy = LayeredCompactionStrategy(
        tool_store=ToolResultStore(tmp_path / "tools.db", session_id="t"),
        core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
        archival_memory=archival,
        config=CompactionConfig(layer1_threshold=10**9, layer2_threshold=1, layer3_threshold=10**9,
                                target_tokens=10**7, max_messages=0, deduplicate_media=False,
                                archive_after_turns=1),
    )
    return strategy, archival


def _conversation(turns: int) -> list[dict]:
    """`turns` old user/assistant pairs, then the current request."""
    messages = [{"role": "system", "content": "You are an agent."}]
    for t in range(turns):
        messages.append({"role": "user", "content": f"Frage {t}"})
        messages.append({"role": "assistant", "content": f"Antwort {t}"})
    messages.append({"role": "user", "content": "Die aktuelle Frage"})
    return messages


def _rows(archival) -> int:
    return archival._db.execute("SELECT COUNT(*) FROM archived_messages").fetchone()[0]


def _archived(message: dict) -> bool:
    try:
        return json.loads(message.get("content") or "").get("type") == "archived_ref"
    except (TypeError, ValueError, AttributeError):
        return False


@pytest.mark.asyncio
async def test_a_failure_mid_pass_leaves_no_rows_behind(tmp_path, monkeypatch):
    """The fifth summary call fails. Row by row -- store() and the placeholder
    each asked for a summary -- that was during the third message, with two
    rows committed before it that the next compaction would archive again."""
    strategy, archival = _strategy(tmp_path)
    original = archival._generate_summary
    seen = []

    def fails_on_the_fifth(message):
        seen.append(message)
        if len(seen) == 5:
            raise RuntimeError("disk full")
        return original(message)

    monkeypatch.setattr(archival, "_generate_summary", fails_on_the_fifth)

    with pytest.raises(RuntimeError, match="disk full"):
        await strategy.compact(_conversation(6), current_tokens=10, force=True)

    assert len(seen) >= 5, "fixture: the pass must reach the failing message"
    assert _rows(archival) == 0, (
        f"{_rows(archival)} rows committed before the failure -- the next compaction "
        f"archives those messages again")


def test_a_failed_write_leaves_nothing_pending_for_the_next_commit(tmp_path):
    """The test above fails while the rows are BUILT, before anything is
    written. This one fails the write itself, after the first insert: sqlite3
    opens a transaction implicitly and never closes it on an error, so without
    a rollback those rows stay pending on the shared connection -- and the next
    commit, which is the retry archiving the same messages, saves them twice."""
    import sqlite3

    _, archival = _strategy(tmp_path)
    real = archival._db

    class FailsOnTheSecondInsert:
        def __init__(self):
            self.inserts = 0

        def execute(self, *args):
            self.inserts += 1
            if self.inserts == 2:
                raise sqlite3.OperationalError("database or disk is full")
            return real.execute(*args)

        def __getattr__(self, name):
            return getattr(real, name)

    archival._db = FailsOnTheSecondInsert()
    with pytest.raises(sqlite3.OperationalError):
        archival.store_many([{"role": "user", "content": f"Nachricht {i}"} for i in range(4)])
    archival._db = real

    archival.store_many([{"role": "user", "content": "die naechste, gelungene"}])

    assert _rows(archival) == 1, (
        f"{_rows(archival)} rows: the failed batch's first insert was saved by "
        f"the next commit")


def test_a_single_failed_write_leaves_nothing_pending_either(tmp_path):
    """store() writes one row in two statements and had no rollback either:
    the same pending row, saved by the next commit."""
    import sqlite3

    _, archival = _strategy(tmp_path)
    real = archival._db

    class FailsOnTheSecondStatement:
        def __init__(self):
            self.statements = 0

        def execute(self, *args):
            self.statements += 1
            if self.statements == 2:
                raise sqlite3.OperationalError("database or disk is full")
            return real.execute(*args)

        def __getattr__(self, name):
            return getattr(real, name)

    archival._db = FailsOnTheSecondStatement()
    with pytest.raises(sqlite3.OperationalError):
        archival.store({"role": "user", "content": "die gescheiterte"})
    archival._db = real

    archival.store({"role": "user", "content": "die naechste, gelungene"})

    assert _rows(archival) == 1, f"{_rows(archival)} rows: the failed row was saved later"


@pytest.mark.parametrize("write", ["store", "store_many"])
def test_a_closed_archive_reports_its_own_error_not_the_rollbacks(tmp_path, write):
    """Closed before the call -- the lock makes before and during the same --
    the connection is None. Rolling that back raised a second error over the
    first, and the log then blamed the rollback. Both write paths have it."""
    _, archival = _strategy(tmp_path)
    archival.close()
    message = {"role": "user", "content": "zu spaet"}

    with pytest.raises(AttributeError) as raised:
        if write == "store":
            archival.store(message)
        else:
            archival.store_many([message])

    assert "rollback" not in str(raised.value), str(raised.value)


@pytest.mark.asyncio
async def test_each_placeholder_points_at_its_own_row(tmp_path):
    """The ids come back from one call now, and they are paired with the
    messages by position. An off-by-one there would label every placeholder
    with its neighbour's content -- the read tool would return the wrong turn."""
    strategy, archival = _strategy(tmp_path)

    result = await strategy.compact(_conversation(6), current_tokens=10, force=True)

    placeholders = [m for m in result.modified_messages if _archived(m)]
    assert len(placeholders) >= 6, "fixture: the old turns must be archived"
    for message in placeholders:
        ref = json.loads(message["content"])["ref_id"]
        stored = archival.get(ref)
        assert stored is not None and stored.role == message["role"], ref
        # The placeholder's summary is built from the original message; the
        # row with that id must hold the very same text.
        # (Whole content, not a token of it: "Frage 1" and "Antwort 1" share the 1.)
        assert stored.content in json.loads(message["content"])["summary"], ref


@pytest.mark.asyncio
async def test_a_pass_past_the_chunk_embeds_in_the_background(tmp_path, monkeypatch):
    strategy, archival = _strategy(tmp_path)
    archival.enable_semantic_search = True
    monkeypatch.setattr(compaction_mod, "_SEMANTIC_INDEX_MAX_BATCH", 3)
    started = []
    monkeypatch.setattr(strategy, "_index_in_background",
                        lambda ids, docs, metas, caller: started.append((len(ids), caller)))

    result = await strategy.compact(_conversation(6), current_tokens=10, force=True)

    archived = sum(1 for m in result.modified_messages if _archived(m))
    assert started == [(archived, "Layer 2")], started
    assert _rows(archival) == archived


@pytest.mark.asyncio
async def test_a_pass_lost_after_its_write_leaves_no_second_copy(tmp_path, monkeypatch):
    """The hook's timeout cancels the compaction while the worker thread is
    still writing. The thread commits anyway, the placeholders are lost with the
    pass, the messages stay -- and the next pass archives them again. With a
    random id per row that was a second copy of each."""
    import asyncio
    import threading

    strategy, archival = _strategy(tmp_path)
    real = archival.store_many
    release, written = threading.Event(), threading.Event()

    def slow_store_many(*args):
        release.wait(5)
        try:
            return real(*args)
        finally:
            written.set()

    monkeypatch.setattr(archival, "store_many", slow_store_many)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            strategy.compact(_conversation(6), current_tokens=10, force=True), timeout=0.2)
    release.set()
    assert await asyncio.to_thread(written.wait, 5), "fixture: the lost write never ran"
    lost = _rows(archival)
    assert lost, "fixture: the lost pass must have committed its rows"

    monkeypatch.setattr(archival, "store_many", real)
    result = await strategy.compact(_conversation(6), current_tokens=10, force=True)

    refs = [json.loads(m["content"])["ref_id"]
            for m in result.modified_messages if _archived(m)]
    assert len(refs) == lost, "fixture: the retry must archive the same messages"
    assert _rows(archival) == lost, (
        f"{_rows(archival)} rows after the retry, {lost} before it: every message "
        f"of the lost pass is in the archive twice")
    assert all(archival.get(ref) is not None for ref in refs)


@pytest.mark.parametrize("write", ["store", "store_many"])
def test_archiving_the_same_message_again_adds_nothing(tmp_path, write):
    """Nor to the text index: an FTS entry per write would find the row twice."""
    _, archival = _strategy(tmp_path)
    message = {"role": "user", "content": "Kennung QS-4711"}

    if write == "store":
        first, again = archival.store(message), archival.store(message)
    else:
        (first,), (again,) = archival.store_many([message]), archival.store_many([message])

    assert first == again
    assert _rows(archival) == 1
    assert archival._db.execute(
        "SELECT COUNT(*) FROM archived_fts WHERE archived_fts MATCH '\"4711\"'"
    ).fetchone()[0] == 1


def test_a_repeated_batch_hands_back_every_row_to_embed(tmp_path):
    """A lost pass past the embedding cap wrote its rows and never scheduled
    their embedding. The retry is the only chance to, so the rows that were
    already there come back for the index too, not only the new ones."""
    _, archival = _strategy(tmp_path)
    messages = [{"role": "user", "content": f"Nachricht {i}"} for i in range(3)]
    ids, _, _ = archival.store_many_unindexed(messages)

    again, documents, metadatas = archival.store_many_unindexed(messages)

    assert again == ids and len(documents) == len(metadatas) == 3


def test_a_message_keeps_its_entry_after_its_reasoning_was_stripped(tmp_path):
    """A model switch strips reasoning_details from the whole session before
    the hooks run. Hashed along, the retry of a lost pass came back under new
    ids -- and wrote the message a second time."""
    _, archival = _strategy(tmp_path)
    answered = {"role": "assistant", "content": "Die Antwort",
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "x"}],
                "served_by": "anthropic"}
    stripped = {"role": "assistant", "content": "Die Antwort",
                "reasoning_content": "gekuerzt", "rd_orphaned": True}

    assert archival.store_many([answered]) == archival.store_many([stripped])
    assert _rows(archival) == 1


def test_identical_messages_of_one_batch_keep_their_own_rows(tmp_path):
    """Two "ok" in one pass are two turns -- and the vector store refuses a
    batch that names one id twice."""
    _, archival = _strategy(tmp_path)
    message = {"role": "tool", "content": "ok"}

    ids = archival.store_many([message, dict(message)])

    assert len(set(ids)) == 2 and _rows(archival) == 2
