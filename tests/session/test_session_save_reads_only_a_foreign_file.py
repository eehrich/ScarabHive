"""save_session takes the file's metadata over the caller's, and read the whole file for it before every save --
under load (50 agents) that read held the GIL for about 4 % of the API's Python time. The file this manager wrote
last is not read again: its metadata is what was written into it. Any other file -- another process's write, a file
system without file ids -- is read as before."""
from __future__ import annotations

from types import SimpleNamespace

from agent_system.services import session_manager as session_manager_module
from agent_system.services.session_manager import SessionManager, _file_key
from agent_system.utils.io import atomic_write_json


async def _manager(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path))
    session = await manager.create_session("ada", title="t", agent_name="coordinator")
    reads = []
    read = manager._read_session_file

    def counted(path):
        reads.append(path)
        return read(path)
    manager._read_session_file = counted
    return manager, session["session_id"], reads


async def test_a_save_after_this_managers_own_write_does_not_read_the_file(tmp_path):
    manager, sid, reads = await _manager(tmp_path)
    copy = await manager.load_session("ada", sid, bypass_cache=True)
    copy["messages"] = [{"role": "user", "content": "one"}]
    reads.clear()
    await manager.save_session(copy)
    copy["messages"].append({"role": "assistant", "content": "two"})
    await manager.save_session(copy)
    assert reads == [], f"the file this manager wrote was read again: {reads}"
    stored = await manager.load_session("ada", sid, bypass_cache=True)
    assert [m["content"] for m in stored["messages"]] == ["one", "two"] and stored["metadata"]["message_count"] == 2


async def test_metadata_this_manager_merged_meanwhile_is_kept_without_a_read(tmp_path):
    manager, sid, reads = await _manager(tmp_path)
    stale = await manager.load_session("ada", sid, bypass_cache=True)
    await manager.update_session_metadata("ada", sid, {"sub_agents": {"helper": {"status": "running"}}})
    reads.clear()
    await manager.save_session(stale)
    assert reads == []
    stored = await manager.load_session("ada", sid, bypass_cache=True)
    assert stored["metadata"]["sub_agents"] == {"helper": {"status": "running"}}


async def test_a_file_another_process_wrote_is_read_and_its_metadata_kept(tmp_path):
    manager, sid, reads = await _manager(tmp_path)
    copy = await manager.load_session("ada", sid, bypass_cache=True)
    await manager.save_session(copy)
    path = manager._get_session_path("ada", sid)
    # another process: its own write of the file, with metadata of its own
    theirs = await manager.load_session("ada", sid, bypass_cache=True)
    theirs["metadata"]["linked_elsewhere"] = {"by": "another process"}
    atomic_write_json(path, theirs)
    reads.clear()
    await manager.save_session(copy)
    assert len(reads) == 1, "a file another process wrote was not read"
    stored = await manager.load_session("ada", sid, bypass_cache=True)
    assert stored["metadata"]["linked_elsewhere"] == {"by": "another process"}


async def test_what_was_written_is_kept_not_what_the_caller_changes_after(tmp_path):
    manager, sid, reads = await _manager(tmp_path)
    copy = await manager.load_session("ada", sid, bypass_cache=True)
    copy["metadata"]["settings"] = {"level": 1}
    await manager.save_session(copy)
    copy["metadata"]["settings"]["level"] = 2      # changed in the caller's hands, never saved
    other = await manager.load_session("ada", sid, bypass_cache=True)
    other["metadata"].pop("settings")
    await manager.save_session(other)
    stored = await manager.load_session("ada", sid, bypass_cache=True)
    assert stored["metadata"]["settings"] == {"level": 1}, "the file's metadata came from the caller's later copy"


async def test_without_file_ids_every_save_reads_the_file(tmp_path, monkeypatch):
    # a file system that gives files no id (FAT, some network shares): st_ino 0 for what is written
    def written_without_id(path, data, **kwargs):
        stat = atomic_write_json(path, data, **kwargs)
        return SimpleNamespace(st_ino=0, st_mtime_ns=stat.st_mtime_ns, st_size=stat.st_size)
    monkeypatch.setattr(session_manager_module, "atomic_write_json", written_without_id)
    manager, sid, reads = await _manager(tmp_path)
    copy = await manager.load_session("ada", sid, bypass_cache=True)
    await manager.save_session(copy)
    reads.clear()
    await manager.save_session(copy)
    assert len(reads) == 1, "a file system without file ids skipped the read"


async def test_the_record_of_writes_is_bounded_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(session_manager_module, "_WRITTEN_KEPT", 2)
    manager, first, reads = await _manager(tmp_path)
    copy = await manager.load_session("ada", first, bypass_cache=True)
    others = [(await manager.create_session("ada", title=f"t{n}", agent_name="coordinator"))["session_id"]
              for n in range(2)]
    assert list(manager._written) == others, "the record kept more than its bound, or not the latest writes"
    reads.clear()
    await manager.save_session(copy)
    assert len(reads) == 1, "a session left out of the record was not read again"


def test_a_files_key_is_its_id_mtime_and_size_and_none_without_an_id():
    assert _file_key(SimpleNamespace(st_ino=7, st_mtime_ns=8, st_size=9)) == (7, 8, 9)
    assert _file_key(SimpleNamespace(st_ino=0, st_mtime_ns=8, st_size=9)) is None
    assert _file_key(None) is None


async def test_a_write_landing_right_after_this_one_is_not_taken_for_it(tmp_path, monkeypatch):
    """Another process's write right after this manager's rename, before anything here looked at the file: the file
    is theirs. The stat that marks this manager's write is the temp file's, taken before the rename."""
    import agent_system.utils.io as io_module

    manager, sid, reads = await _manager(tmp_path)
    copy = await manager.load_session("ada", sid, bypass_cache=True)
    path = manager._get_session_path("ada", sid)
    theirs = await manager.load_session("ada", sid, bypass_cache=True)
    theirs["metadata"]["linked_elsewhere"] = {"by": "another process"}
    rename, landed = io_module.os.replace, []

    def rename_then_theirs(source, target):
        rename(source, target)
        if not landed and str(target) == str(path):
            landed.append(True)
            monkeypatch.setattr(io_module.os, "replace", rename)
            atomic_write_json(path, theirs)
    monkeypatch.setattr(io_module.os, "replace", rename_then_theirs)
    await manager.save_session(copy)
    assert landed, "fixture: the other write did not land"
    reads.clear()
    await manager.save_session(copy)
    assert len(reads) == 1, "the other process's file was taken for this manager's own"
    stored = await manager.load_session("ada", sid, bypass_cache=True)
    assert stored["metadata"]["linked_elsewhere"] == {"by": "another process"}
