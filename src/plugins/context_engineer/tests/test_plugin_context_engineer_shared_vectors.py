"""One vector store for all sessions, and a TTL for their data on disk.

Why this exists: chromadb parks one System per persist directory in a
process-global registry, each with its own tokio runtime (~6 descriptors,
4 threads). context_engineer gave every session its own directory, so the
writer host accumulated 229 runtimes and ran into the API's descriptor
limit (30.08.2026). The vectors never needed separate directories — every
entry carries its session_id and every query filters on it — so all
sessions now share one store, and nothing ever deleted a session directory
before, so a sweep removes them after ``session_data_ttl_days``.

Every test here was run against a mutation of the production code it
guards; the mutation named in each docstring turned it red.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agent_system.utils.vector_store import VectorStore
from plugins.context_engineer import hooks as hooks_mod
from plugins.context_engineer.archival_memory import (
    ARCHIVAL_COLLECTION,
    ArchivalMemory,
)
from plugins.context_engineer.hooks import ContextEngineerPlugin

PLUGIN_DIR = Path(__file__).parent.parent

_QUERY = "Hardware die Speicher bewegt"
_HARDWARE = "Der Grafikchip verschiebt Bilddaten parallel zum Hauptprozessor."
_HORSE = "Das Pferd stand am Brunnen und trank."


def _age(path: Path, days: float) -> None:
    """Backdate *path* — and, for a directory, every marker file in it."""
    stamp = time.time() - days * 86400
    if path.is_dir():
        for child in path.iterdir():
            os.utime(child, (stamp, stamp))
    os.utime(path, (stamp, stamp))


def _session_dir(base: Path, name: str, marker: str = "archive.db", days_old: float = 0) -> Path:
    d = base / name
    d.mkdir()
    (d / marker).write_bytes(b"")
    if days_old:
        _age(d, days_old)
    return d


@pytest.fixture
def plugin(tmp_path):
    """A real plugin on a scratch storage base, semantic search on, TTL 1 day."""
    p = ContextEngineerPlugin(PLUGIN_DIR)
    p.apply_config({
        "enable_semantic_search": True,
        "session_data_ttl_days": 1,
        "storage_path": str(tmp_path / "ce"),
    })
    yield p
    key = str(p._storage_base.resolve())
    try:
        for sid in list(p._session_components):
            p.cleanup_session(sid)
    finally:
        # Registry and sweep bookkeeping are process-global; leave nothing
        # behind — even when a session's cleanup throws, or a failing test
        # would leak exactly the runtime this change exists to end.
        _wait_for_sweep(key)
        store = hooks_mod._SHARED_VECTOR_STORES.pop(key, None)
        if store is not None:
            store.close()
        hooks_mod._SWEEP_LAST_RUN.pop(key, None)


def _require_chromadb() -> None:
    """The shared store and its where-filters exist only on the chromadb backend."""
    from agent_system.utils.vector_store import get_vector_backend
    if get_vector_backend() != "chromadb":
        pytest.skip("the shared store needs the chromadb backend (sqlite-vec has no where filters)")


def _wait_for_sweep(key: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while key in hooks_mod._SWEEP_RUNNING and time.time() < deadline:
        time.sleep(0.05)


class _SpyStore:
    """Stands in for a VectorStore where only close() matters."""

    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class _EmptyStore:
    """A vector store that has indexed nothing yet — the shared store on day one.

    ``shape`` is what an empty answer looks like: chromadb nests per query
    (``[[]]``, truthy!), the flat normalisation answers ``[]``.
    """

    def __init__(self, shape):
        self.shape = shape

    def add(self, *a, **k):
        pass

    def query(self, *a, **k):
        return {"ids": self.shape}


def _vector_store_or_skip(path: Path) -> VectorStore:
    try:
        return VectorStore(persist_path=path)
    except Exception as exc:  # noqa: BLE001 — no backend on this host
        pytest.skip(f"no vector backend available on this host: {exc}")


# ---------------------------------------------------------------------------
# Isolation and ownership
# ---------------------------------------------------------------------------

def test_sessions_on_one_store_see_only_their_own_messages(tmp_path):
    """Two sessions, ONE store: the session_id filter is all that separates them.

    Both sessions archive into the same SQLite file here on purpose. With
    separate files a leaked foreign id would be dropped at the SQL step and
    the leak would only thin the result list — invisible to an assertion.
    Shared file, the leak shows as foreign content.

    Mutation: drop the ``where`` filter in ``_search_semantic`` → the
    Grafikchip sentence of session A turns up in session B's result.
    """
    _require_chromadb()
    store = _vector_store_or_skip(tmp_path / "shared")
    archive = ArchivalMemory(tmp_path / "shared.db", session_id="sess_a",
                             enable_semantic_search=True, vector_store=store)
    try:
        archive.store({"role": "assistant", "content": _HARDWARE}, session_id="sess_a")
        archive.store({"role": "assistant", "content": _HORSE}, session_id="sess_b")

        hits_a = archive.search(_QUERY, session_id="sess_a", limit=5, use_semantic=True)
        hits_b = archive.search(_QUERY, session_id="sess_b", limit=5, use_semantic=True)

        assert any("Grafikchip" in m.content for m in hits_a), "fixture: A cannot find its own message"
        assert all("Grafikchip" not in m.content for m in hits_b), (
            "session B received session A's message from the shared store"
        )
        assert all(m.session_id == "sess_b" for m in hits_b)
    finally:
        archive.close()
        store.close()


@pytest.mark.parametrize("injected,expected_close_calls", [(True, 0), (False, 1)])
def test_close_touches_only_a_store_it_owns(tmp_path, injected, expected_close_calls):
    """An injected store belongs to the plugin; a self-built one to the session.

    Closing the shared store from one session would stop the ONE runtime
    every other session uses. Measured through a spy, not through chromadb:
    the locally installed 1.4 reconnects lazily after close() and would show
    a still-working second session either way.

    Mutation: invert the ``_owns_vector_store`` check in ``close()`` → both
    rows flip.
    """
    spy = _SpyStore()
    if injected:
        archive = ArchivalMemory(tmp_path / "a.db", session_id="s",
                                 enable_semantic_search=True, vector_store=spy)
    else:
        archive = ArchivalMemory(tmp_path / "a.db", session_id="s",
                                 enable_semantic_search=True,
                                 vector_store_path=tmp_path / "own")
        if not archive.enable_semantic_search:
            pytest.skip("no vector backend available on this host")
        archive._vector_store.close()  # release the real one, count on the spy
        archive._vector_store = spy

    archive.close()

    assert spy.close_calls == expected_close_calls
    assert archive._vector_store is None


@pytest.mark.parametrize("shape", [[[]], []], ids=["chroma_nested", "flat"])
def test_empty_vector_index_falls_back_to_text_search(tmp_path, shape):
    """No vector hit does not mean no archived row.

    The shared store starts empty after the move, and batches above the
    semantic cap are only ever FTS-indexed. Answering ``[]`` hid rows that
    sit in archive.db — this is what made "the index rebuilds itself" true
    instead of a blind spot. Two early returns exist, one per answer shape;
    each parametrisation reaches one of them.

    Mutation: restore ``return []`` at either early return → the matching
    parametrisation goes red.
    """
    archive = ArchivalMemory(tmp_path / "a.db", session_id="s",
                             enable_semantic_search=True, vector_store=_EmptyStore(shape))
    try:
        archive.store({"role": "assistant", "content": "Der Blitter kopiert Speicherbloecke."},
                      session_id="s")
        hits = archive.search("Blitter", session_id="s", limit=5, use_semantic=True)
        assert [m.content for m in hits] == ["Der Blitter kopiert Speicherbloecke."]
    finally:
        archive.close()


# ---------------------------------------------------------------------------
# Production path: the hook hands every session the same store
# ---------------------------------------------------------------------------

def test_hook_gives_every_session_the_same_store(plugin):
    """The claim the whole change rests on: n sessions, ONE VectorStore.

    Mutation: drop ``vector_store=shared_store`` in ``_get_session_components``
    → each session builds its own store again and the identity check fails.
    """
    _require_chromadb()
    a = plugin._get_session_components("sess_one")["archival_memory"]
    b = plugin._get_session_components("sess_two")["archival_memory"]

    assert a._vector_store is b._vector_store
    assert a._owns_vector_store is False and b._owns_vector_store is False
    assert not (plugin._storage_base / "sess_one" / "vectors").exists(), (
        "a per-session vector directory was created — that is one runtime per session again"
    )
    assert (plugin._storage_base / hooks_mod._SHARED_VECTORS_DIRNAME).is_dir()


# ---------------------------------------------------------------------------
# TTL sweep
# ---------------------------------------------------------------------------

def test_sweep_removes_only_idle_session_directories(plugin):
    """What the sweep may touch is decided by marker files, not by name.

    Mutations: drop the marker check → ``_shared_vectors`` and ``stray`` go;
    drop the ``_is_session_live`` check → the reserved session goes.
    """
    base = plugin._storage_base
    base.mkdir(parents=True)
    old = _session_dir(base, "old_session", days_old=3)
    fresh = _session_dir(base, "fresh_session")
    reserved = _session_dir(base, "reserved_session", marker="tool_results.db", days_old=3)
    plugin._creating.add("reserved_session")
    shared = base / hooks_mod._SHARED_VECTORS_DIRNAME
    shared.mkdir()
    (shared / "chroma.sqlite3").write_bytes(b"")
    _age(shared, 30)
    stray = base / "stray"
    stray.mkdir()
    (stray / "pic.png").write_bytes(b"")
    _age(stray, 30)
    history = base / "history.json"
    history.write_text("[]")
    _age(history, 30)

    removed = plugin._sweep_stale_session_dirs()

    assert removed == 1
    assert not old.exists()
    assert fresh.exists() and reserved.exists()
    assert shared.exists() and stray.exists() and history.exists()


def test_sweep_uses_file_mtimes_not_only_the_directory(plugin):
    """A file rewritten in place moves its own mtime, not the directory's.

    Mutation: judge by the directory mtime alone → the session whose
    core_memory.json was just rewritten is deleted while in use.
    """
    base = plugin._storage_base
    base.mkdir(parents=True)
    d = _session_dir(base, "rewritten", marker="core_memory.json", days_old=3)
    now = time.time()
    os.utime(d / "core_memory.json", (now, now))  # the rewrite; directory stays old

    assert plugin._sweep_stale_session_dirs() == 0
    assert d.exists()


def test_sweep_counts_recent_media_as_activity(plugin):
    """Media lives one level deeper and moves no marker the sweep used to read.

    Mutation: drop media/metadata.json from _SESSION_MARKERS → a session whose
    databases are old but which stored media an hour ago is deleted, and the
    files its hints name are gone.
    """
    base = plugin._storage_base
    base.mkdir(parents=True)
    d = _session_dir(base, "media_only_lately", marker="archive.db")
    (d / "media").mkdir()
    (d / "media" / "metadata.json").write_text("{}")
    # Backdate the directory and its direct entries — creating media/ moved the
    # session directory's own mtime, which would keep it alive by itself.
    _age(d, 3)
    now = time.time()
    os.utime(d / "media" / "metadata.json", (now, now))  # stored media just now

    assert plugin._sweep_stale_session_dirs() == 0
    assert d.exists()


def test_sweep_drops_the_swept_sessions_vectors(plugin):
    """Rows and vectors leave together — one criterion for both.

    Mutation: drop the ``_forget_session_vectors`` call → the vectors of the
    deleted session stay in the shared collection.
    """
    _require_chromadb()
    base = plugin._storage_base
    gone = plugin._get_session_components("gone")["archival_memory"]
    stays = plugin._get_session_components("stays")["archival_memory"]
    if gone._vector_store is None:
        pytest.skip("no vector backend available on this host")
    gone.store({"role": "assistant", "content": _HARDWARE}, session_id="gone")
    stays.store({"role": "assistant", "content": _HORSE}, session_id="stays")
    store = gone._vector_store
    plugin.cleanup_session("gone")           # release handles, as eviction does
    # Session creation kicked a background sweep (fixture TTL = 1 day); let it
    # finish before backdating, or it races the foreground call below.
    _wait_for_sweep(str(base.resolve()))
    _age(base / "gone", 3)

    assert plugin._sweep_stale_session_dirs() == 1

    left = store.query(ARCHIVAL_COLLECTION, query_text=_QUERY, n_results=5,
                       where={"session_id": "gone"})
    kept = store.query(ARCHIVAL_COLLECTION, query_text=_QUERY, n_results=5,
                       where={"session_id": "stays"})
    assert not [i for ids in left["ids"] for i in ids], "vectors of the swept session survived"
    assert [i for ids in kept["ids"] for i in ids], "the other session's vectors were removed too"


def test_session_creation_triggers_the_sweep(plugin):
    """The wiring, not the algorithm: creating a session starts the sweep thread.

    Mutation: remove ``self._maybe_start_sweep()`` from
    ``_get_session_components`` → the old directory is still there.
    """
    base = plugin._storage_base
    base.mkdir(parents=True)
    old = _session_dir(base, "old_session", days_old=3)

    plugin._get_session_components("newcomer")

    key = str(base.resolve())
    _wait_for_sweep(key)
    assert key in hooks_mod._SWEEP_LAST_RUN, "no sweep was scheduled"
    assert not old.exists()
    assert (base / "newcomer").exists()


def test_one_sweep_per_storage_base_at_a_time(plugin):
    """Two triggers, one sweep — and none again before the interval.

    Per-instance bookkeeping let a process with many plugin instances run
    many concurrent sweeps against the same directory (seen as
    PermissionError/FileNotFoundError races in the plugin's own test run).

    Mutations: drop the ``_SWEEP_RUNNING`` check → two threads while the
    first still runs; drop the interval check → a second run right after
    the first finished.
    """
    import threading

    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_sweep():
        calls.append(1)
        started.set()
        release.wait(30)   # generous: a 5 s stall under load turned this into a flake
        return 0

    plugin._sweep_stale_session_dirs = slow_sweep
    plugin._storage_base.mkdir(parents=True, exist_ok=True)
    key = str(plugin._storage_base.resolve())

    # Phase 1 — a sweep that outlasts the interval: with the interval check
    # out of the way, only the running check stands between two threads.
    plugin._SWEEP_INTERVAL_SECONDS = 0
    plugin._maybe_start_sweep()
    assert started.wait(5), "the first sweep never started"
    plugin._maybe_start_sweep()          # while the first is still running
    release.set()
    _wait_for_sweep(key)
    assert len(calls) == 1, f"a second sweep started while the first ran ({len(calls)})"

    # Phase 2 — inside the interval, after the first finished.
    plugin._SWEEP_INTERVAL_SECONDS = 3600
    plugin._maybe_start_sweep()
    _wait_for_sweep(key)
    assert len(calls) == 1, f"a sweep started again inside the interval ({len(calls)})"


def test_sweep_finishes_a_leftover_from_an_interrupted_run(plugin):
    """``<sid>.sweeping`` is finished regardless of age or markers.

    A daemon thread stopped mid-rmtree can leave a directory whose markers
    are already gone while ``media/`` (the bulk) remains. Without this
    branch the allow-list would skip it forever.

    Mutation: drop the leftover branch → the directory survives.
    """
    base = plugin._storage_base
    base.mkdir(parents=True)
    leftover = base / ("old_session" + hooks_mod._SWEEPING_SUFFIX)
    (leftover / "media").mkdir(parents=True)
    (leftover / "media" / "pic.png").write_bytes(b"")   # no marker, fresh mtime

    assert plugin._sweep_stale_session_dirs() == 1
    assert not leftover.exists()


def test_leftover_cleanup_spares_the_vectors_of_a_returned_session(plugin):
    """A leftover's vectors went in the run that renamed it — never again.

    rename succeeded, rmtree failed, the session came back and wrote fresh
    vectors: the next run must remove ``<sid>.sweeping`` and leave those
    fresh vectors alone.

    Mutation: call ``_forget_session_vectors`` for leftovers too → the
    returned session's vectors are gone.
    """
    _require_chromadb()
    base = plugin._storage_base
    returned = plugin._get_session_components("came_back")["archival_memory"]
    if returned._vector_store is None:
        pytest.skip("no vector backend available on this host")
    returned.store({"role": "assistant", "content": _HARDWARE}, session_id="came_back")
    _wait_for_sweep(str(base.resolve()))
    leftover = base / ("came_back" + hooks_mod._SWEEPING_SUFFIX)
    leftover.mkdir()
    (leftover / "media").mkdir()

    assert plugin._sweep_stale_session_dirs() == 1
    assert not leftover.exists()
    kept = returned._vector_store.query(ARCHIVAL_COLLECTION, query_text=_QUERY, n_results=5,
                                        where={"session_id": "came_back"})
    assert [i for ids in kept["ids"] for i in ids], "the returned session's fresh vectors were deleted"


def test_search_does_not_hold_the_archive_lock_while_querying(tmp_path):
    """The read path gets the same split as the write path.

    ``search`` used to hold the archive lock across the vector query; with a
    shared store that query can wait seconds for another session's batch,
    and every loop-synchronous reader of this archive would wait with it.

    Mutation: make ``search`` ``@_synchronized`` again (or run the vector
    query under the lock) → ``get_stats`` blocks behind the stuck query.
    """
    import threading

    entered = threading.Event()
    release = threading.Event()

    class _BlockingStore:
        def add(self, *a, **k):
            pass

        def query(self, *a, **k):
            entered.set()
            release.wait(30)
            return {"ids": [[]]}

    archive = ArchivalMemory(tmp_path / "a.db", session_id="s",
                             enable_semantic_search=True, vector_store=_BlockingStore())
    searcher = threading.Thread(
        target=archive.search, args=("anything",),
        kwargs={"session_id": "s", "limit": 3, "use_semantic": True}, daemon=True,
    )
    try:
        searcher.start()
        assert entered.wait(5), "search() never reached the vector query"
        read_done = threading.Event()

        def reader():
            archive.get_stats(session_id="s")
            read_done.set()

        threading.Thread(target=reader, daemon=True).start()
        assert read_done.wait(2), (
            "get_stats blocked behind a vector query — the archive lock is held across it"
        )
    finally:
        release.set()
        searcher.join(5)
        archive.close()


def test_sweep_renames_before_it_removes(plugin, monkeypatch):
    """The rename is what makes an interrupted rmtree recoverable.

    Mutation: rmtree the directory in place → on failure the original name
    is still there, complete with markers, and nothing tells the next run
    that a sweep had already decided on it.
    """
    base = plugin._storage_base
    base.mkdir(parents=True)
    old = _session_dir(base, "old_session", days_old=3)

    def _interrupted(path, *a, **k):
        raise OSError("stopped mid-rmtree")

    monkeypatch.setattr(hooks_mod.shutil, "rmtree", _interrupted)

    assert plugin._sweep_stale_session_dirs() == 0
    assert not old.exists()
    assert (base / ("old_session" + hooks_mod._SWEEPING_SUFFIX)).is_dir()


def test_sweep_keeps_the_directory_when_vectors_cannot_be_dropped(plugin, monkeypatch):
    """Vectors go first; if they cannot, the directory stays for a retry.

    The other order orphans them for good: once the directory is gone no
    sweep ever sees that session_id again.

    Mutation: rmtree before ``_forget_session_vectors`` → the directory is
    gone although the delete raised.
    """
    base = plugin._storage_base
    base.mkdir(parents=True)
    old = _session_dir(base, "old_session", days_old=3)

    class _FailingStore:
        def delete(self, *a, **k):
            raise RuntimeError("chroma down")

        def close(self):
            pass

    monkeypatch.setitem(hooks_mod._SHARED_VECTOR_STORES, str(base.resolve()), _FailingStore())

    assert plugin._sweep_stale_session_dirs() == 0
    assert old.exists(), "directory removed although its vectors could not be — orphaned for good"


def test_shared_store_failure_is_retried_and_the_half_built_store_closed(tmp_path, monkeypatch):
    """A failed construction must neither leak nor become permanent.

    Mutations: cache the failure as final → the third call still answers
    None; drop the ``close()`` in the except → the half-built store keeps
    its runtime (``closed`` stays empty).
    """
    import agent_system.utils.vector_store as vs_mod

    attempts: list[int] = []
    closed: list[int] = []

    class _FlakyStore:
        def __init__(self, persist_path):
            self.persist_path = persist_path
            attempts.append(1)

        def get_or_create_collection(self, name):
            if len(attempts) == 1:
                raise RuntimeError("busy")

        def close(self):
            closed.append(1)

    monkeypatch.setattr(vs_mod, "VectorStore", _FlakyStore)
    monkeypatch.setattr(vs_mod, "get_vector_backend", lambda: "chromadb")
    base = tmp_path / "ce"
    key = str(base.resolve())
    try:
        assert hooks_mod._shared_vector_store(base) is None
        assert closed == [1], "the half-built store kept its Chroma runtime"
        assert hooks_mod._shared_vector_store(base) is None, "retried inside the back-off window"
        hooks_mod._SHARED_STORE_FAILED_AT[key] -= hooks_mod._SHARED_STORE_RETRY_SECONDS + 1
        assert hooks_mod._shared_vector_store(base) is not None, "a failed construction was never retried"
    finally:
        hooks_mod._SHARED_VECTOR_STORES.pop(key, None)
        hooks_mod._SHARED_STORE_FAILED_AT.pop(key, None)


def test_failed_sweep_thread_start_does_not_stick(plugin, monkeypatch):
    """Thread exhaustion must not silence every later sweep — or fail the session.

    Mutation: drop the try/except around ``_start_sweep_thread`` → the
    RuntimeError escapes and ``_SWEEP_RUNNING`` keeps the key.
    """
    plugin._storage_base.mkdir(parents=True, exist_ok=True)
    key = str(plugin._storage_base.resolve())

    def _cannot_start(_key):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(plugin, "_start_sweep_thread", _cannot_start)

    plugin._maybe_start_sweep()   # must not raise

    assert key not in hooks_mod._SWEEP_RUNNING, "the running flag stuck after a failed thread start"


def test_store_does_not_hold_the_archive_lock_while_indexing(tmp_path):
    """Indexing a message must not hold the archive's own lock.

    The vector store is shared, so its lock can be held for seconds by
    another session's batch embedding. If ``store()`` waited for it while
    holding the archive lock, every loop-synchronous reader of this archive
    (stats, counts, reads) would wait too — and with it the event loop.

    Mutation: move the vector add back inside the synchronized row write →
    ``get_stats`` blocks behind the stuck add.
    """
    import threading

    entered = threading.Event()
    release = threading.Event()

    class _BlockingStore:
        def add(self, *a, **k):
            entered.set()
            release.wait(5)

        def query(self, *a, **k):
            return {"ids": [[]]}

    archive = ArchivalMemory(tmp_path / "a.db", session_id="s",
                             enable_semantic_search=True, vector_store=_BlockingStore())
    writer = threading.Thread(
        target=archive.store, args=({"role": "assistant", "content": "x"},),
        kwargs={"session_id": "s"}, daemon=True,
    )
    try:
        writer.start()
        assert entered.wait(5), "store() never reached the vector add"
        read_done = threading.Event()

        def reader():
            archive.get_stats(session_id="s")
            read_done.set()

        threading.Thread(target=reader, daemon=True).start()
        assert read_done.wait(2), (
            "get_stats blocked behind a vector add — the archive lock is held across indexing"
        )
    finally:
        release.set()
        writer.join(5)
        archive.close()


def test_plugin_without_operator_config_never_sweeps(tmp_path):
    """Instantiating the plugin must not delete anything. Ever.

    The sweep is opt-in through the operator's config. The binding default is
    the one in schema.yaml (apply_config merges the schema defaults in; the
    ``.get(..., 0)`` in the code is only reached if the schema lost the key).
    This is not theory: with a default of 14 the plugin's own test suite —
    which builds instances against the default storage path — swept ~18,000
    session directories out of the local data/context_engineer.

    Mutation: set the schema.yaml default to 14 → the old directory is gone
    after session creation. (Changing only the code default stays green —
    measured; the schema wins.)
    """
    p = ContextEngineerPlugin(PLUGIN_DIR)
    p._storage_base = tmp_path / "ce"
    p._storage_base.mkdir()
    old = _session_dir(p._storage_base, "old_session", days_old=400)
    key = str(p._storage_base.resolve())
    try:
        p._get_session_components("newcomer")
        _wait_for_sweep(key)
        assert p._session_data_ttl_days == 0
        assert key not in hooks_mod._SWEEP_LAST_RUN, "a sweep was scheduled without opt-in"
        assert old.exists()
    finally:
        for sid in list(p._session_components):
            p.cleanup_session(sid)
        store = hooks_mod._SHARED_VECTOR_STORES.pop(key, None)
        if store is not None:
            store.close()
