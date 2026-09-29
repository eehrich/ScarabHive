"""Confinement for spawned processes.

``PathSandbox`` guards paths we resolve ourselves. This guards what a process
we spawn can reach — a different problem, because once ``bash -c`` runs no
argument check binds it.

Most of this is deterministic argv construction, which is exactly what can be
tested on a host without a backend. The one thing these tests cannot show is
that the kernel honours the argv; that was verified live against bubblewrap
0.9.0 (writes outside the workspace refused through ``sh -c``, ``python3 -c``,
``$HOME`` and a bind-mounted drive; reads still allowed; read-only refusing
writes inside the workspace). ``--new-session``, ``--die-with-parent``,
``--unshare-pid`` and the read-only binds over git metadata came later
(2026-09-28) and are pinned here as argv only: no Linux host was at hand to
run them live.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_system.utils import process_sandbox as ps
from agent_system.utils.process_sandbox import (
    ProcessSandbox,
    SandboxUnavailable,
)

CMD = ("/bin/bash", "-c", "echo hi")


@pytest.fixture
def no_backend(monkeypatch):
    """A host where nothing can confine — Windows today."""
    monkeypatch.setattr(ps, "_select_backend", lambda: None)


@pytest.fixture
def bubblewrap(monkeypatch):
    """A host with a working bwrap, without needing one installed."""
    monkeypatch.setattr(ps, "_select_backend", lambda: ps._Bubblewrap("/usr/bin/bwrap"))



#: How a process gets started: (module, function). Counted as calls in the parsed source, so a comment that
#: names one does not count, and a spawn written in any of these forms does.
SPAWN_CALLS = frozenset({
    ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
    ("subprocess", "Popen"), ("subprocess", "run"), ("subprocess", "call"),
    ("subprocess", "check_output"), ("subprocess", "check_call"),
    ("os", "system"), ("os", "popen"), ("pty", "spawn"),
})
#: Imported bare, these names say what they do; ``run``, ``call`` and ``system`` would not.
BARE_SPAWN_NAMES = frozenset({"create_subprocess_exec", "create_subprocess_shell", "Popen", "check_output",
                              "check_call"})


def _calls(module, match):
    """How many calls in *module*'s source have a callee *match* accepts."""
    import ast
    import inspect

    return sum(1 for node in ast.walk(ast.parse(inspect.getsource(module)))
               if isinstance(node, ast.Call) and match(node.func))


def _is_spawn(func):
    import ast

    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr) in SPAWN_CALLS
    return isinstance(func, ast.Name) and func.id in BARE_SPAWN_NAMES


def _spawn_sites(module):
    return _calls(module, _is_spawn)

class TestFullAccessChangesNothing:
    """The default must leave existing deployments exactly as they were."""

    def test_argv_is_passed_through_untouched(self, no_backend):
        confined = ProcessSandbox().confine(CMD)
        assert confined.argv == CMD
        assert confined.enforcement == "none"

    def test_it_does_not_even_look_for_a_backend(self, monkeypatch):
        def explode():
            raise AssertionError("probed a backend for danger-full-access")
        monkeypatch.setattr(ps, "_select_backend", explode)
        assert ProcessSandbox(mode="danger-full-access").confine(CMD).argv == CMD

    def test_confines_reports_false(self):
        assert ProcessSandbox().confines is False


class TestFailClosed:
    """The one failure that must never be quiet: running unconfined anyway."""

    @pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
    def test_a_confining_mode_without_a_backend_raises(self, no_backend, mode):
        with pytest.raises(SandboxUnavailable):
            ProcessSandbox(mode=mode, workspace_root=Path("/ws")).confine(CMD)

    def test_the_message_says_what_to_install(self, no_backend):
        with pytest.raises(SandboxUnavailable, match="bubblewrap"):
            ProcessSandbox(mode="workspace-write").confine(CMD)

    def test_with_a_backend_the_same_call_succeeds(self, bubblewrap):
        """Counter-check: the tests above must fail for lack of a backend, not
        because confine() refuses everything."""
        assert ProcessSandbox(mode="workspace-write",
                              workspace_root=Path("/ws")).confine(CMD).argv != CMD


class TestBubblewrapArgv:
    @staticmethod
    def _wrap(mode, root="/ws", cwd=None):
        return ps._Bubblewrap("/usr/bin/bwrap").wrap(
            ProcessSandbox(mode=mode, workspace_root=Path(root)), CMD, cwd)

    def test_the_command_is_last_and_separated(self):
        argv = self._wrap("workspace-write").argv
        assert argv[0] == "/usr/bin/bwrap"
        assert argv[-len(CMD):] == CMD
        assert argv[-len(CMD) - 1] == "--", \
            "without the separator bwrap reads the command as its own options"

    def test_everything_is_read_only_first(self):
        argv = self._wrap("workspace-write").argv
        binds = [i for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
        assert argv[binds[0]:binds[0] + 3] == ("--ro-bind", "/", "/")

    def test_the_sandbox_is_taken_off_the_callers_terminal(self):
        """Without its own session a confined process can push keystrokes into
        the caller's terminal (TIOCSTI, CVE-2017-5226)."""
        argv = self._wrap("read-only").argv
        assert "--new-session" in argv[:argv.index("--")]

    def test_the_whole_tree_ends_with_bwrap(self):
        """A private pid namespace: when bwrap's pid 1 goes, the kernel ends
        everything below it -- not only the command, its children as well."""
        argv = self._wrap("read-only").argv
        assert "--unshare-pid" in argv[:argv.index("--")]

    def test_the_sandboxed_command_ends_with_bwrap(self):
        """Out of the caller's session, a Ctrl+C no longer reaches the command
        itself; it has to die with the bwrap the caller can signal."""
        argv = self._wrap("read-only").argv
        assert "--die-with-parent" in argv[:argv.index("--")]

    def test_workspace_write_binds_the_workspace_writable(self):
        argv = self._wrap("workspace-write").argv
        assert "--bind" in argv
        # str(Path(...)) rather than the literal: on Windows the separator
        # differs, and these tests must run where there is no backend at all.
        assert argv[argv.index("--bind") + 1] == str(Path("/ws"))

    def test_read_only_binds_the_workspace_read_only(self):
        argv = self._wrap("read-only").argv
        assert "--bind" not in argv, "read-only must not bind anything writable"
        assert argv.count("--ro-bind") == 2, "root plus workspace"

    def test_the_workspace_bind_comes_after_the_tmpfs(self):
        """Order invariant, found by a live run rather than by reading.

        ``--tmpfs /tmp`` shadows a workspace that lives under /tmp. bwrap
        applies binds in order, so the workspace must be bound AFTER it —
        otherwise read-only mode loses sight of the very directory it exists
        to expose, and ``cat`` inside the workspace fails.
        """
        root = str(Path("/tmp/ws"))
        for mode in ("read-only", "workspace-write"):
            argv = self._wrap(mode, root="/tmp/ws").argv
            tmpfs = argv.index("--tmpfs")
            workspace = max(i for i, a in enumerate(argv)
                            if a in ("--bind", "--ro-bind") and argv[i + 1] == root)
            assert workspace > tmpfs, f"{mode}: workspace bound before the tmpfs"

    def test_cwd_is_passed_when_given(self):
        argv = self._wrap("workspace-write", cwd="/ws/sub").argv
        assert "--chdir" in argv
        assert argv[argv.index("--chdir") + 1] == "/ws/sub"

    def test_no_chdir_when_none(self):
        assert "--chdir" not in self._wrap("workspace-write").argv

    def test_enforcement_is_reported_as_full(self):
        assert self._wrap("workspace-write").enforcement == "full"


class TestBubblewrapGitMetadata:
    """Hooks and config run code for the next unconfined git. Argv only: the
    kernel side was not run -- no Linux host (2026-09-28)."""

    @staticmethod
    def _repo(root: Path) -> Path:
        git = root / ".git"
        (git / "hooks").mkdir(parents=True)
        (git / "config").write_text("[core]\n")
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "refs" / "heads" / "config").write_text("0" * 40)  # a branch named config
        sub = git / "modules" / "libs" / "sub"
        (sub / "hooks").mkdir(parents=True)
        (sub / "HEAD").write_text("ref: refs/heads/main\n")
        (sub / "config").write_text("[core]\n")
        (sub / "refs" / "heads").mkdir(parents=True)
        (sub / "refs" / "heads" / "config").write_text("0" * 40)
        (git / "worktrees" / "wt").mkdir(parents=True)
        (git / "worktrees" / "wt" / "config.worktree").write_text("[core]\n")
        return git

    @staticmethod
    def _pairs(argv, flag):
        return {argv[i + 1] for i, a in enumerate(argv[:argv.index("--")])
                if a == flag and argv[i + 1] == argv[i + 2]}

    def test_hooks_and_configs_are_bound_read_only_after_the_workspace(self, tmp_path):
        git = self._repo(tmp_path)
        argv = ps._Bubblewrap("/usr/bin/bwrap").wrap(
            ProcessSandbox(mode="workspace-write", workspace_root=tmp_path), CMD, None).argv
        expected = {str(p) for p in (git / "hooks", git / "config",
                                     git / "modules/libs/sub/hooks", git / "modules/libs/sub/config",
                                     git / "worktrees/wt/config.worktree")}
        assert self._pairs(argv, "--ro-bind") >= expected
        workspace_bind = argv.index("--bind")
        for path in expected:
            assert argv.index(path) > workspace_bind, f"{path} bound before the workspace"

    def test_the_git_directory_becomes_a_mount_point(self, tmp_path):
        """A mount point cannot be renamed or removed: no prepared directory
        can take the place of .git."""
        git = self._repo(tmp_path)
        argv = ps._Bubblewrap("/usr/bin/bwrap").wrap(
            ProcessSandbox(mode="workspace-write", workspace_root=tmp_path), CMD, None).argv
        assert str(git) in self._pairs(argv, "--bind")

    def test_objects_and_refs_stay_writable(self, tmp_path):
        """The walk stops at each git directory: a branch named config is a
        ref, and commits must go on."""
        git = self._repo(tmp_path)
        argv = ps._Bubblewrap("/usr/bin/bwrap").wrap(
            ProcessSandbox(mode="workspace-write", workspace_root=tmp_path), CMD, None).argv
        bound = self._pairs(argv, "--ro-bind")
        assert str(git / "refs/heads/config") not in bound
        assert str(git / "modules/libs/sub/refs/heads/config") not in bound

    def test_a_gitfile_is_bound_read_only(self, tmp_path):
        (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
        argv = ps._Bubblewrap("/usr/bin/bwrap").wrap(
            ProcessSandbox(mode="workspace-write", workspace_root=tmp_path), CMD, None).argv
        assert str(tmp_path / ".git") in self._pairs(argv, "--ro-bind")

    def test_no_repository_no_extra_binds(self, tmp_path):
        argv = ps._Bubblewrap("/usr/bin/bwrap").wrap(
            ProcessSandbox(mode="workspace-write", workspace_root=tmp_path), CMD, None).argv
        assert argv.count("--bind") == 1

    def test_read_only_mode_needs_none(self, tmp_path):
        self._repo(tmp_path)
        argv = ps._Bubblewrap("/usr/bin/bwrap").wrap(
            ProcessSandbox(mode="read-only", workspace_root=tmp_path), CMD, None).argv
        assert "--bind" not in argv
        assert argv.count("--ro-bind") == 2


class TestConfigIsValidatedAtLoad:
    """A typo in the mode must not become 'no confinement'."""

    def test_unknown_mode_raises(self, tmp_path):
        with pytest.raises(ValueError, match="unknown sandbox mode"):
            ProcessSandbox.from_config({"mode": "workspace_write"}, base=tmp_path)

    def test_absent_config_is_full_access(self, tmp_path):
        assert ProcessSandbox.from_config(None, base=tmp_path).mode == \
            "danger-full-access"

    def test_workspace_root_defaults_to_the_base(self, tmp_path):
        box = ProcessSandbox.from_config({"mode": "read-only"}, base=tmp_path)
        assert box.workspace_root == tmp_path.resolve()

    def test_relative_workspace_root_counts_against_the_base(self, tmp_path):
        (tmp_path / "work").mkdir()
        box = ProcessSandbox.from_config(
            {"mode": "workspace-write", "workspace_root": "work"}, base=tmp_path)
        assert box.workspace_root == (tmp_path / "work").resolve()

    @pytest.mark.parametrize("mode", ["read-only", "workspace-write",
                                      "danger-full-access"])
    def test_every_documented_mode_is_accepted(self, tmp_path, mode):
        assert ProcessSandbox.from_config({"mode": mode}, base=tmp_path).mode == mode


class TestTerminalPluginUsesIt:
    """The wiring, not the mechanism: all three spawn sites must ask."""

    def test_executor_defaults_to_full_access(self):
        from plugins.terminal.executor import CommandExecutor
        from plugins.terminal.security import CommandSecurityValidator

        ex = CommandExecutor(bash_path="/bin/bash",
                             security_validator=CommandSecurityValidator())
        assert ex.sandbox.mode == "danger-full-access"

    async def test_a_host_without_a_backend_refuses_and_says_why(self, monkeypatch, tmp_path):
        """The model-facing contract of fail-closed.

        Not the log line — that is an operational nicety and testing it would
        be testing the logger. What must hold is that the command does not
        run, and that the model can tell "this host cannot confine me" from
        "your command was rejected", so it does not retry a reworded command.
        """
        from plugins.terminal.executor import CommandExecutor
        from plugins.terminal.security import CommandSecurityValidator

        monkeypatch.setattr(ps, "_select_backend", lambda: None)
        target = tmp_path / "never.txt"
        ex = CommandExecutor(
            bash_path="/bin/bash",
            security_validator=CommandSecurityValidator(),
            initial_cwd=str(tmp_path),
            sandbox=ProcessSandbox(mode="workspace-write", workspace_root=tmp_path))

        result = await ex.execute(f"echo x > {target}")

        assert result["status"] == "error"
        assert result["error_type"] == "SandboxUnavailable"
        assert not target.exists(), "the command ran despite an unusable sandbox"

    def test_every_spawn_goes_through_confine(self):
        """Anti-drift: a third spawn site added later must not skip the cage.

        The two sites are the one-shot execution paths (execute and
        execute_background), both verified live. ``PersistentTerminal``, which
        nothing ever instantiated, and its spawn site are gone.
        """
        import inspect

        from plugins.terminal import executor

        src = inspect.getsource(executor)
        spawns = _spawn_sites(executor)
        # Both call forms count: direct `.sandbox.confine(...)` and the
        # off-loop `asyncio.to_thread(self.sandbox.confine, ...)` (the first
        # confine() probes the backend with a blocking subprocess.run).
        confines = src.count(".sandbox.confine(") + src.count(".sandbox.confine,")
        assert spawns == 2, f"spawn sites changed ({spawns}) — re-check the wiring"
        assert confines == spawns, \
            f"{spawns} spawn sites but {confines} confine() calls"
