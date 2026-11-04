"""The base layers import nothing above them -- not even inside a function.

``core/`` and ``utils/`` sit at the bottom of the package map
(docs/_arch_review_core.md §2): everything may import them, and they import
none of the application, presentation or agent layers. A lazy import inside a
function works at run time, which is how every one of these crept in, but it is
the same dependency and a cycle the day someone hoists it. The fix is to move
the shared constant or function down (as ``core/session_ids.py`` did for the
session id rules), or to invert the call with a registered callback.

The allowlist names the upward imports that remain, each with why it is not
simply moved. It is meant to shrink: a new upward import fails this test, and
an entry that no longer matches fails it too, so a fixed one is crossed off.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, Optional

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "agent_system"

#: The layers checked: nothing in them may import a layer listed in UPPER.
BASE_LAYERS = ("core", "utils")

#: The layers above them.
UPPER = ("api", "services", "servers", "auth", "plugins", "cli_utils", "ui",
         "llm", "tools", "hooks", "skills")

#: (importing module, imported module) pairs that are known and still allowed.
ALLOWED = {
    # Whether a person reads a run: asks the live stream forwarders and the
    # background job manager, both state of the agent server and the services.
    ("agent_system.core.run_questions",
     "agent_system.servers.agent.components.status_forwarding"),
    # Loading a plugin schema from its directory: the loader lives with the
    # plugin machinery; moving it is a plugins/ change.
    ("agent_system.core.schema_base_mixin", "agent_system.plugins.schema_loader"),
    # Waking a session runs an agent for its owner: the access check and the
    # session defaults are auth and CLI policy, needed only on that path.
    ("agent_system.core.session_presence.presence", "agent_system.auth.agent_access"),
    ("agent_system.core.session_presence.presence", "agent_system.cli_utils.session_defaults"),
    # A woken process signs a token for its own run.
    ("agent_system.core.session_presence.process", "agent_system.auth.security"),
    # Building a multimodal message needs the message model and what a model accepts; both live with the LLM layer.
    ("agent_system.utils.multimodal_processor", "agent_system.llm.models"),
    ("agent_system.utils.multimodal_processor", "agent_system.llm.capabilities"),
    ("agent_system.utils.multimodal_tool_content", "agent_system.llm.models"),
    # A wake reports which request started it; the status bus knows the current one.
    ("agent_system.core.session_presence.wake", "agent_system.tools.status"),
    # The sandbox keeps the user database out of reach; it asks auth where that is.
    ("agent_system.utils.path_sandbox", "agent_system.auth.database"),
}


def _module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve(module: str, is_package: bool, node: ast.ImportFrom) -> Optional[str]:
    """The absolute name an ``ImportFrom`` names (``from ..x import y`` -> ``agent_system.x``)."""
    if node.level == 0:
        return node.module
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _imports(path: Path) -> Iterator[tuple[str, int]]:
    """Every module ``path`` imports, at any depth, with its line."""
    module = _module_name(path)
    is_package = path.name == "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            target = _resolve(module, is_package, node)
            if target is None:
                continue
            yield target, node.lineno
            # ``from agent_system import services`` names the layer as a member
            for alias in node.names:
                yield f"{target}.{alias.name}", node.lineno


def _upper_layer(target: str) -> Optional[str]:
    parts = target.split(".")
    if len(parts) >= 2 and parts[0] == "agent_system" and parts[1] in UPPER:
        return parts[1]
    return None


def _upward_imports() -> list[tuple[str, str, int]]:
    found = []
    for layer in BASE_LAYERS:
        for path in sorted((PACKAGE_ROOT / layer).rglob("*.py")):
            module = _module_name(path)
            for target, line in _imports(path):
                if _upper_layer(target) is not None:
                    found.append((module, target, line))
    return found


def _covers(entry: str, target: str) -> bool:
    """Whether an allowlist entry covers ``target``: the entry itself, a name in it, or the
    package it is imported from (``from agent_system.auth import security`` names both)."""
    return target == entry or target.startswith(f"{entry}.") or entry.startswith(f"{target}.")


def _allowed(module: str, target: str) -> bool:
    return any(module == m and _covers(t, target) for m, t in ALLOWED)


def test_base_layers_import_nothing_above_them():
    offending = sorted({f"{m}:{line} imports {t}" for m, t, line in _upward_imports() if not _allowed(m, t)})
    assert not offending, (
        "core/ and utils/ must not import the layers in UPPER; "
        "move the shared piece down instead:\n  " + "\n  ".join(offending))


def test_every_allowlist_entry_is_still_needed():
    seen = {(m, t) for m, t, _ in _upward_imports()}
    stale = sorted(f"{m} -> {t}" for m, t in ALLOWED
                   if not any(sm == m and (st == t or st.startswith(f"{t}.")) for sm, st in seen))
    assert not stale, "fixed upward imports, cross them off ALLOWED:\n  " + "\n  ".join(stale)


@pytest.mark.parametrize("source, expected", [
    ("from ...services.session_paths import x", "agent_system.services.session_paths"),
    ("from agent_system.api import app", "agent_system.api"),
    ("def f():\n    import agent_system.ui.catalog", "agent_system.ui.catalog"),
])
def test_the_checker_sees_relative_absolute_and_lazy_imports(monkeypatch, source, expected):
    pkg = PACKAGE_ROOT / "core" / "session_presence"
    probe = pkg / "_layering_probe.py"
    monkeypatch.setattr(Path, "read_text", lambda self, encoding=None: source)
    targets = [t for t, _ in _imports(probe)]
    assert expected in targets and _upper_layer(expected) is not None
