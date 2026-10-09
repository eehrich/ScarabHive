"""The session index holds when several PROCESSES write it at once.

Production runs many agent-cli processes next to the API, all on the same
users. Every one of them keeps its own SessionManager, and every session they
create or save goes into the same ``index.json``. The manager's asyncio lock
says nothing about the other processes. Measured 21.09.2026, 8 processes
creating 25 sessions each for one user:

* 35 and 116 of the sessions on disk were in no index -- invisible in the
  sidebar. Read-modify-write, last writer wins.
* a process crashed in both runs: Windows refuses to OPEN a file another
  process is replacing, and the readers, unlike the writers, did not retry.

After the fix, 8 and 16 processes: every session on disk, every one indexed,
no process dead.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from filelock import FileLock

from agent_system.core.session_presence import SessionPresence
from agent_system.services import session_manager as sm_module
from agent_system.utils import io as io_module
from agent_system.services.session_manager import SessionManager

USER = "u"
SRC = str(Path(__file__).resolve().parents[2] / "src")

# What an agent-cli does per conversation, as a process of its own.
_CREATOR = """
import asyncio, sys
sys.path.insert(0, sys.argv[4])
from agent_system.services.session_manager import SessionManager

async def main(storage, tag, count):
    manager = SessionManager(storage_path=storage)
    for i in range(count):
        await manager.create_session(user_id="u", title=f"{tag}-{i}", session_id=f"s_{tag}_{i}")

asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
"""


def test_parallel_processes_lose_no_session_from_the_index(tmp_path):
    processes, per_process = 8, 20
    storage = tmp_path / "sessions"
    storage.mkdir()

    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CREATOR, str(storage), f"p{n}", str(per_process), SRC],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        for n in range(processes)
    ]
    failures = []
    for child in children:
        _out, err = child.communicate(timeout=300)
        if child.returncode:
            failures.append(err.strip().splitlines()[-1] if err.strip() else "?")

    assert not failures, f"{len(failures)} process(es) died: {failures[:2]}"
    user_dir = storage / USER
    on_disk = {path.stem for path in user_dir.glob("s_*.json")}
    assert len(on_disk) == processes * per_process
    indexed = set(json.loads((user_dir / "index.json").read_text(encoding="utf-8")))
    missing = on_disk - indexed
    assert not missing, f"{len(missing)} session(s) on disk are in no index"


def test_an_index_being_replaced_is_read_again_not_given_up(tmp_path, monkeypatch):
    """The window in which Windows refuses to open a file being replaced."""
    path = tmp_path / "index.json"
    path.write_text('{"s1": {}}', encoding="utf-8")
    real_open = open
    refusals = []

    def refuse_twice(file, *args, **kwargs):
        if Path(file) == path and len(refusals) < 2:
            refusals.append(file)
            raise PermissionError(13, "being replaced by another process")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io_module, "open", refuse_twice, raising=False)

    assert io_module.read_json_retrying(path) == {"s1": {}}
    assert len(refusals) == 2, "the test never refused anything"


@pytest.mark.asyncio
async def test_a_row_written_during_a_rebuild_survives_it(tmp_path, monkeypatch):
    """A rebuild scans for minutes; what another process writes meanwhile is newer.

    It used to write its snapshot over the file, dropping every row that
    arrived during the scan.
    """
    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    await manager.create_session(user_id=USER, title="old", session_id="s_old")
    (tmp_path / "sessions" / USER / "index.json").unlink()   # lost: a rebuild is due

    real_read = manager._read_session_file_async
    arrived = []

    async def scan_while_another_process_writes(path):
        if not arrived:
            def put(index):
                index["s_meanwhile"] = {"session_id": "s_meanwhile", "title": "new"}
                return sm_module._WRITE
            await manager._edit_index(USER, None, put, missing={})
            arrived.append(True)
        return await real_read(path)

    monkeypatch.setattr(manager, "_read_session_file_async", scan_while_another_process_writes)

    await manager._rebuild_index(USER)

    index = json.loads((tmp_path / "sessions" / USER / "index.json").read_text(encoding="utf-8"))
    assert arrived, "nothing was written during the scan -- the test measured nothing"
    assert "s_meanwhile" in index, "the rebuild wrote its snapshot over a newer row"
    assert "s_old" in index


def test_an_index_lock_is_not_taken_for_a_running_session(tmp_path):
    """Every *.lock in a user directory is a session presence lock to its readers."""
    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    index_path = manager._get_index_path(USER)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = manager._index_lock_path(index_path)

    assert not lock_path.name.endswith(".lock")
    with FileLock(str(lock_path)):
        running = SessionPresence(root=tmp_path / "sessions").list_for_user(USER)
    assert running == [], f"the index lock showed up as a session: {running}"


def test_the_retrying_reader_is_what_every_index_read_uses():
    """One raw open() left beside it would bring the crash back for that path."""
    source = Path(sm_module.__file__).read_text(encoding="utf-8")
    assert "json.load(" not in source, "an index or session file is read without the retry"
    assert "read_json_retrying(" in source
