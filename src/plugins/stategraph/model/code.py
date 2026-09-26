"""Python in machines: compile, check, evaluate; templates; scopes (docs/stategraph_design.md §2.6, §3.6).

* **Code fields** (``guard``, ``effect``, ``entry``, ``exit``, ``map``, ``error_if``)
  hold plain Python.
* **Template fields** are literal unless they contain ``{{ }}``; a value that is
  exactly one ``{{ expr }}`` keeps the expression's type.

Machine code is trusted code (saving and running machines is admin-only). What
this module enforces is not a sandbox but typo protection and determinism:
unknown names are reported at validation time, read-only code that mutates the
context is refused, and impure calls are flagged.
"""

from __future__ import annotations

import ast
import builtins
import copy
import dataclasses
import functools
import hashlib
import json
import types
from dataclasses import dataclass
from collections.abc import ItemsView, Iterator, KeysView, ValuesView
from typing import Any, Callable, Iterable, Mapping, Optional

from .spec import GUARD_ELSE

#: Names the engine binds (depending on where the code runs; see BINDINGS).
SCOPE_NAMES = frozenset({"ctx", "params", "out", "error", "event", "run", "activity", "sg", "resources", "ending",
                         "fork_source"})
BUILTIN_NAMES = frozenset(dir(builtins))

#: Which scope names are bound for which trigger of a transition (§2.6).
BINDINGS = {
    "done": frozenset({"ctx", "params", "run", "sg", "out", "activity"}),
    "error": frozenset({"ctx", "params", "run", "sg", "error", "activity"}),
    "event": frozenset({"ctx", "params", "run", "sg", "event"}),
    "state": frozenset({"ctx", "params", "run", "sg"}),          # entry, exit, do templates, map, output
    "any": SCOPE_NAMES,
}

_IMPURE_CALLS = {
    "random", "time", "uuid", "secrets",                   # modules: any attribute call
}
_IMPURE_ATTRS = {("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"), ("date", "today"),
                 ("os", "getenv"), ("os", "urandom")}
_IMPURE_NAMES = {"open", "input", "hash", "set", "frozenset", "id"}


class CodeError(Exception):
    """Machine code failed to compile or raised while running."""

    def __init__(self, where: str, message: str, *, cause: Optional[BaseException] = None):
        super().__init__(f"{where}: {message}")
        self.where = where
        self.message = message
        self.cause = cause


# --------------------------------------------------------------------- compile

@functools.lru_cache(maxsize=4096)
def compile_expression(source: str, where: str = "<expression>") -> types.CodeType:
    try:
        return compile(source.strip(), where, "eval")
    except SyntaxError as exc:
        raise CodeError(where, f"not a Python expression: {exc.msg} ({source.strip()!r})", cause=exc) from None


@functools.lru_cache(maxsize=4096)
def compile_statements(source: str, where: str = "<statements>") -> types.CodeType:
    try:
        return compile(source, where, "exec")
    except SyntaxError as exc:
        raise CodeError(where, f"not Python statements: {exc.msg} (line {exc.lineno})", cause=exc) from None


def braced(source: str) -> bool:
    """A code field written like a template (``{{ ... }}``): an authoring slip, never valid."""
    text = source.strip()
    return text.startswith("{{") and text.endswith("}}")


# ------------------------------------------------------------------- templates

def scan_template(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, expression)`` for every ``{{ expr }}`` in ``text``.

    An expression ends at the first ``}}`` that closes a parseable Python
    expression, so dict literals inside work. An unclosed or unparseable
    ``{{`` raises CodeError.
    """
    found: list[tuple[int, int, str]] = []
    position = 0
    while True:
        start = text.find("{{", position)
        if start < 0:
            return found
        cursor = start + 2
        while True:
            end = text.find("}}", cursor)
            if end < 0:
                raise CodeError("template", f"unclosed '{{{{' in {text[start:start + 40]!r}")
            candidate = text[start + 2:end]
            try:
                ast.parse(candidate.strip(), mode="eval")
            except SyntaxError:
                cursor = end + 1
                continue
            found.append((start, end + 2, candidate))
            position = end + 2
            break


def template_expressions(value: Any) -> Iterator[str]:
    """Every ``{{ expr }}`` source in a (nested) template value."""
    if isinstance(value, str):
        for _, _, expression in scan_template(value):
            yield expression
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from template_expressions(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from template_expressions(item)


# -------------------------------------------------------------------- analysis

@dataclass(frozen=True)
class NameUse:
    """What a piece of code reads and writes, for the validator."""

    loads: frozenset[str]
    unknown: tuple[str, ...]
    ctx_reads: frozenset[str]
    ctx_writes: frozenset[str]
    impure: tuple[str, ...]
    params_reads: frozenset[str] = frozenset()
    misuse: tuple[str, ...] = ()
    resources_reads: frozenset[str] = frozenset()


def analyse(source: str, *, mode: str, allowed: Iterable[str]) -> NameUse:
    """Names ``source`` loads that nothing defines, the ``ctx`` fields it touches, impure calls.

    Approximate on purpose: it is typo protection, not a type checker. A name
    counts as defined if it is assigned anywhere in the snippet, is an argument,
    a comprehension target, an import, or one of ``allowed``.
    """
    tree = ast.parse(source.strip() if mode == "eval" else source, mode=mode)
    stores: set[str] = set()
    loads: list[str] = []
    reads: set[str] = set()
    writes: set[str] = set()
    impure: list[str] = []
    params_reads: set[str] = set()
    resources_reads: set[str] = set()
    misuse: list[str] = []
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        misuse.extend(_misuse(node, called))
        if isinstance(node, ast.Name):
            (loads.append(node.id) if isinstance(node.ctx, ast.Load) else stores.add(node.id))
        elif isinstance(node, ast.arg):
            stores.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stores.add(node.name)
        elif isinstance(node, ast.alias):
            stores.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            stores.add(node.name)
        elif isinstance(node, (ast.Set, ast.SetComp)):
            impure.append("a set (its iteration order changes between processes)")
        elif isinstance(node, ast.Call):
            impure.extend(_impure_call(node))
        elif (isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name)
              and node.value.id == "os"):
            impure.append("os.environ")
        param = _ctx_field(node, "params")
        if param is not None:
            params_reads.add(param)
        resource = _ctx_field(node, "resources")
        if resource is not None:
            resources_reads.add(resource)
        field = _ctx_field(node)
        if field is not None:
            if isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
                writes.add(field)
            else:
                reads.add(field)
        if isinstance(node, ast.AugAssign):
            target = _ctx_field(node.target)
            if target is not None:
                reads.add(target)
    known = set(allowed) | SCOPE_NAMES | BUILTIN_NAMES | stores
    unknown = tuple(sorted({name for name in loads if name not in known}))
    free = frozenset(name for name in loads if name not in stores)  # a local `for event in ...` is not the scope's
    return NameUse(free, unknown, frozenset(reads), frozenset(writes), tuple(dict.fromkeys(impure)),
                   frozenset(params_reads), tuple(dict.fromkeys(misuse)), frozenset(resources_reads))


_NAMESPACES = ("ctx", "params", "error", "event", "run", "activity", "resources", "ending")
_DICT_METHODS = {"get", "keys", "items", "values", "pop", "update", "setdefault", "copy", "clear"}
#: Methods of JSON values: passing one as a value (``max(out, key=out.get)``) is valid Python.
_DATA_METHODS = frozenset(name for kind in (dict, list, str, int, float) for name in dir(kind)
                          if not name.startswith("_"))

#: The fields of the engine's fixed namespaces; code reading another field fails at run time (§2.6).
NAMESPACE_FIELDS = {
    "activity": ("instance_id", "request_id", "attempts", "duration_s", "mocked", "agent", "feedback_rounds",
                 "model", "cost"),
    "error": ("type", "message", "state", "data", "cause", "branch", "index", "visits"),
    "event": ("name", "data"),
    "run": ("id", "origin", "step", "machine", "state", "visits", "frame"),
    "ending": ("reason", "state", "error"),
}


def _misuse(node: ast.AST, called: set[int]) -> list[str]:
    """Slips that compile but always fail at run time (§2.6 access rules)."""
    if isinstance(node, ast.Set) and len(node.elts) == 1 and isinstance(node.elts[0], (ast.Set, ast.Dict)):
        return ["code fields are plain Python: remove the {{ }}"]
    if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
        return []
    base = node.value
    if isinstance(base, ast.Name) and base.id in NAMESPACE_FIELDS and node.attr not in NAMESPACE_FIELDS[base.id]:
        return [f"{base.id} has no field {node.attr!r} (it has: {', '.join(NAMESPACE_FIELDS[base.id])})"]
    if isinstance(base, ast.Name) and base.id == "out" and id(node) not in called and node.attr not in _DATA_METHODS:
        return [f"out is plain data: write out[{node.attr!r}], not out.{node.attr}"]
    if isinstance(base, ast.Name) and base.id in _NAMESPACES and node.attr in _DICT_METHODS and id(node) in called:
        return [f"{base.id} has no dict methods: use '{node.attr == 'get' and 'x' or node.attr}' in {base.id} and "
                f"{base.id}['x'] (it is a namespace of fields)"]
    if (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
            and base.value.id in ("ctx", "params", "resources")
            and id(node) not in called and node.attr not in _DATA_METHODS):
        return [f"{base.value.id}.{base.attr}.{node.attr}: below the top level values are plain data -- write "
                f"{base.value.id}.{base.attr}[{node.attr!r}]"]
    return []


def _impure_call(node: ast.Call) -> list[str]:
    func = node.func
    if isinstance(func, ast.Name) and func.id in _IMPURE_NAMES:
        return [f"{func.id}()"]
    if isinstance(func, ast.Attribute):
        base = func.value
        while isinstance(base, ast.Attribute):
            base = base.value
        root = base.id if isinstance(base, ast.Name) else None
        if root in _IMPURE_CALLS:
            return [f"{root}.{func.attr}()"]
        owner = func.value.attr if isinstance(func.value, ast.Attribute) else root
        if (owner, func.attr) in _IMPURE_ATTRS:
            return [f"{owner}.{func.attr}()"]
    return []


def _ctx_field(node: ast.AST, root: str = "ctx") -> Optional[str]:
    """``ctx.name`` or ``ctx["name"]`` -> ``name`` (``root`` names the namespace)."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == root:
        return node.attr
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == root
            and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
        return node.slice.value
    return None


def module_names(source: str) -> tuple[frozenset[str], frozenset[str]]:
    """Public names and public function names a companion module defines -- without executing it."""
    tree = ast.parse(source)
    names: set[str] = set()
    functions: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
            functions.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
            functions.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    public = frozenset(n for n in names if not n.startswith("_"))
    return public, frozenset(n for n in functions if not n.startswith("_"))


# ------------------------------------------------------------------- namespaces

class Scope:
    """A namespace over a JSON dict: attribute access at the top level, item access everywhere.

    Values are the plain JSON data themselves (``ctx.critique["notes"]``). There
    are no dict methods, so a field named ``items`` or ``keys`` is simply a field.
    A frozen scope refuses top-level writes; nested mutation in read-only code is
    caught by the engine's hash check (``Namespace.evaluate(readonly=True)``).
    """

    __slots__ = ("_data", "_frozen", "_label")

    def __init__(self, data: dict, *, frozen: bool = False, label: str = "ctx"):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_frozen", frozen)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(f"{self._label} has no field {name!r}") from None

    def __getitem__(self, key: str) -> Any:
        try:
            return self._data[key]
        except KeyError:
            raise KeyError(f"{self._label} has no field {key!r}") from None

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._data))

    def __len__(self) -> int:
        return len(self._data)

    def __bool__(self) -> bool:
        return True

    def __eq__(self, other: object) -> bool:
        return self._data == (other._data if isinstance(other, Scope) else other)

    def __repr__(self) -> str:
        return f"{self._label}({json.dumps(self._data, default=str)[:200]})"

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __setitem__(self, key: str, value: Any) -> None:
        if self._frozen:
            raise TypeError(f"{self._label} is read-only here")
        self._data[key] = plain(value)

    def __delattr__(self, name: str) -> None:
        del self[name]

    def __delitem__(self, key: str) -> None:
        if self._frozen:
            raise TypeError(f"{self._label} is read-only here")
        self._data.pop(key, None)


def namespace_of(value: Optional[Mapping[str, Any]], label: str) -> Optional[Scope]:
    """A frozen namespace for error/event/run/activity (None stays None)."""
    return None if value is None else Scope(dict(value), frozen=True, label=label)


def plain(value: Any) -> Any:
    """A deep, independent, JSON-like copy (scopes, tuples, views, iterators, dataclasses, pydantic models resolved)."""
    if isinstance(value, Scope):
        return copy.deepcopy(value._data)
    if isinstance(value, (KeysView, ValuesView, ItemsView, Iterator, range)):
        return [plain(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return plain(dataclasses.asdict(value))
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return plain(value.model_dump(mode="json"))
        except TypeError:
            pass
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return copy.deepcopy(value)


def jsonable(value: Any) -> Any:
    """The canonical JSON round trip every boundary value goes through (§3.6)."""
    return json.loads(json.dumps(plain(value), ensure_ascii=False, default=str))


def fingerprint(value: Any) -> str:
    """sha256 of canonical JSON (sorted keys): input and context hashes of the journal."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


# ------------------------------------------------------------------- evaluation

class Namespace:
    """The code environment of one machine file: its companion module's public names."""

    def __init__(self, module_globals: Optional[Mapping[str, Any]] = None, *,
                 names: Iterable[str] = (), functions: Iterable[str] = ()):
        self.module = dict(module_globals or {})
        self.public = {k: v for k, v in self.module.items() if not k.startswith("_")}
        self._names = frozenset(self.public) | frozenset(names)
        self._functions = frozenset(k for k, v in self.public.items() if callable(v)) | frozenset(functions)

    def names(self) -> frozenset[str]:
        return self._names

    def has_function(self, name: str) -> bool:
        return name in self._functions

    def function(self, name: str, where: str) -> Callable[..., Any]:
        fn = self.public.get(name)
        if not callable(fn):
            raise CodeError(where, f"the companion module defines no function {name!r}")
        return fn

    def _globals(self, scope: Mapping[str, Any]) -> dict[str, Any]:
        env = dict(self.public)
        env.update(scope)
        env["sg"] = types.SimpleNamespace(**scope)
        env["__builtins__"] = builtins
        return env

    def evaluate(self, source: str, scope: Mapping[str, Any], where: str, *, readonly: bool = True) -> Any:
        code = compile_expression(source, where)
        if readonly and isinstance(scope.get("ctx"), Scope):
            # a copy: a mutation read-only code attempts is refused AND leaves the real ctx untouched
            scope = {**scope, "ctx": Scope(copy.deepcopy(scope["ctx"]._data), frozen=True, label="ctx")}
        before = _ctx_hash(scope) if readonly else None
        try:
            value = eval(code, self._globals(scope))  # noqa: S307 -- machine code is trusted (§8.3)
        except CodeError:
            raise
        except Exception as exc:
            raise CodeError(where, f"{type(exc).__name__}: {exc}", cause=exc) from exc
        if readonly and _ctx_hash(scope) != before:
            raise CodeError(where, "ctx mutated in read-only code (guards, templates and conditions must not "
                                   "change ctx; do it in an effect)")
        return value

    def execute(self, source: str, scope: Mapping[str, Any], where: str) -> None:
        code = compile_statements(source, where)
        try:
            exec(code, self._globals(scope))  # noqa: S102 -- machine code is trusted (§8.3)
        except CodeError:
            raise
        except Exception as exc:
            raise CodeError(where, f"{type(exc).__name__}: {exc}", cause=exc) from exc

    def guard(self, source: Optional[str], scope: Mapping[str, Any], where: str) -> bool:
        if source is None or source.strip() == GUARD_ELSE:
            return True
        return bool(self.evaluate(source, scope, where))

    def render(self, value: Any, scope: Mapping[str, Any], where: str) -> Any:
        """Resolve a template value: literal unless it contains ``{{ }}``; the result is plain JSON-like data."""
        if isinstance(value, str):
            try:
                found = scan_template(value)
            except CodeError as exc:
                raise CodeError(where, exc.message) from None
            if not found:
                return value
            if len(found) == 1 and value.strip() == value[found[0][0]:found[0][1]]:
                return plain(self.evaluate(found[0][2], scope, where))
            parts, last = [], 0
            for start, end, expression in found:
                parts.append(value[last:start])
                parts.append(_text(self.evaluate(expression, scope, where)))
                last = end
            parts.append(value[last:])
            return "".join(parts)
        if isinstance(value, Mapping):
            return {key: self.render(item, scope, f"{where}.{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [self.render(item, scope, f"{where}[{i}]") for i, item in enumerate(value)]
        return value


def _ctx_hash(scope: Mapping[str, Any]) -> Optional[str]:
    ctx = scope.get("ctx")
    return fingerprint(ctx._data) if isinstance(ctx, Scope) else None


def _text(value: Any) -> str:
    value = plain(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def load_module(source: str, filename: str) -> Namespace:
    """Execute a companion module's source in a fresh namespace (not in sys.modules).

    Kept out of ``sys.modules`` so a changed file is picked up on the next load,
    two machines' modules never shadow each other, and a run's snapshot source
    is what runs.
    """
    module_globals: dict[str, Any] = {"__name__": f"stategraph_machine:{filename}", "__file__": filename,
                                      "__builtins__": builtins}
    try:
        code = compile(source, filename, "exec")
    except SyntaxError as exc:
        raise CodeError(filename, f"companion module does not compile: {exc.msg} (line {exc.lineno})",
                        cause=exc) from None
    try:
        exec(code, module_globals)  # noqa: S102 -- machine code is trusted (§8.3)
    except Exception as exc:
        raise CodeError(filename, f"companion module raised on import: {type(exc).__name__}: {exc}",
                        cause=exc) from exc
    return Namespace(module_globals)


def scan_module(source: str, filename: str) -> Namespace:
    """The companion module's names for validation, without executing it."""
    try:
        names, functions = module_names(source)
    except SyntaxError as exc:
        raise CodeError(filename, f"companion module does not compile: {exc.msg} (line {exc.lineno})",
                        cause=exc) from None
    return Namespace(names=names, functions=functions)
