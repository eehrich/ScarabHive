"""One vocabulary for path boundaries — a single resolution point for plugins.

Several plugins take a path straight from LLM arguments and may only use it
inside a set of allowed roots. Each had grown its own reading of that idea,
and they disagreed on three points:

* **What a relative path counts against.** ``media_ops`` captured a base at
  startup, ``file_ops`` resolved against whatever the process working
  directory happened to be at call time. Both call sites still pass
  ``Path.cwd()`` — there is nothing better to pass, ``AgentSystemConfig`` has
  no project root — so this does NOT yet make the boundary independent of the
  working directory. What it does buy: the base is captured ONCE, at
  construction. A ``chdir`` at runtime used to move ``file_ops``' boundary
  under it; now it cannot. Making the base something other than the cwd is a
  separate change, and it needs a project root to exist first.
* **Whether ``..`` is allowed.** ``file_ops`` rejected any path containing
  ``..`` lexically; ``media_ops`` resolved it and judged the result.
* **Whether a read-only mode exists.** ``file_ops`` had one, ``media_ops`` did
  not — even though both write.

The rules, decided here once:

**Fail closed.** No roots means nothing is allowed, never everything. That is
the most expensive line in this file if it is ever written the other way
round.

**Canonicalize first, compare second.** ``resolve()`` follows symlinks and
collapses ``..``; containment is then checked on the result. The order is
deliberate: for ``a/link/..`` the operating system lands in the parent of the
link's TARGET, not in the lexical parent. Normalizing lexically first means
judging a path nobody will open.

**No lexical ban on ``..``.** It is powerless once the path is canonicalized
and contained: ``data/../../etc/passwd`` resolves outside and is refused,
``data/../data/x`` resolves inside and IS the file that was named. An extra
ban only rejects harmless paths — and a rule that gets in the way daily gets
worked around instead of followed.

**A LEADING ``~`` is refused, a ``~`` inside a name is not.** ``resolve()``
does not expand ``~``, so ``~/notes.md`` becomes a directory literally named
``~``. Where an allowed root is an ancestor of the base — ``allowed_directories:
[.]`` — that lands INSIDE the sandbox: the write succeeds, reports success, and
the model believes it wrote to the home directory. Containment cannot catch
this because nothing escaped; only the meaning did. Refusing beats expanding:
the boundary should not reach into the home directory, it should name the
mistake. ``src/tmp/~lock.db`` stays allowed — an ordinary filename.

Deliberate false positive in that rule: ``~lock.db`` as the FIRST component is
refused too, because it cannot be told apart from ``~user`` — ``expanduser``
reads it that way. A refused editor lock file is friction the caller sees and
routes around by naming its directory; a silent write into a directory called
``~`` is a wrong result nobody sees. Do not "fix" this without reopening that
trade.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable

from agent_system.paths import resolve_data_path

logger = logging.getLogger(__name__)


def remote_outside(path: str, base: Path, roots: Iterable[Path]) -> bool:
    """Whether ``path`` names a host or a device (``\\\\host\\share``, ``//host/share``,
    ``\\\\?\\...``, ``\\\\.\\...``) that lies in no root -- judged on the text alone.

    Resolving such a path is not lexical: Windows opens it, and for a share that
    means connecting to the host and signing in with the user's NTLM credentials
    -- before containment could refuse it (measured in file_ops: four file system
    calls on the host per refused path). ``abspath`` and ``commonpath`` only
    compute, so nothing here is opened. A share that is itself a root is allowed.
    """
    # \??\ is the NT object namespace: no drive to pathlib, yet \??\UNC\host\share reaches the host too
    if path.replace("/", "\\").startswith("\\??\\"):
        return True
    if not PureWindowsPath(path).drive.startswith("\\\\"):
        return False
    target = os.path.normcase(os.path.abspath(os.path.join(base, path)))
    for root in roots:
        folder = os.path.normcase(str(root))
        try:
            if os.path.commonpath([target, folder]) == folder:
                return False
        except ValueError:  # another drive or host
            continue
    return True


class PathSandboxDenied(Exception):
    """The path lies outside the allowed roots — or may not be written."""


@dataclass(frozen=True)
class PathSandbox:
    """Allowed roots plus the base that relative paths count against.

    Args:
        roots: canonicalized absolute directories. EMPTY means nothing allowed.
        base: what a relative input path is resolved against.
        read_only: when True, every call with ``write=True`` is refused.
    """

    roots: tuple[Path, ...]
    base: Path
    read_only: bool = False

    @classmethod
    def from_config(
        cls,
        allowed_directories: Iterable[str] | None,
        *,
        base: Path | str,
        read_only: bool = False,
    ) -> "PathSandbox":
        """Build from plugin configuration.

        Relative entries in ``allowed_directories`` count against ``base``, so
        ``- data`` names the same root in every configuration regardless of
        where the process was started.
        """
        resolved_base = Path(base).resolve()
        # A data/... root lands in the data directory like a data/... request
        # does (resolve below) -- a root left behind would deny every one.
        roots = []
        for entry in allowed_directories or ():
            root = resolve_data_path(entry)
            roots.append((root if root.is_absolute() else resolved_base / root).resolve())
        if not roots:
            logger.warning(
                "PathSandbox has no allowed directories — every path will be "
                "refused (fail closed)")
        return cls(roots=tuple(roots), base=resolved_base, read_only=read_only)

    def resolve(self, path: str, *, write: bool = False) -> Path:
        """Canonicalize the path and return it if it lies inside.

        Raises:
            PathSandboxDenied: outside every root, unusable path, or a write
                attempt against a read-only sandbox.
        """
        if write and self.read_only:
            raise PathSandboxDenied(
                f"Sandbox is read-only, refusing to write: {path}")

        if isinstance(path, str) and remote_outside(path, self.base, self.roots):
            logger.warning("PathSandbox rejected a host or device path: %r", path)
            raise PathSandboxDenied(
                f"Path is outside the allowed directories: {path}. "
                f"Allowed: {self.describe_roots()}")
        try:
            candidate = Path(path)
            # A leading "~" is a home-directory intent that nothing here
            # honours: resolve() leaves it as a literal directory name, so with
            # a root that is an ancestor of the base the write lands INSIDE the
            # sandbox and reports success. Containment cannot see it — nothing
            # escaped, only the meaning did. A "~" further along the path is an
            # ordinary character and stays allowed.
            if candidate.parts and candidate.parts[0].startswith("~"):
                raise PathSandboxDenied(
                    f"Path starts with '~', which is not expanded here and "
                    f"would create a directory literally named '~': {path}. "
                    f"Use an explicit path under: {self.describe_roots()}")
            # A relative data/... names the data directory, wherever it is
            # configured (agent_system/paths.py) -- prompts and skills brief
            # the model with those paths. Nothing configured, nothing moves.
            candidate = resolve_data_path(candidate)
            full = (candidate if candidate.is_absolute() else self.base / candidate).resolve()
        except (OSError, ValueError) as exc:
            # Embedded null bytes (ValueError on every platform) and
            # over-long paths on Windows. A separate null-byte check used to
            # live here and was measurably dead — the mutation "check removed"
            # stayed green because this branch produces the same refusal. Do
            # not add it back.
            #
            # NOT the Windows device names: measured 05.09.2026, `Path("CON")`
            # and its siblings (PRN, AUX, COM1-9, LPT1-9) resolve without an
            # error and land inside the roots, so a caller that needs them
            # refused has to say so itself. `NUL` is the odd one out — it
            # resolves to \\.\NUL, which then fails containment.
            raise PathSandboxDenied(f"Path cannot be resolved: {path} ({exc})") from exc

        if not any(full == root or root in full.parents for root in self.roots):
            logger.warning("PathSandbox rejected out-of-root path: %r", path)
            raise PathSandboxDenied(
                f"Path is outside the allowed directories: {path}. "
                f"Allowed: {self.describe_roots()}")

        return full

    def describe_roots(self) -> str:
        """The roots as the model sees them in a refusal message."""
        return ", ".join(str(r) for r in self.roots) or "(none)"

    def relative(self, resolved: Path) -> str:
        """Path relative to the closest root, absolute when none matches."""
        for root in self.roots:
            try:
                return str(resolved.relative_to(root))
            except ValueError:
                continue
        return str(resolved)


# Deliberately NO helper that reads the configuration itself: the plugins get
# their values differently (top-level attributes, a `config:` sub-block, their
# own defaults). A shared reader would be a third reading next to the two that
# already exist. What is shared is the boundary, not the parsing.
