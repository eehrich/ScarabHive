"""Machine files on disk: discovery across roots, reading, versioned writing.

Roots are glob patterns relative to the project root; the first root that
has a machine id wins (writable roots come first by default, so a panel copy
can shadow a shipped machine only when the operator configures it that way).
A machine is ``<id>.yaml``; its companion module and layout sidecar sit next
to it. Writes use the version (sha256 of the text) the caller read, so two
editors cannot overwrite each other silently.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .model.loader import MachineTree, load_tree
from .model.spec import NAME_PATTERN

_ID = re.compile(NAME_PATTERN)
LAYOUT_SUFFIX = ".layout.json"


def version_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class VersionConflict(Exception):
    def __init__(self, current: str):
        super().__init__(f"the file changed since you read it (current version {current})")
        self.current = current


class FileInTheWay(Exception):
    """A file written as new exists already: it is not one of the files the caller read (an earlier module)."""

    def __init__(self, rel: str):
        super().__init__(f"{rel} exists already next to the machine but is not one of its files: name it in the "
                         "YAML to use it, or give the new file another name")
        self.rel = rel


@dataclass
class MachineFile:
    id: str
    path: Path
    root: str
    writable: bool


class FileSources:
    """``Sources`` over the store: relative paths next to the importing file, or machine ids."""

    def __init__(self, store: "MachineStore", overrides: Optional[dict[str, str]] = None):
        self.store = store
        self.overrides = dict(overrides or {})

    def read(self, path: str) -> str:
        if path in self.overrides:
            return self.overrides[path]
        if not self.store.inside_roots(path):  # python:/imports: never reach files outside the machine roots
            raise FileNotFoundError(f"{path} (outside the machine roots)")
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:  # a directory (`python: .`) is a PermissionError on Windows
            raise FileNotFoundError(f"{path}: {exc}") from None

    def resolve(self, ref: str, base: str) -> str:
        if ref.startswith("./") or ref.startswith("../") or ref.endswith(".yaml"):
            path = os.path.normpath(os.path.join(os.path.dirname(base), ref))
            if path in self.overrides:
                return path
            if not self.store.inside_roots(path):
                raise LookupError(f"{ref}: outside the machine roots")
            if Path(path).is_file():
                return path
            raise LookupError(f"{ref}: no such file next to {base}")
        found = self.store.find(ref)
        if found is None:
            raise LookupError(f"{ref}: no machine with this id in the machine roots")
        return str(found.path)

    def sibling(self, name: str, base: str) -> str:
        return os.path.normpath(os.path.join(os.path.dirname(base), name))


class MachineStore:
    def __init__(self, roots: Iterable[str], writable: Iterable[str], base: Optional[Path] = None):
        self.base = Path(base) if base else Path.cwd()
        self.roots = list(roots)
        self.writable = [str(self._abs(w)) for w in writable]

    def _abs(self, path: str | Path) -> Path:
        path = Path(path)
        return path if path.is_absolute() else self.base / path

    # ------------------------------------------------------------ discovery
    def root_dirs(self) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        for root in self.roots:
            pattern = str(self._abs(root))
            matches = sorted(glob.glob(pattern)) if any(ch in root for ch in "*?[") else [pattern]
            for match in matches:
                path = Path(match)
                if path.is_dir():
                    found.append((root, path))
        return found

    def list(self) -> list[MachineFile]:
        machines: dict[str, MachineFile] = {}
        for root, directory in self.root_dirs():
            for file in sorted(directory.glob("*.yaml")):
                machine_id = file.stem
                if (file.name.endswith(".layout.yaml") or not _ID.fullmatch(machine_id) or machine_id in machines
                        or not file.is_file()):
                    continue
                machines[machine_id] = MachineFile(machine_id, file, root, self.is_writable(file))
        return list(machines.values())

    def find(self, machine_id: str) -> Optional[MachineFile]:
        if not _ID.fullmatch(machine_id or ""):
            return None
        return next((m for m in self.list() if m.id == machine_id), None)

    def inside_roots(self, path: str | Path) -> bool:
        """Whether a path lies inside one of the machine roots (symlinks resolved)."""
        real = os.path.realpath(str(path))
        roots = [os.path.realpath(str(d)) for _, d in self.root_dirs()] + [os.path.realpath(w) for w in self.writable]
        return any(real == root or real.startswith(root + os.sep) for root in roots)

    def is_writable(self, path: Path) -> bool:
        resolved = str(path.resolve())
        return any(resolved.startswith(str(Path(w).resolve()) + os.sep) for w in self.writable)

    # ------------------------------------------------------------ reading
    def read(self, machine_id: str) -> tuple[MachineFile, str]:
        found = self.find(machine_id)
        if found is None:
            raise KeyError(machine_id)
        return found, found.path.read_text(encoding="utf-8")

    def load(self, machine_id: str, *, execute_python: bool = False) -> MachineTree:
        """Load (not validate) a machine from disk; ``execute_python`` runs companion modules (for a run)."""
        found = self.find(machine_id)
        if found is None:
            raise KeyError(machine_id)
        return load_tree(str(found.path), FileSources(self), execute_python=execute_python)

    def base_dir(self, machine_id: str) -> Path:
        """Where a machine's files live: next to the existing file, else the first writable root."""
        found = self.find(machine_id)
        if found is not None:
            return found.path.parent
        if not self.writable:
            raise PermissionError("no writable machine root configured")
        return Path(self.writable[0])

    def load_files(self, files: dict[str, str], *, machine_id: str, execute_python: bool = False) -> MachineTree:
        """Load unsaved files (relative path -> text) as if written next to the machine; the rest from disk."""
        base = self.base_dir(machine_id)
        overrides = {os.path.normpath(str(base / rel)): text for rel, text in files.items()}
        root = os.path.normpath(str(base / f"{machine_id}.yaml"))
        return load_tree(root, FileSources(self, overrides), execute_python=execute_python)

    def relative(self, machine_id: str, path: str) -> str:
        """With / on every platform: the panel matches problems' files to these keys."""
        return Path(os.path.relpath(path, str(self.base_dir(machine_id)))).as_posix()

    def layout(self, machine_id: str) -> dict[str, Any]:
        found = self.find(machine_id)
        if found is None:
            return {}
        sidecar = found.path.with_name(found.path.stem + LAYOUT_SUFFIX)
        try:
            return json.loads(sidecar.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    # ------------------------------------------------------------ writing
    def write_files(self, machine_id: str, files: dict[str, str],
                    expected_versions: Optional[dict[str, str]] = None) -> dict[str, str]:
        """Write a machine tree (relative path -> text) into its directory: all files or none, version checks first.

        Every target must lie inside a writable root. An existing file needs its
        expected version (no blind overwrite); a new file must not appear meanwhile.
        Returns each file's version as a read gives it back (line endings are ``\\n``).
        """
        base = self.base_dir(machine_id)
        expected_versions = dict(expected_versions or {})
        targets: dict[str, Path] = {}
        for rel in files:
            target = Path(os.path.normpath(str(base / rel)))
            if not self.is_writable(target) and not self.is_writable(target.parent / "x"):
                raise PermissionError(f"{rel}: {target} is not in a writable machine root -- a shipped machine is "
                                      "read-only: save a copy under a new id")
            if target.exists():
                if rel not in expected_versions:
                    raise FileInTheWay(rel)
                current = version_of(target.read_text(encoding="utf-8"))
                if expected_versions[rel] != current:
                    raise VersionConflict(current)
            elif rel in expected_versions:
                raise VersionConflict("absent")
            targets[rel] = target
        texts = {rel: _lf(files[rel]) for rel in targets}
        _write_all({rel: (targets[rel], texts[rel]) for rel in targets})
        return {rel: version_of(text) for rel, text in texts.items()}

    def write(self, machine_id: str, text: str, *, expected_version: Optional[str]) -> MachineFile:
        found = self.find(machine_id)
        if found is None:
            if not self.writable:
                raise PermissionError("no writable machine root configured")
            path = Path(self.writable[0]) / f"{machine_id}.yaml"
            if expected_version is not None and path.exists():
                raise VersionConflict(version_of(path.read_text(encoding="utf-8")))
            found = MachineFile(machine_id, path, "writable", True)
        else:
            if not found.writable:
                raise PermissionError(f"{found.path} is not in a writable machine root")
            current = version_of(found.path.read_text(encoding="utf-8"))
            if expected_version != current:
                raise VersionConflict(current)
        _atomic_write(found.path, text)
        return found

    def delete(self, machine_id: str, *, expected_version: str, companion: Optional[Path] = None) -> list[str]:
        """Remove a machine from a writable root: its file (the version the caller saw, no blind delete), its layout
        sidecar and ``companion`` -- the caller names the module only when no other machine uses it. Returns the
        paths removed. The file goes first: a failure after it leaves a stray module, never a machine without one."""
        found = self.find(machine_id)
        if found is None:
            raise KeyError(machine_id)
        if not found.writable:
            raise PermissionError(f"{found.path} is not in a writable machine root")
        current = version_of(found.path.read_text(encoding="utf-8"))
        if expected_version != current:
            raise VersionConflict(current)
        paths = [Path(p) for p in (found.path, found.path.with_name(found.path.stem + LAYOUT_SUFFIX), companion)
                 if p is not None and Path(p).is_file()]
        for path in paths:  # every check before the first removal
            if not self.is_writable(path):
                raise PermissionError(f"{path} is not in a writable machine root")
        for path in paths:
            path.unlink()
        return [str(path) for path in paths]

    def write_layout(self, machine_id: str, layout: dict[str, Any]) -> None:
        found = self.find(machine_id)
        if found is None:
            raise KeyError(machine_id)
        if not found.writable:
            raise PermissionError(f"{found.path} is not in a writable machine root")
        _atomic_write(found.path.with_name(found.path.stem + LAYOUT_SUFFIX),
                      json.dumps(layout, indent=1, sort_keys=True))


def _lf(text: str) -> str:
    """Line endings as a read returns them (universal newlines): what is written is what the version is of."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _atomic_write(path: Path, text: str) -> None:
    _write_all({path.name: (path, _lf(text))})


def _write_all(files: dict[str, tuple[Path, str]]) -> None:
    """Replace every file (name -> (path, text)) or none.

    Each text goes into a temp file next to its target first; then the temp files replace the targets. If one
    fails, the files replaced so far get their old bytes back (a new one is removed) and the error is raised again,
    naming the file and saying so. Written as UTF-8 with ``\\n`` line endings on every OS.
    """
    staged: dict[str, str] = {}
    replaced: list[tuple[str, Path, Optional[bytes]]] = []
    current = ""
    try:
        for current, (path, text) in files.items():
            staged[current] = _stage(path, text.encode("utf-8"))
        for current, (path, _) in files.items():
            old = path.read_bytes() if path.exists() else None
            os.replace(staged[current], path)  # no copystat: the new mtime must show (writer invariants on .envrc)
            replaced.append((current, path, old))
    except OSError as exc:
        stuck = [name for name, path, old in reversed(replaced) if not _put_back(path, old)]
        outcome = (f"not saved, but {', '.join(stuck)} could not be restored and still hold the new text" if stuck
                   else "nothing was saved")
        raise type(exc)(f"{current}: {exc.strerror or exc}; {outcome}") from exc
    finally:
        for temp in staged.values():
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def _stage(path: Path, data: bytes) -> str:
    """A temp file with ``data`` next to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
    except BaseException:
        os.unlink(temp)
        raise
    return temp


def _put_back(path: Path, old: Optional[bytes]) -> bool:
    """Undo a replace: the old bytes back, or no file where there was none. False if that fails too."""
    try:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            temp = _stage(path, old)
            try:
                os.replace(temp, path)
            except OSError:
                os.unlink(temp)
                raise
        return True
    except OSError:
        return False
