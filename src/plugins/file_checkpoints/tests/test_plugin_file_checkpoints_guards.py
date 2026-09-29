"""What a rewind refuses, what it leaves alone, and how far the record reaches.

Through the real path (file_rewind_rig): a real agent run, real file_ops, the
plugin's own hooks. Pinned:

* a file changed outside the agent since it wrote it is not overwritten: the
  rewind refuses and touches nothing, ``overwrite`` puts it back anyway;
* a directory is never removed with files in it the agent did not create;
* the bounds hold: turns per session, bytes per file, entries per call,
  bytes per session -- and what was not kept is named, not guessed;
* a rewind writes only inside the directories of the server that recorded the
  path, only for the user whose session it is, and never while a run of the
  session is still going;
* a sub-agent's changes belong to the turn that started it;
* calls that change files unseen (a shell) are named in the report;
* an agent with only one of the two hooks on records nothing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.file_rewind import NOTHING, PARTIAL, REFUSED, REWOUND, UNKNOWN_CHECKPOINT
from agent_system.tools.base import ToolServer, ToolServerRegistry
from file_rewind_rig import (AFTER, BEFORE, SESSION, USER, Rig, ScriptedModel, call, create, delete,
                             replace, tree)


@pytest.fixture
async def rig(tmp_path):
    rig = await Rig(tmp_path).start()
    yield rig
    await rig.close()


@pytest.fixture
async def make_rig(tmp_path):
    made = []

    async def make(**config):
        rig = await Rig(tmp_path).start(**config)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        await rig.close()


class TestChangedOutsideTheAgent:

    async def test_a_file_edited_since_is_refused_and_nothing_is_touched(self, rig):
        work = rig.work
        (work / "a.txt").write_text("original a")
        await rig.turn("edit", [replace(work / "a.txt", "original", "agent"),
                                create(work / "b.txt", "agent b")])
        (work / "a.txt").write_text("the person's own edit")

        report = await rig.rewind()

        assert report["status"] == REFUSED, report["text"]
        assert [c["path"] for c in report["conflicts"]] == [str(work / "a.txt")]
        assert (work / "a.txt").read_text() == "the person's own edit"
        assert (work / "b.txt").read_text() == "agent b", "a refused rewind removed a file anyway"
        assert "overwrite" in report["text"]

        report = await rig.rewind(overwrite=True)

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {"a.txt": b"original a"}

    async def test_a_file_created_by_the_agent_and_edited_since_is_not_deleted(self, rig):
        work = rig.work
        await rig.turn("create", [create(work / "new.txt", "agent")])
        (work / "new.txt").write_text("the person kept working in it")

        report = await rig.rewind()

        assert report["status"] == REFUSED
        assert (work / "new.txt").read_text() == "the person kept working in it"

    async def test_an_edit_between_two_turns_is_caught_when_both_are_rewound(self, rig):
        work = rig.work
        (work / "f.txt").write_text("v0")
        await rig.turn("one", [replace(work / "f.txt", "v0", "v1")])
        (work / "f.txt").write_text("v1 and the person's line")
        await rig.turn("two", [replace(work / "f.txt", "v1", "v2")])

        refused = await rig.rewind(checkpoint=1)
        only_two = await rig.rewind()

        assert refused["status"] == REFUSED, refused["text"]
        assert "between turns" in refused["conflicts"][0]["reason"]
        assert only_two["status"] == REWOUND, only_two["text"]
        assert (work / "f.txt").read_text() == "v1 and the person's line"

    async def test_an_edit_made_while_the_turn_runs_is_not_overwritten(self, rig):
        """Between two edits of the agent, the person fixes a line of the same
        file. The turn's record kept only its first state -- without a look at
        the file before the second edit, the fix went with the rewind."""
        work = rig.work
        main = work / "main.py"
        main.write_text("agent: a\nperson: todo\nagent2: x\n")

        def persons_fix():
            main.write_text(main.read_text().replace("person: todo", "person: MY FIX"))

        await rig.turn("edit twice", [replace(main, "agent: a", "agent: b")],
                       [replace(main, "agent2: x", "agent2: y")], before_round={1: persons_fix})
        refused = await rig.rewind()
        forced = await rig.rewind(overwrite=True)

        assert refused["status"] == REFUSED, refused["text"]
        assert "while the turn was running" in refused["conflicts"][0]["reason"]
        assert forced["status"] == REWOUND
        assert main.read_text() == "agent: a\nperson: todo\nagent2: x\n"

    async def test_a_helper_writing_the_file_its_caller_just_wrote_is_no_outside_edit(self, rig):
        """The caller's post hooks come after every call of the step -- the
        helper's run included -- so the helper finds the caller's write not yet
        confirmed. That is the agent's own work, not a change from outside."""
        work = rig.work
        target = work / "p.txt"
        helper = rig.agent("helper")
        helper.llm = ScriptedModel([replace(target, "v2", "v3")])
        helper._tool_visible = True
        rig.registry.register("helper", helper)
        coder = rig.agent("delegating", allowed=["fs/*", "helper"])

        await rig.turn("work", [create(target, "v1")],
                       [replace(target, "v1", "v2"), call("helper", task="bump it")], agent=coder)
        report = await rig.rewind(agent=coder)

        assert helper.llm.calls == 2, "the helper never ran"
        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {}

    async def test_a_directory_with_files_the_agent_did_not_create_is_kept(self, rig):
        work = rig.work
        await rig.turn("make a dir", [create(work / "d" / "agent.txt", "a")])
        (work / "d" / "mine.txt").write_text("not the agent's")

        refused = await rig.rewind()
        forced = await rig.rewind(overwrite=True)

        assert refused["status"] == REFUSED
        # The directory stays -- and stays recorded: once the person clears it, a rewind finishes it.
        assert forced["status"] == PARTIAL, forced["text"]
        assert tree(work) == {"d": "<dir>", "d/mine.txt": b"not the agent's"}, (
            "the rewind removed a file it had no record of")
        assert any("did not create" in u["reason"] for u in forced["unrestorable"])
        (work / "d" / "mine.txt").unlink()
        assert (await rig.rewind())["status"] == REWOUND
        assert tree(work) == {}


class TestBounds:

    async def test_only_the_newest_turns_are_kept(self, make_rig):
        rig = await make_rig(max_checkpoints=2)
        work = rig.work
        for name in ("one", "two", "three"):
            await rig.turn(name, [create(work / f"{name}.txt", name)])

        listing = (await rig.checkpoints())["checkpoints"]
        forgotten = await rig.rewind(checkpoint=1)
        report = await rig.rewind(checkpoint=2)

        # Numbered by the record's own sequence: forgetting turn one shifts nothing.
        assert [(c["number"], c["question"]) for c in listing] == [(2, "two"), (3, "three")]
        assert forgotten["status"] == UNKNOWN_CHECKPOINT
        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {"one.txt": b"one"}, "a forgotten turn was rewound anyway"

    async def test_a_file_too_large_to_keep_is_named_not_guessed(self, make_rig):
        rig = await make_rig(max_file_bytes=64)
        work = rig.work
        (work / "big.txt").write_text("x" * 1000)
        (work / "small.txt").write_text("s")
        await rig.turn("overwrite", [create(work / "big.txt", "short now", overwrite=True),
                                     replace(work / "small.txt", "s", "S")])

        refused = await rig.rewind()
        forced = await rig.rewind(overwrite=True)

        assert refused["status"] == REFUSED
        assert [u["path"] for u in refused["unrestorable"]] == [str(work / "big.txt")]
        assert "not kept" in refused["unrestorable"][0]["reason"]
        assert forced["status"] == REWOUND
        assert (work / "small.txt").read_text() == "s"
        assert (work / "big.txt").read_text() == "short now", "a file with no copy was written"
        assert not any(p.stat().st_size >= 1000 for p in (rig.store).rglob("*") if p.is_file()
                       and p.parent.name == "blobs"), "the large file was kept after all"

    async def test_a_directory_beyond_the_entry_limit_is_recorded_as_not_kept(self, make_rig):
        rig = await make_rig(max_entries_per_call=3)
        work = rig.work
        (work / "many").mkdir()
        for i in range(10):
            (work / "many" / f"{i}.txt").write_text(str(i))
        await rig.turn("wipe", [delete(work / "many", recursive=True)])

        report = await rig.rewind()

        assert report["status"] == REFUSED
        assert [u["path"] for u in report["unrestorable"]] == [str(work / "many")]
        assert "not kept" in report["unrestorable"][0]["reason"]

    async def test_older_turns_make_room_within_the_session_bytes(self, make_rig):
        rig = await make_rig(max_session_bytes=150)
        work = rig.work
        (work / "a.txt").write_text("a" * 100)
        (work / "b.txt").write_text("b" * 100)
        await rig.turn("one", [create(work / "a.txt", "A", overwrite=True)])
        await rig.turn("two", [create(work / "b.txt", "B", overwrite=True)])

        listing = (await rig.checkpoints())["checkpoints"]
        blobs = sum(p.stat().st_size for p in rig.store.rglob("blobs/*"))

        assert [c["question"] for c in listing] == ["two"]
        assert blobs <= 150


class TestRoom:

    async def test_a_file_edited_again_in_a_turn_takes_no_room_from_older_turns(self, make_rig):
        rig = await make_rig(max_session_bytes=250)
        work = rig.work
        (work / "a.txt").write_text("a" * 100)
        (work / "b.txt").write_text("b" * 100)
        await rig.turn("one", [create(work / "a.txt", "A" * 100, overwrite=True)])
        await rig.turn("two", [create(work / "b.txt", "B" * 100, overwrite=True)],
                       [create(work / "b.txt", "C" * 100, overwrite=True)])

        listing = (await rig.checkpoints())["checkpoints"]

        assert [c["question"] for c in listing] == ["one", "two"], "turn one gave way for nothing"

    async def test_a_grandchild_of_an_earlier_branch_stands_before_a_later_sibling(self, rig):
        """X, then Y on top of it, both dropped without their files; then Z where
        X stood, dropped too; then W. y.txt was on disk when Z began: rewinding to
        before Z must keep it."""
        w = rig.work
        await rig.turn("base", [create(w / "base.txt", "0")])
        await rig.turn("X", [create(w / "x.txt", "x")])
        await rig.turn("Y", [create(w / "y.txt", "y")])
        rig.undo_conversation()
        rig.undo_conversation()
        await rig.turn("Z", [create(w / "z.txt", "z")])
        rig.undo_conversation()
        await rig.turn("W", [create(w / "w.txt", "w")])
        listing = (await rig.checkpoints())["checkpoints"]
        z = next(c["number"] for c in listing if c["question"] == "Z")

        report = await rig.rewind(checkpoint=z)

        assert [c["question"] for c in listing] == ["base", "X", "Y", "Z", "W"]
        assert report["status"] == REWOUND, report["text"]
        assert sorted(tree(w)) == ["base.txt", "x.txt", "y.txt"]

    async def test_dropped_turns_behind_one_turn_keep_the_order_they_were_asked_in(self, rig):
        """A turn's record is made by its first change -- an async sub-agent of an
        earlier turn may make it after a later turn made its own. Dropped behind
        the same turn, the earlier one still comes first."""
        from plugins.file_checkpoints.store import Journal
        from plugins.file_checkpoints.turns import TurnRef, head_key

        await rig.turn("base", [create(rig.work / "base.txt", "0")])
        messages = rig.messages()
        base = head_key([m for m in messages if m.role == "user"][0])
        journal = Journal(rig.store, USER, "rewind-s1")
        with journal.connect() as conn:
            # "late" (asked second) is recorded first, "early" (asked first) after it
            late = Journal.turn_seq(conn, TurnRef("late", (base, "early"), "late", 2), create=True)
            early = Journal.turn_seq(conn, TurnRef("early", (base,), "early", 1), create=True)
            Journal.note_untracked(conn, late, "shell_execute")
            Journal.note_untracked(conn, early, "shell_execute")

        listing = (await rig.checkpoints())["checkpoints"]

        assert [c["question"] for c in listing] == ["base", "early", "late"]
        assert late < early, "fixture: the later turn's record must be the older one"


class TestMemory:

    def test_the_turn_cache_holds_a_bounded_number_of_sessions(self, rig):
        """It keeps message objects alive -- multimodal ones too -- after the
        tracker let go of them; bounded, a long-running API cannot grow it."""
        from types import SimpleNamespace

        from agent_system.llm.models import ChatMessage
        from plugins.file_checkpoints.hooks import MAX_CACHED_SESSIONS

        conversations = {f"s{i}": [ChatMessage(role="user", content=f"q{i}")]
                         for i in range(MAX_CACHED_SESSIONS + 50)}
        agent = SimpleNamespace(_session_tracker=SimpleNamespace(
            get_session_messages=lambda sid: conversations[sid]))
        for session_id in conversations:
            assert rig.plugin._turn_of(agent, session_id) is not None

        assert len(rig.plugin._turn_cache) == MAX_CACHED_SESSIONS


class TestRetention:
    """Deleting records by age is the operator's decision: off unless configured."""

    @staticmethod
    def _record(store: Path, user: str, session: str, days_old: float) -> Path:
        directory = store / user / session
        directory.mkdir(parents=True)
        (directory / "journal.db").write_bytes(b"")
        stamp = time.time() - days_old * 86400
        os.utime(directory / "journal.db", (stamp, stamp))
        return directory

    async def test_records_older_than_the_retention_go_the_rest_stay(self, tmp_path):
        store = tmp_path / "store"
        old = self._record(store, USER, "old-s", days_old=10)
        fresh = self._record(store, USER, "fresh-s", days_old=1)

        rig = await Rig(tmp_path).start(retention_days=5)
        await rig.close()

        assert not old.exists()
        assert fresh.exists()

    async def test_nothing_goes_by_age_unless_configured(self, tmp_path):
        store = tmp_path / "store"
        old = self._record(store, USER, "old-s", days_old=400)

        rig = await Rig(tmp_path).start()
        await rig.close()

        assert old.exists()


class TestIsolation:

    async def test_another_users_rewind_finds_nothing_and_touches_nothing(self, rig):
        await rig.turn("create", [create(rig.work / "a.txt", "A")])

        report = await rig.rewind(user="bob")
        listing = await rig.checkpoints(user="bob")

        assert report["status"] == NOTHING
        assert listing["checkpoints"] == []
        assert (rig.work / "a.txt").read_text() == "A"

    async def test_a_record_written_for_another_user_is_refused(self, rig):
        """``a/b`` and ``a_b`` share a directory name (SessionManager's rule);
        the record says whose it is."""
        await rig.turn("create", [create(rig.work / "a.txt", "A")], user="a/b")

        report = await rig.rewind(user="a_b")

        assert report["status"] == REFUSED
        assert (rig.work / "a.txt").read_text() == "A"

    async def test_a_path_outside_the_servers_directories_now_is_not_written(self, rig, tmp_path):
        await rig.turn("create", [create(rig.work / "a.txt", "A")])
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        registry = ToolServerRegistry()
        registry.register("fs", rig._file_server("fs_moved", [elsewhere]))

        report = await rig.rewind(registry=registry)
        unloaded = await rig.rewind(registry=ToolServerRegistry())

        assert report["status"] == REFUSED
        assert "no longer inside" in report["unrestorable"][0]["reason"]
        assert "not loaded" in unloaded["unrestorable"][0]["reason"]
        assert (rig.work / "a.txt").read_text() == "A"

    async def test_a_directory_swapped_for_a_link_out_of_the_root_is_not_followed(self, rig, tmp_path):
        work = rig.work
        (work / "d").mkdir()
        (work / "d" / "f.txt").write_text("before")
        await rig.turn("edit", [replace(work / "d" / "f.txt", "before", "after")])
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "f.txt").write_text("after")      # what the agent left, so no conflict hides it
        os.rename(work / "d", tmp_path / "d_moved")
        os.symlink(outside, work / "d")

        report = await rig.rewind(overwrite=True)

        assert (outside / "f.txt").read_text() == "after", "the rewind wrote outside the root"
        assert any("no longer inside" in u["reason"] for u in report["unrestorable"])

    @pytest.mark.parametrize("link_to", ["outside the root", "outside the root, empty", "inside the root",
                                         "inside the root, empty"])
    async def test_a_link_this_rewind_puts_back_carries_no_write(self, rig, tmp_path, link_to):
        """D/l was a directory when the agent deleted D/l/x, a link when it deleted
        D. Rewinding both restores the link first -- the file below it must not be
        written through it: out of the root (into an empty directory too, where no
        changed file gives it away), or onto a file of the person's in it."""
        work = rig.work
        elsewhere = work / "other" if link_to.startswith("inside") else tmp_path / "outside"
        elsewhere.mkdir()
        if not link_to.endswith("empty"):
            (elsewhere / "x").write_text("the person's")
        (work / "D" / "l").mkdir(parents=True)
        (work / "D" / "l" / "x").write_text("the agent's")
        await rig.turn("one", [delete(work / "D" / "l" / "x")])
        os.rmdir(work / "D" / "l")
        os.symlink(elsewhere, work / "D" / "l")
        await rig.turn("two", [delete(work / "D", recursive=True)])

        report = await rig.rewind(checkpoint=1)

        if link_to.endswith("empty"):
            assert os.listdir(elsewhere) == [], "the rewind wrote through the link"
        else:
            assert (elsewhere / "x").read_text() == "the person's", "the rewind wrote through the link"
        assert report["status"] == PARTIAL, report["text"]
        assert [f["path"] for f in report["failed"]] == [str(work / "D" / "l" / "x")]
        assert (work / "D" / "l").is_symlink(), "the link itself was not put back"

    async def test_no_rewind_while_a_run_of_the_session_is_going(self, rig):
        release = asyncio.Event()

        class _Waiter(ToolServer):
            def __init__(self):
                super().__init__("probe", AgentSystemConfig(), ToolServerConfig(type="probe", enabled=True))

            def get_tools(self):
                return [{"type": "function", "function": {"name": "probe_wait", "description": "Wait.",
                                                          "parameters": {"type": "object", "properties": {}}}}]

            async def probe_wait(self, params):
                await release.wait()
                return {"status": "ok"}

        rig.registry.register("probe", _Waiter())
        agent = rig.agent("waiting", allowed=["fs/*", "probe/*"])
        await rig.turn("write", [create(rig.work / "a.txt", "A")], agent=agent)
        running = asyncio.create_task(rig.turn("wait", [call("probe_wait")], agent=agent))
        for _ in range(200):
            if len(rig.request_ids) == 2 and rig.plugin._live_runs((USER, SESSION)):
                break
            await asyncio.sleep(0.01)

        report = await rig.rewind(agent=agent)
        release.set()
        await running

        assert report["status"] == REFUSED, report["text"]
        assert "still going" in report["text"]
        assert (rig.work / "a.txt").read_text() == "A"
        assert (await rig.rewind(checkpoint=1, agent=agent))["status"] == REWOUND


class TestReach:

    @staticmethod
    async def _sub_session(rig, tmp_path, agent, sub_id, parent):
        """A sub-session as the sub-agent manager creates it: a record naming its parent."""
        from agent_system.services.session_manager import SessionManager
        from agent_system.services.session_service import SessionService

        sessions = SessionManager(storage_path=str(tmp_path / "sessions"))
        agent._session_service = SessionService(sessions)
        record = await sessions.create_session(user_id=USER, session_id=sub_id, agent_name=agent.name,
                                               llm_profile="normal", parent_session_id=parent)
        assert (record.get("parent_session") or {}).get("session_id") == parent, record.get("parent_session")

    async def test_a_sub_agents_changes_belong_to_the_turn_that_started_it(self, rig, tmp_path):
        await rig.turn("delegate", [create(rig.work / "parent.txt", "P")])
        parent_run = rig.request_ids[-1]
        child = rig.agent("child")
        await self._sub_session(rig, tmp_path, child, "child-s", SESSION)

        await rig.turn("child task", [create(rig.work / "child.txt", "C")], agent=child,
                       session_id="child-s", request_id=f"{parent_run}_003_sub_x1")
        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert tree(rig.work) == {}, "the sub-agent's file outlived the turn that asked for it"
        assert not (rig.store / USER / "child-s").exists(), "the sub-agent kept a record of its own"

    async def test_a_sub_session_filed_after_a_run_on_it_was_asked_about_belongs_to_its_parent(self, rig, tmp_path):
        """A run on the session before its record was filed found no parent -- an
        agent called as a tool opens its session a moment before it files it --
        and that answer was kept for the life of the process: every later run on
        the session recorded in a journal of its own, out of the calling turn."""
        from agent_system.services.session_manager import SessionManager
        from agent_system.services.session_service import SessionService

        await rig.turn("delegate", [create(rig.work / "parent.txt", "P")])
        parent_run = rig.request_ids[-1]
        child = rig.agent("child")
        sessions = SessionManager(storage_path=str(tmp_path / "sessions"))
        child._session_service = SessionService(sessions)
        await rig.turn("before its record", [create(rig.work / "early.txt", "E")], agent=child,
                       session_id="child-late", request_id=f"{parent_run}_003_sub_x1")
        record = await sessions.load_session(USER, "child-late")  # its first save made it; now it names its parent
        record["parent_session"] = {"session_id": SESSION}
        await sessions.save_session(record)

        await rig.turn("after it", [create(rig.work / "late.txt", "L")], agent=child,
                       session_id="child-late", request_id=f"{parent_run}_004_sub_x2")
        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert "late.txt" not in tree(rig.work), "the run after the filing recorded out of the calling turn"

    async def test_a_run_whose_id_only_looks_like_a_sub_run_records_in_its_own_session(self, rig, tmp_path):
        """A client may choose its request ids: "client-run_2" in another session,
        or a sub-run id in a session that is not a sub-session of the run's."""
        await rig.turn("session A", [create(rig.work / "a.txt", "A")], request_id="client-run")
        other = rig.agent("other")
        await self._sub_session(rig, tmp_path, other, "unrelated-s", "somebody-else")

        await rig.turn("session B", [create(rig.work / "b.txt", "B")], agent=other,
                       session_id="sess-b", request_id="client-run_2")
        await rig.turn("session C", [create(rig.work / "c.txt", "C")], agent=other,
                       session_id="unrelated-s", request_id="client-run_004")
        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert tree(rig.work) == {"b.txt": b"B", "c.txt": b"C"}, "another session's files went with A's turn"
        assert (rig.store / USER / "sess-b" / "journal.db").exists()
        assert (rig.store / USER / "unrelated-s" / "journal.db").exists()

    async def test_a_later_run_of_the_same_session_is_its_own_turn_whatever_its_id(self, rig):
        """ "client-run_002" after "client-run" in one session: the next turn, not a
        call of the first, which has ended -- filed under turn one, its change
        survived the rewind of turn two."""
        await rig.turn("one", [create(rig.work / "a.txt", "A")], request_id="client-run")
        # "_002" is the shape of a tool call of "client-run" -- which has ended
        await rig.turn("two", [create(rig.work / "b.txt", "B")], request_id="client-run_002")

        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert tree(rig.work) == {"a.txt": b"A"}

    @pytest.mark.parametrize("helper_records", [True, False])
    async def test_an_agent_called_as_a_tool_records_into_the_turn_or_is_named(self, rig, helper_records):
        """With the hooks, a helper's changes are the calling turn's; without
        them they are not recorded -- and the call that started it is named."""
        helper = rig.agent("helper", hooks=None if helper_records else {})
        helper.llm = ScriptedModel([create(rig.work / "by_helper.txt", "H")])
        helper._tool_visible = True          # metadata.visibility: tool
        rig.registry.register("helper", helper)
        coder = rig.agent("delegating", allowed=["fs/*", "helper"])

        await rig.turn("delegate", [create(rig.work / "by_coder.txt", "C"), call("helper", task="write it")],
                       agent=coder)
        report = await rig.rewind(agent=coder)

        assert helper.llm.calls == 2, "the helper never ran"
        assert (rig.work / "by_coder.txt").exists() is False
        assert report["status"] == REWOUND, report["text"]
        if helper_records:
            assert tree(rig.work) == {}, "the helper's file outlived the turn that asked for it"
            assert report["untracked"] == []
        else:
            assert tree(rig.work) == {"by_helper.txt": b"H"}
            assert report["untracked"] == [
                {"tool": "helper (agent 'helper' records no checkpoints)", "count": 1}]

    async def test_a_sub_agent_manager_spawn_of_an_agent_without_checkpoints_is_named(self, rig, tmp_path):
        """create names its agent_type, continue the agent of the caller's
        sub-session; an agent that records, or one the manager would refuse, is
        not named. The hook runs before the call, so the manager itself is a stub
        of its type."""
        from agent_system.hooks import HookContext, HookType
        from agent_system.services.session_manager import SessionManager
        from agent_system.services.session_service import SessionService

        class _Manager(ToolServer):
            def __init__(self):
                super().__init__("sam", AgentSystemConfig(),
                                 ToolServerConfig(type="sub_agent_manager", enabled=True))

        rig.registry.register("sam", _Manager())
        rig.registry.register("silent", rig.agent("silent", hooks={}))
        rig.registry.register("careful", rig.agent("careful"))
        coder = rig.agent()
        sessions = SessionManager(storage_path=str(tmp_path / "sessions"))
        coder._session_service = SessionService(sessions)
        for sub, agent_name, parent in (("sub-silent", "silent", SESSION), ("sub-careful", "careful", SESSION),
                                        ("sub-other", "silent", "another-session")):
            record = await sessions.create_session(user_id=USER, session_id=sub, agent_name=agent_name,
                                                   llm_profile="p")
            record["parent_session"] = {"session_id": parent}
            await sessions.save_session(record)
        await rig.turn("delegate", [create(rig.work / "a.txt", "A")])

        async def spawn(**arguments):
            await rig.plugin.record_before_change(HookContext(
                hook_type=HookType.PRE_TOOL_CALL, request_id=rig.request_ids[-1], session_id=SESSION,
                agent=coder, agent_name=coder.name, user_id=USER,
                tool_call={"id": None, "name": "sam_manage_sub_agent", "server": "sam",
                           "arguments": arguments, "source": "model"}))

        await spawn(operation="create", agent_type="silent", task="t")
        await spawn(agent_type="careful", task="t")                      # create, inferred
        await spawn(operation="create", agent_type="nobody", task="t")   # refused by the manager
        await spawn(instance_id="sub-silent", message="more")            # continue, inferred
        await spawn(operation="continue", instance_id="sub-careful", message="more")
        await spawn(operation="continue", instance_id="sub-other", message="more")  # not this session's
        listing = await rig.checkpoints()

        assert listing["checkpoints"][0]["untracked"] == [
            {"tool": "sam_manage_sub_agent (agent 'silent' records no checkpoints)", "count": 2}]

    async def test_a_scripts_calls_are_recorded_like_the_models(self, rig):
        await rig.turn("prepare", [create(rig.work / "a.txt", "A")])
        agent = rig.agent()

        result = await agent.dispatch_tool_call(
            "fs_manage", {"operation": "create", "path": str(rig.work / "s.txt"), "content": "S"},
            session_id=SESSION, user_id=USER, request_id=f"{rig.request_ids[-1]}_004",
            hook_source="tool_script")
        report = await rig.rewind()

        assert result.get("status") == "success", result
        assert report["status"] == REWOUND, report["text"]
        assert tree(rig.work) == {}

    async def test_a_shell_call_is_named_in_the_report(self, rig):
        class _Shell(ToolServer):
            def __init__(self, work: Path):
                super().__init__("shell", AgentSystemConfig(), ToolServerConfig(type="terminal", enabled=True))
                self.work = work

            def get_tools(self):
                return [{"type": "function", "function": {"name": "shell_execute", "description": "Run.",
                                                          "parameters": {"type": "object", "properties": {}}}}]

            async def execute(self, params):
                (self.work / "made_by_shell.txt").write_text("shell")
                return {"status": "success"}

            async def shell_execute(self, params):
                return await self.execute(params)

        rig.registry.register("shell", _Shell(rig.work))
        agent = rig.agent("with_shell", allowed=["fs/*", "shell/*"])
        await rig.turn("build", [create(rig.work / "a.txt", "A"), call("shell_execute")], agent=agent)

        listing = await rig.checkpoints(agent=agent)
        report = await rig.rewind(agent=agent)

        assert listing["checkpoints"][0]["untracked"] == [{"tool": "shell_execute", "count": 1}]
        assert report["status"] == REWOUND
        assert report["untracked"] == [{"tool": "shell_execute", "count": 1}]
        assert "shell_execute x1" in report["text"]
        assert tree(rig.work) == {"made_by_shell.txt": b"shell"}, "an untracked change was touched"

    async def test_one_hook_without_the_other_records_nothing(self, rig, caplog):
        agent = rig.agent("half", hooks={BEFORE: {"enabled": True}})

        with caplog.at_level(logging.ERROR, logger="plugins.file_checkpoints.hooks"):
            await rig.turn("create", [create(rig.work / "a.txt", "A")], agent=agent)

        assert not rig.store.exists() or not any(rig.store.rglob("journal.db"))
        assert any(AFTER in r.getMessage() for r in caplog.records), "the misconfiguration went unsaid"

    async def test_an_agent_without_the_hooks_records_nothing(self, rig):
        agent = rig.agent("plain", hooks={})
        await rig.turn("create", [create(rig.work / "a.txt", "A")], agent=agent)

        assert not rig.store.exists() or not any(rig.store.rglob("journal.db"))
        assert (await rig.rewind(agent=agent))["status"] == NOTHING
