"""The semantic index catches up before it answers, and tells the agent what changed.

12.11.2026: a search answered from the index as the last pass left it -- a file
copied in a minute ago did not exist for it, and nothing said so. Now the disk
is compared with the stamps of the last complete pass (0.3 s on the coder
tree), a pass that takes seconds is waited for, a longer one is reported with
its progress, and a hook lists the files that changed since the agent's
previous turn.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from agent_system.llm.models import ChatMessage
from plugins.file_ops import search, textsearch
from plugins.file_ops import server as server_module
from plugins.file_ops.server import FileOpsServer

pytestmark = pytest.mark.slow

AUTH = '''\
def verify_credentials(username, password):
    """Check a name and a secret against the user store."""
    return True
'''

TEA = '''\
def steep(leaves, minutes):
    """Leave the leaves in the water for a while."""
    return leaves
'''

KETTLE = '''\
def boil_the_kettle(volume):
    """Heat the water until it bubbles for the tea."""
    return volume
'''


def age(root: Path, seconds: float = 600) -> None:
    """Move every mtime below *root* into the past: out of the racy window."""
    past = time.time() - seconds
    for path in [root, *root.rglob("*")]:
        os.utime(path, (past, past))


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    (root / "pkg").mkdir(parents=True)
    (root / "auth.py").write_text(AUTH)
    (root / "pkg" / "tea.py").write_text(TEA)
    (root / ".gitignore").write_text("ignored/\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "x.py").write_text(KETTLE)
    (root / "logo.bin").write_bytes(b"\x89PNG\x00\x00\x01")
    (root / "empty.txt").write_text("")
    age(root)
    return root


def build_server(tmp_path: Path, roots, name: str = "idx", **search_config) -> FileOpsServer:
    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(r) for r in roots]
    server_config.search = {
        "enable_semantic_search": True,
        "enable_indexing": True,
        "index_on_startup": False,   # the tests drive every pass themselves
        "chroma_db_path": str(tmp_path / "store"),
        **search_config,
    }
    return FileOpsServer(name, Mock(spec=AgentSystemConfig), server_config)


async def built(tmp_path, tree, *more_roots, **search_config) -> FileOpsServer:
    server = build_server(tmp_path, [tree, *more_roots], **search_config)
    await server.search_engine.rebuild_index(incremental=False)
    return server


def changes(engine) -> int:
    return search._changes_since(engine._stamps, engine._state_path)


# --------------------------------------------------------------------------
# The quick check
# --------------------------------------------------------------------------

def _create(tree, tmp_path):
    (tree / "pkg" / "new.py").write_text(KETTLE)


def _copy_with_old_mtime(tree, tmp_path):
    source = tmp_path / "outside.py"
    source.write_text(KETTLE)
    os.utime(source, (1_000_000_000, 1_000_000_000))
    shutil.copy2(source, tree / "pkg" / "copied.py")


def _copy_over_with_old_mtime(tree, tmp_path):
    source = tmp_path / "outside.py"
    source.write_text(KETTLE)
    os.utime(source, (1_000_000_000, 1_000_000_000))
    shutil.copy2(source, tree / "pkg" / "tea.py")


def _rename(tree, tmp_path):
    (tree / "pkg" / "tea.py").rename(tree / "pkg" / "leaves.py")


def _delete(tree, tmp_path):
    (tree / "pkg" / "tea.py").unlink()


def _edit(tree, tmp_path):
    (tree / "auth.py").write_text(AUTH + "\n# edited\n")


def _edit_ignore_file(tree, tmp_path):
    (tree / ".gitignore").write_text("")


def _binary_becomes_text(tree, tmp_path):
    (tree / "logo.bin").write_text("now text")


class TestTheQuickCheck:

    @pytest.mark.parametrize("change", [
        _create, _copy_with_old_mtime, _copy_over_with_old_mtime, _rename, _delete,
        _edit, _edit_ignore_file, _binary_becomes_text])
    async def test_it_sees_every_kind_of_change(self, tmp_path, tree, change, monkeypatch):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        assert changes(engine) == 0, "nothing changed yet"

        change(tree, tmp_path)

        assert changes(engine) > 0
        await engine.stop()

    async def test_a_root_that_appears_later_counts(self, tmp_path, tree):
        later = tmp_path / "later"
        server = await built(tmp_path, tree, later)
        engine = server.search_engine
        assert changes(engine) == 0

        later.mkdir()

        assert changes(engine) > 0
        await engine.stop()

    async def test_another_process_writing_the_state_counts(self, tmp_path, tree):
        """It may have emptied the collection with no file changing at all."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        assert changes(engine) == 0

        engine._state_path.write_text(engine._state_path.read_text(encoding="utf-8") + " ",
                                      encoding="utf-8")

        assert changes(engine) > 0
        await engine.stop()

    async def test_a_stamp_inside_the_racy_window_counts(self, tmp_path, tree):
        """A change in the same timestamp tick leaves the mtime as it was."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        path = tree / "auth.py"
        racy = engine._stamps.taken_at - 1
        os.utime(path, (racy, racy))
        engine._stamps = search._Stamps(
            taken_at=engine._stamps.taken_at, entries=engine._stamps.entries,
            files={**engine._stamps.files, path: os.stat(path).st_mtime},
            state=engine._stamps.state)

        assert changes(engine) == 1
        await engine.stop()

    async def test_a_pass_whose_mend_dropped_files_leaves_no_stamps(self, tmp_path, tree):
        """A mend drops files without a file changing; a pass dying after it
        left stamps that still matched the disk -- and the index lacked them."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        lost = engine.file_symbol_ids[tree / "auth.py"][0]
        engine._vector_store.delete(engine._collection_name, [lost])

        def dies(*args):
            raise RuntimeError("the pass ends here")

        engine._walk_for_index = dies
        with pytest.raises(RuntimeError):
            await engine.rebuild_index(incremental=True)

        assert tree / "auth.py" not in engine.file_mtimes, "the mend did not run"
        assert engine._stamps is None
        await engine.stop()


# --------------------------------------------------------------------------
# What a pass reads
# --------------------------------------------------------------------------

class TestWhatAPassReads:

    async def test_binary_and_empty_files_are_looked_at_once(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        assert {tree / "logo.bin", tree / "empty.txt"} <= set(engine.file_skipped)
        read = []
        original = engine._prepare_for_index

        def spy(chunk, *args):
            read.extend(path for path, _ in chunk)
            return original(chunk, *args)

        engine._prepare_for_index = spy
        await engine.rebuild_index(incremental=True)

        assert read == [], f"read again though unchanged: {read}"
        await engine.stop()

    async def test_an_unreadable_file_is_tried_again(self, tmp_path, tree, monkeypatch):
        """A locked file read as "binary" was left out until it changed."""
        locked = tree / "pkg" / "tea.py"
        verdict = textsearch.text_verdict
        monkeypatch.setattr(textsearch, "text_verdict",
                            lambda path: None if Path(path) == locked else verdict(path))
        server = await built(tmp_path, tree)
        engine = server.search_engine
        assert locked not in engine.file_mtimes and locked not in engine.file_skipped

        monkeypatch.setattr(textsearch, "text_verdict", verdict)
        await engine.rebuild_index(incremental=True)

        assert locked in engine.file_mtimes
        await engine.stop()


# --------------------------------------------------------------------------
# The search waits
# --------------------------------------------------------------------------

class TestTheSearchWaits:

    async def test_nothing_changed_means_no_pass(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        passes = []
        original = engine.rebuild_index

        async def counted(*args, **kwargs):
            passes.append(1)
            return await original(*args, **kwargs)

        engine.rebuild_index = counted
        result = await server.semantic_search({"query": "check a password"})

        assert result["status"] == "success" and "index_fresh" not in result
        assert passes == []
        await engine.stop()

    async def test_a_file_written_just_before_is_found(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        (tree / "kettle.py").write_text(KETTLE)

        result = await server.semantic_search({"query": "heat water until it bubbles"})

        assert "index_fresh" not in result
        assert str(tree / "kettle.py") in {hit["file_path"] for hit in result["results"]}
        await server.search_engine.stop()

    async def test_a_search_that_cannot_wait_says_how_far_the_index_is(self, tmp_path, tree):
        server = await built(tmp_path, tree, index_wait_seconds=0.3)
        engine = server.search_engine
        for name in ("one.py", "two.py", "three.py"):
            (tree / name).write_text(KETTLE.replace("boil_the_kettle", name[:-3]))
        original = engine._add_embedded

        def slow(*args):
            time.sleep(1.0)
            return original(*args)

        engine._add_embedded = slow
        result = await server.semantic_search({"query": "heat water"})

        assert result["index_fresh"] is False
        assert result["index_update"]["files_total"] == 3
        assert "catching up" in result["note"] and "grep_search" in result["note"]
        await engine._fresh_task
        engine._add_embedded = original
        # The three are racy for the pass that read them: one more pass, which
        # a loaded machine must not turn into a second "not caught up".
        engine.config["index_wait_seconds"] = 20
        after = await server.semantic_search({"query": "heat water until it bubbles"})
        assert "index_fresh" not in after
        await engine.stop()

    async def test_a_pass_already_running_is_not_enough(self, tmp_path, tree):
        """It may have walked past the change: the search waits for one that
        started after it came."""
        server = await built(tmp_path, tree, index_wait_seconds=20)
        engine = server.search_engine
        (tree / "auth.py").write_text(AUTH + "\n# edited\n")   # pass 1 has work
        gate = threading.Event()
        original = engine._add_embedded

        def held(*args):
            gate.wait(10)
            return original(*args)

        engine._add_embedded = held
        first = asyncio.create_task(engine.rebuild_index(incremental=True))
        for _ in range(200):
            if engine._progress is not None and engine._progress.phase == "reading":
                break
            await asyncio.sleep(0.02)
        (tree / "pkg" / "late.py").write_text(KETTLE)          # behind pass 1's walk
        searching = asyncio.create_task(
            server.semantic_search({"query": "heat water until it bubbles"}))
        await asyncio.sleep(0.3)
        gate.set()
        result = await searching
        await first

        assert str(tree / "pkg" / "late.py") in {hit["file_path"] for hit in result["results"]}
        await engine.stop()

    async def test_a_reader_asks_its_builder_in_this_process(self, tmp_path, tree):
        """The coder's sub-agents search through a reader, which runs no pass."""
        owner = await built(tmp_path, tree)
        reader_config = ToolServerConfig(type="file_ops", enabled=True)
        reader_config.allowed_directories = [str(tree)]
        reader_config.search = {"enable_semantic_search": True, "enable_indexing": False,
                                "chroma_db_path": str(tmp_path / "store"),
                                "collection_name": owner.search_engine._collection_name}
        reader = FileOpsServer("reader", Mock(spec=AgentSystemConfig), reader_config)
        (tree / "kettle.py").write_text(KETTLE)

        result = await reader.semantic_search({"query": "heat water until it bubbles"})

        assert str(tree / "kettle.py") in {hit["file_path"] for hit in result["results"]}
        await reader.search_engine.stop()
        await owner.search_engine.stop()


# --------------------------------------------------------------------------
# The journal
# --------------------------------------------------------------------------

class TestTheJournal:

    async def test_it_records_what_changed_and_nothing_else(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        assert engine.changes_since(0) == [], "a build is not news"

        (tree / "kettle.py").write_text(KETTLE)
        (tree / "photo.bin").write_bytes(b"\x00\x01\x02")
        (tree / "auth.py").write_text(AUTH + "\n# edited\n")
        os.utime(tree / "pkg" / "tea.py", None)                 # touched, not changed
        (tree / "empty.txt").unlink()
        await engine.rebuild_index(incremental=True)

        found = {(kind, Path(path).name) for kind, path in engine.changes_since(0)}
        assert found == {("added", "kettle.py"), ("added", "photo.bin"),
                         ("modified", "auth.py"), ("removed", "empty.txt")}

        again = build_server(tmp_path, [tree]).search_engine
        again._init_vector_store()
        assert {(k, Path(p).name) for k, p in again.changes_since(0)} == found, \
            "the journal did not survive the process"
        await engine.stop()
        await again.stop()


# --------------------------------------------------------------------------
# The hook
# --------------------------------------------------------------------------

def _turns(tree: Path, previous_turn_extra=(), now=None):
    """[turn 1: user + reply], [turn 2: user + tool_preload's pair]."""
    now = now or datetime.now(timezone.utc)
    return [
        ChatMessage(role="user", content="look at auth", timestamp=now - timedelta(minutes=5)),
        *previous_turn_extra,
        ChatMessage(role="assistant", content="done", timestamp=now - timedelta(minutes=4)),
        ChatMessage(role="user", content="and now?", timestamp=now),
        ChatMessage(role="assistant", content=None, injected_by="tool_preload",
                    tool_calls=[{"id": "preload_1", "type": "function",
                                 "function": {"name": "idx_grep_search", "arguments": "{}"}}]),
        ChatMessage(role="tool", tool_call_id="preload_1", name="idx_grep_search",
                    content="{}"),
    ]


async def _starts(server, session: str = "s1") -> None:
    """The session's first LLM call: from here on the hook looks after it."""
    await server.on_pre_llm_call(_context([ChatMessage(role="user", content="go")], session))


async def _agent_edits(server, path: Path, session: str = "s1") -> None:
    """The agent's own edit, through its own write tool, as the loop calls it."""
    await _starts(server, session)
    result = await server.replace_string_in_file({
        "filePath": str(path), "oldString": "return True", "newString": "return False",
        "_session_id": session})
    assert result["status"] == "success", result


def _context(messages, session="s1"):
    return SimpleNamespace(messages=messages, hook_config={}, session_id=session)


def _notes(context):
    return [m for m in context.messages if getattr(m, "injected_by", None) == "file_ops"]


class TestTheChangedFilesNotice:

    async def test_it_lists_what_changed_behind_tool_preloads_results(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        (tree / "kettle.py").write_text(KETTLE)
        context = _context(_turns(tree))

        result = await server.on_pre_llm_call(context)

        assert result.modified
        [note] = _notes(context)
        assert note.role == "developer" and "added: kettle.py" in note.content
        assert context.messages[-1] is note, "appended, never inserted"
        await server.search_engine.stop()

    async def test_its_own_edits_are_left_out(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        await _agent_edits(server, tree / "auth.py")
        (tree / "kettle.py").write_text(KETTLE)
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "kettle.py" in note.content and "auth.py" not in note.content
        await server.search_engine.stop()

    async def test_a_copy_over_a_file_with_an_old_mtime_is_listed(self, tmp_path, tree):
        """File times would have put it before the previous turn."""
        server = await built(tmp_path, tree)
        _copy_over_with_old_mtime(tree, tmp_path)
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "changed: pkg/tea.py" in note.content
        await server.search_engine.stop()

    async def test_once_per_turn_and_never_twice_for_the_same_change(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        (tree / "kettle.py").write_text(KETTLE)
        messages = _turns(tree)
        context = _context(messages)
        await server.on_pre_llm_call(context)
        assert len(_notes(context)) == 1

        # The turn's next LLM call: news or not, the note stands once.
        (tree / "mug.py").write_text(TEA.replace("steep", "fill"))
        await server.search_engine.rebuild_index(incremental=True)
        second_step = _context(context.messages)
        assert not (await server.on_pre_llm_call(second_step)).modified
        (tree / "mug.py").unlink()
        await server.search_engine.rebuild_index(incremental=True)

        # Found by a pass while the model was still answering: after this
        # turn's look, before its reply -- news for the next turn.
        (tree / "cup.py").write_text(TEA.replace("steep", "pour"))
        await server.search_engine.rebuild_index(incremental=True)
        later = datetime.now(timezone.utc) + timedelta(seconds=1)
        next_turn = _context(context.messages + [
            ChatMessage(role="assistant", content="ok", timestamp=later),
            ChatMessage(role="user", content="next", timestamp=later)])
        await server.on_pre_llm_call(next_turn)

        [_, note] = _notes(next_turn)
        assert "cup.py" in note.content
        assert "kettle.py" not in note.content, "reported in the turn before"
        await server.search_engine.stop()

    async def test_nothing_on_the_first_turn_or_without_changes(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        first = _context([ChatMessage(role="user", content="hi",
                                      timestamp=datetime.now(timezone.utc))])
        quiet = _context(_turns(tree))

        assert not (await server.on_pre_llm_call(first)).modified
        assert not (await server.on_pre_llm_call(quiet)).modified
        await server.search_engine.stop()


class TestTheReviewOfTheBuild:
    """12.11.2026: what the review of the built round found."""

    async def test_a_build_carried_on_is_not_news(self, tmp_path, tree, monkeypatch):
        """Every file the first build had not reached came out "added"."""
        monkeypatch.setattr(search, "EMBED_BATCH_SIZE", 1)
        monkeypatch.setattr(search, "_STATE_SAVE_SECONDS", 0.0)
        server = build_server(tmp_path, [tree])
        engine = server.search_engine
        original = engine._add_embedded
        calls = []

        def dies(*args):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("the process ends here")
            return original(*args)

        engine._add_embedded = dies
        with pytest.raises(RuntimeError):
            await engine.rebuild_index(incremental=False)
        engine._add_embedded = original
        await engine.stop()

        resumed = build_server(tmp_path, [tree]).search_engine
        resumed._init_vector_store()
        assert resumed.file_mtimes and not resumed._index_built
        await resumed.rebuild_index(incremental=True)

        assert resumed._index_built and resumed.changes_since(0) == []
        await resumed.stop()

    async def test_a_state_from_before_skipped_files_were_kept_reports_no_binaries(
            self, tmp_path, tree):
        """The first pass after the upgrade met every binary for the first time."""
        import json

        server = await built(tmp_path, tree)
        await server.search_engine.stop()
        state = json.loads(server.search_engine._state_path.read_text(encoding="utf-8"))
        del state["skipped"], state["pass_started"]
        server.search_engine._state_path.write_text(json.dumps(state), encoding="utf-8")

        upgraded = build_server(tmp_path, [tree]).search_engine
        upgraded._init_vector_store()
        read = []
        original = upgraded._prepare_for_index

        def spy(chunk, *args):
            read.extend(path for path, _ in chunk if path.suffix == ".py")
            return original(chunk, *args)

        upgraded._prepare_for_index = spy
        await upgraded.rebuild_index(incremental=True)

        assert {tree / "logo.bin", tree / "empty.txt"} <= set(upgraded.file_skipped)
        assert upgraded.changes_since(0) == []
        assert read == [], "without the pass start every file counted as racy and was read again"
        await upgraded.stop()

    async def test_a_refused_move_hides_nothing_a_deleted_folder_its_files(self, tmp_path, tree):
        """manage refuses an existing destination: nothing of it is the agent's."""
        (tree / "gone").mkdir()
        (tree / "gone" / "a.py").write_text(TEA)
        age(tree)
        server = await built(tmp_path, tree)
        await _starts(server)
        refused = await server.manage({"operation": "move", "path": str(tree / "auth.py"),
                                       "destination": str(tree / "pkg"), "_session_id": "s1"})
        deleted = await server.manage({"operation": "delete", "path": str(tree / "gone"),
                                       "recursive": True, "_session_id": "s1"})
        assert refused["status"] == "error" and deleted["status"] == "success"
        (tree / "pkg" / "cup.py").write_text(TEA.replace("steep", "pour"))
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "pkg/cup.py" in note.content, "the refused move hid the folder"
        assert "gone/a.py" not in note.content, "the agent deleted that folder itself"
        await server.search_engine.stop()

    async def test_a_folder_the_agent_moved_is_its_own_on_both_ends(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        await _starts(server)
        moved = await server.manage({"operation": "move", "path": str(tree / "pkg"),
                                     "destination": str(tree / "kitchen"), "_session_id": "s1"})
        assert moved["status"] == "success", moved
        (tree / "kettle.py").write_text(KETTLE)
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "kettle.py" in note.content
        assert "tea.py" not in note.content, note.content
        await server.search_engine.stop()

    async def test_what_the_user_did_after_the_agent_stops_being_the_agents(self, tmp_path, tree):
        """The agent deleted a file, the user made it again and deleted it
        again: that second removal is the user's."""
        server = await built(tmp_path, tree)
        await _starts(server)
        deleted = await server.manage({"operation": "delete", "path": str(tree / "auth.py"),
                                       "_session_id": "s1"})
        assert deleted["status"] == "success"
        (tree / "auth.py").write_text(AUTH)
        context = _context(_turns(tree))
        await server.on_pre_llm_call(context)
        assert "added: auth.py" in _notes(context)[0].content
        (tree / "auth.py").unlink()
        later = datetime.now(timezone.utc) + timedelta(seconds=1)
        next_turn = _context(context.messages + [
            ChatMessage(role="assistant", content="ok", timestamp=later),
            ChatMessage(role="user", content="next", timestamp=later)])

        await server.on_pre_llm_call(next_turn)

        assert "removed: auth.py" in _notes(next_turn)[-1].content
        await server.search_engine.stop()

    async def test_the_user_taking_the_agents_edit_back_is_news(self, tmp_path, tree):
        """Back to what the index holds, so no pass reports it -- but the
        agent's edit is gone, and it would build on it."""
        server = await built(tmp_path, tree)
        await _agent_edits(server, tree / "auth.py")
        (tree / "auth.py").write_text(AUTH)                       # the user's undo
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "changed: auth.py" in note.content
        await server.search_engine.stop()

    async def test_one_sessions_edit_is_news_to_another(self, tmp_path, tree):
        """Sessions share an instance: what one agent wrote, the other did not."""
        server = await built(tmp_path, tree)
        await _agent_edits(server, tree / "auth.py", session="s1")
        context = _context(_turns(tree), session="s2")

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "changed: auth.py" in note.content
        await server.search_engine.stop()

    async def test_an_own_edit_found_a_turn_late_is_still_its_own(self, tmp_path, tree):
        """The index did not catch up in the turn after the edit; the turn
        after that saw the edit and called it somebody else's."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        now = datetime.now(timezone.utc)
        turn_k1 = [ChatMessage(role="user", content="edit auth", timestamp=now - timedelta(minutes=9)),
                   ChatMessage(role="assistant", content="edited", timestamp=now - timedelta(minutes=8))]
        await _agent_edits(server, tree / "auth.py")
        turn_k = [ChatMessage(role="user", content="go on", timestamp=now - timedelta(minutes=5))]
        real_freshen = engine.freshen

        async def behind(budget=None):
            return {"files_done": 0, "files_total": 1}

        engine.freshen = behind                       # turn K: the index lags
        await server.on_pre_llm_call(_context(turn_k1 + turn_k))
        engine.freshen = real_freshen
        (tree / "kettle.py").write_text(KETTLE)
        turn_k_reply = [ChatMessage(role="assistant", content="ok",
                                    timestamp=now - timedelta(minutes=4))]
        turn_k2 = [ChatMessage(role="user", content="and now", timestamp=now)]
        context = _context(turn_k1 + turn_k + turn_k_reply + turn_k2)

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "kettle.py" in note.content and "auth.py" not in note.content
        await engine.stop()

    async def test_a_finding_that_reaches_the_journal_late_is_told(self, tmp_path, tree):
        """Another process's pass reaches this journal only when this one next
        reads the state -- with the time it was found, which may be older than
        this process's last look."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        (tree / "kettle.py").write_text(KETTLE)
        context = _context(_turns(tree))
        await server.on_pre_llm_call(context)
        synced, looked = engine._journal_synced_at, server._notice_state["s1"]
        assert synced, "fixture: the look ran a pass"
        import json
        state = json.loads(engine._state_path.read_text(encoding="utf-8"))   # the other process
        state["journal"].append([synced + 0.0001, "added", str(tree / "from_elsewhere.py")])
        engine._state_path.write_text(json.dumps(state), encoding="utf-8")
        later = datetime.now(timezone.utc) + timedelta(seconds=1)
        next_turn = _context(context.messages + [
            ChatMessage(role="assistant", content="ok", timestamp=later),
            ChatMessage(role="user", content="next", timestamp=later)])

        await server.on_pre_llm_call(next_turn)

        notes = _notes(next_turn)
        assert len(notes) == 2 and "from_elsewhere.py" in notes[1].content
        assert "kettle.py" not in notes[1].content and looked is server._notice_state["s1"]
        await engine.stop()

    async def test_a_pass_that_died_leaves_no_progress(self, tmp_path, tree):
        """Its rate only grows: a search would stop waiting on it at once."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        (tree / "kettle.py").write_text(KETTLE)

        def dies(*args):
            raise RuntimeError("no model")

        engine._add_embedded = dies
        with pytest.raises(RuntimeError):
            await engine.rebuild_index(incremental=True)

        assert engine._progress is None
        await engine.stop()

    async def test_an_unreadable_file_that_opens_now_counts(self, tmp_path, tree, monkeypatch):
        locked = tree / "pkg" / "tea.py"
        verdict = textsearch.text_verdict
        monkeypatch.setattr(textsearch, "text_verdict",
                            lambda path: None if Path(path) == locked else verdict(path))
        server = await built(tmp_path, tree)
        engine = server.search_engine
        assert changes(engine) == 0

        monkeypatch.setattr(textsearch, "text_verdict", verdict)

        assert changes(engine) == 1
        await engine.stop()

    async def test_a_journal_that_forgot_says_the_list_may_be_incomplete(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        old = time.time() - 2 * search._JOURNAL_SECONDS
        engine._journal.append((old, "added", str(tree / "ancient.py")))
        engine._trim_journal()
        assert engine._journal_trimmed_until == old and not engine._journal
        engine._journal_trimmed_until = time.time()   # forgot something after the boundary
        engine._save_state(True)
        (tree / "kettle.py").write_text(KETTLE)
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "may be incomplete" in note.content
        await engine.stop()


class TestTheReviewOfTheFixes:
    """12.11.2026: what the review of the review's fixes found."""

    @pytest.mark.parametrize("pass_between", [True, False])
    async def test_the_users_edit_after_the_agents_own_is_news(self, tmp_path, tree, pass_between):
        """By content: with no pass between the agent's edit and the user's,
        the two are one finding -- and the file holds the user's content."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        await _agent_edits(server, tree / "auth.py")
        if pass_between:
            await engine.rebuild_index(incremental=True)
        (tree / "auth.py").write_text(AUTH + "\n# by the user\n")
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "changed: auth.py" in note.content
        await engine.stop()

    async def test_a_second_change_after_a_pass_that_died_is_news(self, tmp_path, tree):
        """What a dead pass found is not found twice -- unless the file changed since."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        (tree / "kettle.py").write_text(KETTLE)
        original = engine._add_embedded

        def dies(*args):
            raise RuntimeError("no model right now")

        engine._add_embedded = dies
        with pytest.raises(RuntimeError):
            await engine.rebuild_index(incremental=True)
        engine._add_embedded = original
        older = time.time() - 300           # a copy keeps the mtime of its source
        (tree / "kettle.py").write_text(KETTLE + "\n# again\n")
        os.utime(tree / "kettle.py", (older, older))
        await engine.rebuild_index(incremental=True)

        kettle = [entry for entry in engine._journal if entry[2].endswith("kettle.py")]
        assert len(kettle) == 2, kettle
        await engine.stop()

    async def test_a_file_a_mend_dropped_and_the_user_deleted_is_removed(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        lost = engine.file_symbol_ids[tree / "auth.py"][0]
        engine._vector_store.delete(engine._collection_name, [lost])
        (tree / "auth.py").unlink()

        await engine.rebuild_index(incremental=True)

        assert ("removed", str(tree / "auth.py")) in engine.changes_since(0)
        await engine.stop()

    async def test_what_a_pass_found_outlives_the_process(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        engine._pass_found = {("modified", str(tree / "auth.py")): 12.5,
                              ("removed", str(tree / "gone.py")): None}
        engine._save_state(False)
        await engine.stop()

        again = build_server(tmp_path, [tree]).search_engine
        again._init_vector_store()

        assert again._pass_found == engine._pass_found
        await again.stop()

    async def test_a_pass_cut_short_does_not_silence_the_next(self, tmp_path, tree):
        """Every checkpoint writes "unfinished": a restart after one made the
        next pass report nothing at all."""
        import json

        server = await built(tmp_path, tree)
        await server.search_engine.stop()
        path = server.search_engine._state_path
        state = json.loads(path.read_text(encoding="utf-8"))
        state["complete"] = False                                   # a checkpoint's
        path.write_text(json.dumps(state), encoding="utf-8")
        (tree / "kettle.py").write_text(KETTLE)

        again = build_server(tmp_path, [tree]).search_engine
        again._init_vector_store()
        assert not again._index_built
        await again.rebuild_index(incremental=True)

        assert ("added", str(tree / "kettle.py")) in again.changes_since(0)
        await again.stop()

    async def test_a_file_a_mend_dropped_is_news_only_if_it_changed(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        lost = engine.file_symbol_ids[tree / "auth.py"][0]
        engine._vector_store.delete(engine._collection_name, [lost])

        await engine.rebuild_index(incremental=True)

        assert tree / "auth.py" in engine.file_mtimes, "fixture: the mend dropped and re-read it"
        assert engine.changes_since(0) == []
        await engine.stop()

    async def test_a_checkpoint_on_an_older_state_writes_no_skipped_files(self, tmp_path, tree):
        import json

        server = await built(tmp_path, tree)
        engine = server.search_engine
        engine._skipped_known = False
        engine._save_state(False)

        assert "skipped" not in json.loads(engine._state_path.read_text(encoding="utf-8"))
        await engine.stop()

    async def test_a_new_process_starts_from_the_last_notice(self, tmp_path, tree):
        """It used to start from the model's first reply -- after which the
        previous look may long have told, or not yet told, what it found."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        (tree / "kettle.py").write_text(KETTLE)
        first = _context(_turns(tree))
        await server.on_pre_llm_call(first)                         # tells kettle.py
        (tree / "cup.py").write_text(TEA.replace("steep", "pour"))
        await engine.rebuild_index(incremental=True)
        # The reply carries a time BEFORE the look: replies used to be the start.
        early = datetime.now(timezone.utc) - timedelta(minutes=3)
        later = datetime.now(timezone.utc) + timedelta(seconds=1)
        context = _context(first.messages + [
            ChatMessage(role="assistant", content="ok", timestamp=early),
            ChatMessage(role="user", content="next", timestamp=later)])
        server._notice_state.clear()                                # a new process

        await server.on_pre_llm_call(context)

        note = _notes(context)[-1]
        assert "cup.py" in note.content and "kettle.py" not in note.content
        await engine.stop()

    async def test_a_look_with_no_progress_to_tell_still_says_incomplete(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        (tree / "kettle.py").write_text(KETTLE)
        await engine.rebuild_index(incremental=True)
        real = engine.freshen

        async def nothing_known(budget=None):
            return {}                                              # not caught up, no detail

        engine.freshen = nothing_known
        context = _context(_turns(tree))
        await server.on_pre_llm_call(context)
        engine.freshen = real

        [note] = _notes(context)
        assert "may be incomplete" in note.content
        await engine.stop()

    async def test_notice_state_and_journal_stay_bounded(self, tmp_path, tree, monkeypatch):
        from plugins.file_ops import server as server_mod

        monkeypatch.setattr(server_mod, "_NOTICE_SESSIONS", 2)
        server = await built(tmp_path, tree)
        for session in ("a", "b", "c"):
            await server.on_pre_llm_call(_context(_turns(tree), session=session))
        assert list(server._notice_state) == ["b", "c"]
        await server.search_engine.stop()

        monkeypatch.setattr(search, "_JOURNAL_LIMIT", 2)
        storeless = build_server(tmp_path / "other", [tree], enable_semantic_search=False)
        engine = storeless.search_engine
        await engine.rebuild_index(incremental=False)
        for i in range(8):
            (tree / f"new{i}.py").write_text(KETTLE)
        await engine.rebuild_index(incremental=True)
        assert len(engine._journal) <= 2 * search._JOURNAL_LIMIT, "nothing saves, nothing trimmed"
        await engine.stop()

    async def test_a_finding_of_a_pass_that_died_is_not_found_twice(self, tmp_path, tree):
        """Told from memory, lost from disk, found again with a new time: told again."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        (tree / "kettle.py").write_text(KETTLE)
        original = engine._add_embedded

        def dies(*args):
            raise RuntimeError("no model right now")

        engine._add_embedded = dies
        first = _context(_turns(tree))
        await server.on_pre_llm_call(first)              # its pass dies; told from memory
        assert "kettle.py" in _notes(first)[0].content
        engine._add_embedded = original
        later = datetime.now(timezone.utc) + timedelta(seconds=1)
        context = _context(first.messages + [
            ChatMessage(role="assistant", content="ok", timestamp=later),
            ChatMessage(role="user", content="next", timestamp=later)])

        await server.on_pre_llm_call(context)            # its pass finds kettle.py again

        assert len(_notes(context)) == 1, "told twice"
        assert tree / "kettle.py" in engine.file_mtimes, "fixture: the second pass ran"
        await engine.stop()


# --------------------------------------------------------------------------
# The review of the content model (12.11.2026)
# --------------------------------------------------------------------------


def _next_turn(context):
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    return _context(context.messages + [
        ChatMessage(role="assistant", content="ok", timestamp=later),
        ChatMessage(role="user", content="next", timestamp=later)], context.session_id)


class TestTheReviewOfTheContentModel:
    async def test_the_agent_writing_into_a_folder_it_deleted_is_its_own(self, tmp_path, tree):
        (tree / "gone").mkdir()
        (tree / "gone" / "a.py").write_text(TEA)
        age(tree)
        server = await built(tmp_path, tree)
        await _starts(server)
        deleted = await server.manage({"operation": "delete", "path": str(tree / "gone"),
                                       "recursive": True, "_session_id": "s1"})
        created = await server.manage({"operation": "create", "path": str(tree / "gone" / "b.py"),
                                       "content": KETTLE, "_session_id": "s1"})
        assert deleted["status"] == created["status"] == "success", (deleted, created)
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        assert _notes(context) == [], _notes(context)[0].content
        await server.search_engine.stop()

    async def test_a_folder_moved_away_and_back_is_its_own(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        await _starts(server)
        for source, dest in (("pkg", "kitchen"), ("kitchen", "pkg")):
            moved = await server.manage({"operation": "move", "path": str(tree / source),
                                         "destination": str(tree / dest), "_session_id": "s1"})
            assert moved["status"] == "success", moved
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        assert _notes(context) == [], _notes(context)[0].content
        await server.search_engine.stop()

    async def test_a_folder_move_larger_than_the_records_keeps_its_own(self, tmp_path, tree, monkeypatch):
        """The cap threw out the folder's own removal first: every file of it
        came back as somebody else's."""
        for name in ("a.py", "b.py"):
            (tree / "pkg" / name).write_text(KETTLE.replace("boil", name[0]))
        age(tree)
        monkeypatch.setattr(server_module, "_OWN_LIMIT", 1)
        server = await built(tmp_path, tree)
        await _starts(server)
        moved = await server.manage({"operation": "move", "path": str(tree / "pkg"),
                                     "destination": str(tree / "kitchen"), "_session_id": "s1"})
        assert moved["status"] == "success", moved
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        assert _notes(context) == [], _notes(context)[0].content
        await server.search_engine.stop()

    async def test_a_change_told_while_the_index_lagged_is_not_told_again(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        await _agent_edits(server, tree / "auth.py")
        (tree / "auth.py").write_text(AUTH.replace("a secret", "a password"))    # the user's
        real_freshen = engine.freshen

        async def behind(budget=None):
            return {"files_done": 0, "files_total": 1}

        engine.freshen = behind
        context = _context(_turns(tree))
        await server.on_pre_llm_call(context)
        assert "changed: auth.py" in _notes(context)[0].content
        engine.freshen = real_freshen
        next_turn = _next_turn(context)

        await server.on_pre_llm_call(next_turn)              # the index catches up

        assert any(e[2].endswith("auth.py") for e in engine._journal), "fixture: the pass found it"
        assert len(_notes(next_turn)) == 1, _notes(next_turn)[-1].content
        await engine.stop()

    async def test_a_user_edit_found_before_the_agents_own_is_news(self, tmp_path, tree, monkeypatch):
        """The agent's partial edit came after the user's elsewhere in the
        file: the file holds what the agent left, the user's edit included."""
        monkeypatch.setattr(server_module, "_OWN_CLOCK_SLACK", 0.0)
        server = await built(tmp_path, tree)
        engine = server.search_engine
        await _starts(server)
        (tree / "auth.py").write_text(AUTH.replace("a secret", "a password"))    # the user's
        await engine.rebuild_index(incremental=True)              # e.g. the agent's own search
        assert any(e[2].endswith("auth.py") for e in engine._journal), "fixture: the pass found it"
        time.sleep(0.1)
        await _agent_edits(server, tree / "auth.py")
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "changed: auth.py" in note.content
        await engine.stop()

    async def test_a_file_a_mend_dropped_and_that_went_while_it_was_read_is_removed(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        lost = engine.file_symbol_ids[tree / "auth.py"][0]
        engine._vector_store.delete(engine._collection_name, [lost])
        real_triage = engine._triage

        def triage_then_gone(paths):
            todo = real_triage(paths)
            (tree / "auth.py").unlink()
            return todo

        engine._triage = triage_then_gone

        await engine.rebuild_index(incremental=True)

        assert ("removed", str(tree / "auth.py")) in engine.changes_since(0)
        await engine.stop()

    async def test_an_edit_through_a_symlink_is_its_own(self, tmp_path, tree):
        try:
            os.symlink(tree / "auth.py", tree / "link.py")
        except (OSError, NotImplementedError):
            pytest.skip("no symlinks here")
        age(tree)
        server = await built(tmp_path, tree)
        await _agent_edits(server, tree / "link.py")
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        assert _notes(context) == [], _notes(context)[0].content
        await server.search_engine.stop()

    async def test_writes_of_a_session_no_notice_looks_after_are_not_kept(self, tmp_path, tree):
        """The hook is on per agent; every other session writing through the
        instance pushed the coder's out of the bounded notice state."""
        server = await built(tmp_path, tree)
        result = await server.replace_string_in_file({
            "filePath": str(tree / "auth.py"), "oldString": "return True",
            "newString": "return False", "_session_id": "other"})

        assert result["status"] == "success"
        assert "other" not in server._notice_state
        await server.search_engine.stop()

    async def test_a_second_removal_after_passes_that_died_is_news(self, tmp_path, tree):
        """Removed, back, removed again: what the first dead pass found of the
        removal does not swallow the second."""
        server = await built(tmp_path, tree)
        engine = server.search_engine
        original = engine._add_embedded

        def dies(*args):
            raise RuntimeError("no model right now")

        engine._add_embedded = dies
        (tree / "kettle.py").write_text(KETTLE)              # keeps every pass dying
        (tree / "auth.py").unlink()
        with pytest.raises(RuntimeError):
            await engine.rebuild_index(incremental=True)
        (tree / "auth.py").write_text(AUTH + "# back, and different\n")
        with pytest.raises(RuntimeError):
            await engine.rebuild_index(incremental=True)
        engine._add_embedded = original
        (tree / "auth.py").unlink()

        await engine.rebuild_index(incremental=True)

        removals = [e for e in engine._journal if e[1:] == ("removed", str(tree / "auth.py"))]
        assert len(removals) == 2, engine._journal
        assert engine._pass_found == {}, "a complete pass forgets what the dead ones found"
        await engine.stop()

    async def test_a_file_the_user_puts_back_beside_the_agents_own_is_news(self, tmp_path, tree):
        """The agent deleted a folder and wrote into it again; the user put
        an old file back there, which no pass has seen yet."""
        (tree / "gone").mkdir()
        (tree / "gone" / "a.py").write_text(TEA)
        age(tree)
        server = await built(tmp_path, tree)
        await _starts(server)
        await server.manage({"operation": "delete", "path": str(tree / "gone"),
                             "recursive": True, "_session_id": "s1"})
        await server.manage({"operation": "create", "path": str(tree / "gone" / "b.py"),
                             "content": KETTLE, "_session_id": "s1"})
        (tree / "gone" / "a.py").write_text(TEA)                  # the user's, from a backup
        engine = server.search_engine

        async def behind(budget=None):
            return {"files_done": 0, "files_total": 1}

        engine.freshen = behind                                   # no pass has looked yet
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        [note] = _notes(context)
        assert "added: gone" in note.content, note.content
        await engine.stop()

    async def test_a_write_before_the_first_notice_is_its_own(self, tmp_path, tree):
        """tool_preload runs before the notice in the same call: in a session's
        first call its writes come before the notice ever saw the session."""
        server = await built(tmp_path, tree)
        result = await server.replace_string_in_file({
            "filePath": str(tree / "auth.py"), "oldString": "return True",
            "newString": "return False", "_session_id": "s1"})
        assert result["status"] == "success"
        context = _context(_turns(tree))

        await server.on_pre_llm_call(context)

        assert _notes(context) == [], _notes(context)[0].content
        await server.search_engine.stop()

    async def test_a_folder_told_about_keeps_its_own_removals(self, tmp_path, tree):
        """The agent deleted a folder, the user made a file in it before a
        pass looked: told, and the agent's own removals found later stay its own."""
        (tree / "gone").mkdir()
        for name in ("y.py", "z.py"):
            (tree / "gone" / name).write_text(TEA.replace("steep", name[0]))
        age(tree)
        server = await built(tmp_path, tree)
        engine = server.search_engine
        await _starts(server)
        await server.manage({"operation": "delete", "path": str(tree / "gone"),
                             "recursive": True, "_session_id": "s1"})
        (tree / "gone").mkdir()
        (tree / "gone" / "x.py").write_text(KETTLE)               # the user's
        real_freshen = engine.freshen

        async def behind(budget=None):
            return {"files_done": 0, "files_total": 1}

        engine.freshen = behind
        context = _context(_turns(tree))
        await server.on_pre_llm_call(context)
        assert "added: gone" in _notes(context)[0].content
        engine.freshen = real_freshen
        next_turn = _next_turn(context)

        await server.on_pre_llm_call(next_turn)              # the pass finds it all

        assert any(e[1] == "removed" for e in engine._journal), "fixture: the pass found the removals"
        assert len(_notes(next_turn)) == 1, _notes(next_turn)[-1].content
        await engine.stop()

    async def test_what_a_pass_found_yields_to_a_later_complete_pass_elsewhere(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        found = {("removed", str(tree / "auth.py")): None}
        engine._pass_found = dict(found)
        engine._pass_found_at = engine._last_pass_started + 1     # after the last complete pass
        engine._load_state(True)
        assert engine._pass_found == found, "nothing newer on disk: it stands"

        engine._pass_found_at = engine._last_pass_started - 1     # a complete pass started later
        engine._load_state(True)

        assert engine._pass_found == {}
        await engine.stop()

    async def test_one_kind_per_path_the_journals_latest(self, tmp_path, tree):
        import json

        server = await built(tmp_path, tree)
        engine = server.search_engine
        path = str(tree / "auth.py")
        state = json.loads(engine._state_path.read_text(encoding="utf-8"))   # the other process's
        state["pass_found"] = [["added", path, 12.5]]
        state["journal"] = [[time.time() - 60, "added", path]]
        engine._state_path.write_text(json.dumps(state), encoding="utf-8")
        engine._pass_found = {("removed", path): None}                       # this one's, newer
        engine._pass_found_at = engine._last_pass_started + 1
        engine._journal.append((time.time(), "removed", path))

        engine._load_state(True)

        assert engine._pass_found == {("removed", path): None}
        await engine.stop()

    async def test_what_a_dead_pass_found_stands_at_the_next_load(self, tmp_path, tree):
        server = await built(tmp_path, tree)
        engine = server.search_engine
        (tree / "kettle.py").write_text(KETTLE)

        def dies(*args):
            raise RuntimeError("no model right now")

        engine._add_embedded = dies
        with pytest.raises(RuntimeError):
            await engine.rebuild_index(incremental=True)
        engine._state_path.write_text(engine._state_path.read_text(encoding="utf-8")
                                      .replace('"pass_found"', '"pass_found_gone"'), encoding="utf-8")

        engine._load_state(True)                     # nothing of it on disk

        assert ("added", str(tree / "kettle.py")) in engine._pass_found
        await engine.stop()
