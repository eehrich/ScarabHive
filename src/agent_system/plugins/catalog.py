"""The plugin TYPES, read from manifests -- without importing a single plugin.

Answering "is this type a known plugin?" (config inheritance in settings.py)
ran a full discovery. Measured 2026-09-04 in a fresh process: the first
inheritance resolution took 0.85 s and left 187 plugin modules in sys.modules,
for a membership test.

The catalog names a directory the way discovery does: by its FOLDER name. A
manifest ``name`` field renames nothing (``todo`` ships one saying
``todo_management`` and is discovered as ``todo``), and a directory counts only
when its entrypoint module exists -- a library-only plugin (``coder``,
``writer_publish``: manifest, no plugin.py) is not a type, discovery skips it.

Whatever cannot be named without executing the module makes the catalog fall
back to the real discovery, so its answer stays identical to discovery's:
a legacy directory without a manifest, a single-file plugin (its ``register()``
may return any name), a ``PLUGIN_NAME`` constant, an entry-point plugin. None
of these exist in this repo today (measured: 0/0/0/0 across all three plugin
directories), which is why the fallback is cheap to keep and worth keeping.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from .plugin_manifest import load_plugin_metadata

logger = logging.getLogger(__name__)


class PluginCatalog:
    """Plugin types and manifests for a set of plugin directories."""

    def __init__(self, dirs: Iterable[Path]):
        self._dirs = tuple(Path(d) for d in dirs)
        self._types: frozenset[str] | None = None
        self._manifests: dict[str, dict] = {}

    @property
    def dirs(self) -> tuple[Path, ...]:
        return self._dirs

    def types(self) -> frozenset[str]:
        """Every type name discovery would register, without importing."""
        if self._types is None:
            names, needs_import = self._scan()
            if needs_import:
                names |= self._names_from_discovery()
            self._types = frozenset(names)
        return self._types

    def manifest(self, typ: str) -> dict | None:
        """The plugin.toml/plugin.yaml contents of a type, or None."""
        self.types()
        return self._manifests.get(typ)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _scan(self) -> tuple[set[str], bool]:
        names: set[str] = set()
        needs_import = False
        for directory in self._dirs:
            if not directory.is_dir():
                continue
            if any(p.name != "__init__.py" for p in directory.glob("*.py")):
                needs_import = True  # single-file plugin: register() picks the name
            for sub in sorted(directory.iterdir()):
                if not sub.is_dir():
                    continue
                metadata = load_plugin_metadata(sub)
                if not metadata:
                    if (sub / "plugin.py").exists():
                        needs_import = True  # legacy plugin, name only known after exec
                    continue
                entrypoint = metadata.get("entrypoint", "plugin:PLUGIN_FACTORY")
                module = entrypoint.split(":", 1)[0] if isinstance(entrypoint, str) else "plugin"
                entry_file = sub / f"{module}.py"
                if not entry_file.exists():
                    continue  # library-only plugin; discovery skips it too
                if self._declares_own_name(entry_file):
                    needs_import = True
                names.add(sub.name)
                self._manifests[sub.name] = metadata
        if self._entry_point_plugins_exist():
            needs_import = True
        return names, needs_import

    @staticmethod
    def _declares_own_name(entry_file: Path) -> bool:
        try:
            return "PLUGIN_NAME" in entry_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.debug("Cannot read %s: %s", entry_file, e)
            return True  # unreadable: let discovery decide

    @staticmethod
    def _entry_point_plugins_exist(group: str = "agent_system.mcp_plugins") -> bool:
        try:
            from importlib.metadata import entry_points
            return bool(list(entry_points(group=group)))
        except Exception as e:
            logger.debug("Entry point scan failed: %s", e)
            return False

    def _names_from_discovery(self) -> set[str]:
        from .discovery import discover_all_plugins
        logger.debug("Plugin catalog falls back to discovery for %s", [str(d) for d in self._dirs])
        return set(discover_all_plugins(dirs=list(self._dirs)))
