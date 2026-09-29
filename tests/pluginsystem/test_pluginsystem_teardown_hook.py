"""A plugin's teardown must carry the name the framework calls: ``stop_plugin``.

``plugins/capabilities.stop_plugin`` looks up exactly ONE attribute and has no
fallback::

    hook = getattr(plugin, "stop_plugin", None)
    if hook is None:
        return

and ``tool_adapter`` asks it of ``getattr(adapter, "plugin_server", adapter)``
-- what ``PLUGIN_FACTORY`` returned, not an inner helper. A plugin that calls
its teardown ``shutdown``, ``close`` or ``cleanup`` is therefore never asked to
stop, silently: nothing logs, nothing fails, the sockets and tasks simply stay.

That is not a hypothetical. Three plugins had it at once (2026-09-21):
``terminal`` (``cleanup``) left every background process with its capture task
attached, ``ssh_control`` (``close``) left remote commands and their channels
open, and ``file_ops`` (``shutdown``) never released the semantic indexer or
its vector store -- the shape the chroma FD leak had. Being unreachable,
``file_ops``' had also rotted: it ended on a ``super().shutdown()`` that no
class in its MRO defines.

So the rule pinned here is deliberately blunt: **if the class PLUGIN_FACTORY
hands back has a teardown-looking method at all, it must also have
``stop_plugin``.** No fallback is added in the framework instead, on purpose --
guessing names at runtime would mean calling whatever a plugin happens to call
``cleanup``, and some of those are tools that delete things.

Bases outside the package are not followed, so agent plugins are judged by
what they add, not by what they inherit from ``Agent``. The ``Agent`` base
has its own test: ``tests/agent/test_agent_stop_plugin.py`` pins that it has
no teardown under a dead name any more (it had ``shutdown()``, which nothing
called) and that stopping an agent touches neither the tool integration --
the process entry point's -- nor the session state of runs still going.

Read statically, like ``tests/plugins/test_status_end_lines.py``'s second
layer: importing every plugin would need a GPU, an SSH server and half the
optional dependencies. All 62 factories resolve today; anything that stops
resolving goes into UNRESOLVED with its reason, and that list is asserted to
be exactly what is expected -- a plugin whose factory cannot be read gets
looked at rather than skipped.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytest

PLUGIN_ROOTS = [
    Path(__file__).resolve().parents[2] / "src" / "plugins",
    Path(__file__).resolve().parents[2] / "src" / "plugins_writer",
]

#: The names a teardown gets called when nobody says which one counts. Exact
#: matches only: ``cleanup_lessons`` is a tool, ``close_all`` belongs to a
#: pool, and neither is what the framework would have called.
TEARDOWN_NAMES = {"shutdown", "close", "cleanup", "teardown", "aclose", "dispose", "stop"}

#: The hook the framework actually calls.
HOOK = "stop_plugin"

#: Packages whose PLUGIN_FACTORY cannot be read statically, with the reason.
#: Each is checked by hand; the assert below keeps the list honest, because a
#: package that quietly joins it would otherwise be a plugin nobody checks.
UNRESOLVED: Dict[str, str] = {}

#: Plugins whose factory class carries a teardown-looking name that is NOT a
#: teardown. Empty today; an entry needs the reason, not just the name.
ALLOWED: Dict[str, str] = {}


def _package_dirs() -> List[Path]:
    out = []
    for root in PLUGIN_ROOTS:
        if not root.is_dir():
            continue
        # No __init__.py requirement: `basic_agent` has none and is still a
        # plugin. _declares_a_factory does the filtering.
        out += [d for d in sorted(root.iterdir())
                if d.is_dir() and not d.name.startswith(("_", "."))
                and any(d.glob("*.py"))]
    return out


def _modules(package: Path) -> List[Tuple[Path, ast.Module]]:
    """Every module of a package except its tests, parsed."""
    out = []
    for path in sorted(package.rglob("*.py")):
        if "tests" in path.parts:
            continue
        try:
            out.append((path, ast.parse(path.read_text(encoding="utf-8"))))
        except (OSError, SyntaxError):  # a plugin that does not parse is another test's job
            continue
    return out


def _declares_a_factory(modules: List[Tuple[Path, ast.Module]]) -> bool:
    """Is this a plugin package at all?

    ``llm_*`` are LLM providers loaded by ``llm.registry`` from their
    ``plugin.toml``, and ``writer_core`` and friends are libraries. Neither
    ever reaches ``capabilities.stop_plugin``, and neither names a factory.
    """
    return any(
        (isinstance(node, ast.Name) and node.id == "PLUGIN_FACTORY")
        or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "PLUGIN_FACTORY")
        for _path, tree in modules for node in ast.walk(tree))


def _factory_name(modules: List[Tuple[Path, ast.Module]]) -> Optional[str]:
    """The class PLUGIN_FACTORY hands back, as far as the source says.

    Three forms carry it: ``PLUGIN_FACTORY = TheClass``, a factory function
    with a return annotation, and one whose body ends in ``return TheClass(...)``.
    A call expression (``= make_factory(X)``) does not, and lands in UNRESOLVED.
    """
    for _path, tree in modules:
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "PLUGIN_FACTORY" not in targets:
                    continue
                if isinstance(node.value, ast.Name):
                    return node.value.id
                # `PLUGIN_FACTORY = make_agent_plugin_factory(TheAgent)`: the
                # helper hands back an instance of its argument.
                if (isinstance(node.value, ast.Call) and node.value.args
                        and isinstance(node.value.args[0], ast.Name)):
                    return node.value.args[0].id
            elif (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "PLUGIN_FACTORY"):
                if isinstance(node.returns, ast.Name):
                    return node.returns.id
                if isinstance(node.returns, ast.Constant) and isinstance(node.returns.value, str):
                    return node.returns.value.strip("'\"")   # a string annotation
                for inner in ast.walk(node):                 # ... or what it builds
                    if (isinstance(inner, ast.Return) and isinstance(inner.value, ast.Call)
                            and isinstance(inner.value.func, ast.Name)):
                        return inner.value.func.id
    return None


def _classes(modules: List[Tuple[Path, ast.Module]]) -> Dict[str, ast.ClassDef]:
    found: Dict[str, ast.ClassDef] = {}
    for _path, tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                found.setdefault(node.name, node)
    return found


def _methods(name: str, classes: Dict[str, ast.ClassDef], seen: Set[str]) -> Set[str]:
    """Method names of a class and of its bases that live in the same package.

    A base outside the package (``SchemaBasedToolServer``) is not followed: it
    is framework code, and the framework has no teardown to inherit -- which is
    half of why plugins keep inventing one.
    """
    node = classes.get(name)
    if node is None or name in seen:
        return set()
    seen.add(name)
    out = {child.name for child in node.body
           if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for base in node.bases:
        if isinstance(base, ast.Name):
            out |= _methods(base.id, classes, seen)
    return out


def _factories() -> Dict[str, Tuple[Optional[str], Set[str]]]:
    """Per package: the factory class name and its methods."""
    out = {}
    for package in _package_dirs():
        modules = _modules(package)
        if not _declares_a_factory(modules):
            continue
        name = _factory_name(modules)
        if name is None:
            out[package.name] = (None, set())
            continue
        out[package.name] = (name, _methods(name, _classes(modules), set()))
    return out


ALL = _factories()


def test_a_plugin_with_a_teardown_carries_the_name_the_framework_calls():
    offenders = []
    for package, (cls, methods) in sorted(ALL.items()):
        if cls is None or package in ALLOWED or HOOK in methods:
            continue
        found = sorted(methods & TEARDOWN_NAMES)
        if found:
            offenders.append(f"{package}.{cls}: {', '.join(found)}")
    assert not offenders, (
        "these plugins tear down under a name nothing calls -- "
        f"add `async def {HOOK}` to the class PLUGIN_FACTORY returns "
        "(capabilities.stop_plugin has NO fallback):\n  "
        + "\n  ".join(offenders))


def test_every_plugin_factory_can_be_read():
    """Otherwise the test above quietly covers less every time one is added."""
    unresolved = {package for package, (cls, _m) in ALL.items() if cls is None}
    assert unresolved == set(UNRESOLVED), (
        "PLUGIN_FACTORY could not be resolved for "
        f"{sorted(unresolved - set(UNRESOLVED))} and is no longer unresolved for "
        f"{sorted(set(UNRESOLVED) - unresolved)}. Write `PLUGIN_FACTORY = TheClass` "
        "or give the factory function a return annotation -- or add it to "
        "UNRESOLVED with the reason and check it by hand.")


@pytest.mark.parametrize("package", ["file_ops", "terminal", "ssh_control"])
def test_the_three_that_were_broken_stay_fixed(package):
    """Named, because this is where the rule came from."""
    cls, methods = ALL[package]
    assert HOOK in methods, f"{package}.{cls} lost its {HOOK} again"


def test_the_rule_catches_a_plugin_that_renames_its_hook():
    """The matcher on a plugin that does not exist.

    Everything else here asks about the plugins as they are, and they are all
    correct today -- so an empty TEARDOWN_NAMES, or a resolver that stopped
    resolving, would read exactly the same. This one stays true however the
    real plugins are written.
    """
    source = "\n".join([
        "class Renamed:",
        "    async def shutdown(self) -> None: ...",
        "PLUGIN_FACTORY = Renamed",
    ])
    modules = [(Path("plugin.py"), ast.parse(source))]
    name = _factory_name(modules)
    assert name == "Renamed"
    methods = _methods(name, _classes(modules), set())
    assert methods & TEARDOWN_NAMES, "the teardown names no longer match a teardown"
    assert HOOK not in methods, "the hook is not supposed to be there"


def test_the_rule_is_looking_at_something():
    """A resolver that quietly stops resolving would make all of this green."""
    resolved = [c for c, _m in ALL.values() if c]
    assert len(resolved) >= 60, f"only {len(resolved)} plugin factories were read"
