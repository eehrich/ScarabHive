"""The semantic index end to end: what it holds, what it answers, what it keeps.

Written on 18.09.2026, when the index changed from one document per file to
one per symbol. Every test here covers a failure that is silent in production:
an index that answers about the wrong tree, a first call that blocks for
minutes, an empty index answering "nothing found", a restart that pays for the
whole tree again, and a stale hit for code that was deleted.

These run against a real VectorStore on a tmp path -- the embedding model is
loaded once per session and the trees are a handful of files.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
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


def discard(leaves):
    """Throw the spent leaves into the compost."""
    return None
'''


def build_server(tmp_path: Path, tree: Path, name: str = "idx", **search):
    system_config = Mock(spec=AgentSystemConfig)
    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(tree)]
    server_config.search = {
        "enable_semantic_search": True,
        "enable_indexing": True,
        # The background indexer must not start by itself here: these tests
        # drive the build so they can assert on what a given pass did.
        "index_on_startup": False,
        "chroma_db_path": str(tmp_path / "store"),
        **search,
    }
    return FileOpsServer(name, system_config, server_config)


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "tree"
    src.mkdir()
    (src / "auth.py").write_text(AUTH, encoding="utf-8")
    (src / "tea.py").write_text(TEA, encoding="utf-8")
    (src / "README.md").write_text("# Tea\n\n## Brewing\nWater and leaves.\n",
                                   encoding="utf-8")
    return src


async def test_a_hit_is_a_symbol_with_its_line(tmp_path, tree):
    """The whole point of the rebuild: a file path alone makes the caller read
    the file to find out why it won. The model reads 256 tokens, so a
    file-sized document also embedded the module head and nothing else."""
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)

    result = await server.semantic_search({"query": "end a login session",
                                           "max_results": 5})
    assert result["status"] == "success"
    top = result["results"][0]
    assert top["filename"] == "auth.py"
    assert top["symbol"] == "def SessionStore.revoke(self, token)"
    assert top["line"] == AUTH.splitlines().index("    def revoke(self, token):") + 1
    await server.search_engine.stop()


async def test_a_document_names_its_file_relative_to_the_indexed_directory(
        tmp_path, tree, monkeypatch):
    """The absolute path put the same prefix in front of every document --
    home directory, checkout, temp dir -- and the ranking came to depend on
    where the tree lies: under the macOS temp path the test above ranked the
    class first."""
    from plugins.file_ops import search, symbols

    named = []
    real_documents = symbols.documents

    def recording(path, text):
        named.append(path)
        return real_documents(path, text)

    monkeypatch.setattr(search.symbols, "documents", recording)
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)
    await server.search_engine.stop()

    assert sorted(map(str, named)) == ["README.md", "auth.py", "tea.py"]


async def test_the_first_search_is_what_starts_the_build(tmp_path, tree):
    """The only line in the whole plugin that ever starts an index.

    Nothing else calls it -- not the constructor, not a hook -- so if it goes,
    no index is ever built and every search answers IndexNotReady forever.
    Every other test here drives rebuild_index by hand and would not notice.
    """
    server = build_server(tmp_path, tree, index_on_startup=True)
    assert server.search_engine._indexing_task is None

    await server.semantic_search({"query": "anything"})

    assert server.search_engine._indexing_task is not None, \
        "the search did not start the background build"
    await server.search_engine.stop()


async def test_a_reader_starts_nothing(tmp_path, tree):
    """An instance that only reads a shared index must not build one -- that
    is what keeps two instances off the same collection."""
    reader = build_server(tmp_path, tree, index_on_startup=True,
                          enable_indexing=False)

    await reader.semantic_search({"query": "anything"})

    assert reader.search_engine._indexing_task is None
    await reader.search_engine.stop()


async def test_a_filter_matches_on_the_file_name_too(tmp_path, tree):
    """The stored path is absolute, so "test_*.py" -- the obvious way to ask
    for tests -- used to match nothing, and the tool answered with an empty
    result instead of saying that the pattern was the problem."""
    (tree / "test_auth.py").write_text(AUTH, encoding="utf-8")
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)

    result = await server.semantic_search({"query": "end a login session",
                                           "max_results": 10,
                                           "filter_pattern": "test_*.py"})

    assert result["results"], "a name pattern found nothing"
    assert all(Path(m["file_path"]).name.startswith("test_")
               for m in result["results"])

    # Counter-check: the filter must still exclude, or the assertion above
    # would pass for a filter that does nothing at all.
    other = await server.semantic_search({"query": "end a login session",
                                          "max_results": 10,
                                          "filter_pattern": "*.md"})
    assert all(m["file_path"].endswith(".md") for m in other["results"])
    await server.search_engine.stop()


async def test_a_filter_selects_from_more_than_the_top_hits(tmp_path, tree):
    """Seen live on 18.09.2026: max_results 2 with "*.py" answered count 0,
    because the two nearest symbols were YAML and the filter then had nothing
    left -- and the agent read that as "this code does not exist".

    The filter selects from what came back, so the search has to look deeper
    when one is set.
    """
    for i in range(12):
        (tree / f"note_{i}.md").write_text(
            f"# Session {i}\n\nHow a login session is ended and revoked.\n",
            encoding="utf-8")
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)

    result = await server.semantic_search({"query": "end a login session",
                                           "max_results": 2,
                                           "filter_pattern": "*.py"})

    assert result["results"], result.get("message")
    assert len(result["results"]) <= 2, "more than was asked for"
    assert all(m["file_path"].endswith(".py") for m in result["results"])
    await server.search_engine.stop()


async def test_a_filter_that_matches_nothing_says_that_and_not_nothing(
    tmp_path, tree
):
    """Two different answers: "nothing matched your filter" is about the
    filter, "no results" reads as a verdict about the code."""
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)

    result = await server.semantic_search({"query": "end a login session",
                                           "filter_pattern": "*.rs"})

    assert result["status"] == "success" and result["count"] == 0
    assert "*.rs" in result["message"] and "without the filter" in result["message"]
    await server.search_engine.stop()


async def test_an_index_that_is_still_building_says_so(tmp_path, tree):
    """An empty index answering "no files found" reads as a verdict about the
    code. It is a verdict about the clock."""
    server = build_server(tmp_path, tree)

    result = await server.semantic_search({"query": "anything"})

    assert result["status"] == "error"
    assert result["error_type"] == "IndexNotReady"
    assert "grep_search" in result["error"]
    await server.search_engine.stop()


async def test_a_store_with_documents_is_not_the_same_as_a_finished_index(
    tmp_path, tree
):
    """`count() > 0` is not readiness, and that was the whole failure mode.

    A full build writes in batches of 2000, so on the coder tree the store is
    non-empty about 30 s in while 96 % of it is still missing -- and every
    search in those minutes answered confidently out of whatever happened to
    be walked first. Same shape here: the documents are there, this instance
    has no idea what they cover, and it must say so rather than answer.
    """
    first = build_server(tmp_path, tree)
    await first.search_engine.rebuild_index(incremental=False)
    state = first.search_engine._state_path
    documents = first.search_engine._vector_store.count(
        first.search_engine._collection_name)
    await first.search_engine.stop()
    state.unlink()  # the store survives, the knowledge of its coverage does not

    second = build_server(tmp_path, tree)
    result = await second.semantic_search({"query": "end a login session"})

    assert documents > 0, "the store must be non-empty, or this proves nothing"
    assert result["status"] == "error" and result["error_type"] == "IndexNotReady"
    await second.search_engine.stop()


async def test_the_first_search_does_not_build_the_index_itself(tmp_path, tree):
    """It used to, and on the coder tree that was a four-minute tool call.

    The build belongs to the background indexer; the search returns at once.
    """
    server = build_server(tmp_path, tree)

    await server.semantic_search({"query": "anything"})

    assert server.search_engine.file_mtimes == {}, \
        "the search walked the tree -- that is the wait this fix removed"
    await server.search_engine.stop()


async def test_a_restart_reads_the_index_it_already_has(tmp_path, tree):
    """Without the state file the vectors survived a restart but the knowledge
    of what they cover did not, so the first search rebuilt everything."""
    first = build_server(tmp_path, tree)
    await first.search_engine.rebuild_index(incremental=False)
    documents = first.search_engine._vector_store.count(
        first.search_engine._collection_name)
    await first.search_engine.stop()

    second = build_server(tmp_path, tree)
    second.search_engine._init_vector_store()

    assert len(second.search_engine.file_mtimes) == 3
    assert sum(len(ids) for ids in second.search_engine.file_symbol_ids.values()) \
        == documents
    await second.search_engine.stop()


async def test_a_warm_index_is_not_wiped_by_the_first_background_pass(
    tmp_path, tree
):
    """A full rebuild CLEARS the collection before it refills it.

    The background indexer used to start with one unconditionally, so every
    restart threw away the index that had just been read back from disk and
    answered nothing for the minutes it took to build the same vectors again.
    Measured here the same way it hurts: what the pass asks for, and whether
    the documents are still there afterwards.
    """
    first = build_server(tmp_path, tree)
    await first.search_engine.rebuild_index(incremental=False)
    documents = first.search_engine._vector_store.count(
        first.search_engine._collection_name)
    await first.search_engine.stop()

    second = build_server(tmp_path, tree)
    passes = []
    original = second.search_engine.rebuild_index

    async def spy(*args, **kwargs):
        # Positional too: rebuild_index(None, False) would otherwise read as
        # the default, and the test would pass on a full wipe.
        passes.append(args[1] if len(args) > 1 else kwargs.get("incremental", True))
        return await original(*args, **kwargs)

    second.search_engine.rebuild_index = spy
    task = asyncio.create_task(second.search_engine._background_indexer())
    for _ in range(100):
        if passes:
            break
        await asyncio.sleep(0.05)
    task.cancel()

    assert passes[:1] == [True], \
        "the first pass on a warm index must be incremental, not a full wipe"
    assert second.search_engine._vector_store.count(
        second.search_engine._collection_name) == documents
    await second.search_engine.stop()


async def test_a_search_does_not_wait_for_the_freshness_pass(tmp_path, tree):
    """The pass that picks up changed files stats every file in the tree.

    Measured live on 18.09.2026: 8.3 s over the coder tree with nothing
    changed. Awaited inside the search, that turned a 0.05 s query into a
    ten-second tool call whenever it fell outside the 30 s window.
    """
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)
    server.search_engine._last_incremental_update = 0  # force it to be due

    # Finite, so an awaited pass fails this test on its assertion instead of
    # hanging until the suite's timeout -- a hung test reads as broken
    # infrastructure, not as a finding.
    async def crawl(*args, **kwargs):
        await asyncio.sleep(3)

    server.search_engine.rebuild_index = crawl
    started = asyncio.get_running_loop().time()
    result = await server.semantic_search({"query": "end a login session"})
    waited = asyncio.get_running_loop().time() - started

    assert result["status"] == "success" and result["results"]
    assert waited < 1.0, f"the search waited {waited:.1f}s for the index pass"
    await server.search_engine.stop()


async def test_two_index_passes_never_run_at_the_same_time(tmp_path, tree):
    """Seen live on 18.09.2026: after a restart the background indexer and the
    first search's freshness check each started a pass, and both walked the
    same 3.648 files at once. A pass deletes a changed file's old documents and
    writes its new ones, so two of them interleaved can delete what the other
    just wrote -- and the file is gone from the index until it changes again.
    """
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)

    running = []
    overlapped = []
    original = server.search_engine._run_index_pass

    async def spy(*args, **kwargs):
        overlapped.append(bool(running))
        running.append(1)
        try:
            await asyncio.sleep(0.05)  # long enough for a second pass to start
            return await original(*args, **kwargs)
        finally:
            running.pop()

    server.search_engine._run_index_pass = spy
    await asyncio.gather(*(server.search_engine.rebuild_index(incremental=True)
                           for _ in range(3)))

    assert not any(overlapped), "two index passes ran at the same time"
    assert len(overlapped) == 1, (
        "a second incremental pass was queued instead of dropped -- it would "
        f"only repeat the work the running one is doing ({len(overlapped)} ran)")
    await server.search_engine.stop()


async def test_a_state_file_that_does_not_match_its_store_is_not_trusted(
    tmp_path, tree
):
    """Half a written store, a store cleared by hand, a state from another
    version: all of them look like a warm index and answer out of a corpus
    that is not there."""
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)
    state = server.search_engine._state_path
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["files"][str(tree / "auth.py")]["ids"].append("tree/ghost.py#1")
    state.write_text(json.dumps(payload), encoding="utf-8")
    await server.search_engine.stop()

    second = build_server(tmp_path, tree)
    second.search_engine._init_vector_store()

    assert second.search_engine.file_mtimes == {}, \
        "a state that disagrees with the store must be dropped, not used"
    await second.search_engine.stop()


async def test_a_store_of_another_document_format_is_rebuilt(tmp_path, tree):
    """Only the files that change are embedded again: without this, documents
    of the old format would stay next to the new ones for good. Two links: the
    state of the old format is not trusted, and the first background pass is
    then a full one, which clears the store before it fills it."""
    from agent_system.utils.vector_store import compute_embeddings

    server = build_server(tmp_path, tree)
    engine = server.search_engine
    await engine.rebuild_index(incremental=False)
    built = engine._vector_store.count(engine._collection_name)
    # A document of the old format, booked in the state like any other.
    engine._vector_store.add(collection=engine._collection_name, ids=["old#1"],
                             documents=[f"{tree / 'auth.py'}\nclass SessionStore()"],
                             embeddings=compute_embeddings(["class SessionStore()"]),
                             metadatas=[{"file_path": str(tree / "auth.py")}])
    state = engine._state_path
    payload = json.loads(state.read_text(encoding="utf-8"))
    del payload["format"]  # as a state written before the format was named
    payload["files"][str(tree / "auth.py")]["ids"].append("old#1")
    state.write_text(json.dumps(payload), encoding="utf-8")
    await engine.stop()

    second = build_server(tmp_path, tree).search_engine
    second._init_vector_store()
    assert second.file_mtimes == {}, "a store of the old format was trusted"

    indexer = asyncio.create_task(second._background_indexer())
    try:
        for _ in range(600):
            if second._index_built:
                break
            await asyncio.sleep(0.05)
    finally:
        indexer.cancel()
        await asyncio.gather(indexer, return_exceptions=True)

    assert second._index_built, "the first background pass did not finish"
    assert second._vector_store.count(second._collection_name) == built, \
        "the old document stayed next to the new ones"
    assert json.loads(state.read_text(encoding="utf-8"))["format"] == 2
    await second.stop()


async def test_deleted_code_stops_being_found(tmp_path, tree):
    """A symbol index has many documents per file, so 'forget this file' is no
    longer 'delete the id that is its path'. Get that wrong and the index
    keeps answering with functions that no longer exist."""
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)

    (tree / "auth.py").unlink()
    await server.search_engine.rebuild_index(incremental=True)

    result = await server.semantic_search({"query": "end a login session",
                                           "max_results": 10})
    paths = [match["file_path"] for match in result.get("results", [])]
    assert not any("auth.py" in path for path in paths), paths
    await server.search_engine.stop()


async def test_a_symbol_deleted_from_a_file_stops_being_found(tmp_path, tree):
    """The same failure from the other side, and the one a re-add cannot hide.

    Both backends upsert by id, so a symbol that stays on its line is replaced
    for free -- that is why this test removes one instead: `discard` is gone
    from the file, nothing overwrites its id, and only the explicit deletion of
    the file's OLD document ids takes it out of the index.
    """
    server = build_server(tmp_path, tree)
    await server.search_engine.rebuild_index(incremental=False)

    (tree / "tea.py").write_text(
        '"""Brewing, steeping and pouring."""\n\n\ndef pour(cup):\n'
        '    """Tip the pot over the cup."""\n    return cup\n',
        encoding="utf-8")
    await server.search_engine.rebuild_index(incremental=True)

    result = await server.semantic_search(
        {"query": "throw the spent leaves into the compost", "max_results": 10})
    symbols_found = [match["symbol"] for match in result["results"]]
    assert not any("discard" in symbol for symbol in symbols_found), symbols_found
    assert any("pour" in symbol for symbol in symbols_found), symbols_found
    await server.search_engine.stop()


async def test_two_instances_do_not_share_one_collection(tmp_path, tree):
    """Two file_ops instances are two trees. With one shared collection name
    the second instance's full rebuild CLEARED the first one's index, and each
    answered about files the other one cannot even read."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "unrelated.py").write_text('def ping():\n    """Say hi."""\n',
                                        encoding="utf-8")

    first = build_server(tmp_path, tree, name="alpha")
    second = build_server(tmp_path, other, name="beta")
    assert first.search_engine._collection_name != \
        second.search_engine._collection_name

    await first.search_engine.rebuild_index(incremental=False)
    await second.search_engine.rebuild_index(incremental=False)

    result = await first.semantic_search({"query": "end a login session"})
    assert result["status"] == "success" and result["results"], \
        "the second instance's rebuild emptied the first one's index"
    await first.search_engine.stop()
    await second.search_engine.stop()


async def test_instances_that_want_to_share_an_index_may(tmp_path, tree):
    """The coder harness runs a read-write and a read-only twin over the same
    tree. Indexing it twice is the same four minutes twice, so they share a
    collection name and only one of them builds."""
    builder = build_server(tmp_path, tree, name="rw",
                           collection_name="shared_tree")
    reader = build_server(tmp_path, tree, name="ro",
                          collection_name="shared_tree", enable_indexing=False)

    await builder.search_engine.rebuild_index(incremental=False)

    # A reader that still writes puts two writers on one collection, which is
    # exactly the concurrent rebuild the sharing exists to avoid.
    passes = []
    original = reader.search_engine.rebuild_index

    async def counted(*args, **kwargs):
        passes.append(kwargs)
        return await original(*args, **kwargs)

    reader.search_engine.rebuild_index = counted
    result = await reader.semantic_search({"query": "end a login session"})

    assert result["status"] == "success" and result["results"]
    assert passes == [], "the reader must not write into the shared index"
    await builder.search_engine.stop()
    await reader.search_engine.stop()


async def test_a_reader_whose_index_is_missing_names_the_reason(tmp_path, tree):
    """Indexing off plus an empty collection is a configuration mistake, and
    it must not read as "this tree has no such code"."""
    reader = build_server(tmp_path, tree, name="ro", enable_indexing=False)

    result = await reader.semantic_search({"query": "anything"})

    assert result["error_type"] == "IndexNotReady"
    # Not "it is being built": nothing in this instance builds it.
    assert "only reads" in result["error"] and "grep_search" in result["error"]
    await reader.search_engine.stop()
