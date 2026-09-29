"""Pins the layering inside ``agent_system.tools``.

The package holds two layers that only look like one:

* the **plugin base** -- ``base``, ``status``, ``schema_mixin``,
  ``schema_based``, ``hook_tool_server`` -- which every plugin is written
  against, and
* the **integration layer** -- ``tool_cache``, ``integration`` -- which
  bootstraps plugins and finds whoever federates external tools.

The upper layer may know the base. The base must never know the upper layer.
That independence is what allowed the external MCP client to move out into the
``mcp_client`` plugin, and what keeps it out.

Nothing about that invariant is visible in a normal test run: an import added
at the top of ``base.py`` keeps every existing test green while quietly
welding the layers back together. The check therefore runs in a SUBPROCESS --
by the time pytest reaches this file the parent process has long imported
everything, so ``sys.modules`` there proves nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

#: Everything a plugin needs. Importing any of these must stay client-free.
PLUGIN_BASE_MODULES = [
    "agent_system.tools.schema_based",  # what the 44 plugins actually import
    "agent_system.tools.base",
    "agent_system.tools.status",
    "agent_system.tools.schema_mixin",
    "agent_system.tools.hook_tool_server",  # the base for a plugin that is also a hook
]

#: The integration layer. None of it may be dragged in by the modules above.
PROTOCOL_CLIENT_MODULES = {
    "agent_system.tools.integration",
    "agent_system.tools.tool_cache",
}


def _imported_modules(target: str) -> list[str]:
    """Import *target* in a fresh interpreter, return the agent_system modules loaded."""
    code = (
        "import json, sys\n"
        f"import {target}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('agent_system.'))))"
    )
    env = dict(os.environ)
    # Hand the child the parent's import path; the package may be reachable
    # through the checkout (src/) rather than through site-packages.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert proc.returncode == 0, f"importing {target} failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("target", PLUGIN_BASE_MODULES)
def test_plugin_base_does_not_import_protocol_client(target):
    """A plugin-facing import must not drag the MCP protocol client along."""
    loaded = set(_imported_modules(target))
    leaked = sorted(loaded & PROTOCOL_CLIENT_MODULES)
    assert not leaked, (
        f"Importing {target} pulled in the protocol client: {leaked}. "
        "The plugin base must stay independent of it -- check for a new "
        "top-level import, or for an eager re-export in tools/__init__.py."
    )


def test_package_init_stays_lazy():
    """Importing the package itself must not load any submodule eagerly.

    tools/__init__.py runs on every submodule import, so an eager re-export
    there costs every plugin the whole client (PEP 562 defers them instead).
    """
    loaded = set(_imported_modules("agent_system.tools"))
    submodules = sorted(m for m in loaded if m.startswith("agent_system.tools."))
    assert not submodules, (
        f"agent_system.tools imported submodules eagerly: {submodules}. "
        "Re-exports belong in _LAZY_EXPORTS, not in a top-level import."
    )


def test_every_module_is_assigned_to_a_layer():
    """No module in the package may sit outside both lists.

    This is how the gap around the old ``security`` module happened: the two
    lists are hand-written,
    nothing compared them against the directory, and a module that appears in
    neither is simply not guarded -- while the file still reads as if it
    covered the package. A new module now has to be classified, or this fails.
    """
    from pathlib import Path

    import agent_system.tools as pkg

    on_disk = {
        f"agent_system.tools.{p.stem}"
        for p in Path(pkg.__file__).parent.glob("*.py")
        if p.stem != "__init__"
    }
    classified = set(PLUGIN_BASE_MODULES) | PROTOCOL_CLIENT_MODULES

    unclassified = sorted(on_disk - classified)
    assert not unclassified, (
        f"Modules in agent_system.tools belong to neither layer: {unclassified}. "
        "Add each to PLUGIN_BASE_MODULES (plugin-facing) or "
        "PROTOCOL_CLIENT_MODULES (talks to external servers)."
    )

    stale = sorted(classified - on_disk)
    assert not stale, f"Listed modules that no longer exist: {stale}"


def test_lazy_exports_all_resolve():
    """Every name in __all__ must actually be reachable.

    A lazy re-export fails at ATTRIBUTE ACCESS, not at import, so a typo in
    _LAZY_EXPORTS would otherwise surface only when some caller happens to
    touch that one name.
    """
    import agent_system.tools as pkg

    for name in pkg.__all__:
        assert getattr(pkg, name) is not None, f"{name} did not resolve"

    with pytest.raises(AttributeError):
        pkg.ThisNameDoesNotExist
