"""Load a machine file and everything it imports into a ``MachineTree``.

Loading never raises on a broken machine: every defect becomes a ``Problem``
with a path into the file and, where ruamel knows it, a line. The engine only
compiles trees without errors.

Sources are abstract so the same loader reads machine files from disk (the
store) and from a run's definition snapshot (replay must use the definition the
run started with, not whatever the file says today).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional, Protocol

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError, YAMLError

from .code import CodeError, Namespace, load_module, scan_module
from .spec import FORMAT_VERSION, MachineSpec


@dataclass
class Problem:
    level: Literal["error", "warning"]
    code: str
    message: str
    path: str = ""
    file: str = ""
    line: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message,
                "path": self.path, "file": self.file, "line": self.line}


class Sources(Protocol):
    """Where machine files come from."""

    def read(self, path: str) -> str:
        """Text of a file; raises FileNotFoundError."""

    def resolve(self, ref: str, base: str) -> str:
        """Path for an import ref (``./x.yaml`` relative to ``base``, or a machine id); raises LookupError."""

    def sibling(self, name: str, base: str) -> str:
        """Path of a file named relative to ``base`` (the companion module)."""


class SnapshotSources:
    """Sources from a run's definition snapshot: ``{path: text}`` plus ``{machine id: path}``."""

    def __init__(self, files: dict[str, str], ids: Optional[dict[str, str]] = None):
        self.files = dict(files)
        self.ids = dict(ids or {})

    def read(self, path: str) -> str:
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def resolve(self, ref: str, base: str) -> str:
        if ref.startswith("./") or ref.startswith("../") or ref.endswith(".yaml"):
            path = posixpath.normpath(posixpath.join(posixpath.dirname(base), ref))
            if path in self.files:
                return path
            raise LookupError(f"{ref}: no such file next to {base}")
        if ref in self.ids:
            return self.ids[ref]
        raise LookupError(f"{ref}: no machine with this id")

    def sibling(self, name: str, base: str) -> str:
        return posixpath.normpath(posixpath.join(posixpath.dirname(base), name))


@dataclass
class LoadedFile:
    path: str
    text: str
    doc: Any = None                      # ruamel round-trip document (line numbers)
    spec: Optional[MachineSpec] = None
    namespace: Namespace = field(default_factory=Namespace)
    python_path: Optional[str] = None
    python_text: Optional[str] = None
    imports: dict[str, str] = field(default_factory=dict)   # alias -> path

    def line_of(self, path: Iterable[Any]) -> Optional[int]:
        return line_of(self.doc, path)


@dataclass
class MachineTree:
    """A root machine file and the files it imports, with every problem found while loading."""

    root: str
    files: dict[str, LoadedFile] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(p.level == "error" for p in self.problems)

    @property
    def root_file(self) -> LoadedFile:
        return self.files[self.root]

    def snapshot(self) -> dict[str, Any]:
        """Everything needed to load this tree again later: texts and machine ids.

        Keys are portable (``portable_snapshot``), so a run started on one OS resumes on another.
        """
        texts: dict[str, str] = {}
        ids: dict[str, str] = {}
        for path, loaded in self.files.items():
            texts[path] = loaded.text
            if loaded.python_path and loaded.python_text is not None:
                texts[loaded.python_path] = loaded.python_text
            if loaded.spec:
                ids[loaded.spec.id] = path
        return portable_snapshot({"root": self.root, "files": texts, "ids": ids})

    def add(self, level: Literal["error", "warning"], code: str, message: str, *,
            file: str = "", path: str = "", line: Optional[int] = None) -> None:
        self.problems.append(Problem(level, code, message, path=path, file=file, line=line))


def load_tree(root: str, sources: Sources, *, execute_python: bool = False) -> MachineTree:
    """Load a machine and its imports.

    ``execute_python`` runs the companion modules (needed to RUN the machine).
    Validation leaves it off: the modules' names come from an AST scan, so
    checking a machine never executes its code (docs/stategraph_design.md §4).
    """
    tree = MachineTree(root=root)
    _load(root, sources, tree, stack=[], execute=execute_python)
    return tree


def load_snapshot(snapshot: dict[str, Any], *, execute_python: bool = True) -> MachineTree:
    snapshot = portable_snapshot(snapshot)  # a run stored before snapshots were portable holds host paths
    return load_tree(snapshot["root"], SnapshotSources(snapshot["files"], snapshot.get("ids")),
                     execute_python=execute_python)


def portable_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """A definition snapshot whose paths mean the same on every OS: relative to the root file's directory, ``/``.

    ``SnapshotSources`` resolves ``python:`` and ``./x.yaml`` next to a path with ``posixpath``, but the store's
    paths are the host's (``C:\\machines\\m.yaml`` on Windows). Rewriting them once, here, keeps every relative
    reference resolvable wherever the run resumes. A file with nothing in common with the root's directory (another
    Windows drive) keeps its absolute path, with ``/``. Applying this to a portable snapshot changes nothing.
    """
    root = _slashed(snapshot["root"])
    base = posixpath.dirname(root)
    start = base.rstrip("/").split("/") if base else []  # "/" is the one part "" (the root), like "/m" is "", "m"

    def key(path: str) -> str:
        parts = _slashed(path).split("/")
        common = 0
        while common < min(len(parts) - 1, len(start)) and parts[common] == start[common]:
            common += 1
        if start and not common:
            return "/".join(parts)
        return "/".join([".."] * (len(start) - common) + parts[common:])

    return {**snapshot, "root": key(root), "files": {key(p): text for p, text in snapshot["files"].items()},
            "ids": {machine_id: key(p) for machine_id, p in (snapshot.get("ids") or {}).items()}}


def _slashed(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


# ------------------------------------------------------------------- internals

def parse_yaml(text: str) -> Any:
    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = False
    return yaml.load(text)


#: Bounds of a machine document with its aliases expanded. Everything past parsing -- to_plain, the validator,
#: ``copy.deepcopy`` of the context, the journal's JSON -- recurses per level and copies per alias.
MAX_DEPTH = 100
MAX_VALUES = 100_000
MAX_TEXT = 10_000_000  # characters: an alias of a long text copies all of it


def yaml_bounds(doc: Any) -> Optional[str]:
    """Why a parsed document is too big to work with, or None.

    Aliases make it cheap to write a document that expands without bound: a chain of ``&a [*b]`` nests deeper
    than any recursion, ten lines of ``&x [*y, *y]`` hold a million values, and an alias of a long text copies
    the text each time. Counted per distinct node (an alias costs one lookup), without recursion; an alias of an
    enclosing node (a cycle) is too deep.
    """
    extent: dict[int, tuple[int, int, int]] = {}  # id -> (values, characters, depth), of every finished node
    open_: set[int] = set()
    todo: list[tuple[Any, bool]] = [(doc, False)]
    while todo:
        node, finished = todo.pop()
        children = (list(node.keys()) + list(node.values()) if isinstance(node, dict)
                    else list(node) if isinstance(node, (list, tuple)) else None)
        if children is None or (id(node) in extent and not finished):
            continue
        if finished:
            inner = [extent.get(id(child), (1, len(child) if isinstance(child, str) else 0, 0)) for child in children]
            extent[id(node)] = (1 + sum(v for v, _, _ in inner), sum(c for _, c, _ in inner),
                                1 + max((d for _, _, d in inner), default=0))
            open_.discard(id(node))
        elif id(node) in open_:
            return "YAML: an alias refers to a node that contains it"
        else:
            open_.add(id(node))
            todo.append((node, True))
            todo.extend((child, False) for child in children)
    values, characters, depth = extent.get(id(doc), (1, len(doc) if isinstance(doc, str) else 0, 0))
    if depth > MAX_DEPTH:
        return f"YAML: nested {depth} levels deep (at most {MAX_DEPTH})"
    if values > MAX_VALUES:
        return f"YAML: its aliases expand it to {values} values (at most {MAX_VALUES})"
    if characters > MAX_TEXT:
        return f"YAML: its aliases expand it to {characters} characters of text (at most {MAX_TEXT})"
    return None


def to_plain(value: Any) -> Any:
    """ruamel round-trip values -> plain dict/list/str/int/float/bool/None."""
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


def line_of(doc: Any, path: Iterable[Any]) -> Optional[int]:
    """1-based line of the deepest node of ``path`` that exists in a round-trip document."""
    node = doc
    line: Optional[int] = None
    for key in path:
        try:
            if isinstance(node, CommentedMap) and key in node:
                line = node.lc.key(key)[0] + 1
                node = node[key]
            elif isinstance(node, CommentedSeq) and isinstance(key, int) and 0 <= key < len(node):
                line = node.lc.item(key)[0] + 1
                node = node[key]
            else:
                break
        except Exception:  # lc info is best effort
            break
    return line


def dotted(path: Iterable[Any]) -> str:
    out = ""
    for key in path:
        out += f"[{key}]" if isinstance(key, int) else (f".{key}" if out else str(key))
    return out


def _non_string_keys(node: Any, path: list[Any]) -> Optional[list[Any]]:
    """Path of the first mapping key that is not a string (an unquoted {{ ... }} becomes a mapping key)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, (str, int)) or isinstance(key, bool):
                return path
            found = _non_string_keys(value, path + [key])
            if found is not None:
                return found
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found = _non_string_keys(value, path + [index])
            if found is not None:
                return found
    return None


def _bad_key_message(doc: Any, path: list[Any]) -> str:
    node = doc
    for key in path:
        node = node[key]
    bad = next((k for k in node if not isinstance(k, (str, int)) or isinstance(k, bool)), None)
    if isinstance(bad, bool):
        return (f"a key was read as the boolean {bad!r}: YAML turns true/false/yes/no/on/off into booleans -- "
                "quote the key ('true': ...)")
    return ("a mapping key is not a string: an unquoted {{ ... }} is read as a nested mapping -- quote values "
            "that start with {{")


def _load(path: str, sources: Sources, tree: MachineTree, stack: list[str], execute: bool) -> Optional[LoadedFile]:
    if path in tree.files:
        return tree.files[path]
    try:
        text = sources.read(path)
    except FileNotFoundError:
        tree.add("error", "SG006", f"machine file not found: {path}", file=path)
        return None
    loaded = LoadedFile(path=path, text=text)
    tree.files[path] = loaded

    try:
        doc = parse_yaml(text)
    except MarkedYAMLError as exc:
        mark = exc.problem_mark
        tree.add("error", "SG001", f"YAML: {exc.problem or exc}", file=path,
                 line=(mark.line + 1) if mark else None)
        return loaded
    except YAMLError as exc:
        tree.add("error", "SG001", f"YAML: {exc}", file=path)
        return loaded
    except RecursionError:  # ruamel's parser recurses once per level: a few hundred levels exhaust it
        tree.add("error", "SG001", "YAML: nested too deeply to read", file=path)
        return loaded
    except Exception as exc:  # ruamel fails in its constructor too: a merge key that names its own anchor
        tree.add("error", "SG001", f"YAML: does not construct ({type(exc).__name__}: {exc})", file=path)
        return loaded
    too_big = yaml_bounds(doc)
    if too_big:  # before anything walks it (the editor's graph too)
        tree.add("error", "SG001", too_big, file=path)
        return loaded
    loaded.doc = doc
    bad_key = _non_string_keys(doc, [])
    if bad_key is not None:
        tree.add("error", "SG001", _bad_key_message(doc, bad_key), file=path, path=dotted(bad_key),
                 line=loaded.line_of(bad_key))
        return loaded
    if not isinstance(doc, dict):
        tree.add("error", "SG001", "a machine file is a YAML mapping (stategraph: 1, id, initial, states, ...)",
                 file=path, line=1)
        return loaded
    version = doc.get("stategraph")
    if type(version) is not int or version != FORMAT_VERSION:  # not true, not 1.0: both compare equal to 1
        tree.add("error", "SG001",
                 f"unknown format version stategraph: {version!r}; this engine reads stategraph: {FORMAT_VERSION}",
                 file=path, path="stategraph", line=loaded.line_of(["stategraph"]) or 1)
        return loaded
    data = to_plain(doc)
    try:
        loaded.spec = MachineSpec.model_validate(data)
    except ValidationError as exc:
        for loc, message in schema_problems(data, exc.errors(), _pydantic_message):
            tree.add("error", "SG001", message, file=path, path=dotted(loc), line=loaded.line_of(loc))
        return loaded
    spec = loaded.spec
    if posixpath.basename(path.replace("\\", "/")) != f"{spec.id}.yaml":
        tree.add("error", "SG001", f"machine {spec.id!r} must live in {spec.id}.yaml (ids resolve imports, runs and "
                                   f"forks by file name), not in {posixpath.basename(path)}", file=path, path="id",
                 line=loaded.line_of(["id"]))

    python = _reference(spec.python) if spec.python else None
    if spec.python and python is None:
        tree.add("error", "SG004", f"python: {spec.python!r} is absolute; write it relative to this file",
                 file=path, path="python", line=loaded.line_of(["python"]))
    elif python:
        python_path = sources.sibling(python, path)
        loaded.python_path = python_path
        try:
            loaded.python_text = sources.read(python_path)
        except FileNotFoundError:
            tree.add("error", "SG004", f"companion module not found: {spec.python}", file=path, path="python",
                     line=loaded.line_of(["python"]))
        else:
            try:
                loaded.namespace = (load_module if execute else scan_module)(loaded.python_text, python_path)
            except CodeError as exc:
                tree.add("error", "SG004", exc.message, file=python_path)

    stack = stack + [path]
    for alias, ref in spec.imports.items():
        try:
            relative = _reference(ref)
            if relative is None:
                raise LookupError(f"{ref} is absolute; write it relative to this file, or name the machine id")
            target = sources.resolve(relative, path)
        except LookupError as exc:
            tree.add("error", "SG006", f"import {alias}: {exc}", file=path, path=f"imports.{alias}",
                     line=loaded.line_of(["imports", alias]))
            continue
        if target in stack:
            chain = " -> ".join(stack[stack.index(target):] + [target])
            tree.add("error", "SG006", f"import cycle: {chain}", file=path, path=f"imports.{alias}",
                     line=loaded.line_of(["imports", alias]))
            continue
        loaded.imports[alias] = target
        _load(target, sources, tree, stack, execute)
    return loaded


def schema_problems(data: Any, errors: Iterable[dict[str, Any]],
                    message: Callable[[dict[str, Any]], str]) -> list[tuple[list[Any], str]]:
    """pydantic errors as ``(YAML path, message)``, one per path.

    A union (``vars``, a duration) fails once per member, each with the member's tag in its location
    (``vars.dict[str,any]``, ``timeout.int``): the tags are no keys of the document, so the path leaves them out and
    the members' messages become one.
    """
    merged: dict[tuple[Any, ...], list[str]] = {}
    for error in errors:
        path = yaml_path(data, error["loc"], missing=error.get("type") == "missing")
        merged.setdefault(tuple(path), []).append(message(error))
    out = []
    for path, messages in merged.items():
        messages = list(dict.fromkeys(messages))
        prefix = "Input should be "
        if len(messages) > 1 and all(m.startswith(prefix) for m in messages):
            messages = [prefix + " or ".join(m.removeprefix(prefix) for m in messages)]
        out.append((list(path), "; ".join(messages)))
    return out


def yaml_path(data: Any, loc: Iterable[Any], *, missing: bool = False) -> list[Any]:
    """The part of a pydantic location that exists in ``data``; the last part too for a ``missing`` key."""
    parts = list(loc)
    path: list[Any] = []
    node = data
    for index, part in enumerate(parts):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif (isinstance(node, list) and isinstance(part, int) and not isinstance(part, bool)
              and 0 <= part < len(node)):
            node = node[part]
        elif not (missing and index == len(parts) - 1):
            continue  # a union member's or a validator's tag
        path.append(part)
    return path


def _reference(ref: str) -> Optional[str]:
    """A ``python:`` or ``imports:`` path as every OS reads it: ``/`` separators. None for an absolute path.

    The store resolves references with the host's ``os.path``, a run's snapshot with ``posixpath`` on paths relative
    to the machine: ``sub\\m.py`` or ``C:/x/m.py`` would load from disk and then be missing from the run.
    """
    ref = ref.replace("\\", "/")
    return None if ref.startswith("/") or re.match(r"[A-Za-z]:", ref) else ref


def _pydantic_message(error: dict[str, Any]) -> str:
    kind = error.get("type", "")
    loc = error.get("loc") or ()
    field_name = loc[-1] if loc else ""
    if kind == "extra_forbidden":
        return f"unknown key {field_name!r} (keys are fixed by the format; see docs/format.md)"
    if kind == "missing":
        return f"missing required key {field_name!r}"
    message = str(error.get("msg", "invalid value"))
    return message.removeprefix("Value error, ")


def find_state(spec: MachineSpec, name: str) -> Optional[list[Any]]:
    """YAML path of a state by name (nested states included)."""

    def walk(states: dict[str, Any], prefix: list[Any]) -> Optional[list[Any]]:
        for state_name, state in states.items():
            here = prefix + [state_name]
            if state_name == name:
                return here
            if state.states:
                found = walk(state.states, here + ["states"])
                if found:
                    return found
        return None

    return walk(spec.states, ["states"])


LineLookup = Callable[[Iterable[Any]], Optional[int]]
