"""
Concurrency tests for TodoServer cache eviction (PB27).

Eviction runs from worker threads (asyncio.to_thread) and mutates the shared
_sessions / _task_counters / _session_locks dicts that the event loop also
reads. These tests pin the fix:
  - eviction never drops a session whose per-session lock is in-flight (held)
  - eviction never pops a per-session lock object (would break mutual exclusion)
  - heavy concurrent load/save/evict raises no dict-mutation race
"""

import asyncio
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plugins.todo.server import TodoServer, TaskCollection


@pytest.fixture
def server(tmp_path: Path) -> TodoServer:
    storage = tmp_path / "todos"
    storage.mkdir()
    cfg = MagicMock()
    cfg.storage_path = str(storage)
    cfg.max_tasks_per_session = 100
    cfg.enable_dependencies = True
    cfg.auto_save = True
    cfg.max_cache_size = 4  # small -> forces eviction
    return TodoServer(name="todo", system_config=MagicMock(), mcp_config=cfg)


def _fill(server: TodoServer, n: int) -> None:
    """Populate n cached sessions with strictly increasing updated_at (s0 oldest)."""
    base = datetime(2020, 1, 1, tzinfo=UTC)
    for i in range(n):
        sid = f"s{i}"
        c = TaskCollection(session_id=sid)
        c.updated_at = base + timedelta(seconds=i)
        server._sessions[sid] = c


class TestEvictionSafety:
    @pytest.mark.asyncio
    async def test_eviction_skips_in_flight_locked_session(self, server: TodoServer):
        server._max_cache_size = 4
        _fill(server, 6)
        # Hold the lock of the OLDEST session (s0) — the prime eviction candidate.
        lock = asyncio.Lock()
        await lock.acquire()
        server._session_locks["s0"] = lock
        try:
            server._evict_cache_if_needed()
            # In-flight session must survive despite being oldest.
            assert "s0" in server._sessions
        finally:
            lock.release()

    def test_eviction_does_not_pop_session_locks(self, server: TodoServer):
        server._max_cache_size = 4
        _fill(server, 6)
        for sid in list(server._sessions):
            server._session_locks[sid] = asyncio.Lock()
        sentinel = server._session_locks["s0"]  # oldest -> data will be evicted
        server._evict_cache_if_needed()
        assert "s0" not in server._sessions          # heavy data evicted
        assert server._session_locks["s0"] is sentinel  # lock object preserved

    def test_eviction_pops_task_counters(self, server: TodoServer):
        server._max_cache_size = 4
        _fill(server, 6)
        for sid in list(server._sessions):
            server._task_counters[sid] = 1
        server._evict_cache_if_needed()
        # evicted sessions lose their counter; survivors keep theirs
        for sid in server._sessions:
            assert sid in server._task_counters
        evicted = {f"s{i}" for i in range(6)} - set(server._sessions)
        assert evicted, "expected at least one eviction"
        for sid in evicted:
            assert sid not in server._task_counters


class TestConcurrentCacheStress:
    @pytest.mark.asyncio
    async def test_concurrent_load_save_evict_no_race(self, server: TodoServer):
        server._max_cache_size = 4

        async def worker(i: int) -> None:
            sid = f"sess_{i}"
            for _ in range(6):
                await server._load_session_async(sid)
                await asyncio.sleep(0)  # yield so eviction interleaves
                await server._save_session_async(sid)

        # 30 sessions through a 4-slot cache -> constant eviction while other
        # sessions are mid load/save. A dict-mutation race would surface as
        # RuntimeError / KeyError / StorageError here (reaching this line = pass).
        await asyncio.gather(*[worker(i) for i in range(30)])

        # Data integrity: every session survives the eviction round-trips (each
        # was persisted to disk and reloads as its own collection). The cache is
        # intentionally allowed to exceed max_cache_size while sessions are
        # in-flight (correctness over strict bounding); once load subsides the
        # next load evicts again.
        for i in range(30):
            sid = f"sess_{i}"
            coll = await server._load_session_async(sid)
            assert coll.session_id == sid
