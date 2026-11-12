"""The semantic index on a tree too large to build in one process.

Measured on 12.11.2026 on the coder tree: 735.841 documents, about 100 minutes
of embedding, and a pass that booked nothing until its very end. No process
lived that long, so the index answered "being built" for good. These tests pin
what changed: a build keeps what it stored, the next process finishes it, a
search answers meanwhile and says so, and nine copies of one file are one
file's documents.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
import weakref
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.file_ops import search
from plugins.file_ops.server import FileOpsServer

pytestmark = pytest.mark.slow

AUTH = '''\
"""Everything about letting people in."""


def verify_credentials(username, password):
    """Check a name and a secret against the user store."""
    return True


class SessionStore:
    """Where a login is remembered."""

    def revoke(self, token):
        """Throw a session away so the next request is a stranger."""
        return None
'''

TEA = '''\
"""Brewing, steeping and pouring."""


def steep(leaves, minutes):
    """Leave the leaves in the water for a while."""
    return leaves
'''


def build_server(tmp_path: Path, tree: Path, name: str = "idx") -> FileOpsServer:
    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(tree)]
    server_config.search = {
        "enable_semantic_search": True,
        "enable_indexing": True,
        "index_on_startup": False,   # the tests drive every pass themselves
        "chroma_db_path": str(tmp_path / "store"),
    }
    return FileOpsServer(name, Mock(spec=AgentSystemConfig), server_config)


async def _no_pass(budget=None):
    """Stands in for FileSearchEngine.freshen where a pass would decide the
    test on timing instead of on what it checks."""
    return None


def booked(engine) -> int:
    return sum(len(ids) for ids in engine.file_symbol_ids.values())


def stored(engine) -> int:
    return engine._vector_store.count(engine._collection_name)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(6):
        (root / f"module_{index}.py").write_text(
            f'def function_{index}_a():\n    """Number {index}, first."""\n\n\n'
            f'def function_{index}_b():\n    """Number {index}, second."""\n',
            encoding="utf-8")
    (root / "auth.py").write_text(AUTH, encoding="utf-8")
    (root / "tea.py").write_text(TEA, encoding="utf-8")
    return root


async def test_a_build_that_dies_is_finished_by_the_next_process(tmp_path, tree, monkeypatch):
    monkeypatch.setattr(search, "EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(search, "_STATE_SAVE_SECONDS", 0.0)
    first = build_server(tmp_path, tree).search_engine
    original = first._add_embedded
    calls = []

    def dies_on_the_third_batch(*args):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("the process ends here")
        return original(*args)

    first._add_embedded = dies_on_the_third_batch
    with pytest.raises(RuntimeError):
        await first.rebuild_index(incremental=False)
    state = json.loads(first._state_path.read_text(encoding="utf-8"))
    await first.stop()

    assert state["complete"] is False
    assert state["files"], "the batches stored before the end were not booked"

    second = build_server(tmp_path, tree)
    engine = second.search_engine
    engine._init_vector_store()
    assert engine.file_mtimes and not engine._index_built
    assert stored(engine) == booked(engine)

    answer = await second.semantic_search({"query": "end a login session"})
    assert answer["status"] == "success"
    assert answer["index_complete"] is False and "still being built" in answer["note"]

    # The search itself set off the pass that carries the build on.
    assert engine._fresh_task is not None, "nothing finishes a half-built index"
    await engine._fresh_task

    assert engine._index_built
    assert set(engine.file_mtimes) == set(tree.iterdir())
    assert stored(engine) == booked(engine)
    assert json.loads(engine._state_path.read_text(encoding="utf-8"))["complete"] is True
    finished = await second.semantic_search({"query": "end a login session"})
    assert "index_complete" not in finished
    await engine.stop()


async def test_identical_files_are_indexed_once_and_named_by_the_hit(tmp_path, tree):
    (tree / "copy" / "deeper").mkdir(parents=True)
    shutil.copy(tree / "auth.py", tree / "copy" / "deeper" / "auth.py")
    server = build_server(tmp_path, tree)
    engine = server.search_engine

    await engine.rebuild_index(incremental=False)

    copy = tree / "copy" / "deeper" / "auth.py"
    assert engine.file_symbol_ids[copy] == [], "the copy got documents of its own"
    assert copy in engine.file_aliases
    assert stored(engine) == booked(engine)
    answer = await server.semantic_search({"query": "throw a login session away",
                                           "max_results": 3})
    top = answer["results"][0]
    assert top["file_path"] == str(tree / "auth.py"), "the shallower file is the original"
    assert top["same_content"] == [str(copy)] and top["same_content_count"] == 1
    await engine.stop()


async def test_when_the_original_goes_a_copy_takes_its_place(tmp_path, tree):
    (tree / "copy").mkdir()
    shutil.copy(tree / "auth.py", tree / "copy" / "auth.py")
    server = build_server(tmp_path, tree)
    engine = server.search_engine
    await engine.rebuild_index(incremental=False)
    documents = stored(engine)

    (tree / "auth.py").unlink()
    await engine.rebuild_index(incremental=True)

    copy = tree / "copy" / "auth.py"
    assert engine.file_symbol_ids[copy], "the copy was left without documents"
    assert copy not in engine.file_aliases
    assert stored(engine) == documents == booked(engine)
    answer = await server.semantic_search({"query": "throw a login session away",
                                           "max_results": 1})
    assert answer["results"][0]["file_path"] == str(copy)
    await engine.stop()


async def test_a_hit_on_a_file_that_is_gone_is_not_reported(tmp_path, tree):
    """Documents outlive their file until the next pass; the answer must not."""
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)
    (tree / "tea.py").unlink()
    # No freshness pass: one would take tea.py out of the store while the
    # query runs, and the test would pass on timing instead of on the check.
    server.search_engine.freshen = _no_pass

    answer = await server.semantic_search({"query": "steep the leaves in water",
                                           "max_results": 10})

    assert str(tree / "tea.py") not in {hit["file_path"] for hit in answer["results"]}
    await server.search_engine.stop()


async def test_a_touched_file_keeps_its_documents(tmp_path, tree):
    """A new mtime with the same content re-embedded the whole file."""
    import os

    server = build_server(tmp_path, tree)
    engine = server.search_engine
    await engine.rebuild_index(incremental=False)
    before = list(engine.file_symbol_ids[tree / "auth.py"])
    os.utime(tree / "auth.py", None)
    embedded = []
    original = engine._add_embedded
    engine._add_embedded = lambda ids, *rest: (embedded.extend(ids), original(ids, *rest))

    await engine.rebuild_index(incremental=True)

    assert embedded == [] and engine.file_symbol_ids[tree / "auth.py"] == before
    await engine.stop()


# --- the review round of 12.11.2026 ------------------------------------------

async def test_a_failed_pass_is_retried_where_it_stopped(tmp_path, tree, monkeypatch):
    """The background indexer asked for a FULL pass until one succeeded: a
    build that died half way was cleared by its own retry a minute later."""
    monkeypatch.setattr(search, "EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(search, "_STATE_SAVE_SECONDS", 0.0)
    monkeypatch.setattr(search, "_RETRY_SECONDS", 0.0)
    engine = build_server(tmp_path, tree).search_engine
    original = engine._add_embedded
    calls, clears = [], []

    def fails_once(*args):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("one bad batch")
        return original(*args)

    real_clear = engine._clear_collection

    async def counting_clear(*args, **kwargs):
        clears.append(1)
        return await real_clear(*args, **kwargs)

    engine._add_embedded = fails_once
    engine._clear_collection = counting_clear
    indexer = asyncio.create_task(engine._background_indexer())
    try:
        for _ in range(600):
            if engine._index_built:
                break
            await asyncio.sleep(0.05)
    finally:
        indexer.cancel()
        await asyncio.gather(indexer, return_exceptions=True)

    assert engine._index_built, "the retry did not finish the build"
    assert clears == [1], "the retry cleared what the first attempt had stored"
    assert stored(engine) == booked(engine)
    await engine.stop()


async def test_a_reader_learns_that_the_build_has_finished(tmp_path, tree, monkeypatch):
    """A reader runs no pass: it read "unfinished" once and said so for good."""
    monkeypatch.setattr(search, "EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(search, "_STATE_SAVE_SECONDS", 0.0)
    owner = build_server(tmp_path, tree).search_engine
    original = owner._add_embedded
    calls = []

    def dies(*args):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("ends here")
        return original(*args)

    owner._add_embedded = dies
    with pytest.raises(RuntimeError):
        await owner.rebuild_index(incremental=False)
    owner._add_embedded = original
    # Another PROCESS: in this one the reader would ask its builder directly.
    monkeypatch.setattr(search, "_BUILDERS", weakref.WeakValueDictionary())

    reader_config = ToolServerConfig(type="file_ops", enabled=True)
    reader_config.allowed_directories = [str(tree)]
    reader_config.search = {"enable_semantic_search": True, "enable_indexing": False,
                            "chroma_db_path": str(tmp_path / "store"),
                            "collection_name": owner._collection_name}
    reader = FileOpsServer("reader", Mock(spec=AgentSystemConfig), reader_config)
    during = await reader.semantic_search({"query": "end a login session"})
    await owner.rebuild_index(incremental=True)
    after = await reader.semantic_search({"query": "end a login session"})

    assert during.get("index_complete") is False
    assert "index_complete" not in after, "the reader still calls a finished index unfinished"
    await reader.search_engine.stop()
    await owner.stop()


async def test_an_unfinished_index_whose_pass_failed_says_why(tmp_path, tree):
    """With files booked the answer is a success, and the failure that keeps
    the build from finishing used to stand only in the log."""
    server = build_server(tmp_path, tree)
    engine = server.search_engine
    await engine.rebuild_index(incremental=False)
    engine._index_built = False
    engine._index_error = "RuntimeError: embedding model not available"
    engine.freshen = _no_pass

    answer = await server.semantic_search({"query": "end a login session"})
    filtered = await server.semantic_search({"query": "end a login session",
                                             "filter_pattern": "*.nothing"})

    assert answer["index_complete"] is False
    assert "embedding model not available" in answer["note"]
    assert filtered["count"] == 0 and filtered["index_complete"] is False, \
        "an empty filtered answer from half an index reads as a verdict"
    await engine.stop()


async def test_a_stop_in_the_middle_of_a_batch_books_nothing_it_did_not_store(
        tmp_path, tree, monkeypatch):
    monkeypatch.setattr(search, "EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(search, "_STATE_SAVE_SECONDS", 0.0)
    engine = build_server(tmp_path, tree).search_engine
    original = engine._add_embedded
    calls = []

    def stop_arrives(*args):
        calls.append(1)
        if len(calls) == 2:
            engine._closing = True   # what stop() sets while this batch runs
        return original(*args)

    engine._add_embedded = stop_arrives
    await engine.rebuild_index(incremental=False)
    engine._closing = False

    # Stored but unbooked is allowed -- the next pass mends it. Booked but not
    # stored is the claim that must never be made.
    present = set(engine._vector_store.list_ids(engine._collection_name))
    assert all(doc_id in present
               for ids in engine.file_symbol_ids.values() for doc_id in ids), \
        "documents were booked that were never stored"
    state = json.loads(engine._state_path.read_text(encoding="utf-8")) \
        if engine._state_path.is_file() else {"complete": False}
    assert state["complete"] is False
    await engine.stop()


async def test_a_state_from_before_hashes_learns_them_without_embedding_again(tmp_path, tree):
    server = build_server(tmp_path, tree)
    engine = server.search_engine
    await engine.rebuild_index(incremental=False)
    state = engine._state_path
    payload = json.loads(state.read_text(encoding="utf-8"))
    for entry in payload["files"].values():
        entry.pop("hash", None)
    state.write_text(json.dumps(payload), encoding="utf-8")
    await engine.stop()

    second = build_server(tmp_path, tree).search_engine
    second._init_vector_store()
    embedded = []
    original = second._add_embedded
    second._add_embedded = lambda ids, *rest: (embedded.extend(ids), original(ids, *rest))
    await second.rebuild_index(incremental=True)

    assert embedded == [], "unchanged files were embedded again"
    assert all(second.file_hashes.get(path) for path in second.file_mtimes), \
        "the hashes were not learned"
    await second.stop()


async def test_a_store_whose_ids_cannot_be_listed_is_not_mended_into_nothing(tmp_path, tree):
    """list_ids answers [] when the store cannot be read: mending from that
    dropped every file, and the coder tree was embedded from scratch again."""
    server = build_server(tmp_path, tree)
    engine = server.search_engine
    await engine.rebuild_index(incremental=False)
    documents = stored(engine)
    payload = json.loads(engine._state_path.read_text(encoding="utf-8"))
    payload["complete"] = False
    engine._state_path.write_text(json.dumps(payload), encoding="utf-8")
    engine._vector_store.list_ids = lambda collection: []

    engine._load_state(mend=True)

    assert set(engine.file_mtimes) == set(tree.iterdir()), "the bookkeeping was dropped"
    assert stored(engine) == documents, "the store was emptied"
    await engine.stop()
