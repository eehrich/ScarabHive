"""Confinement for child processes — one vocabulary, swappable backends.

``PathSandbox`` governs paths WE resolve. This governs what a process we spawn
can reach, which is a different problem: once ``bash -c`` runs, no argument
check can bind it. ``terminal/security.py`` says so about itself — a pattern
list "can never contain what an agent is able to do: everything below is
reachable through ``sh -c``, a pipe, ``xargs`` or ``python -c``". That is
correct, and it leaves only two settings: the tool is granted or it is not.

This adds the third: granted AND confined. Not by inspecting the command —
by handing the kernel a process that cannot reach outside its workspace.

The contract, one line: ``confine(argv)`` returns the argv to spawn INSTEAD of
yours, wrapped so the process and everything it spawns runs confined. When no
backend can enforce the requested mode it RAISES. It never returns the argv
unconfined — a sandbox that silently degrades is worse than none, because the
caller stops checking.

Modes describe file effects only. No network, syscall or device restrictions
are expressed here; claiming them would be a lie the vocabulary cannot keep.
Reads stay allowed everywhere, in every mode and on every backend.

    read-only            the process may not modify files
    workspace-write      it may modify files under the workspace root
    danger-full-access   no restriction (what the plugin did before)

Both confining modes leave the process what it needs to run at all: the
harmless devices (``/dev/null`` and friends, its terminal, pseudo-terminals it
opens itself) and a temp directory. Which temp directory differs per backend,
see below.

Backends, chosen by platform — never by configuration, so a config cannot ask
for a weaker one:

    Linux    bubblewrap (``bwrap``), enforcement "full"
    macOS    Seatbelt (``/usr/bin/sandbox-exec``), enforcement "partial"
    Windows  none — a confining mode raises

What neither backend binds in general: a confined process may still ASK an
unconfined one to act for it — a tmux server, an ssh agent, an editor over its
socket (measured under Seatbelt: a unix socket outside the workspace
connects), systemd over D-Bus. That is IPC, and the vocabulary does not claim
it; the Seatbelt profile closes the macOS channels it can name (see there).
Nor does it bind an unconfined tool that writes into the workspace and
follows a symlink a confined process left there. One deferred channel both
backends do close: the git hooks and config of the workspace's repository,
which the next unconfined ``git`` would run. What the modes hold is the
process and everything it spawns.

**The default is ``danger-full-access``.** Not because it is right, but
because every existing deployment runs that way today and a stricter default
would fail closed on the spot — on Windows always, since no backend exists
there. Confinement is opt-in until a backend covers every platform we run on.
That is a deliberate step, not the destination: with Linux and macOS covered,
the default change waits on a Windows backend alone.

Policy travels with the call, not with the backend: two callers may confine
under different modes at the same instant, and an approved escalation is just
another call with a wider mode.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

logger = logging.getLogger(__name__)

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]

SANDBOX_MODES: tuple[SandboxMode, ...] = (
    "read-only", "workspace-write", "danger-full-access")

#: Enforcement completeness a backend claims. "none" is only ever reported for
#: danger-full-access, so a caller can tell "not confined" from "confined by a
#: mechanism that covers part of the surface".
Enforcement = Literal["full", "partial", "none"]


class SandboxUnavailable(Exception):
    """No backend can enforce the requested mode on this host.

    Deliberately an exception rather than a falsy return: the one failure mode
    that must never be quietly ignored is running unconfined while believing
    otherwise.
    """


@dataclass(frozen=True)
class ConfinedCommand:
    """What to spawn, and how well it is actually held."""

    argv: tuple[str, ...]
    enforcement: Enforcement
    backend: str


@dataclass(frozen=True)
class ProcessSandbox:
    """A mode plus the workspace it applies to."""

    mode: SandboxMode = "danger-full-access"
    workspace_root: Path = Path(".")

    @classmethod
    def from_config(cls, config: dict | None, *, base: Path | str) -> "ProcessSandbox":
        """Build from a plugin's ``sandbox:`` block.

        An unknown mode is refused at load rather than silently downgraded —
        a typo in the mode name must not become "no confinement".
        """
        cfg = config or {}
        mode = cfg.get("mode", "danger-full-access")
        if mode not in SANDBOX_MODES:
            raise ValueError(
                f"unknown sandbox mode {mode!r}; expected one of "
                f"{', '.join(SANDBOX_MODES)}")
        root = cfg.get("workspace_root") or base
        resolved = Path(root)
        if not resolved.is_absolute():
            resolved = Path(base) / resolved
        return cls(mode=mode, workspace_root=resolved.resolve())

    @property
    def confines(self) -> bool:
        """Whether this sandbox restricts anything at all."""
        return self.mode != "danger-full-access"

    def confine(self, argv: Sequence[str], *, cwd: Path | str | None = None) -> ConfinedCommand:
        """Return the argv to spawn instead of ``argv``.

        Raises:
            SandboxUnavailable: the mode requires enforcement and no backend
                on this host can provide it.
        """
        argv = tuple(argv)
        if not self.confines:
            return ConfinedCommand(argv=argv, enforcement="none", backend="none")

        backend = _select_backend()
        if backend is None:
            why = f" ({_backend_problem})" if _backend_problem else ""
            raise SandboxUnavailable(
                f'sandbox mode "{self.mode}" is requested but no sandbox backend '
                f"is usable on this host{why}; refusing to run the command "
                f"unconfined. Linux needs bubblewrap, macOS a working "
                f"/usr/bin/sandbox-exec, Windows has none — otherwise switch the "
                f"consumer to danger-full-access.")
        confined = backend.wrap(self, argv, cwd)
        _announce(confined, self.mode)
        return confined

    def describe_for_model(self) -> str:
        """One sentence for a tool description: what the sandbox does to commands.

        Empty for danger-full-access, so an unconfined deployment's tool text
        stays exactly as it was. Decided without probing (see
        ``expected_enforcement``), and the same for every call -- it belongs in
        a description that is rendered once, not in results.
        """
        if not self.confines:
            return ""
        enforcement = expected_enforcement()
        if enforcement is None:
            return (f"Commands are refused here: sandbox mode {self.mode} is set, and "
                    f"this host has no sandbox backend.")
        temp = ("$TMPDIR, which is shared with the user's other programs (partial "
                "confinement)" if enforcement == "partial" else "a private /tmp")
        if self.mode == "read-only":
            return (f"Commands run sandboxed (read-only): they cannot modify files "
                    f"except in {temp}; writes fail with a permission error.")
        return (f"Commands run sandboxed (workspace-write): they can modify files "
                f"only under {self.workspace_root} and in {temp}; other writes fail "
                f"with a permission error, and so do writes to the git hooks and "
                f"config of the workspace's repository.")


def expected_enforcement() -> "Enforcement | None":
    """The enforcement a confining mode gets on this host, without probing.

    By platform, like the backend choice itself. A backend that then fails
    its probe refuses every command -- it never runs one less confined than
    this says.
    """
    return {"macos": _Seatbelt.enforcement,
            "linux": _Bubblewrap.enforcement}.get(_host())


#: (backend, mode) pairs already logged: once per process is information,
#: once per command is noise.
_announced: set[tuple[str, str]] = set()


def _announce(confined: ConfinedCommand, mode: str) -> None:
    key = (confined.backend, mode)
    if key in _announced:
        return
    _announced.add(key)
    if confined.enforcement == "partial":
        logger.warning(
            "process sandbox: %s confines %s with PARTIAL enforcement -- see the "
            "backend's differences in agent_system/utils/process_sandbox.py",
            confined.backend, mode)
    else:
        logger.info("process sandbox: %s confines %s, enforcement %s",
                    confined.backend, mode, confined.enforcement)


# ── git metadata ────────────────────────────────────────────────────────

#: What in a repository's git directory runs code or decides what runs: hooks,
#: and the config files (core.hooksPath, core.fsmonitor, core.pager, aliases,
#: filter drivers). A confined process that may write these gets the NEXT
#: unconfined git to execute its code -- a sandbox escape with a delay.
_GIT_EXECUTABLE_METADATA = ("hooks", "config", "config.worktree")


def _git_protected(root: Path) -> list[Path]:
    """The existing paths below ``root/.git`` that must stay read-only.

    The repository's own hooks and config, those of every submodule git
    directory under ``.git/modules`` (nested submodules included), and the
    per-worktree ``config.worktree`` files. The walk under ``modules`` stops
    at each git directory, so it does not wander through objects.
    """
    git = root / ".git"
    if not git.is_dir() or git.is_symlink():
        return []
    found = [git / name for name in _GIT_EXECUTABLE_METADATA
             if (git / name).exists() or (git / name).is_symlink()]
    worktrees = git / "worktrees"
    if worktrees.is_dir():
        found += [w / "config.worktree" for w in sorted(worktrees.iterdir())
                  if (w / "config.worktree").exists()]
    pending = [git / "modules"]
    while pending:
        current = pending.pop()
        if not current.is_dir() or current.is_symlink():
            continue
        for child in sorted(current.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            if (child / "HEAD").exists():  # a submodule's git directory
                found += [child / name for name in _GIT_EXECUTABLE_METADATA
                          if (child / name).exists()]
                pending.append(child / "modules")
            else:  # part of a submodule name with a slash
                pending.append(child)
    return found


class _Bubblewrap:
    """Linux confinement via ``bwrap``.

    Chosen because it needs no native code of ours: the whole mechanism is the
    argv we prepend. Everything is mounted read-only, then the workspace is
    re-bound writable on top — bwrap applies binds in order, so the later
    writable bind wins over the earlier read-only one.

    ``--new-session`` takes the sandbox off the caller's terminal. Without it
    a confined process can push keystrokes into that terminal (the TIOCSTI
    ioctl, CVE-2017-5226), and whatever reads the terminal next -- the CLI's
    prompt, the user's shell -- runs them unconfined. Measured under Seatbelt,
    where the profile now forbids that ioctl; bwrap(1) names this flag as the
    fix. The price: the command no longer shares the caller's process group,
    so a Ctrl+C at the terminal reaches bwrap alone. ``--die-with-parent``
    passes that on to the command itself (bash), not to what bash started --
    those outlive it as orphans, still confined, as they outlive a timeout's
    kill of bash in every mode. ``--unshare-pid`` closes that too: the
    command then runs below a pid 1 of bwrap's own that dies with bwrap, and
    when a pid namespace loses its pid 1 the kernel ends everything in it --
    a Ctrl+C or a timeout's kill takes the whole tree along. The command sees
    its own processes only (``--proc`` mounts the namespace's /proc), so
    ``ps`` or ``kill`` inside cannot reach the user's other processes; the
    terminal needs nothing from there, it signals the bwrap pid it spawned.
    None of the three flags has run live -- no Linux host was at hand when
    they were added (2026-09-28); they come from bwrap(1) and its source.

    In workspace-write the workspace's git metadata that runs code is bound
    read-only again on top (see ``_git_protected``), and ``.git`` itself is
    bound onto itself, which makes it a mount point: it cannot be renamed or
    removed, so no prepared directory can take its place. Only what exists at
    spawn time is covered: a ``.git`` created later (``git init`` at the root)
    is not -- Seatbelt refuses that one by path.
    """

    name = "bubblewrap"
    enforcement: Enforcement = "full"

    def __init__(self, executable: str) -> None:
        self.executable = executable

    def wrap(self, sandbox: "ProcessSandbox", argv: tuple[str, ...],
             cwd: Path | str | None) -> ConfinedCommand:
        wrapped: list[str] = [
            self.executable,
            "--new-session", "--die-with-parent", "--unshare-pid",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            # A private /tmp: mkstemp-family tools keep working and their
            # leftovers cannot reach the host.
            "--tmpfs", "/tmp",
        ]
        # The workspace is bound in BOTH modes, always after the tmpfs. Live
        # test caught this: a workspace under /tmp is shadowed by the private
        # tmpfs, so read-only mode lost sight of the very directory it was
        # meant to expose — `cat` inside the workspace failed. Binding it back
        # (read-only or writable, per mode) restores it in either case.
        root = str(sandbox.workspace_root)
        if sandbox.mode == "workspace-write":
            wrapped += ["--bind", root, root]
            git = sandbox.workspace_root / ".git"
            if git.is_dir() and not git.is_symlink():
                wrapped += ["--bind", str(git), str(git)]
            elif git.exists() or git.is_symlink():
                # A gitfile (worktree, submodule) names the git directory;
                # re-pointing it is the same escape as rewriting a config.
                wrapped += ["--ro-bind", str(git), str(git)]
            for path in _git_protected(sandbox.workspace_root):
                wrapped += ["--ro-bind", str(path), str(path)]
        else:
            wrapped += ["--ro-bind", root, root]
        if cwd is not None:
            wrapped += ["--chdir", str(cwd)]
        wrapped += ["--"]
        wrapped += list(argv)
        return ConfinedCommand(argv=tuple(wrapped), enforcement=self.enforcement,
                               backend=self.name)


def _probe_bubblewrap() -> "_Bubblewrap | None":
    """Find a bwrap that actually runs — presence on PATH is not enough.

    In containers and on hosts with user namespaces disabled, bwrap installs
    fine and fails at exec time. A functional probe is what makes the
    fail-closed promise true instead of hopeful.
    """
    executable = shutil.which("bwrap")
    if not executable:
        return _unusable("bwrap is not installed")
    try:
        probe = subprocess.run(
            # The flags wrap() uses: a bwrap too old to know them must fail
            # here, not on every command.
            [executable, "--new-session", "--die-with-parent", "--unshare-pid",
             "--ro-bind", "/", "/", "--proc", "/proc", "--", "true"],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return _unusable(f"bwrap at {executable} does not start: {exc}")
    if probe.returncode != 0:
        return _unusable(f"bwrap at {executable} failed its probe: "
                         f"{_first_line(probe.stderr)}")
    return _Bubblewrap(executable)


# ── macOS: Seatbelt ─────────────────────────────────────────────────────

#: The only sandbox-exec we run. Absolute, never looked up on PATH: a
#: ``sandbox-exec`` earlier on PATH could be anything, and SIP protects this one.
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: ``_CS_DARWIN_USER_TEMP_DIR`` and ``..._CACHE_DIR`` from <unistd.h>;
#: ``os.confstr_names`` lacks both.
_CS_DARWIN_USER_TEMP_DIR = 65537
_CS_DARWIN_USER_CACHE_DIR = 65538

#: ``_PC_CASE_SENSITIVE`` from <sys/unistd.h>; ``os.pathconf_names`` lacks it.
_PC_CASE_SENSITIVE = 11

#: MAXPATHLEN on Darwin, the buffer ``F_GETPATH`` fills.
_MAXPATHLEN = 1024

# The profile is CONSTANT text. Every path reaches it as a parameter
# (``sandbox-exec -D NAME=value``, read with ``(param "NAME")``), never by
# splicing it into the text -- so no directory name can change the profile's
# syntax. Measured on macOS 27: spliced in, a workspace named
# ``x")) (allow file-write* (subpath "/")) (allow file-write* (subpath "y``
# rewrites the profile; passed as a parameter it is just a directory.
#
# SBPL precedence, measured, because the rules below rely on it: among rules
# WITH a filter the last match wins; a rule without a filter only sets the
# default for its operation. So the specific denies come last.
_SEATBELT_HEAD = r"""(version 1)
(allow default)
(deny file-write*)
(allow file-write*
    (literal "/dev/null") (literal "/dev/zero")
    (literal "/dev/random") (literal "/dev/urandom")
    (literal "/dev/tty") (literal "/dev/ptmx") (subpath "/dev/fd")
    (require-all (regex #"^/dev/ttys[0-9]+$")
                 (extension "com.apple.sandbox.pty"))
    (subpath (param "TEMP_DIR")))
"""
_SEATBELT_WORKSPACE = r"""(allow file-write* (subpath (param "WORKSPACE_ROOT")))
(deny file-write-unlink (literal (param "WORKSPACE_ROOT")))
(deny file-write*
    (literal (param "WORKSPACE_GIT"))
    (require-all
        (subpath (param "WORKSPACE_GIT"))
        (regex #"/\.git/(modules/.+/|worktrees/[^/]+/)?(hooks|config|config\.worktree)(/|$)")))
"""
_SEATBELT_TAIL = r"""(deny file-write-unlink (literal (param "TEMP_DIR")))
(deny file-ioctl (ioctl-command TIOCSTI))
(deny file-read* file-revoke
    (require-all (regex #"^/dev/ttys[0-9]+$")
                 (require-not (extension "com.apple.sandbox.pty"))))
(deny system-fcntl (fcntl-command F_SETPROTECTIONCLASS))
(deny user-preference-write)
(deny appleevent-send)
(deny mach-lookup
    (global-name "com.apple.coreservices.launchservicesd"
                 "com.apple.coreservices.quarantine-resolver"
                 "com.apple.runningboard"))
(deny signal)
(allow signal (target same-sandbox))
"""


class _Seatbelt:
    """macOS confinement via ``sandbox-exec`` and a generated SBPL profile.

    Mirrors ``_Bubblewrap`` rule for rule, as far as Seatbelt can:

    * ``(allow default)`` and a denial of ``file-write*`` -- the same surface
      bubblewrap restricts with its read-only root. Network, IPC, process
      creation and reads stay as they were; so they do under bubblewrap, which
      unshares nothing but the mount table.
    * ``--dev /dev`` becomes writes to /dev/null, zero, random, urandom, the
      process's terminal (/dev/tty), its own descriptors (/dev/fd, so
      /dev/stdout) and pseudo-terminals. bubblewrap gives the process a private
      devpts, so it can reach only ptys it opened. Here the
      ``com.apple.sandbox.pty`` extension marks the ptys the process opened,
      and every other ``/dev/ttys*`` is closed to it for opening, reading and
      revoke -- measured: without the read rule a confined process read the
      pending input of a pty somebody else had open, and revoke() would have
      hung up the user's other terminals. Descriptors it inherited (a terminal
      as stdin) keep working, ioctls included, as under bubblewrap; only their
      name cannot be looked up, so ``tty`` answers "not a tty" (measured).
    * ``--new-session`` becomes a denial of the TIOCSTI ioctl. The process
      stays in the caller's session -- sandbox-exec has no setsid, and a new
      session would change who gets the terminal's Ctrl+C -- but it can no
      longer push keystrokes into the terminal (measured: via /dev/tty and via
      an inherited stdin, both refused; before, both went through).
    * The workspace root, writable in workspace-write; bubblewrap's writable
      bind. Its mount point cannot be removed there (EBUSY), so the root itself
      may not be unlinked here either.
    * The workspace repository's git hooks and config stay read-only, like
      bubblewrap's read-only binds over them: ``.git`` itself may not be
      created, renamed or removed, nor anything named hooks, config or
      config.worktree directly below it, below a submodule's git directory
      in ``.git/modules`` or a worktree's in ``.git/worktrees``. Measured:
      commits, branches and checkouts work; writing the config, a hook, a
      submodule's config, replacing ``.git`` or the config by rename, and
      reaching the config through a hard or symbolic link are refused. So is
      ``git init`` at the root; nested repositories are not covered (on
      either backend -- protecting them would forbid ``git clone`` in the
      workspace), nor is a bare repository planted anywhere in it. This
      covers git's own places only: a config that already points hooks or
      fsmonitor into the workspace (``core.hooksPath = scripts/hooks``) makes
      those files as writable as any other code there (measured) -- and all
      of the workspace is code the user may run unconfined later: a
      Makefile, package.json scripts, direnv's .envrc.

    Beyond bubblewrap, because macOS offers channels Linux does not (all
    measured on macOS 27, each refused only with its rule):

    * ``F_SETPROTECTIONCLASS`` -- an fcntl on a descriptor opened READ-ONLY
      changes a file's data-protection class, and a class the user's session
      cannot open locks the user out of that file (EPERM on every open).
      ``file-write*`` does not cover it; ``system-fcntl`` with that command
      does, the form Apple's own profiles use. The other fcntls that change
      a file need a writable descriptor, and ``setattrlist`` is a
      ``file-write*`` already.
    * cfprefsd writes a preference domain for any client that asks
      (``defaults write`` landed in ~/Library/Preferences, even in
      read-only): ``user-preference-write`` is denied.
    * LaunchServices starts applications for any client, unconfined
      (``open -a`` ran a throwaway app outside the sandbox). With
      launchservicesd and quarantine-resolver refused -- one alone is not
      enough, it falls back to the other -- the app's code no longer runs,
      but launchd still spawns it, suspended; refusing RunningBoard as well
      stops that spawn. AppleEvents are denied too; that rule is NOT
      measured, since that would have meant sending events to the user's
      applications -- so opening a document in an application that is
      ALREADY running is not shown to be closed.
    * Signals go only to processes of the same sandbox. Before, a confined
      process could resume that suspended app with SIGCONT, and it ran
      unconfined (measured); it could also stop or kill any process of the
      user, ScarabHive included. bubblewrap's private pid namespace hides
      them in the same way. The price, measured: a confined command cannot
      signal a background process that an EARLIER confined command started
      -- every sandbox-exec is a sandbox of its own; the terminal's
      kill_process tool, which runs unconfined, still can.
    * launchd refuses ``launchctl submit`` and ``bootstrap`` from a
      sandboxed client on its own (measured against an unconfined control);
      no rule of ours is needed there.

    Differences, each measured on macOS 27 and none of them hidden:

    * **The temp directory is the user's own, not a private one.** bubblewrap
      mounts a fresh tmpfs on /tmp that vanishes with the process. Seatbelt
      cannot mount anything. What stays writable is the per-user temp
      directory macOS assigns (``getconf DARWIN_USER_TEMP_DIR``, where TMPDIR
      points): ``mktemp(1)`` writes there whatever TMPDIR says, and Python's
      tempfile, pip, pytest and the compilers follow TMPDIR there. A private
      directory passed as TMPDIR would not reach mktemp(1). That directory is
      shared with the user's other processes, which a confined process can
      therefore disturb there -- ScarabHive's own included: the API stages
      multipart uploads there (``tempfile.mkdtemp`` in ``api/run_routes.py``), so under
      the API a confined command can alter another request's upload while it
      is being processed. That is why this backend reports
      ``enforcement="partial"``.
    * **/tmp and /var/tmp stay read-only.** Opening them would open host-wide
      directories (bubblewrap keeps /var/tmp read-only as well, and its /tmp
      is private). The cost, measured: ``/bin/bash`` 3.2 -- what the terminal
      runs on a Mac unless another bash comes first on PATH -- writes
      here-documents to /var/tmp or /tmp and falls back to the working
      directory. Under workspace-write with the
      working directory inside the workspace (the terminal's default) they
      work; in read-only mode, or from a directory outside, ``cat <<EOF``
      fails with "cannot create temp file for here document". zsh puts its
      here-documents in /tmp and fails either way, as does any tool that
      hard-codes /tmp. Under bubblewrap all of that works, in private.
    * **The host's /tmp stays visible** -- readable, and its sockets
      connectable (bubblewrap's private /tmp hides both).
    * **Paths are compared without regard to case**, even on a case-sensitive
      volume (measured, for ``subpath`` and ``regex`` alike). Where two
      spellings can name two directories, a case variant of the workspace
      would be writable too; see ``_case_sensitive_ancestor``, which refuses
      that situation instead of confining loosely.
    * **No sandbox inside the sandbox.** A confined process cannot apply a
      Seatbelt profile of its own (``sandbox_apply: Operation not
      permitted``), so tools that sandbox their own children (Claude Code's
      shell sandbox, SwiftPM) fail inside. For the same reason this backend is
      unusable when ScarabHive itself already runs sandboxed; the probe finds
      that out.

    ``cwd`` is not part of the argv: sandbox-exec starts the command where it
    was started itself, so the caller's spawn ``cwd`` carries over -- the
    terminal passes the same directory to both.
    """

    name = "seatbelt"
    enforcement: Enforcement = "partial"

    def __init__(self, executable: str, temp_dir: str) -> None:
        self.executable = executable
        # Already the kernel's spelling, and already checked against case
        # sensitivity -- by the probe, which owns that decision.
        self.temp_dir = temp_dir

    def wrap(self, sandbox: "ProcessSandbox", argv: tuple[str, ...],
             cwd: Path | str | None) -> ConfinedCommand:
        profile = _SEATBELT_HEAD
        params = ["-D", f"TEMP_DIR={self.temp_dir}"]
        if sandbox.mode == "workspace-write":
            root = _kernel_path(sandbox.workspace_root)
            refusal = _case_sensitive_ancestor(root)
            if refusal:
                raise SandboxUnavailable(
                    f"the workspace {root} lies below {refusal}, a directory "
                    f"that tells names apart by case alone; Seatbelt compares "
                    f"paths without regard to case, so a case variant of the "
                    f"workspace would be writable as well. Refusing to "
                    f"confine loosely -- put the workspace on a "
                    f"case-insensitive volume, or switch the consumer to "
                    f"danger-full-access.")
            profile += _SEATBELT_WORKSPACE
            params += ["-D", f"WORKSPACE_ROOT={root}",
                       "-D", f"WORKSPACE_GIT={os.path.join(root, '.git')}"]
        profile += _SEATBELT_TAIL
        wrapped = [self.executable, "-p", profile, *params, "--", *argv]
        return ConfinedCommand(argv=tuple(wrapped), enforcement=self.enforcement,
                               backend=self.name)


def _kernel_path(path: Path | str) -> str:
    """The spelling Seatbelt compares against: the kernel's path of the vnode.

    Seatbelt matches the path the kernel resolved, not the one the process
    typed -- measured: ``/tmp/x`` is ``/private/tmp/x`` to it, and
    ``/System/Volumes/Data/private/tmp/x`` (the firmlinked data volume) is
    ``/private/tmp/x`` as well. A profile naming another spelling never
    matches, and the workspace is not writable at all. ``realpath`` resolves
    symlinks but not firmlinks; ``F_GETPATH`` asks the kernel for exactly its
    own spelling.

    A root that does not exist yet keeps its missing tail below the deepest
    ancestor that does.
    """
    import fcntl  # POSIX only; this module is also imported on Windows

    resolved = Path(os.path.realpath(path))
    current, tail = resolved, []
    while not current.exists() and current.parent != current:
        tail.append(current.name)
        current = current.parent
    get_path = getattr(fcntl, "F_GETPATH", None)
    if get_path is None:  # not Darwin: nothing better than realpath
        return str(resolved)
    try:
        fd = os.open(current, os.O_RDONLY)
    except OSError:  # e.g. a directory we may not read: keep realpath's answer
        return str(resolved)
    try:
        raw = fcntl.fcntl(fd, get_path, bytes(_MAXPATHLEN))
    except OSError:
        return str(resolved)
    finally:
        os.close(fd)
    kernel = os.fsdecode(raw.split(b"\0", 1)[0])
    return os.path.join(kernel, *reversed(tail))


def _case_sensitive_ancestor(path: str) -> str | None:
    """The nearest directory above ``path`` that tells names apart by case.

    Seatbelt folds case when it matches a path (measured on a case-sensitive
    APFS image: with the workspace ``.../ws``, a sibling ``.../WS`` was
    writable). A case variant of ``path`` differs from it in some component,
    so it lives in one of the directories ABOVE ``path`` -- and only a
    directory on a case-sensitive file system can hold such a variant beside
    the original. None of them may be one. A directory that cannot answer
    counts as one: unknown is not safe.
    """
    for ancestor in Path(path).parents:
        if not ancestor.exists():
            # Below the deepest existing ancestor nothing is mounted yet; that
            # one decides, and it comes up further along this loop.
            continue
        try:
            if os.pathconf(ancestor, _PC_CASE_SENSITIVE) != 0:
                return str(ancestor)
        except (OSError, ValueError):
            return str(ancestor)
    return None


def _probe_seatbelt() -> "_Seatbelt | None":
    """Find a sandbox-exec that confines — presence is not enough here either.

    It is deprecated and might one day be removed, or turned into something
    that starts the command without applying anything. And it fails at exec
    time when this process is already sandboxed. So one run of the REAL
    profile: it must let a write to /dev/null through (it compiles, applies,
    and allows what it should) and refuse to create a file in a throwaway
    directory outside everything it opens (it enforces).
    """
    if not os.path.exists(_SANDBOX_EXEC):
        return _unusable(f"{_SANDBOX_EXEC} does not exist")
    try:
        temp = os.confstr(_CS_DARWIN_USER_TEMP_DIR)
    except (OSError, ValueError) as exc:
        return _unusable(f"the user temp directory is unknown: {exc}")
    if not temp:
        return _unusable("the user temp directory is unknown")
    temp_dir = _kernel_path(temp)
    refusal = _case_sensitive_ancestor(temp_dir)
    if refusal:
        return _unusable(
            f"the user temp directory {temp_dir} lies below {refusal}, which "
            f"tells names apart by case; Seatbelt matches without case")
    backend = _Seatbelt(_SANDBOX_EXEC, temp_dir)
    try:
        scratch = tempfile.mkdtemp(prefix="scarabhive-sandbox-probe-",
                                   dir=_probe_scratch_parent())
    except OSError as exc:
        return _unusable(f"no scratch directory for the probe: {exc}")
    try:
        target = os.path.join(scratch, "escaped")
        # The REAL profile, in workspace-write so every part of it compiles:
        # /dev/null must go through, a file outside the temp directory and
        # the workspace must not.
        confined = backend.wrap(
            ProcessSandbox(mode="workspace-write", workspace_root=Path(temp_dir)),
            ("/bin/sh", "-c", ': > /dev/null && echo WROTE; : > "$0" && echo ESCAPED',
             target), None)
        try:
            ran = subprocess.run(confined.argv, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            return _unusable(f"{_SANDBOX_EXEC} does not start: {exc}")
        if os.path.lexists(target) or b"ESCAPED" in ran.stdout:
            return _unusable(f"{_SANDBOX_EXEC} runs but does not enforce: a write "
                             f"outside the workspace went through")
        if b"WROTE" not in ran.stdout:
            return _unusable(f"{_SANDBOX_EXEC} failed its probe (exit "
                             f"{ran.returncode}): {_first_line(ran.stderr)}")
        return backend
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _probe_scratch_parent() -> str:
    """Where the probe tries its forbidden write: the user's cache directory.

    It must lie outside everything the profile opens -- the user temp
    directory above all -- and belong to the user alone.
    """
    return os.confstr(_CS_DARWIN_USER_CACHE_DIR)


# ── selection ───────────────────────────────────────────────────────────

#: Cached backend selection. A probe spawns a process; doing that per command
#: would cost more than the command itself.
_backend_cache: object = ...

#: Why the last probe found nothing usable -- named in the refusal, so the
#: operator reads the cause, not only the verdict.
_backend_problem: str = ""


def _unusable(reason: str) -> None:
    """Record why no backend is usable, and answer None for the probe."""
    global _backend_problem
    _backend_problem = reason
    logger.info("process sandbox: %s", reason)
    return None


def _first_line(raw: bytes) -> str:
    text = raw.decode(errors="replace").strip()
    return text.splitlines()[0][:200] if text else "no output"


def _host() -> str:
    """Which backend family this host gets: ``macos``, ``windows`` or ``linux``.

    Linux stands for every other POSIX host: bwrap exists nowhere else, and
    its probe then says so.
    """
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def _select_backend():
    global _backend_cache
    if _backend_cache is ...:
        host = _host()
        if host == "macos":
            _backend_cache = _probe_seatbelt()
        elif host == "windows":
            _backend_cache = _unusable("there is no sandbox backend for Windows")
        else:
            _backend_cache = _probe_bubblewrap()
        logger.info("process sandbox backend: %s",
                    getattr(_backend_cache, "name", "none available"))
    return _backend_cache


def reset_backend_cache() -> None:
    """Forget the probed backend. For tests, and after installing one."""
    global _backend_cache, _backend_problem
    _backend_cache = ...
    _backend_problem = ""
    _announced.clear()
