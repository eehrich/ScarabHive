"""What a call that writes without naming its paths changed -- a shell command, a coding CLI.

file_ops and media_ops name every path before they write it; a shell command does
not. Around such a call the directories of the agent's file tools are looked at
twice, and every file that differs becomes a recorded change like any other
(``hooks.FileCheckpointsPlugin._record_snapshot``).

The state a file had before is taken only from what is KNOWN of it -- a rewind
writes it back, so a guess would destroy work:

* In a git work tree, what git lists as changed or new, and what it was told not
  to look at (skip-worktree, assume-unchanged), is copied beforehand. A file git
  tracks and does not list was as its index entry says: that content is read
  afterwards with ``git cat-file --filters`` -- the bytes as checked out. A path
  git saw and neither tracks nor lists did not exist. The repository is only
  read (``--no-optional-locks``); a commit, stash or reset the call made shows as
  what it did to the files.
* Outside work trees every file is kept in a cache, looked at again by mtime and
  size (files that recent are read again: a write within the clock's tick keeps
  both). A path the look walked to and did not find did not exist.
* Anything else is not known -- what the work tree ignored before the call, a
  submodule's commit, a directory that could not be read, a name that differs
  from a known one only in case, a git call that failed -- and is left as it
  is. A call that changed such places is named in ``Snapshot.unseen`` -- of an
  ignored directory, and of node_modules or a venv outside work trees, as far as
  the entries the directory itself holds (or a venv's site-packages) tell. A
  change to ignored files can be another process's (a service's log); the hooks
  name it on a checkpoint the turn has anyway, but make none for it alone.

Calls running at the same time in the same directories share what they changed:
each one's look sees the other's files too.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .fs import ABSENT, FILE, SYMLINK, UNREADABLE, State, fingerprint, snapshot as read_state

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 20.0
#: Directories never walked outside a work tree: rebuilt, not written by hand.
SKIPPED_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})
#: Of those, the ones whose change is worth a word: what an install put there. Told by the mtimes of the
#: directory and of the one an install writes into (a venv's site-packages; npm writes node_modules itself).
NOTED_DIRS = frozenset({"node_modules", ".venv"})
_INSTALLED_INTO = ("Lib/site-packages", "lib/python*/site-packages")
#: Why a call is partly not recorded when it changed what the work tree ignores.
IGNORED_FILES = "files the work tree ignores"
#: A file whose mtime lies this close to a look is read again at the next one.
RACY_NS = 2_000_000_000
_GIT_WORKERS = 8
#: How long the work trees found for a set of roots are taken as they were.
REGIONS_SECONDS = 60.0
_GITLINK = "160000"

#: git without the caller's GIT_* (a GIT_DIR of a hook would point every call at one
#: repository); pathspecs literal, so a file named ":!x" is that file.
_ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
_ENV.update(GIT_TERMINAL_PROMPT="0")
_PLAIN_ENV = dict(_ENV)
_ENV["GIT_LITERAL_PATHSPECS"] = "1"
_HAS_GIT = shutil.which("git") is not None

#: A cache entry: (mtime_ns, size, kind, digest, kept, mode).
Entry = Tuple[int, int, str, Optional[str], bool, Optional[int]]
_NO_ENTRY: Entry = (0, 0, ABSENT, None, False, None)


class TooBig(Exception):
    """A tree beyond the limits (or the time): the call is counted instead of recorded."""


@dataclass(frozen=True)
class Limits:
    #: Bytes of one file that are kept (larger: recorded, not restorable).
    file_bytes: int
    #: Files looked at outside work trees, copied from one, or changed in one, per call.
    files: int
    #: Bytes copied per call, and kept in the cache of files outside work trees.
    total_bytes: int
    #: Seconds one look may take (the hooks give up at 60).
    seconds: float = 40.0


@dataclass(frozen=True)
class Found:
    """One path the call changed."""

    path: str
    #: The file tool server whose directory holds it: a rewind writes it as that server may.
    server: str
    before: State
    #: Where the bytes before lie (None: not kept -- too large, unreadable, gone from the cache).
    source: Optional[Path]


def _git(cwd: Path, *args: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "--no-optional-locks", "-c", "core.quotepath=off", *args],
                          cwd=str(cwd), capture_output=True, timeout=GIT_TIMEOUT, env=env or _ENV)


def _checked(cwd: Path, *args: str) -> bytes:
    done = _git(cwd, *args)
    if done.returncode != 0:
        raise TooBig(f"git {args[0]} failed in {cwd}: {done.stderr.decode(errors='replace').strip()[:200]}")
    return done.stdout


def _key(path: Path | str) -> str:
    return os.path.normcase(str(path))


def _fold(key: str) -> str:
    """One spelling for names a case- or normalisation-insensitive file system (macOS) takes for one."""
    return unicodedata.normalize("NFC", key).casefold()


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    key = _key(path)
    return any(key == _key(root) or key.startswith(_key(root).rstrip(os.sep) + os.sep) for root in roots)


def _toplevel(directory: Path) -> Optional[Path]:
    if not _HAS_GIT or not directory.is_dir():
        return None
    try:
        done = _git(directory, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return Path(os.fsdecode(done.stdout.strip())).resolve()


def _ignored(top: Path, path: Path) -> bool:
    """Whether ``top`` ignores ``path``. check-ignore knows no literal pathspecs (it fails on them); a
    failure counts as ignored: the directory is then looked at without git, which assumes nothing."""
    done = _git(top, "check-ignore", "-q", "--", path.relative_to(top).as_posix(), env=_PLAIN_ENV)
    return done.returncode != 1


def _is_repo(directory: Path) -> bool:
    """Not to be told (a directory without search permission) reads as no repository: the walk then
    fails to read it and leaves it alone, instead of the whole look failing."""
    try:
        return (directory / ".git").exists()
    except OSError:
        return False


class _Clock:
    def __init__(self, seconds: float):
        self.until = time.monotonic() + seconds
        self.seconds = seconds

    def check(self) -> None:
        if time.monotonic() > self.until:
            raise TooBig(f"looking took longer than {self.seconds:g} s")


class _Budget:
    def __init__(self, limits: Limits):
        self.file_bytes = limits.file_bytes
        self.files = limits.files
        self.left = limits.total_bytes
        self._lock = threading.Lock()     # work trees are looked at side by side

    def take(self, size: int) -> None:
        with self._lock:
            self.left -= size
            if self.left < 0:
                raise TooBig("more bytes to copy than the limit allows")


def _keep(store: Path, content: bytes, digest: str) -> Path:
    path = store / digest
    if not path.exists():
        _write(path, content)
    return path


def _write(path: Path, content: bytes) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_bytes(content)
    os.replace(temp, path)


# --- a work tree ---------------------------------------------------------------


@dataclass
class _Repo:
    top: Path
    head: Optional[str] = None
    #: Copied before the call (listed by status, or hidden from it): key -> (path, state, copy)
    copied: Dict[str, Tuple[Path, State, Optional[Path]]] = field(default_factory=dict)
    #: Tracked and not listed: key -> (mode, object id) of the index entry
    tracked: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    #: What the ignore rules left out before the call: key (a directory's ending in os.sep) -> mtime, size
    ignored: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    #: Work trees inside it (embedded, submodules): key -> path
    nested: Dict[str, Path] = field(default_factory=dict)
    #: Submodules' places: key -> path
    gitlinks: Dict[str, Path] = field(default_factory=dict)
    #: Whether a link git tracks is checked out as a link (core.symlinks), else as a file holding its target
    symlinks: bool = True
    _folded: Optional[frozenset] = None
    _fold_lock: threading.Lock = field(default_factory=threading.Lock)

    def was_ignored(self, key: str) -> bool:
        return any(key == ignored.rstrip(os.sep) or (ignored.endswith(os.sep) and key.startswith(ignored))
                   for ignored in self.ignored)

    def in_gitlink(self, key: str) -> bool:
        return any(key.startswith(link + os.sep) for link in self.gitlinks)

    def has_twin(self, key: str) -> bool:
        """Whether a known path is spelled like ``key`` but for case or normalisation."""
        with self._fold_lock:
            if self._folded is None:
                self._folded = frozenset(_fold(k) for k in (*self.tracked, *self.copied))
        return _fold(key) in self._folded


def _pathspec(top: Path, roots: Sequence[Path]) -> List[str]:
    """The roots inside ``top`` as pathspecs; none (the whole tree) when a root holds the tree."""
    if any(_inside(top, [root]) for root in roots):
        return []
    return ["--", *(root.relative_to(top).as_posix() for root in roots if _inside(root, [top]))]


def _head(top: Path) -> Optional[str]:
    done = _git(top, "rev-parse", "-q", "--verify", "HEAD")
    if done.returncode != 0:
        return None               # no commit yet
    return done.stdout.decode().strip() or None


def _status(top: Path, spec: List[str], ignored: bool = False) -> Tuple[List[Path], List[Path], List[str]]:
    """(changed or new paths -- a rename's both ends --, work trees nested in it, ignored paths as keys, a
    directory's ending in os.sep)."""
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"]
    if ignored:
        args.append("--ignored=matching")
    fields = _checked(top, *args, *spec).split(b"\0")
    paths: List[Path] = []
    nested: List[Path] = []
    hidden: List[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        code, rel = entry[:2], os.fsdecode(entry[3:])
        if b"R" in code or b"C" in code:          # renamed in the index, or in the work tree (intent-to-add)
            if index < len(fields) and fields[index]:
                paths.append(top / os.fsdecode(fields[index]))     # the source, gone from the work tree
            index += 1
        path = top / rel.rstrip("/")
        if code == b"!!":
            hidden.append(_key(path) + os.sep if rel.endswith("/") else _key(path))
        elif rel.endswith("/") or (path.is_dir() and _is_repo(path)):
            if _is_repo(path):
                nested.append(path.resolve())
        else:
            paths.append(path)
    return paths, nested, hidden


def _take_repo(top: Path, roots: Sequence[Path], scratch: Path, budget: _Budget, clock: _Clock) -> _Repo:
    repo = _Repo(top=top, head=_head(top), symlinks=_symlinks(top))
    spec = _pathspec(top, roots)
    paths, nested, ignored = _status(top, spec, ignored=True)
    repo.ignored = _stamped(ignored)
    repo.nested = {_key(p): p for p in nested}
    listed = {_key(p) for p in paths}
    for line in _checked(top, "ls-files", "-z", "-v", "-s", *spec).split(b"\0"):
        if not line:
            continue
        meta, _, rel = line.partition(b"\t")
        tag, mode, oid, stage = (meta.decode().split() + ["", "", "", ""])[:4]
        path = top / os.fsdecode(rel)
        if mode == _GITLINK:
            if _is_repo(path):
                repo.nested[_key(path)] = path.resolve()
            repo.tracked[_key(path)] = (mode, oid)
            repo.gitlinks[_key(path)] = path
            continue
        if stage != "0" or _key(path) in listed:
            continue
        if tag == "S" or tag.islower():
            paths.append(path)        # skip-worktree / assume-unchanged: status would not list its edits
            listed.add(_key(path))
        else:
            repo.tracked[_key(path)] = (mode, oid)
    paths = [p for p in paths if _inside(p, roots)]
    if len(paths) > budget.files:
        raise TooBig(f"{len(paths)} changed files in {top}")
    for path in paths:
        clock.check()
        state, content = read_state(path, budget.file_bytes)
        copy = None
        if content is not None:
            budget.take(len(content))
            copy = _keep(scratch, content, str(state.digest))
        repo.copied[_key(path)] = (path, state, copy)
    return repo


def _symlinks(top: Path) -> bool:
    done = _git(top, "config", "--type=bool", "--get", "core.symlinks")
    value = done.stdout.decode(errors="replace").strip()
    return value == "true" if value else os.name != "nt"


def _candidates(repo: _Repo, roots: Sequence[Path],
                budget: _Budget) -> Tuple[List[Path], List[Path], List[str]]:
    """(paths that may have changed since the look before, the work trees nested in it now, why some of
    its changes are not known)."""
    spec = _pathspec(repo.top, roots)
    paths, nested, ignored = _status(repo.top, spec, ignored=True)
    unseen = []
    # Made, deleted or written -- of an ignored directory only what changes its own entries: what lies
    # deeper (a build's output) is not looked at, which would mean walking it.
    if {k: v for k, v in _stamped(ignored).items() if not _rebuilt(k)} != \
            {k: v for k, v in repo.ignored.items() if not _rebuilt(k)}:
        unseen.append(IGNORED_FILES)
    for key, path in repo.gitlinks.items():
        if key not in repo.nested and _is_repo(path) and _inside(path, roots):
            nested.append(path.resolve())     # a submodule checked out by the call (update --init)
    found = {key: path for key, (path, _, _) in repo.copied.items()}
    found.update((_key(p), p) for p in paths if _inside(p, roots))
    head = _head(repo.top)
    if head and head != repo.head:
        # Committed, checked out, reset: what HEAD moved over changed in the work tree as well.
        names = _checked(repo.top, *(["diff", "--name-only", "-z", "--no-renames", repo.head, head] if repo.head
                                     else ["ls-tree", "-r", "--name-only", "-z", head]), *spec)
        for rel in names.split(b"\0"):
            if rel:
                path = repo.top / os.fsdecode(rel)
                if _inside(path, roots):
                    found[_key(path)] = path
    if len(found) > budget.files:
        raise TooBig(f"{len(found)} files changed in {repo.top}")
    return list(found.values()), nested, unseen


def _rebuilt(key: str) -> bool:
    """Written by Python on its own: no change of the call's worth a word."""
    name = os.path.basename(key.rstrip(os.sep))
    return name == "__pycache__" or name.endswith((".pyc", ".pyo"))


def _stamped(keys: Sequence[str]) -> Dict[str, Tuple[int, int]]:
    stamps = {}
    for key in keys:
        try:
            info = os.lstat(key.rstrip(os.sep))
            stamps[key] = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamps[key] = (-1, -1)
    return stamps


def _from_index(repo: _Repo, path: Path, scratch: Path, budget: _Budget) -> Tuple[State, Optional[Path]]:
    """The content of ``path`` as its index entry had it before the call; an UNREADABLE state when git cannot
    give it (an object gone, a smudge filter failing) -- never empty bytes or "absent" for a file that was."""
    mode, oid = repo.tracked[_key(path)]
    rel = path.relative_to(repo.top).as_posix()
    if mode == "120000":
        done = _git(repo.top, "cat-file", "blob", oid)    # the target: checked out as a link, or as a file
        kind = SYMLINK if repo.symlinks else FILE
    else:
        done = _git(repo.top, "cat-file", "--filters", f"--path={rel}", oid)
        kind = FILE
    if done.returncode != 0:
        return State(UNREADABLE, note="its earlier content could not be read from git"), None
    content = done.stdout
    digest = hashlib.sha256(content).hexdigest()
    if len(content) > budget.file_bytes:
        return State(kind, digest=digest, size=len(content), kept=False,
                     note=f"larger than {budget.file_bytes} bytes, its content was not kept"), None
    budget.take(len(content))
    current = fingerprint(path)
    file_mode = None if kind == SYMLINK else (
        current.mode if current.kind == FILE and current.mode is not None else 0o755 if mode == "100755" else 0o644)
    return State(kind, digest=digest, mode=file_mode, size=len(content), kept=True), _keep(scratch, content, digest)


# --- outside work trees ---------------------------------------------------------


class LooseCache:
    """The files of one directory outside any work tree, kept on disk so a new process starts from the
    last look: ``files`` path -> Entry, the kept bytes in ``blobs`` under their digest."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.blobs = directory / "blobs"
        self.manifest_path = directory / "manifest.json"
        self.lock = threading.Lock()
        self.files: Dict[str, Entry] = {}
        self.looked_at = 0
        #: Manifests of snapshots still to be recorded: their blobs stay.
        self.held: Dict[str, Dict[str, Entry]] = {}
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if raw.get("version") == 2:
                self.files = {path: tuple(entry) for path, entry in raw.get("files", {}).items()}  # type: ignore[misc]
                self.looked_at = int(raw.get("looked_at", 0))
        except (OSError, ValueError, TypeError):
            self.files = {}

    def look(self, root: Path, limits: Limits, clock: _Clock) -> "_Look":
        """Every file under ``root`` now (the work trees nested in it left out and returned)."""
        self.blobs.mkdir(parents=True, exist_ok=True)
        now = time.time_ns()
        found = _Look(root)
        files = found.files
        kept_bytes = 0
        for path in _walk(root, found, limits.files, clock):
            try:
                info = os.lstat(path)
            except OSError:
                found.unknown.add(_key(path))
                continue
            key = str(path)
            old = self.files.get(key)
            if old is not None and old[0] == info.st_mtime_ns and old[1] == info.st_size \
                    and info.st_mtime_ns < self.looked_at - RACY_NS and old[2] != UNREADABLE:
                entry = old
            else:
                state, content = read_state(path, limits.file_bytes)
                if state.kind not in (FILE, SYMLINK, UNREADABLE):
                    continue
                if content is not None:
                    _keep(self.blobs, content, str(state.digest))
                entry = (info.st_mtime_ns, info.st_size, state.kind, state.digest, content is not None, state.mode)
            files[key] = entry
            if entry[4]:
                kept_bytes += entry[1]
                if kept_bytes > limits.total_bytes:
                    raise TooBig(f"more than {limits.total_bytes} bytes under {root}")
        self.files = {**{k: v for k, v in self.files.items() if not _inside(Path(k), [root])}, **files}
        self.looked_at = now
        _write(self.manifest_path, json.dumps({"version": 2, "looked_at": now, "files": self.files}).encode("utf-8"))
        return found

    def collect(self) -> None:
        """Drop the blobs neither the last look nor a snapshot still to be recorded names."""
        named = {entry[3] for manifest in (self.files, *self.held.values()) for entry in manifest.values()}
        try:
            entries = list(os.scandir(self.blobs))
        except FileNotFoundError:
            return
        for entry in entries:
            if entry.name not in named and not entry.name.startswith("."):
                try:
                    os.unlink(entry.path)
                except OSError:
                    pass


@dataclass
class _Look:
    """One look at a directory outside work trees."""

    root: Path
    files: Dict[str, Entry] = field(default_factory=dict)
    #: Work trees inside it, looked at through git
    nested: List[Path] = field(default_factory=list)
    #: What could not be read (a directory not listed, a file not stat'ed): keys
    unknown: set = field(default_factory=set)
    #: Skipped directories worth a word (NOTED_DIRS): key -> mtimes
    skipped: Dict[str, Tuple[int, ...]] = field(default_factory=dict)


def _walk(root: Path, look: _Look, limit: int, clock: _Clock) -> Iterator[Path]:
    count = 0

    def unreadable(error: OSError) -> None:
        look.unknown.add(_key(error.filename or root))

    for base, dirs, files in os.walk(root, onerror=unreadable, followlinks=False):
        clock.check()
        here = Path(base)
        keep = []
        for name in sorted(dirs):
            child = here / name
            if name in SKIPPED_DIRS:
                if name in NOTED_DIRS:
                    look.skipped[_key(child)] = _mtimes(
                        [child, *(found for pattern in _INSTALLED_INTO for found in child.glob(pattern))])
                continue
            if os.path.islink(child):
                files.append(name)          # a link to a directory is recorded as the link
            elif _is_repo(child):
                look.nested.append(child.resolve())
            else:
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            count += 1
            if count > limit:
                raise TooBig(f"more than {limit} files under {root}")
            yield here / name


def _mtimes(paths: Sequence[Path]) -> Tuple[int, ...]:
    """The mtimes of ``paths`` (-1: not there, or not to be read), to tell whether anything in them changed."""
    stamps = []
    for path in sorted(paths):
        try:
            stamps.append(os.lstat(path).st_mtime_ns)
        except OSError:
            stamps.append(-1)
    return tuple(stamps)


def _walked(look: _Look, path: Path) -> bool:
    """Whether the look walked to where ``path`` is (no skipped or unreadable directory, no link on the way)."""
    current = look.root
    if _key(current) in look.unknown or _key(path) in look.unknown:
        return False
    for part in path.relative_to(look.root).parts[:-1]:
        current = current / part
        if part in SKIPPED_DIRS or os.path.islink(current) or _key(current) in look.unknown:
            return False
    return True


# --- one call ----------------------------------------------------------------------


class Snapshot:
    """The look before one call; ``finish`` takes the look after and says what differs. ``release`` lets go of
    the copies -- after the changes are recorded, not before: a change's bytes before lie among them."""

    def __init__(self, roots: Sequence[Tuple[str, Path]], limits: Limits, scratch: Path, caches: "Caches"):
        self.roots = list(roots)
        self.limits = limits
        self.scratch = scratch
        self.caches = caches
        self.repos: Dict[str, _Repo] = {}
        self.loose: Dict[str, _Look] = {}
        self.hold = uuid.uuid4().hex
        self.taken_at = time.monotonic()
        #: After ``finish``: why some of what the call changed is not recorded
        self.unseen: set = set()
        self._folded: Dict[str, frozenset] = {}
        self._fold_lock = threading.Lock()

    @property
    def directories(self) -> List[Path]:
        return [root for _, root in self.roots]

    def take(self) -> "Snapshot":
        """The look before. Raises TooBig."""
        self.scratch.mkdir(parents=True, exist_ok=True)
        clock = _Clock(self.limits.seconds)
        budget = _Budget(self.limits)
        tops, loose = self._regions()
        pending: Dict[str, Path] = {_key(top): top for top in tops}
        for root in loose:
            cache = self.caches.get(root)
            with cache.lock:
                look = cache.look(root, self.limits, clock)
                cache.held[self.hold] = look.files
            self.loose[_key(root)] = look
            pending.update((_key(top), top) for top in look.nested if _inside(top, self.directories))
        while pending:
            taken = self._pooled(lambda top: _take_repo(top, self.directories, self.scratch, budget, clock),
                                 list(pending.values()))
            pending = {}
            for repo in taken:
                self.repos[_key(repo.top)] = repo
            for repo in taken:
                pending.update((k, p) for k, p in repo.nested.items()
                               if k not in self.repos and _inside(p, self.directories))
        return self

    @staticmethod
    def _pooled(work: Callable, items: list) -> list:
        if len(items) <= 1:
            return [work(item) for item in items]
        with ThreadPoolExecutor(max_workers=_GIT_WORKERS) as pool:
            return list(pool.map(work, items))

    def _regions(self) -> Tuple[List[Path], List[Path]]:
        """(work trees, directories outside any) the roots lie in -- kept for REGIONS_SECONDS: two git
        calls per root, and which tree a directory lies in seldom changes."""
        key = tuple(sorted(_key(root) for root in self.directories))
        with self.caches.guard:
            kept = self.caches.regions.get(key)
        if kept is not None and time.monotonic() - kept[0] < REGIONS_SECONDS:
            return kept[1], kept[2]
        tops: Dict[str, Path] = {}
        loose: Dict[str, Path] = {}
        for root in sorted(set(self.directories), key=lambda p: len(p.parts)):
            top = _toplevel(root)
            if top is not None and not (top != root and _ignored(top, root)):
                tops.setdefault(_key(top), top)
            elif not any(_inside(root, [other]) for other in loose.values()):
                loose[_key(root)] = root
        found = (list(tops.values()), list(loose.values()))
        with self.caches.guard:
            self.caches.regions[key] = (time.monotonic(), *found)
        return found

    def finish(self) -> List[Found]:
        """What differs now from the look before. Raises TooBig."""
        clock = _Clock(self.limits.seconds)
        budget = _Budget(self.limits)
        paths: Dict[str, Path] = {}
        new_trees: List[Path] = []
        repos = list(self.repos.values())
        for repo, (candidates, nested, unseen) in zip(repos, self._pooled(
                lambda repo: _candidates(repo, self.directories, budget), repos)):
            paths.update((_key(p), p) for p in candidates)
            new_trees.extend(p for p in nested if _key(p) not in self.repos)
            self.unseen.update(unseen)
        for before in self.loose.values():
            cache = self.caches.get(before.root)
            with cache.lock:
                after = cache.look(before.root, self.limits, clock)
            for key in set(before.files) | set(after.files):
                if before.files.get(key, _NO_ENTRY)[2:5] != after.files.get(key, _NO_ENTRY)[2:5]:
                    paths[_key(key)] = Path(key)
            for key in set(before.skipped) | set(after.skipped):
                if before.skipped.get(key) != after.skipped.get(key):
                    self.unseen.add(f"{os.path.basename(key)} directories")
            new_trees.extend(p for p in after.nested if _key(p) not in self.repos)
        for tree in new_trees:
            if _inside(tree, self.directories):
                # A work tree made by the call (a clone, an init): its files are judged by what was known
                # of the place before -- an init over files that were there changed none of them.
                paths.update((_key(p), p) for p in _walk(tree, _Look(tree), self.limits.files, clock))
        if len(paths) > self.limits.files:
            raise TooBig(f"{len(paths)} files changed")
        judged = self._pooled(lambda path: self._judge(path, budget, clock), list(paths.values()))
        return [found for found in judged if found is not None]

    def _judge(self, path: Path, budget: _Budget, clock: _Clock) -> Optional[Found]:
        clock.check()
        server = next((name for name, root in self.roots if _inside(path, [root])), None)
        if server is None:
            return None
        before, source = self._before(path, budget)
        if before is None:
            # Not known: left alone. It is a candidate -- listed, or found new -- so likely changed.
            self.unseen.add("files it could not tell from before")
            return None
        after = fingerprint(path)
        if before.same(after) or (before.kind == ABSENT and after.kind == ABSENT):
            return None
        if before.kind == ABSENT and after.kind not in (FILE, SYMLINK):
            return None                     # a directory made: its files are what is recorded
        return Found(str(path), server, before, source)

    def _before(self, path: Path, budget: _Budget) -> Tuple[Optional[State], Optional[Path]]:
        """The state ``path`` had before the call, from what the look before knew of its place -- the deepest
        work tree or directory outside one that holds it. (None, None): nothing is known."""
        key = _key(path)
        repo = max((r for r in self.repos.values() if _inside(path, [r.top])),
                   key=lambda r: len(r.top.parts), default=None)
        loose = max((item for item in self.loose.values() if _inside(path, [item.root])),
                    key=lambda item: len(item.root.parts), default=None)
        if loose is not None and (repo is None or len(loose.root.parts) > len(repo.top.parts)):
            entry = loose.files.get(str(path))
            if entry is None:
                entry = next((value for name, value in loose.files.items() if _key(name) == key), None)
            if entry is None:
                if not _walked(loose, path) or self._loose_twin(loose, key):
                    return None, None
                return State(ABSENT), None
            return self._from_cache(loose.root, entry)
        if repo is None:
            return None, None
        if key in repo.copied:
            _, state, copy = repo.copied[key]
            return state, copy
        if repo.was_ignored(key) or repo.in_gitlink(key):
            return None, None               # git did not look there
        if key in repo.tracked:
            if repo.tracked[key][0] == _GITLINK:
                return None, None
            return _from_index(repo, path, self.scratch, budget)
        if repo.has_twin(key):
            return None, None               # on macOS the same file as a known one, after a rename of its case
        return State(ABSENT), None          # git saw the place, tracked nothing there and listed nothing new

    def _loose_twin(self, look: _Look, key: str) -> bool:
        with self._fold_lock:
            folded = self._folded.get(_key(look.root))
            if folded is None:
                folded = self._folded[_key(look.root)] = frozenset(_fold(_key(n)) for n in look.files)
        return _fold(key) in folded

    def _from_cache(self, root: Path, entry: Entry) -> Tuple[State, Optional[Path]]:
        _, size, kind, digest, kept, mode = entry
        if kind == UNREADABLE:
            return State(UNREADABLE, note="it could not be read before the call"), None
        blob = self.caches.get(root).blobs / str(digest)
        if kept and blob.is_file():
            return State(kind, digest=digest, mode=mode, size=size, kept=True), blob
        return State(kind, digest=digest, mode=mode, size=size, kept=False,
                     note="its content was not kept (larger than the limit, or gone from the cache)"), None

    def release(self) -> None:
        """Let go of what the look before kept: copies, held cache entries."""
        for look in self.loose.values():
            cache = self.caches.get(look.root)
            with cache.lock:
                cache.held.pop(self.hold, None)
                cache.collect()
        shutil.rmtree(self.scratch, ignore_errors=True)


class Caches:
    """The caches of the directories outside work trees, one per directory, under ``root``."""

    def __init__(self, root: Path):
        self.root = root
        self._caches: Dict[str, LooseCache] = {}
        self.guard = threading.Lock()
        #: roots -> (when, work trees, directories outside any)
        self.regions: Dict[Tuple[str, ...], Tuple[float, List[Path], List[Path]]] = {}

    def get(self, directory: Path) -> LooseCache:
        key = hashlib.sha1(_key(directory).encode("utf-8")).hexdigest()[:16]
        with self.guard:
            cache = self._caches.get(key)
            if cache is None:
                cache = self._caches[key] = LooseCache(self.root / key)
            return cache


def clear_stale_scratch(root: Path, older_than: float = 86400.0) -> None:
    """Remove the copies of calls a process never finished (it ended mid-call): older than a day, so a call
    another process has under way keeps its own."""
    cutoff = time.time() - older_than
    for calls in root.glob("*/calls"):
        try:
            entries = list(calls.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
