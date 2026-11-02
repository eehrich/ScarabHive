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

**A read reads past an imagined root, a write does not.** Models put the root
of a sandbox they picture before a path that is right relative to the base
(``/workspace/data/...``, ``/home/user/data/...``). For a read, folders are
dropped from the first one this machine does not have on, as many as it takes
-- the path as written names nothing -- when the rest names a root that is not the base or above it
(there every rest lands inside). A write behind such a root is refused: it
would land wherever the rest points, also on a real file. Refusals
name the roots as a relative path names them, not absolute: the absolute form
taught the model this machine's paths, which no prompt can carry.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable

from agent_system.paths import PROJECT_ROOT, data_path, resolve_data_path

logger = logging.getLogger(__name__)

#: The installation's private files. No plugin path reaches them, whatever the
#: roots allow -- a root on the repository (".") or above it would otherwise
#: hand them out:
#:   config/*.env               the secrets (local.env, secrets.env)
#:   data/users.db              the user store (password hashes), with its -wal/-shm/-journal
#:                              -- or wherever auth keeps it
#:   data/sessions/             every user's conversations (AGENT_SESSION_STORAGE_PATH when set)
#:   data/session_archive/      the same, archived (beside the sessions)
#:   data/message_debugger/     every LLM request and answer of every user
SECRETS_DIR = PROJECT_ROOT / "config"
USERS_DB = Path("users.db")
SESSIONS_DIR = Path("sessions")
ARCHIVE_DIR = Path("session_archive")
DEBUGGER_DIR = Path("message_debugger")
_DB_COMPANIONS = ("", "-wal", "-shm", "-journal")


def _canonical(path: Path | str) -> str | None:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except (OSError, ValueError):
        return None


def _within(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # another drive
        return False


def private_places() -> tuple[Path, Path, tuple[Path, ...]]:
    """(secrets directory, user store, the private directories) -- read at each
    call: the data directory, the session store and the user store's place are
    configurable. A message_debugger db_path set outside its folder is not known
    here (its config lives with the plugin)."""
    data = PROJECT_ROOT / data_path()
    sessions = Path(os.getenv("AGENT_SESSION_STORAGE_PATH") or data / SESSIONS_DIR)
    users_db = data / USERS_DB
    from agent_system.auth import database  # the auth package is imported lazily throughout the tools

    if database._db is not None:  # the user store this process opened (auth's db_path)
        users_db = Path(database._db.db_path)
    folders = (sessions, sessions.parent / ARCHIVE_DIR, data / SESSIONS_DIR, data / ARCHIVE_DIR, data / DEBUGGER_DIR)
    return SECRETS_DIR, users_db, tuple(dict.fromkeys(folders))


def _private_places() -> tuple[str, str, tuple[str, ...]]:
    """private_places, canonical."""
    secrets, users_db, folders = private_places()
    return (_canonical(secrets) or "", _canonical(users_db) or "",
            tuple(c for c in (_canonical(f) for f in folders) if c))


def private_folders() -> tuple[str, ...]:
    """The private directories (private_places), canonical: read once for a whole listing, compared with
    os.path.normcase of a path that is canonical already."""
    return _private_places()[2]


def might_be_private(name: str) -> bool:
    """A cheap test on one name of a directory walk: only these can be private,
    so only these are resolved (is_secret)."""
    name = name.lower()
    if ".env" in name or name.startswith(USERS_DB.name) or name in (SESSIONS_DIR.name, ARCHIVE_DIR.name, DEBUGGER_DIR.name):
        return True
    from agent_system.auth import database  # a user store auth keeps under a name of its own

    return database._db is not None and name.startswith(Path(database._db.db_path).name.lower())


def is_secret(path: Path | str) -> bool:
    """Whether ``path`` is one of the installation's private files (above) or
    lies in one of the private directories.

    Judged on the canonical path, so a symlink, ``..`` or an 8.3 short name
    pointing at one counts. Windows opens ``local.env.``, ``local.env `` and
    ``local.env::$DATA`` as local.env: trailing dots and spaces and a stream
    suffix are dropped before the name is read.
    """
    full = _canonical(path)
    if full is None:
        return False
    parent, name = os.path.split(full)
    name = name.split(":", 1)[0].rstrip(" .")
    secrets, users_db, folders = _private_places()
    cleaned = os.path.join(parent, name)
    return ((parent == secrets and name.endswith(".env"))
            or any(cleaned == users_db + suffix for suffix in _DB_COMPANIONS)
            or any(_within(cleaned, folder) for folder in folders))


def holds_secrets(directory: Path | str) -> bool:
    """Whether deleting, moving or copying ``directory`` would take a private
    file along: it is one of their directories or an ancestor of one."""
    full = _canonical(directory)
    if full is None:
        return False
    secrets, users_db, folders = _private_places()
    return any(_within(place, full) for place in (secrets, os.path.dirname(users_db), *folders))


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
        # On Python 3.12 resolve() raises ValueError on an embedded null byte
        # and the except branch below refused it. On 3.14 (measured on
        # Windows) resolve() returns the path unchanged and containment passes
        # it; the refusal has to be explicit.
        if "\x00" in str(path):
            raise PathSandboxDenied(f"Path contains a null byte: {path!r}")
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
            # Over-long paths on Windows (null bytes are refused above).
            #
            # NOT the Windows device names: measured 05.09.2026, `Path("CON")`
            # and its siblings (PRN, AUX, COM1-9, LPT1-9) resolve without an
            # error and land inside the roots, so a caller that needs them
            # refused has to say so itself. `NUL` is the odd one out — it
            # resolves to \\.\NUL, which then fails containment.
            raise PathSandboxDenied(f"Path cannot be resolved: {path} ({exc})") from exc

        if not self._inside(full):
            behind = self._behind_an_imagined_root(Path(path))
            if behind is None or write:
                logger.warning("PathSandbox rejected out-of-root path: %r", path)
                # The read of the same path went through: say how to write it.
                hint = f". Write it as {self._as_named(behind)}" if behind is not None else ""
                raise PathSandboxDenied(
                    f"Path is outside the allowed directories: {path}. "
                    f"Allowed: {self.describe_roots()}{hint}")
            logger.info("PathSandbox read %r from the base, as %s", path, behind)
            full = behind  # read from the base: still never a private file

        if is_secret(full):
            logger.warning("PathSandbox rejected a private file: %r", path)
            raise PathSandboxDenied(f"Access to the installation's private files (secrets, user store, sessions) is refused: {path}")

        return full

    def _inside(self, full: Path) -> bool:
        return any(full == root or root in full.parents for root in self.roots)

    def _behind_an_imagined_root(self, named: Path) -> Path | None:
        """For a READ: an absolute ``named`` that cannot exist here, read from the base past its
        leading folders, or None.

        Models put the root of a sandbox they imagine before a path that is right as it stands
        relative to the project: measured 2026-10-07..14, 36 refusals, every one a correct
        ``data/...`` behind ``/workspace`` or ``/home/user``. Folders are dropped from the first
        one that does not exist on, as many as it takes: the path as written names nothing on this
        machine, so reading the file inside takes nothing it could have meant. Never for a write (the caller
        passes ``write``): a write, move or delete behind ``/tmp/build/src/...`` would land on the
        project's own ``src``. And the rest must name a root that is not the base or above it --
        there every rest lands inside, ``/tmp/x.txt`` as ``./x.txt``.
        """
        if not named.anchor or named.drive:
            return None             # relative; or a drive or share, a real place whatever is behind it
        parts = named.parts[1:]
        for cut in range(1, len(parts)):
            try:
                if Path(named.anchor, *parts[:cut]).exists():
                    continue
                full = (self.base / resolve_data_path(Path(*parts[cut:]))).resolve()
            except (OSError, ValueError):
                return None         # too long, not allowed to look: refused as before
            if any((full == root or root in full.parents)
                   and not (root == self.base or root in self.base.parents) for root in self.roots):
                return full
        return None

    def describe_roots(self) -> str:
        """The roots as the model sees them in a refusal message: as a relative path names them where one
        does (``data/...``, or below the base). The absolute form taught the model to write absolute paths,
        which no prompt can name for every machine."""
        return ", ".join(self._as_named(r) for r in self.roots) or "(none)"

    def _as_named(self, root: Path) -> str:
        # abspath, not resolve: naming the roots asks the file system nothing -- a refused host path
        # must not reach it (remote_outside).
        # The base first: a root that is the base is "." -- named data/..., a caller that joins
        # relative paths to its root (ssh_control's local_root) nested it in itself.
        for anchor, prefix in ((self.base, ""), (Path(os.path.abspath(self.base / data_path())), "data")):
            try:
                below = root.relative_to(anchor)
            except ValueError:
                continue
            return Path(prefix, below).as_posix()
        return str(root)

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
