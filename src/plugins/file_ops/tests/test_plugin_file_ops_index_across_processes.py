"""One index pass at a time -- across processes, not only inside one.

Every agent-cli that loads file_ops runs its own background indexer over the
same semantic index. The in-process asyncio lock kept two passes of ONE
process apart; N processes still walked the same tree N times, each writing
on its own view of the vectors and each writing the state file (one mtime per
file) from its own memory.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.file_ops import search as search_module
from plugins.file_ops.server import FileOpsServer

_HOLDER = "\n".join([
    "import sys",
    "from filelock import FileLock",
    "lock = FileLock(sys.argv[1])",
    "lock.acquire(timeout=10)",
    "print('held', flush=True)",
    "sys.stdin.readline()",
])


def _server(tmp_path: Path) -> FileOpsServer:
    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    (tree / "tea.py").write_text("def steep(leaves):\n    return leaves\n", encoding="utf-8")
    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(tree)]
    server_config.search = {
        "enable_semantic_search": True,
        "enable_indexing": True,
        "index_on_startup": False,
        "chroma_db_path": str(tmp_path / "store"),
    }
    return FileOpsServer("idx", Mock(spec=AgentSystemConfig), server_config)


def _pass_lock_path(tmp_path: Path) -> Path:
    return tmp_path / "store" / search_module._PASS_LOCK_NAME


def _hold(path: Path) -> subprocess.Popen:
    path.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held", "the holder never took the lock"
    return child


@pytest.mark.asyncio
async def test_a_process_skips_its_pass_while_another_one_indexes(tmp_path, monkeypatch):
    """Including its FIRST pass -- the one that opens the store.

    The lock was first derived from the opened store, which opens inside that
    very pass: the first pass of every process, the common case, ran unguarded.
    """
    engine = _server(tmp_path).search_engine
    ran = []

    async def record(*args, **kwargs):
        ran.append(True)

    monkeypatch.setattr(engine, "_run_index_pass", record)
    holder = _hold(_pass_lock_path(tmp_path))
    try:
        assert await engine.rebuild_index(incremental=True) is False
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert ran == [], "a second process indexed next to the one holding the pass"


@pytest.mark.asyncio
async def test_a_pass_starts_from_the_state_on_disk(tmp_path, monkeypatch):
    """Another process may have indexed since this one last read the state."""
    engine = _server(tmp_path).search_engine
    loads = []

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(engine, "_run_index_pass", nothing)
    monkeypatch.setattr(engine, "_load_state", lambda: loads.append(True))

    await engine.rebuild_index(incremental=True)
    await engine.rebuild_index(incremental=True)

    assert len(loads) == 2, "a pass worked from the state it had in memory"


@pytest.mark.asyncio
async def test_the_lock_is_free_again_after_a_full_pass(tmp_path):
    """Taken in a worker thread, released on the event loop.

    filelock counts per thread by default; with that, the release after a
    full rebuild released nothing, and every other process skipped its passes
    until this one exited.
    """
    engine = _server(tmp_path).search_engine
    try:
        assert await engine.rebuild_index(incremental=False) is True
    finally:
        await engine.stop()

    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; from filelock import FileLock;"
         "FileLock(sys.argv[1]).acquire(timeout=0); print('free')",
         str(_pass_lock_path(tmp_path))],
        capture_output=True, text=True, timeout=60)
    assert probe.stdout.strip() == "free", "the pass lock stayed held after the pass"
