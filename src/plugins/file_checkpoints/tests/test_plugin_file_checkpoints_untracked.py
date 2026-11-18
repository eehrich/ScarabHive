"""Calls that change files without naming them -- a shell command, a coding CLI -- through the real path.

A real agent, the plugin's real hooks, real git: a stand-in shell (a tool server of type
``terminal``) runs whatever the test hands it, and a rewind has to put the files back.

What is pinned:

* outside a work tree, what the call created, changed and deleted is recorded and put back
  byte for byte;
* in a work tree, a clean file comes back from HEAD with the bytes it had (CRLF through
  core.autocrlf), a file git listed as changed from the copy taken before; a commit, a
  stash or a reset by the call is undone in the files, the repository itself left alone;
* what the work tree ignores is not recorded, and a tree in a work tree inside the
  directories is looked at as its own;
* a call that changes nothing records nothing, over the limits it is counted with the
  reason, and the copies of the look before are gone once the call reported back;
* what was not known before the call -- an ignored directory, a submodule's place, a
  directory that could not be read, a name known in another case -- is left alone by a
  rewind, and the call is named as partly not recorded;
* a call whose look was cut off, or whose run stopped before it reported back, is counted.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, List

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.file_rewind import REWOUND
from agent_system.tools.base import ToolServer
from file_rewind_rig import Rig, call, tree

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


class _Shell(ToolServer):
    """A shell whose commands are Python functions, handed over one per call."""

    def __init__(self):
        super().__init__("shell", AgentSystemConfig(), ToolServerConfig(type="terminal", enabled=True))
        self.commands: List[Callable[[], Any]] = []

    def get_tools(self):
        return [{"type": "function", "function": {"name": "shell_execute", "description": "Run.",
                                                  "parameters": {"type": "object", "properties": {}}}}]

    async def shell_execute(self, params):
        self.commands.pop(0)()
        return {"status": "success"}


def git(work: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=work,
                          capture_output=True, check=True)
    return done.stdout.decode()


@pytest.fixture
async def rig(tmp_path):
    rig = await Rig(tmp_path).start()
    yield rig
    await rig.close()


async def run(rig: Rig, *commands: Callable[[], Any], question: str = "build") -> None:
    shell = rig.registry.get("shell") if "shell" in rig.registry.list() else None
    if shell is None:
        shell = _Shell()
        rig.registry.register("shell", shell)
    shell.commands.extend(commands)
    agent = rig.agent("with_shell", allowed=["fs/*", "shell/*"])
    await rig.turn(question, [call("shell_execute") for _ in commands], agent=agent)


async def listing(rig: Rig) -> list:
    return (await rig.checkpoints(agent=rig.agent("with_shell")))["checkpoints"]


async def rewind(rig: Rig, **kwargs) -> dict:
    return await rig.rewind(agent=rig.agent("with_shell"), **kwargs)


def _repo(work: Path, autocrlf: str = "false") -> None:
    git(work, "init", "-q")
    git(work, "config", "core.autocrlf", autocrlf)


def counted(entries: list) -> List[str]:
    return [u["tool"] for e in entries for u in e["untracked"]]


class TestOutsideAWorkTree:

    async def test_what_a_shell_created_changed_and_deleted_is_put_back(self, rig):
        work = rig.work
        (work / "keep.txt").write_bytes(b"one\r\ntwo\r\n")
        (work / "gone.bin").write_bytes(bytes(range(256)))
        before = tree(work)

        def command():
            (work / "keep.txt").write_bytes(b"changed")
            (work / "gone.bin").unlink()
            (work / "made" ).mkdir()
            (work / "made" / "new.txt").write_text("new")

        await run(rig, command)
        files = {Path(f["path"]).name: f["change"] for f in (await listing(rig))[0]["files"]}
        report = await rewind(rig)

        assert files == {"keep.txt": "modified", "gone.bin": "deleted", "new.txt": "created"}, files
        assert report["status"] == REWOUND, report["text"]
        assert report["untracked"] == [], "the shell call is still named as untracked"
        assert {k: v for k, v in tree(work).items() if k != "made"} == before

    async def test_a_call_that_changes_nothing_records_nothing(self, rig):
        (rig.work / "a.txt").write_text("A")

        await run(rig, lambda: None)

        assert await listing(rig) == []
        assert not any(rig.store.rglob("journal.db")), "a record was made of a call that changed nothing"

    async def test_over_the_limits_the_call_is_counted_with_the_reason(self, tmp_path):
        rig = await Rig(tmp_path).start(snapshot_max_files=1)
        try:
            for name in ("a.txt", "b.txt"):
                (rig.work / name).write_text(name)

            await run(rig, lambda: (rig.work / "a.txt").write_text("changed"))

            [entry] = await listing(rig)
            [counted] = entry["untracked"]
            assert counted["tool"].startswith("shell_execute (not recorded: more than 1 files"), counted
            assert entry["files"] == []
        finally:
            await rig.close()

    async def test_switched_off_the_call_is_only_counted(self, tmp_path):
        rig = await Rig(tmp_path).start(snapshot_untracked=False)
        try:
            await run(rig, lambda: (rig.work / "made.txt").write_text("shell"))

            [entry] = await listing(rig)
            report = await rewind(rig)

            assert entry["untracked"] == [{"tool": "shell_execute", "count": 1}]
            assert tree(rig.work) == {"made.txt": b"shell"}, "an untracked change was touched"
            assert "shell_execute x1" in report["text"]
        finally:
            await rig.close()

    async def test_the_copies_of_the_look_before_are_gone_afterwards(self, rig):
        (rig.work / "a.txt").write_text("A")

        await run(rig, lambda: (rig.work / "a.txt").write_text("B"))

        calls = rig.store / ".snapshots"
        assert calls.is_dir(), "fixture: nothing was looked at"
        assert not any(p.is_dir() for p in calls.glob("*/calls/*")), "a call's copies stayed behind"


class TestInAWorkTree:

    async def test_clean_dirty_new_and_deleted_files_come_back(self, rig):
        work = rig.work
        _repo(work)
        (work / "clean.txt").write_bytes(b"clean\r\n")
        (work / "dirty.txt").write_text("committed")
        (work / "doomed.txt").write_text("doomed")
        git(work, "add", ".")
        git(work, "commit", "-qm", "start")
        (work / "dirty.txt").write_text("edited, not committed")
        (work / "loose.txt").write_text("untracked")
        before = {k: v for k, v in tree(work).items() if not k.startswith(".git")}
        head = git(work, "rev-parse", "HEAD")

        def command():
            (work / "clean.txt").write_text("rewritten")
            (work / "dirty.txt").write_text("rewritten too")
            (work / "loose.txt").unlink()
            (work / "doomed.txt").unlink()
            (work / "fresh.txt").write_text("fresh")

        await run(rig, command)
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert {k: v for k, v in tree(work).items() if not k.startswith(".git")} == before
        assert git(work, "rev-parse", "HEAD") == head, "the repository was touched"

    async def test_a_commit_by_the_call_is_undone_in_the_files(self, rig):
        work = rig.work
        _repo(work)
        (work / "a.txt").write_text("v1")
        git(work, "add", ".")
        git(work, "commit", "-qm", "v1")

        def command():
            (work / "a.txt").write_text("v2")
            (work / "b.txt").write_text("new in the commit")
            git(work, "add", ".")
            git(work, "commit", "-qm", "v2")

        await run(rig, command)
        files = sorted(Path(f["path"]).name for f in (await listing(rig))[0]["files"])
        report = await rewind(rig)

        assert files == ["a.txt", "b.txt"], files
        assert report["status"] == REWOUND, report["text"]
        assert (work / "a.txt").read_text() == "v1" and not (work / "b.txt").exists()

    async def test_a_stash_by_the_call_is_undone(self, rig):
        work = rig.work
        _repo(work)
        (work / "a.txt").write_text("committed")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        (work / "a.txt").write_text("work in progress")

        await run(rig, lambda: git(work, "stash", "-q"))
        assert (work / "a.txt").read_text() == "committed", "fixture: the stash did nothing"
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert (work / "a.txt").read_text() == "work in progress"

    async def test_a_clean_file_comes_back_with_its_checked_out_line_endings(self, rig):
        work = rig.work
        _repo(work, autocrlf="true")
        (work / "a.txt").write_bytes(b"one\ntwo\n")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        (work / "a.txt").unlink()
        git(work, "checkout", "--", "a.txt")          # as a checkout writes it: CRLF
        checked_out = (work / "a.txt").read_bytes()
        assert b"\r\n" in checked_out, "fixture: autocrlf wrote no CRLF"

        await run(rig, lambda: (work / "a.txt").write_bytes(b"changed\n"))
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert (work / "a.txt").read_bytes() == checked_out

    async def test_what_the_work_tree_ignores_is_not_recorded(self, rig):
        work = rig.work
        _repo(work)
        (work / ".gitignore").write_text("build/\n")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")

        def command():
            (work / "build").mkdir()
            (work / "build" / "out.o").write_bytes(b"\x7fELF")
            (work / "src.c").write_text("int main;")

        await run(rig, command)
        files = [Path(f["path"]).name for f in (await listing(rig))[0]["files"]]

        assert files == ["src.c"], files

    async def test_a_work_tree_inside_the_directory_is_looked_at_on_its_own(self, rig):
        work = rig.work
        inner = work / "project"
        inner.mkdir()
        _repo(inner)
        (inner / "main.c").write_text("v1")
        git(inner, "add", ".")
        git(inner, "commit", "-qm", "c")
        (work / "notes.txt").write_text("loose")

        def command():
            (inner / "main.c").write_text("v2")
            (work / "notes.txt").write_text("loose, changed")

        await run(rig, command)
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert (inner / "main.c").read_text() == "v1" and (work / "notes.txt").read_text() == "loose"

    async def test_a_work_tree_inside_a_work_tree_is_looked_at_on_its_own(self, rig):
        """git status lists an embedded work tree as one untracked entry and never looks inside."""
        work = rig.work
        _repo(work)
        (work / "top.txt").write_text("top")
        git(work, "add", "top.txt")
        git(work, "commit", "-qm", "c")
        inner = work / "vendor"
        inner.mkdir()
        _repo(inner)
        (inner / "lib.c").write_text("v1")
        git(inner, "add", ".")
        git(inner, "commit", "-qm", "c")

        await run(rig, lambda: (inner / "lib.c").write_text("v2"))
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert (inner / "lib.c").read_text() == "v1"

    async def test_a_directory_its_work_tree_ignores_is_looked_at_on_its_own(self, rig, tmp_path):
        """As data/workspace lies ignored inside the installation's repository."""
        _repo(tmp_path)
        (tmp_path / ".gitignore").write_text("work/\nstore/\nvector_*/\n")
        git(tmp_path, "add", ".gitignore")
        git(tmp_path, "commit", "-qm", "c")
        (rig.work / "a.txt").write_text("A")

        await run(rig, lambda: (rig.work / "a.txt").write_text("B"))
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert (rig.work / "a.txt").read_text() == "A"


class TestAlongsideTheFileTools:

    async def test_a_change_that_kept_size_and_mtime_is_seen(self, rig):
        """Written twice within the clock's tick, a file keeps mtime and size: a look by those alone missed it."""
        import os
        target = rig.work / "a.txt"
        target.write_text("AAAA")

        def command():
            stamp = os.stat(target).st_mtime_ns
            target.write_text("BBBB")
            os.utime(target, ns=(stamp, stamp))

        await run(rig, command)
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert target.read_text() == "AAAA"

    async def test_a_file_the_file_tool_then_a_shell_changed_goes_back_to_before_both(self, rig):
        from file_rewind_rig import create
        shell = _Shell()
        rig.registry.register("shell", shell)
        shell.commands.append(lambda: (rig.work / "a.txt").write_text("by the shell"))
        agent = rig.agent("with_shell", allowed=["fs/*", "shell/*"])

        await rig.turn("both", [create(rig.work / "a.txt", "by the file tool")], [call("shell_execute")],
                       agent=agent)
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert not (rig.work / "a.txt").exists()


class TestWhatIsLeftAlone:

    async def test_a_changed_file_the_call_left_alone_is_not_recorded(self, rig):
        """git lists it before and after; unchanged by the call, it is no change of the call's."""
        work = rig.work
        _repo(work)
        (work / "a.txt").write_text("committed")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        (work / "a.txt").write_text("edited by the person")

        await run(rig, lambda: None)

        assert not any(rig.store.rglob("journal.db")), "a record was made of a call that changed nothing"


class TestWhatIsNotKnown:
    """A rewind writes back what the look before took for the state of a file: a guess destroys work."""

    async def test_a_clean_submodule_is_looked_at_before_the_call(self, rig, tmp_path):
        """git status does not list a clean submodule: taken for new, all its files were "created"."""
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _repo(upstream)
        (upstream / "a.txt").write_text("a1")
        (upstream / "b.txt").write_text("b")
        git(upstream, "add", ".")
        git(upstream, "commit", "-qm", "c")
        work = rig.work
        _repo(work)
        git(work, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "sub")
        git(work, "commit", "-qm", "sub")
        sub = work / "sub"

        await run(rig, lambda: (sub / "a.txt").write_text("a2"))
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert (sub / "a.txt").read_text() == "a1"
        assert (sub / "b.txt").read_text() == "b" and (sub / ".git").exists(), "the rewind removed the submodule"

    @pytest.mark.parametrize("inside_a_repo", [False, True])
    async def test_a_git_init_over_files_that_were_there_changes_none(self, rig, inside_a_repo):
        work = rig.work
        if inside_a_repo:
            _repo(work)
        project = work / "project"
        project.mkdir()
        (project / "main.c").write_text("int main;")

        await run(rig, lambda: _repo(project))
        report = await rewind(rig)

        assert (project / "main.c").read_text() == "int main;", report["text"]
        assert not any(f["path"].endswith("main.c") for e in await listing(rig) for f in e["files"])

    async def test_a_file_ignored_before_the_call_is_left_alone(self, rig):
        """The call stops ignoring .env and writes it: what it held before is not known."""
        work = rig.work
        _repo(work)
        (work / ".gitignore").write_text(".env\n")
        git(work, "add", ".gitignore")
        git(work, "commit", "-qm", "c")
        (work / ".env").write_text("SECRET=1")

        def command():
            (work / ".gitignore").write_text("")
            (work / ".env").write_text("SECRET=2")

        await run(rig, command)
        report = await rewind(rig)

        assert (work / ".gitignore").read_text() == ".env\n", report["text"]
        assert (work / ".env").exists(), "the rewind deleted a file it knew nothing of"

    async def test_a_file_git_was_told_to_skip_comes_back_as_it_was(self, rig):
        work = rig.work
        _repo(work)
        (work / "config.ini").write_text("committed")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        git(work, "update-index", "--skip-worktree", "config.ini")
        (work / "config.ini").write_text("local setting")

        def command():
            git(work, "update-index", "--no-skip-worktree", "config.ini")
            (work / "config.ini").write_text("overwritten")

        await run(rig, command)
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert (work / "config.ini").read_text() == "local setting"

    async def test_content_git_cannot_give_is_not_written_back(self, rig):
        """The call replaced the repository: the old objects are gone, nothing may come back empty or deleted."""
        work = rig.work
        _repo(work)
        (work / "main.py").write_text("print(1)")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")

        def command():
            shutil.rmtree(work / ".git", onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
            _repo(work)
            (work / "main.py").write_text("print(2)")

        await run(rig, command)
        report = await rewind(rig, overwrite=True)

        assert (work / "main.py").read_text() == "print(2)", report["text"]

    async def test_a_link_outside_a_work_tree_comes_back_as_a_link(self, rig):
        target = rig.work / "target.txt"
        target.write_text("t")
        link = rig.work / "link"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("no symlinks here")

        await run(rig, lambda: link.unlink())
        report = await rewind(rig)

        assert report["status"] == REWOUND, report["text"]
        assert link.is_symlink(), "a link came back as a file holding its target"


class TestARootInsideAWorkTree:

    async def test_a_root_below_a_work_tree_keeps_its_ignore_rules(self, rig, tmp_path):
        """As src lies in the installation's repository: looked at through git, so what it ignores stays out."""
        _repo(tmp_path)
        (tmp_path / ".gitignore").write_text("build/\nstore/\nvector_*/\n")
        (rig.work / "a.txt").write_text("v1")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "c")

        def command():
            (rig.work / "a.txt").write_text("v2")
            (rig.work / "build").mkdir()
            (rig.work / "build" / "out.o").write_bytes(b"\x7fELF")

        await run(rig, command)
        files = [Path(f["path"]).name for f in (await listing(rig))[0]["files"]]

        assert files == ["a.txt"], files


class TestWhatGitDidNotLookAt:

    async def test_a_file_in_a_directory_ignored_as_a_whole_is_left_alone(self, rig):
        """status lists "!! build/" once for the directory: every file below it was ignored before."""
        work = rig.work
        _repo(work)
        (work / ".gitignore").write_text("build/\n")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        (work / "build").mkdir()
        (work / "build" / "out.bin").write_text("USER DATA")

        await run(rig, lambda: (work / ".gitignore").write_text(""))
        entries = await listing(rig)
        report = await rewind(rig)

        assert (work / "build" / "out.bin").read_text() == "USER DATA", report["text"]
        assert (work / ".gitignore").read_text() == "build/\n"
        assert any("partly not recorded" in tool for tool in counted(entries)), entries

    async def test_a_call_that_wrote_an_ignored_file_is_named_on_its_turns_checkpoint(self, rig):
        work = rig.work
        _repo(work)
        (work / ".gitignore").write_text(".env\n")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        (work / ".env").write_text("SECRET=1")
        os.utime(work / ".env", ns=(1_000_000_000, 1_000_000_000))

        shell = _Shell()
        shell.commands.extend([lambda: (work / "a.txt").write_text("A"),
                               lambda: (work / ".env").write_text("SECRET=2")])
        rig.registry.register("shell", shell)
        # One step after the other: a call beside the first would see the second's change as its own.
        await rig.turn("build", [call("shell_execute")], [call("shell_execute")],
                       agent=rig.agent("with_shell", allowed=["fs/*", "shell/*"]))

        assert counted(await listing(rig)) == [
            "shell_execute (partly not recorded: files the work tree ignores)"]
        # in words it is no "whatever they changed is still as they left it": a.txt is put back, .env is not
        said = (await rig.checkpoints(agent=rig.agent("with_shell")))["text"]
        assert "Partly tracked: shell_execute x1 (files the work tree ignores) -- that part is still" in said
        assert "Not tracked" not in said

    async def test_a_change_to_ignored_files_alone_makes_no_checkpoint(self, rig):
        """A service writes its ignored log while any call runs: a checkpoint for that would come with every
        call and push the real ones out."""
        work = rig.work
        _repo(work)
        (work / ".gitignore").write_text("*.log\n")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        (work / "api.log").write_text("1")

        await run(rig, lambda: (work / "api.log").write_text("12"))

        assert await listing(rig) == []

    async def test_a_submodule_the_call_checked_out_is_named_and_left_alone(self, rig, tmp_path):
        """git status lists no submodule checked out at its commit: its files would pass unseen."""
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _repo(upstream)
        (upstream / "s.txt").write_text("s")
        git(upstream, "add", ".")
        git(upstream, "commit", "-qm", "c")
        work = rig.work
        _repo(work)
        git(work, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "mod")
        git(work, "commit", "-qm", "sub")
        git(work, "submodule", "deinit", "-q", "-f", "mod")

        await run(rig, lambda: git(work, "-c", "protocol.file.allow=always", "submodule", "update", "-q",
                                   "--init"))
        entries = await listing(rig)
        report = await rewind(rig)

        assert (work / "mod" / "s.txt").read_text() == "s", report["text"]
        assert any("partly not recorded" in tool for tool in counted(entries)), entries

    async def test_a_call_that_deleted_an_ignored_file_is_named(self, rig):
        """Not recorded, and not to be taken for "nothing changed": a rewind would say "as before"."""
        work = rig.work
        _repo(work)
        (work / ".gitignore").write_text(".env\n")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        (work / ".env").write_text("SECRET=1")

        await run(rig, lambda: ((work / ".env").unlink(), (work / "a.txt").write_text("A")))

        assert counted(await listing(rig)) == [
            "shell_execute (partly not recorded: files the work tree ignores)"]

    async def test_a_file_in_a_submodules_place_without_its_repository_is_left_alone(self, rig):
        """The index holds a submodule whose directory has no .git: git never looked inside it."""
        work = rig.work
        _repo(work)
        (work / "top.txt").write_text("top")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        commit = git(work, "rev-parse", "HEAD").strip()
        git(work, "update-index", "--add", "--cacheinfo", f"160000,{commit},sub")
        git(work, "commit", "-qm", "sub")
        (work / "sub").mkdir()
        (work / "sub" / "x.txt").write_text("USER DATA")

        await run(rig, lambda: _repo(work / "sub"))
        report = await rewind(rig)

        assert (work / "sub" / "x.txt").read_text() == "USER DATA", report["text"]

    async def test_a_rename_git_lists_in_the_work_tree_column_keeps_its_source_absent(self, rig):
        """" R new\0old": an intent-to-add rename. old.txt was gone before the call; a rewind must not make it."""
        work = rig.work
        _repo(work)
        (work / "old.txt").write_text("content")
        git(work, "add", ".")
        git(work, "commit", "-qm", "c")
        (work / "old.txt").rename(work / "new.txt")
        git(work, "add", "-N", "new.txt")
        assert git(work, "status", "--porcelain=v1").startswith(" R"), "fixture: git lists no rename"

        await run(rig, lambda: (git(work, "add", "-A"), git(work, "commit", "-qm", "rename")))
        report = await rewind(rig)

        assert not (work / "old.txt").exists(), report["text"]
        assert (work / "new.txt").read_text() == "content"


class TestWhatCouldNotBeRead:

    async def test_a_directory_whose_repository_cannot_be_told_does_not_stop_the_look(self, rig, monkeypatch):
        """Without search permission (POSIX) asking for its .git raises: the look goes on around it."""
        (rig.work / "locked").mkdir()
        real = Path.exists

        def exists(self, *args, **kwargs):
            if self.name == ".git" and self.parent.name == "locked":
                raise PermissionError(13, "Permission denied", str(self))
            return real(self, *args, **kwargs)
        monkeypatch.setattr(Path, "exists", exists)

        await run(rig, lambda: (rig.work / "a.txt").write_text("A"))
        entries = await listing(rig)

        assert entries and counted(entries) == [], entries

    async def test_a_directory_that_could_not_be_listed_before_is_left_alone(self, rig, monkeypatch):
        """As after a chmod by the call: files that were there unseen are not "created"."""
        private = rig.work / "private"
        private.mkdir()
        (private / "key.txt").write_text("USER DATA")
        denied = {os.path.normcase(str(private))}
        scandir = os.scandir

        def guarded(path="."):
            if os.path.normcase(os.fspath(path)) in denied:
                raise PermissionError(13, "denied", os.fspath(path))
            return scandir(path)

        monkeypatch.setattr(os, "scandir", guarded)
        await run(rig, denied.clear)
        entries = await listing(rig)
        report = await rewind(rig)

        assert (private / "key.txt").read_text() == "USER DATA", report["text"]
        assert any("partly not recorded" in tool for tool in counted(entries)), entries

    @pytest.mark.parametrize("inside_a_repo", [False, True])
    async def test_a_rename_of_case_only_keeps_the_file(self, rig, monkeypatch, inside_a_repo):
        """On macOS normcase changes nothing and the file system takes Foo.txt and foo.txt for one file:
        what that looks like here, on a case-insensitive file system, with the key left as spelled."""
        import importlib
        snapshot = importlib.import_module("plugins.file_checkpoints.snapshot")
        probe = rig.work / "Probe"
        probe.write_text("")
        if not (rig.work / "probe").exists():
            pytest.skip("needs a case-insensitive file system")
        probe.unlink()
        monkeypatch.setattr(snapshot, "_key", str)
        work = rig.work
        if inside_a_repo:
            _repo(work)
        (work / "Foo.txt").write_text("USER DATA")
        if inside_a_repo:
            git(work, "add", ".")
            git(work, "commit", "-qm", "c")

        def command():
            if inside_a_repo:      # git lists a case-only rename only once it is committed
                git(work, "mv", "Foo.txt", "foo.txt")
                git(work, "commit", "-qm", "rename")
            else:
                os.rename(work / "Foo.txt", work / "foo.txt")

        await run(rig, command)
        report = await rewind(rig)

        assert [p.name for p in work.iterdir() if p.name.lower() == "foo.txt"], report["text"]
        assert (work / "foo.txt").read_text() == "USER DATA"


class _BlockingShell(_Shell):
    """A shell whose one call writes a file and then runs until the run is stopped."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.started = asyncio.Event()

    async def shell_execute(self, params):
        self.path.write_text("by the shell")
        self.started.set()
        await asyncio.sleep(60)
        return {"status": "success"}


class TestWhatTheLookLeavesOut:

    async def test_an_install_into_a_venv_is_named(self, rig):
        """pip writes into site-packages: the venv's own mtime stays."""
        packages = rig.work / ".venv" / "Lib" / "site-packages"
        packages.mkdir(parents=True)
        (rig.work / "a.txt").write_text("A")
        old = 1_000_000_000
        os.utime(packages, ns=(old, old))
        os.utime(rig.work / ".venv", ns=(old, old))

        await run(rig, lambda: (packages / "requests").mkdir())

        assert counted(await listing(rig)) == ["shell_execute (partly not recorded: .venv directories)"]


class TestCallsThatDoNotReportBack:

    def test_only_a_calls_own_endings_lead_back_to_its_run(self, monkeypatch):
        """A script's call leads back to its run; a sub-agent's run does not lead to every other one."""
        from agent_system.core.request_context import in_request_tree
        from plugins.file_checkpoints import hooks
        live = ["run1", "sub_other", "job_002", "chat"]
        monkeypatch.setattr(hooks, "run_is_live", lambda rid: any(in_request_tree(f, rid) for f in live))

        streams = live
        monkeypatch.setattr(hooks, "run_streams", lambda rid: rid in streams)

        assert hooks._run_live("run1_001_ts01")
        assert hooks._run_live("run1_1000_ts01"), "a call number past 999"
        assert hooks._run_live("run1_042_a1b2c3d4e5f6_mcp001"), "a call through the MCP gateway"
        assert not hooks._run_live("sub_mine"), "any live sub-agent kept this run going"
        assert not hooks._run_live("run2_001")
        assert not hooks._run_live("job_001"), "a client's next run kept its ended one going"
        assert not hooks._run_live("chat_2"), "a client's ended run kept going by the one it extends"

    async def test_a_scripts_call_still_running_is_not_taken_for_stopped(self, rig):
        """A script's calls carry ids derived from their run's (<run>_NNN_tsNN); while the run goes, a look
        under such an id is its call's, not one to let go."""
        shell = _BlockingShell(rig.work / "made.txt")
        rig.registry.register("shell", shell)
        agent = rig.agent("with_shell", allowed=["fs/*", "shell/*"])
        turn = asyncio.ensure_future(rig.turn("build", [call("shell_execute")], agent=agent))
        try:
            await asyncio.wait_for(shell.started.wait(), 30)
            [key] = list(rig.plugin._snapshots)
            derived = (f"{key[0]}_001_ts01", *key[1:])
            rig.plugin._snapshots[derived] = rig.plugin._snapshots.pop(key)

            entries = await listing(rig)

            assert derived in rig.plugin._snapshots, "a look of a call still running was let go"
            assert counted(entries) == [], entries
        finally:
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn

    async def test_a_call_whose_run_stopped_is_counted(self, rig):
        shell = _BlockingShell(rig.work / "made.txt")
        rig.registry.register("shell", shell)
        agent = rig.agent("with_shell", allowed=["fs/*", "shell/*"])
        turn = asyncio.ensure_future(rig.turn("build", [call("shell_execute")], agent=agent))
        await asyncio.wait_for(shell.started.wait(), 30)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        entries = await listing(rig)

        assert counted(entries) == ["shell_execute (not recorded: the run stopped before it reported back)"], \
            entries

    async def test_a_call_whose_look_was_cut_off_is_counted(self, rig, monkeypatch):
        import importlib
        import threading
        snapshot = importlib.import_module("plugins.file_checkpoints.snapshot")
        looking, go_on = threading.Event(), threading.Event()
        take = snapshot.Snapshot.take

        def slow_take(self):
            looking.set()
            go_on.wait(30)
            return take(self)

        monkeypatch.setattr(snapshot.Snapshot, "take", slow_take)
        rig.registry.register("shell", _Shell())
        agent = rig.agent("with_shell", allowed=["fs/*", "shell/*"])
        turn = asyncio.ensure_future(rig.turn("build", [call("shell_execute")], agent=agent))
        await asyncio.to_thread(looking.wait, 30)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        go_on.set()

        for _ in range(100):
            entries = await listing(rig)
            if counted(entries):
                break
            await asyncio.sleep(0.05)

        assert counted(entries) == ["shell_execute (not recorded: the look before it was cut off)"], entries
