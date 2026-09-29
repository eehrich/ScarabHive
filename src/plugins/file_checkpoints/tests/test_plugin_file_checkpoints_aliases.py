"""Third review round: one file under two spellings, throwaway sessions, what a
rewind keeps when it cannot put a file back, and calls that share a record.

Through the real path (file_rewind_rig) on tmp_path. The alias tests need a
case- and normalisation-insensitive file system (macOS' APFS); elsewhere
they are skipped, and say so.
"""
from __future__ import annotations

import os
import unicodedata

import pytest

from agent_system.file_rewind import NOTHING, PARTIAL, REFUSED, REWOUND
from agent_system.hooks import HookContext, HookType
from agent_system.tools.base import ToolServerRegistry
from file_rewind_rig import SESSION, USER, Rig, create, replace, tree
from file_rewind_rig import delete as call_delete


@pytest.fixture
async def rig(tmp_path):
    rig = await Rig(tmp_path).start()
    yield rig
    await rig.close()


def _insensitive(directory) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def _nfd_reaches_nfc(directory) -> bool:
    name = unicodedata.normalize("NFC", "Müller-probe")
    (directory / name).write_text("")
    try:
        return (directory / unicodedata.normalize("NFD", name)).exists()
    finally:
        (directory / name).unlink()


class TestOneFileTwoSpellings:

    async def test_a_case_variant_is_the_same_file_across_turns(self, rig):
        if not _insensitive(rig.work):
            pytest.skip("the file system tells Foo.txt from foo.txt")
        work = rig.work
        (work / "Foo.txt").write_text("PERSON")
        await rig.turn("one", [create(work / "Foo.txt", "A", overwrite=True)])
        await rig.turn("two", [create(work / "foo.txt", "B", overwrite=True)])
        first = (await rig.checkpoints())["checkpoints"][0]["number"]

        report = await rig.rewind(checkpoint=first)

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {"Foo.txt": b"PERSON"}

    async def test_a_case_variant_is_the_same_file_within_a_turn(self, rig):
        if not _insensitive(rig.work):
            pytest.skip("the file system tells Foo.txt from foo.txt")
        work = rig.work
        (work / "Foo.txt").write_text("PERSON")
        await rig.turn("one", [create(work / "Foo.txt", "A", overwrite=True)],
                       [create(work / "foo.txt", "B", overwrite=True)])

        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {"Foo.txt": b"PERSON"}

    async def test_a_file_deleted_under_another_spelling_comes_back_under_its_own(self, rig):
        """When the rewind runs the file is gone, so only the record can say that
        "foo.txt" was "Foo.txt": it has to be written down under one spelling."""
        if not _insensitive(rig.work):
            pytest.skip("the file system tells Foo.txt from foo.txt")
        work = rig.work
        (work / "Foo.txt").write_text("PERSON")
        await rig.turn("one", [create(work / "Foo.txt", "A", overwrite=True)])
        await rig.turn("two", [call_delete(work / "foo.txt")])
        first = (await rig.checkpoints())["checkpoints"][0]["number"]

        report = await rig.rewind(checkpoint=first)

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {"Foo.txt": b"PERSON"}

    async def test_two_spellings_made_in_one_step_are_one_file(self, rig):
        """Both calls are recorded before either runs, when neither spelling
        exists: only the rewind, which finds one file, can tell they are one."""
        if not _insensitive(rig.work):
            pytest.skip("the file system tells New.txt from new.txt")
        work = rig.work
        await rig.turn("create", [create(work / "New.txt", "A", overwrite=True),
                                  create(work / "new.txt", "B", overwrite=True)])

        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {}

    async def test_a_decomposed_umlaut_is_the_same_file(self, rig):
        if not _nfd_reaches_nfc(rig.work):
            pytest.skip("the file system tells NFC from NFD names")
        work = rig.work
        nfc, nfd = unicodedata.normalize("NFC", "Müller.txt"), unicodedata.normalize("NFD", "Müller.txt")
        (work / nfd).write_text("PERSON")                      # as Finder writes it
        await rig.turn("one", [create(work / nfc, "A", overwrite=True)])   # as the model spells it
        await rig.turn("two", [create(work / nfd, "B", overwrite=True)])
        first = (await rig.checkpoints())["checkpoints"][0]["number"]

        report = await rig.rewind(checkpoint=first)

        assert report["status"] == REWOUND, report["text"]
        assert [p.read_bytes() for p in work.iterdir()] == [b"PERSON"]
        assert len(os.listdir(work)) == 1

    async def test_a_case_variant_directory_is_the_same_directory(self, rig):
        if not _insensitive(rig.work):
            pytest.skip("the file system tells Sub from sub")
        work = rig.work
        (work / "Sub").mkdir()
        (work / "Sub" / "a.txt").write_text("PERSON")
        await rig.turn("one", [create(work / "Sub" / "a.txt", "A", overwrite=True)])
        await rig.turn("two", [replace(work / "sub" / "a.txt", "A", "B")])
        first = (await rig.checkpoints())["checkpoints"][0]["number"]

        report = await rig.rewind(checkpoint=first)

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {"Sub": "<dir>", "Sub/a.txt": b"PERSON"}


class TestThrowawaySessions:

    async def test_an_ephemeral_session_records_nothing(self, rig):
        await rig.turn("stateless", [create(rig.work / "x.txt", "X")], session_id="ephemeral-1234")

        assert not rig.store.exists() or list(rig.store.rglob("*")) == [], sorted(rig.store.rglob("*"))

    async def test_a_sub_agent_of_an_ephemeral_run_records_nothing_either(self, rig, tmp_path):
        from agent_system.services.session_manager import SessionManager
        from agent_system.services.session_service import SessionService

        await rig.turn("stateless", [create(rig.work / "x.txt", "X")], session_id="ephemeral-1234")
        child = rig.agent("child")
        sessions = SessionManager(storage_path=str(tmp_path / "sessions"))
        child._session_service = SessionService(sessions)
        await sessions.create_session(user_id=USER, session_id="sub-of-ephemeral", agent_name="child",
                                      llm_profile="normal", parent_session_id="ephemeral-1234")
        await rig.turn("child", [create(rig.work / "y.txt", "Y")], agent=child, session_id="sub-of-ephemeral",
                       request_id=f"{rig.request_ids[-1]}_003_sub_ab12")

        assert not rig.store.exists() or list(rig.store.rglob("*")) == [], sorted(rig.store.rglob("*"))


class TestWhatARewindKeeps:

    async def test_a_file_it_could_not_check_stays_recorded_for_later(self, rig):
        await rig.turn("create", [create(rig.work / "a.txt", "A")])

        first = await rig.rewind(registry=ToolServerRegistry(), overwrite=True)
        still_there = (rig.work / "a.txt").exists()
        second = await rig.rewind()

        assert first["status"] == PARTIAL, first["text"]
        assert still_there
        assert second["status"] == REWOUND, second["text"]
        assert tree(rig.work) == {}

    async def test_a_change_another_call_still_waits_on_is_not_dropped(self, rig):
        """Two calls of one turn on one path; the first changes nothing and
        reports before the second has written (an async sub-agent's failed call
        between its caller's pre and post hooks)."""
        work = rig.work
        target = work / "x.txt"
        target.write_text("ORIGINAL")
        agent = rig.agent()
        await rig.turn("q")
        plugin = rig.plugin

        def ctx(kind, call_id, name, arguments, request_id):
            return HookContext(hook_type=kind, request_id=request_id, session_id=SESSION, user_id=USER,
                               agent=agent, agent_name=agent.name,
                               tool_call={"id": call_id, "name": name, "server": "fs",
                                          "arguments": arguments, "source": "model"})

        failing = {"filePath": str(target), "oldString": "nope", "newString": "zzz"}
        writing = {"operation": "create", "path": str(target), "content": "AGENT", "overwrite": True}
        await plugin.record_before_change(ctx(HookType.PRE_TOOL_CALL, "a", "fs_replace_string_in_file", failing, "r1"))
        await plugin.record_before_change(ctx(HookType.PRE_TOOL_CALL, "b", "fs_manage", writing, "r2"))
        await plugin.record_after_change(ctx(HookType.POST_TOOL_CALL, "a", "fs_replace_string_in_file", failing, "r1"))
        target.write_text("AGENT")
        await plugin.record_after_change(ctx(HookType.POST_TOOL_CALL, "b", "fs_manage", writing, "r2"))

        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert target.read_text() == "ORIGINAL"

    async def test_a_pre_hook_cut_off_leaves_no_count_behind(self, rig, monkeypatch):
        """The registry cancels a hook that runs past its timeout: the record may
        be written, but no post hook will ever come for it."""
        import asyncio

        agent = rig.agent()
        await rig.turn("q")
        release = asyncio.Event()
        real = rig.plugin._record_before

        def slow(*args):
            result = real(*args)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
            return result

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(rig.plugin, "_record_before", slow)
        context = HookContext(hook_type=HookType.PRE_TOOL_CALL, request_id="rc", session_id=SESSION, user_id=USER,
                              agent=agent, agent_name=agent.name,
                              tool_call={"id": "c1", "name": "fs_manage", "server": "fs", "source": "model",
                                         "arguments": {"operation": "create", "path": str(rig.work / "a.txt"),
                                                       "content": "A"}})
        task = asyncio.create_task(rig.plugin.record_before_change(context))
        await asyncio.sleep(0.2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)

        assert rig.plugin._awaiting == {}, "a count no post hook will ever take back"


class TestBetweenPlanAndWrite:

    @pytest.mark.parametrize("step", ["a write", "a removal"])
    async def test_a_file_changed_after_the_plan_is_left_as_it_is(self, rig, monkeypatch, step):
        """The plan reads every path once; a person at work may change one before
        the rewind gets to it. Looked at again right before the step."""
        from plugins.file_checkpoints.hooks import FileCheckpointsPlugin

        work = rig.work
        (work / "a.txt").write_text("before")
        await rig.turn("edit", [replace(work / "a.txt", "before", "agent"), create(work / "new.txt", "agent")])
        touched = work / ("a.txt" if step == "a write" else "new.txt")
        planned = FileCheckpointsPlugin._plan

        def plan_then_edit(self, *args):
            plan = planned(self, *args)
            touched.write_text("the person's")
            return plan

        monkeypatch.setattr(FileCheckpointsPlugin, "_plan", plan_then_edit)
        report = await rig.rewind()

        assert report["status"] == PARTIAL, report["text"]
        assert touched.read_text() == "the person's"
        assert any("changed while the rewind ran" in f["reason"] for f in report["failed"])


class TestForget:

    async def test_forget_removes_the_record_of_that_session_only(self, rig):
        await rig.turn("mine", [create(rig.work / "a.txt", "A")])
        # "a/b" and "a_b" share a directory name (SessionManager's rule); the record says whose it is
        await rig.turn("other", [create(rig.work / "b.txt", "B")], session_id="other-s", user="a/b")

        removed = await rig.plugin.forget(user_id=USER, session_id=SESSION)
        again = await rig.plugin.forget(user_id=USER, session_id=SESSION)
        foreign = await rig.plugin.forget(user_id="a_b", session_id="other-s")

        assert (removed, again, foreign) == (True, False, False)
        assert not (rig.store / USER / SESSION).exists()
        assert (rig.store / "a_b" / "other-s" / "journal.db").exists(), "another user's record went"


class TestFourthRound:

    @pytest.mark.parametrize("spelling", ["case", "NFC vs NFD"])
    async def test_a_root_configured_in_another_spelling_still_holds_its_paths(self, tmp_path, spelling):
        """The sandbox takes a root typed "proj" for a folder stored as "Proj"
        (or "Bücher" composed for one stored decomposed); the record spells
        paths as stored -- the roots must be spelled so too."""
        if spelling == "case":
            stored, configured = "Proj", "proj"
        else:
            stored, configured = (unicodedata.normalize("NFD", "Bücher"), unicodedata.normalize("NFC", "Bücher"))
        (tmp_path / stored).mkdir()
        if not (tmp_path / configured).exists():
            pytest.skip("the file system tells the two spellings apart")
        rig = Rig(tmp_path)
        rig.fs = rig._file_server("fs", [tmp_path / configured])
        rig.registry = ToolServerRegistry()
        rig.registry.register("fs", rig.fs)
        await rig.start()
        try:
            work = tmp_path / configured
            (work / "a.txt").write_text("before")
            await rig.turn("edit", [replace(work / "a.txt", "before", "agent"), create(work / "new.txt", "agent")])

            report = await rig.rewind()

            assert report["status"] == REWOUND, report["text"]
            assert sorted(os.listdir(tmp_path / stored)) == ["a.txt"]
            assert (work / "a.txt").read_text() == "before"
        finally:
            await rig.close()

    async def test_a_file_put_in_under_another_spelling_mid_turn_is_an_outside_change(self, rig):
        """Merged as one file's two spellings, the person's file lost its content."""
        if not _insensitive(rig.work):
            pytest.skip("the file system tells New.txt from NEW.TXT")
        work = rig.work

        def persons_file():
            (work / "New.txt").unlink()
            (work / "NEW.TXT").write_text("PERSON")

        await rig.turn("two steps", [create(work / "New.txt", "A")],
                       [create(work / "New.txt", "B", overwrite=True)], before_round={1: persons_file})

        report = await rig.rewind()

        assert report["status"] == REFUSED, report["text"]
        assert tree(work) == {"NEW.TXT": b"B"}

    async def test_a_file_comes_back_under_the_name_it_had(self, rig):
        if not _insensitive(rig.work):
            pytest.skip("the file system tells Foo.txt from foo.txt")
        work = rig.work
        (work / "Foo.txt").write_text("PERSON")
        await rig.turn("one", [call_delete(work / "Foo.txt")])
        await rig.turn("two", [create(work / "foo.txt", "agent")])
        first = (await rig.checkpoints())["checkpoints"][0]["number"]

        report = await rig.rewind(checkpoint=first)

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {"Foo.txt": b"PERSON"}

    async def test_the_count_is_taken_before_another_calls_post_hook_can_look(self, rig):
        """B's pre hook holds the record lock while A's post hook waits for it;
        the loop is busy when B's thread returns. Counted after the lock, A's post
        hook dropped the row B's call was about to change."""
        import asyncio
        import time

        work = rig.work
        target = work / "x.txt"
        target.write_text("ORIGINAL")
        agent = rig.agent()
        await rig.turn("q")
        plugin = rig.plugin

        def ctx(kind, call_id, name, arguments, request_id):
            return HookContext(hook_type=kind, request_id=request_id, session_id=SESSION, user_id=USER,
                               agent=agent, agent_name=agent.name,
                               tool_call={"id": call_id, "name": name, "server": "fs",
                                          "arguments": arguments, "source": "model"})

        failing = {"filePath": str(target), "oldString": "nope", "newString": "zzz"}
        writing = {"operation": "create", "path": str(target), "content": "AGENT", "overwrite": True}
        await plugin.record_before_change(ctx(HookType.PRE_TOOL_CALL, "a", "fs_replace_string_in_file", failing, "r1"))
        real_estimate = plugin._estimate

        def slow_estimate(*args):
            time.sleep(0.2)
            return real_estimate(*args)

        plugin._estimate = slow_estimate
        task_b = asyncio.create_task(plugin.record_before_change(
            ctx(HookType.PRE_TOOL_CALL, "b", "fs_manage", writing, "r2")))
        await asyncio.sleep(0.05)
        plugin._estimate = real_estimate
        task_a = asyncio.create_task(plugin.record_after_change(
            ctx(HookType.POST_TOOL_CALL, "a", "fs_replace_string_in_file", failing, "r1")))
        await asyncio.sleep(0.01)
        time.sleep(0.5)                      # the loop is busy when B's thread returns
        await task_b
        await task_a
        target.write_text("AGENT")
        await plugin.record_after_change(ctx(HookType.POST_TOOL_CALL, "b", "fs_manage", writing, "r2"))

        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert target.read_text() == "ORIGINAL"

    async def test_a_post_hook_cut_off_before_its_thread_ran_takes_its_count_back(self, rig, monkeypatch):
        import asyncio

        agent = rig.agent()
        await rig.turn("q")
        plugin = rig.plugin
        call = {"id": "c1", "name": "fs_manage", "server": "fs", "source": "model",
                "arguments": {"operation": "create", "path": str(rig.work / "a.txt"), "content": "A"}}

        def ctx(kind):
            return HookContext(hook_type=kind, request_id="rc", session_id=SESSION, user_id=USER,
                               agent=agent, agent_name=agent.name, tool_call=dict(call))

        await plugin.record_before_change(ctx(HookType.PRE_TOOL_CALL))
        assert plugin._awaiting, "fixture: nothing counted"
        # The work item still WAITS in the executor when the hook is cut off:
        # its only worker is busy (aiofiles, another hook). A cancelled work item
        # that never started never runs.
        import threading
        from concurrent.futures import ThreadPoolExecutor

        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        gate = threading.Event()
        busy = loop.run_in_executor(None, gate.wait)
        (rig.work / "a.txt").write_text("A")
        task = asyncio.create_task(plugin.record_after_change(ctx(HookType.POST_TOOL_CALL)))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        await busy
        await asyncio.sleep(0.3)

        assert plugin._awaiting == {}, "a count nobody takes back"

    async def test_a_call_under_way_does_not_bring_back_a_forgotten_record(self, rig):
        """The delete endpoint cancels the session's runs, but a pre hook's thread
        already waiting for the record's lock is not interruptible."""
        from file_rewind_rig import USER as user
        from plugins.file_checkpoints.hooks import RunRef
        from plugins.file_checkpoints.targets import Plan, Target
        from plugins.file_checkpoints.turns import TurnRef

        import time

        await rig.turn("mine", [create(rig.work / "a.txt", "A")])
        under_way = time.monotonic()               # this call started before the delete
        assert await rig.plugin.forget(user_id=user, session_id=SESSION)
        ref = RunRef(user, SESSION, TurnRef("k", (), "q", 0))

        pending = rig.plugin._record_before(ref, "fs", "fs_manage", Plan([Target(rig.work / "a.txt")]), under_way)

        assert pending == []
        assert not (rig.store / user / SESSION).exists(), "the deleted session's record came back"

    async def test_a_new_session_under_a_deleted_sessions_id_is_recorded(self, rig):
        """POST /api/sessions takes a client's own id: free again after a delete."""
        await rig.turn("mine", [create(rig.work / "a.txt", "A")])
        assert await rig.plugin.forget(user_id=USER, session_id=SESSION)
        rig.undo_conversation()                    # the new conversation starts empty

        await rig.turn("again", [create(rig.work / "b.txt", "B")])
        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert tree(rig.work) == {"a.txt": b"A"}

    async def test_a_delete_before_the_first_record_still_stops_the_call_under_way(self, rig):
        import time

        from plugins.file_checkpoints.hooks import RunRef
        from plugins.file_checkpoints.targets import Plan, Target
        from plugins.file_checkpoints.turns import TurnRef

        under_way = time.monotonic()
        assert await rig.plugin.forget(user_id=USER, session_id="fresh-s") is False   # nothing recorded yet

        pending = rig.plugin._record_before(RunRef(USER, "fresh-s", TurnRef("k", (), "q", 0)), "fs", "fs_manage",
                                            Plan([Target(rig.work / "a.txt")]), under_way)

        assert pending == []
        assert not (rig.store / USER / "fresh-s").exists()


class TestForgetWhileAHookRuns:

    async def test_a_pre_hook_under_way_when_the_session_is_deleted_records_nothing(self, rig, monkeypatch):
        import asyncio
        import threading

        agent = rig.agent()
        await rig.turn("q")
        entered, release = threading.Event(), threading.Event()
        real = rig.plugin._record_before

        def held(*args):
            entered.set()
            release.wait(5)
            return real(*args)

        monkeypatch.setattr(rig.plugin, "_record_before", held)
        context = HookContext(hook_type=HookType.PRE_TOOL_CALL, request_id="rf", session_id=SESSION, user_id=USER,
                              agent=agent, agent_name=agent.name,
                              tool_call={"id": "c1", "name": "fs_manage", "server": "fs", "source": "model",
                                         "arguments": {"operation": "create", "path": str(rig.work / "a.txt"),
                                                       "content": "A"}})
        hook = asyncio.create_task(rig.plugin.record_before_change(context))
        await asyncio.to_thread(entered.wait, 5)
        await rig.plugin.forget(user_id=USER, session_id=SESSION)
        release.set()
        await hook

        assert not (rig.store / USER / SESSION).exists(), "the deleted session's record came back"


class TestFifthRound:

    async def test_a_failed_write_after_the_rename_keeps_the_record(self, rig, monkeypatch):
        """Renamed back to "Foo.txt", then the write fails: spelled again after the
        apply, the rows matched nothing kept and the only copy of PERSON went."""
        if not _insensitive(rig.work):
            pytest.skip("the file system tells Foo.txt from foo.txt")
        from plugins.file_checkpoints import fs as fs_module

        work = rig.work
        (work / "Foo.txt").write_text("PERSON")
        await rig.turn("one", [call_delete(work / "Foo.txt")])
        await rig.turn("two", [create(work / "foo.txt", "agent")])
        first = (await rig.checkpoints())["checkpoints"][0]["number"]
        real_write = fs_module.write_file

        def full_disk(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(fs_module, "write_file", full_disk)
        failed = await rig.rewind(checkpoint=first)
        monkeypatch.setattr(fs_module, "write_file", real_write)
        retried = await rig.rewind(checkpoint=first)

        assert failed["status"] == PARTIAL, failed["text"]
        assert retried["status"] == REWOUND, retried["text"]
        assert tree(work) == {"Foo.txt": b"PERSON"}

    async def test_two_spellings_of_one_new_file_edited_again_later_in_the_turn(self, rig):
        if not _insensitive(rig.work):
            pytest.skip("the file system tells New.txt from new.txt")
        work = rig.work
        await rig.turn("create", [create(work / "New.txt", "B", overwrite=True),
                                  create(work / "new.txt", "B", overwrite=True)],
                       [replace(work / "new.txt", "B", "C")])

        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {}

    async def test_a_person_deleting_the_file_mid_turn_is_not_hidden_by_another_spelling(self, rig):
        if not _insensitive(rig.work):
            pytest.skip("the file system tells Foo.txt from foo.txt")
        work = rig.work
        (work / "Foo.txt").write_text("PERSON")

        def persons_delete():
            (work / "Foo.txt").unlink()

        await rig.turn("two steps", [create(work / "Foo.txt", "A", overwrite=True)],
                       [create(work / "foo.txt", "B")], before_round={1: persons_delete})

        report = await rig.rewind()

        assert report["status"] == REFUSED, report["text"]
        assert tree(work) == {"foo.txt": b"B"}

    async def test_a_failing_retention_sweep_does_not_leave_a_call_unconfirmed(self, rig, monkeypatch):
        rig.plugin.retention_days = 30
        rig.plugin._last_retention = 0.0

        def broken():
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(rig.plugin, "_apply_retention", broken)
        await rig.turn("create", [create(rig.work / "a.txt", "A")])

        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert rig.plugin._awaiting == {}
