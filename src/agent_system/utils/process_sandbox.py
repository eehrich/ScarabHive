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

    read-only            the process may not modify files
    workspace-write      it may modify files under the workspace root
    danger-full-access   no restriction (what the plugin did before)

**The default is ``danger-full-access``.** Not because it is right, but
because every existing deployment runs that way today and a stricter default
would fail closed on the spot — on Windows always, since no backend exists
there. Confinement is opt-in until a backend covers every platform we run on.
That is a deliberate step, not the destination.

Policy travels with the call, not with the backend: two callers may confine
under different modes at the same instant, and an approved escalation is just
another call with a wider mode.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
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
            raise SandboxUnavailable(
                f'sandbox mode "{self.mode}" is requested but no sandbox backend '
                f"is usable on this host; refusing to run the command "
                f"unconfined. Install bubblewrap (Linux) — otherwise switch the "
                f"consumer to danger-full-access.")
        return backend.wrap(self, argv, cwd)


class _Bubblewrap:
    """Linux confinement via ``bwrap``.

    Chosen because it needs no native code of ours: the whole mechanism is the
    argv we prepend. Everything is mounted read-only, then the workspace is
    re-bound writable on top — bwrap applies binds in order, so the later
    writable bind wins over the earlier read-only one.
    """

    name = "bubblewrap"

    def __init__(self, executable: str) -> None:
        self.executable = executable

    def wrap(self, sandbox: "ProcessSandbox", argv: tuple[str, ...],
             cwd: Path | str | None) -> ConfinedCommand:
        wrapped: list[str] = [
            self.executable,
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
        else:
            wrapped += ["--ro-bind", root, root]
        if cwd is not None:
            wrapped += ["--chdir", str(cwd)]
        wrapped += ["--"]
        wrapped += list(argv)
        return ConfinedCommand(argv=tuple(wrapped), enforcement="full",
                               backend=self.name)


def _probe_bubblewrap() -> "_Bubblewrap | None":
    """Find a bwrap that actually runs — presence on PATH is not enough.

    In containers and on hosts with user namespaces disabled, bwrap installs
    fine and fails at exec time. A functional probe is what makes the
    fail-closed promise true instead of hopeful.
    """
    executable = shutil.which("bwrap")
    if not executable:
        return None
    try:
        probe = subprocess.run(
            [executable, "--ro-bind", "/", "/", "--", "true"],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("bwrap found at %s but not usable: %s", executable, exc)
        return None
    if probe.returncode != 0:
        logger.info("bwrap found at %s but the probe failed: %s",
                    executable, probe.stderr[:200])
        return None
    return _Bubblewrap(executable)


#: Cached backend selection. A probe spawns a process; doing that per command
#: would cost more than the command itself.
_backend_cache: object = ...


def _select_backend():
    global _backend_cache
    if _backend_cache is ...:
        _backend_cache = _probe_bubblewrap() if os.name != "nt" else None
        logger.info("process sandbox backend: %s",
                    getattr(_backend_cache, "name", "none available"))
    return _backend_cache


def reset_backend_cache() -> None:
    """Forget the probed backend. For tests, and after installing one."""
    global _backend_cache
    _backend_cache = ...
