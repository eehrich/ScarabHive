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
import itertools
import json
import posixpath
import re
import sys
import types
import weakref
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

#: What Python's parser and compiler raise for code nested too deeply (``1+1+...``, ``not not ...``).
_TOO_DEEP = (RecursionError, MemoryError)
_TOO_DEEP_MESSAGE = "nested too deeply for Python's parser"


@functools.lru_cache(maxsize=4096)
def compile_expression(source: str, where: str = "<expression>") -> types.CodeType:
    try:
        return compile(source.strip(), where, "eval")
    except SyntaxError as exc:
        raise CodeError(where, f"not a Python expression: {exc.msg} ({source.strip()!r})", cause=exc) from None
    except _TOO_DEEP as exc:
        raise CodeError(where, f"not a Python expression: {_TOO_DEEP_MESSAGE}", cause=exc) from None


@functools.lru_cache(maxsize=4096)
def compile_statements(source: str, where: str = "<statements>") -> types.CodeType:
    try:
        return compile(source, where, "exec")
    except SyntaxError as exc:
        raise CodeError(where, f"not Python statements: {exc.msg} (line {exc.lineno})", cause=exc) from None
    except _TOO_DEEP as exc:
        raise CodeError(where, f"not Python statements: {_TOO_DEEP_MESSAGE}", cause=exc) from None


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
            except _TOO_DEEP:
                raise CodeError("template", f"{{{{ {candidate.strip()[:40]} ... }}}}: {_TOO_DEEP_MESSAGE}") from None
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
    frozen: list[str] = []
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
        if (isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del))
                and isinstance(node.value, ast.Name) and node.value.id in _READ_ONLY):
            frozen.append(node.value.id)
    # a local `for error in ...` is the code's own
    misuse.extend(f"{name} is read-only: only ctx takes assignments" for name in frozen if name not in stores)
    known = set(allowed) | SCOPE_NAMES | BUILTIN_NAMES | stores
    unknown = tuple(sorted({name for name in loads if name not in known}))
    free = frozenset(name for name in loads if name not in stores)  # a local `for event in ...` is not the scope's
    return NameUse(free, unknown, frozenset(reads), frozenset(writes), tuple(dict.fromkeys(impure)),
                   frozenset(params_reads), tuple(dict.fromkeys(misuse)), frozenset(resources_reads))


_NAMESPACES = ("ctx", "params", "error", "event", "run", "activity", "resources", "ending")
#: Namespaces the engine binds frozen everywhere: assigning a field of one always fails.
_READ_ONLY = ("params", "error", "event", "run", "activity", "resources", "ending")
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


#: Values that are data, never callable: a name assigned one of these is no function.
_DATA_VALUES = (ast.Constant, ast.JoinedStr, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.ListComp, ast.SetComp,
                ast.DictComp, ast.GeneratorExp, ast.Compare)
#: Operators whose result is data when all their operands are (``-1``, ``60 * 60``); ``fast or json.dumps`` or a
#: parser combinator ``a | b`` may be a function.
_OPERATORS = (ast.BoolOp, ast.BinOp, ast.UnaryOp)


def _is_data(value: ast.expr) -> bool:
    todo = [value]  # iterative: ``"a" + "b" + ...`` over a thousand terms is one expression
    while todo:
        node = todo.pop()
        if isinstance(node, _OPERATORS):
            todo.extend(child for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr))
        elif not isinstance(node, _DATA_VALUES):
            return False
    return True


def _assigned(target: ast.expr, value: Optional[ast.expr]) -> Iterator[tuple[str, bool]]:
    """``(name, may be a function)`` for every name ``target = value`` binds.

    A tuple or list unpacked from one of the same length pairs up element by element; unpacked from anything else,
    its names are of unknown kind and count as functions (the scan must not refuse what the run accepts).
    """
    if isinstance(target, ast.Name):
        yield target.id, value is None or not _is_data(value)
    elif isinstance(target, ast.Starred):
        yield from _assigned(target.value, None)
    elif isinstance(target, (ast.Tuple, ast.List)):
        pairs = (isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts)
                 and not any(isinstance(item, ast.Starred) for item in target.elts + value.elts))
        for index, item in enumerate(target.elts):
            yield from _assigned(item, value.elts[index] if pairs else None)  # type: ignore[union-attr]


#: A star import's mark in module_names' sets: the names it brings are unknown to a scan.
STAR = "*"


def module_names(source: str) -> tuple[frozenset[str], frozenset[str]]:
    """Public names and public function names a companion module defines -- without executing it.

    What the module binds at its top level, inside top-level ``if``/``try``/``with``/``for`` blocks too (an import
    with a fallback). A function is a ``def``, a class, a name imported from a module, or a name assigned anything
    but data -- literals and operators over them (``build = partial(...)``, a lambda, ``a | b``): the scan cannot
    tell those apart, the run can. So is a name a loop, a ``with`` or a walrus binds. A star import's names are
    unknown to a scan: it adds ``STAR`` to both sets, and the namespace then takes any name.
    """
    names: set[str] = set()
    functions: set[str] = set()
    _bind(ast.parse(source).body, names, functions)
    public = frozenset(n for n in names if not n.startswith("_"))
    return public, frozenset(n for n in functions if not n.startswith("_"))


def _bind(body: list[ast.stmt], names: set[str], functions: set[str]) -> None:
    """The module-level names ``body`` binds (not the ones inside functions and classes)."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            functions.add(node.name)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    names.add(STAR)
                    functions.add(STAR)
                else:
                    name = alias.asname or alias.name.split(".")[0]
                    names.add(name)
                    if isinstance(node, ast.ImportFrom):
                        functions.add(name)
            continue
        if isinstance(node, ast.AnnAssign) and node.value is None:
            continue  # `x: int` binds nothing
        stored = set(_stored(node))
        names.update(stored)
        data: set[str] = set()
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):  # `n += 1` over data is data
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                for name, function in _assigned(target, node.value):
                    (functions if function else data).add(name)
        functions.update(stored - data)  # a loop's, a with's or a walrus's name: whatever the run binds there
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                _bind([child], names, functions)
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                _bind(child.body, names, functions)


#: Below a statement, what binds no module name: nested statements (``_bind`` has them) and scopes of their own.
_NOT_STORED = (ast.stmt, ast.ExceptHandler, ast.match_case, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
               ast.GeneratorExp)


def _stored(node: ast.AST) -> Iterator[str]:
    """Names a statement assigns itself: targets, ``as`` names, walrus names -- not nested statements' or scopes'.

    Iterative: a value of a thousand ``+`` terms compiles, and must not exhaust the recursion limit here.
    """
    todo = [child for child in ast.iter_child_nodes(node) if not isinstance(child, _NOT_STORED)]
    while todo:
        child = todo.pop()
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            yield child.id
        todo.extend(inner for inner in ast.iter_child_nodes(child) if not isinstance(inner, _NOT_STORED))


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
        self.open = STAR in names  # a scanned star import: any name may be the module's, the run decides
        self._names = (frozenset(self.public) | frozenset(names)) - {STAR}
        self._functions = (frozenset(k for k, v in self.public.items() if callable(v)) | frozenset(functions)) - {STAR}

    def names(self) -> frozenset[str]:
        return self._names

    def has_function(self, name: str) -> bool:
        return self.open or name in self._functions

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


_MODULE_NUMBERS = itertools.count(1)


def load_module(source: str, filename: str) -> Namespace:
    """Execute a companion module's source as a new module of its own.

    Every load is a fresh module under a name of its own (``stategraph_machine_<n>_<stem>``), so a changed file is
    picked up on the next load, two machines -- or two versions of one machine -- never shadow each other, and a
    run's snapshot source is what runs. The module sits in ``sys.modules`` for as long as its ``Namespace`` lives:
    what looks a module up there (dataclasses, pydantic's forward references, ``typing.get_type_hints``, pickle)
    works as in any module, and a finished run leaves nothing behind.
    """
    stem = re.sub(r"\W", "_", posixpath.splitext(posixpath.basename(filename.replace("\\", "/")))[0])
    name = f"stategraph_machine_{next(_MODULE_NUMBERS)}_{stem}"  # no dots: pickle imports the name
    try:
        code = compile(source, filename, "exec")
    except SyntaxError as exc:
        raise CodeError(filename, f"companion module does not compile: {exc.msg} (line {exc.lineno})",
                        cause=exc) from None
    except _TOO_DEEP as exc:
        raise CodeError(filename, f"companion module does not compile: {_TOO_DEEP_MESSAGE}", cause=exc) from None
    module = types.ModuleType(name)
    module.__file__ = filename
    module.__builtins__ = builtins  # type: ignore[attr-defined]
    sys.modules[name] = module
    try:
        exec(code, module.__dict__)  # noqa: S102 -- machine code is trusted (§8.3)
    except BaseException as exc:
        sys.modules.pop(name, None)
        if not isinstance(exc, Exception):
            raise
        raise CodeError(filename, f"companion module raised on import: {type(exc).__name__}: {exc}",
                        cause=exc) from exc
    namespace = Namespace(module.__dict__)
    weakref.finalize(namespace, sys.modules.pop, name, None)
    return namespace


def scan_module(source: str, filename: str) -> Namespace:
    """The companion module's names for validation, without executing it."""
    try:
        names, functions = module_names(source)
    except SyntaxError as exc:
        raise CodeError(filename, f"companion module does not compile: {exc.msg} (line {exc.lineno})",
                        cause=exc) from None
    except _TOO_DEEP as exc:
        raise CodeError(filename, f"companion module does not compile: {_TOO_DEEP_MESSAGE}", cause=exc) from None
    return Namespace(names=names, functions=functions)
