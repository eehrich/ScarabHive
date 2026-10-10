"""The plugin TYPES, read from manifests -- without importing a single plugin.

Answering "is this type a known plugin?" (config inheritance in config/inheritance.py)
ran a full discovery. Measured 2026-09-04 in a fresh process: the first
inheritance resolution took 0.85 s and left 187 plugin modules in sys.modules,
for a membership test.

The catalog names a directory the way discovery does: by its FOLDER name. A
manifest ``name`` field renames nothing (``todo`` ships one saying
``todo_management`` and is discovered as ``todo``), and a directory counts only
when its entrypoint module exists AND names a factory -- a library-only plugin
(``coder``, ``writer_publish``: manifest, no plugin.py) is not a type, and
neither is a module without ``PLUGIN_FACTORY``; discovery drops both.

Whatever cannot be named without executing the module makes the catalog fall
back to the real discovery, so its answer stays identical to discovery's:
a legacy directory without a manifest, a single-file plugin (its ``register()``
may return any name), a directory plugin that defines ``register()`` itself, a
``PLUGIN_NAME`` constant, an entry-point plugin. None of these exist in this
repo today (measured 2026-09-04: 0 of each across all three plugin
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
        """The plugin.toml contents of a type, or None."""
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
                # Parse the entrypoint exactly as discovery does: the two
                # defaults hold unless the value carries a colon.
                module, factory = "plugin", "PLUGIN_FACTORY"
                entrypoint = metadata.get("entrypoint", "plugin:PLUGIN_FACTORY")
                if isinstance(entrypoint, str) and ":" in entrypoint:
                    module, factory = entrypoint.split(":", 1)
                entry_file = sub / f"{module}.py"
                if not entry_file.exists():
                    continue  # library-only plugin; discovery skips it too
                source = self._read(entry_file)
                if source is None:
                    needs_import = True  # unreadable: let discovery decide
                    continue
                if "PLUGIN_NAME" in source or "def register(" in source:
                    # The module names itself, and only running it says how.
                    # Adding the folder name here as well would invent a type
                    # discovery never registers. Asked FIRST: discovery reads
                    # register() before it looks for any factory at all.
                    needs_import = True
                    continue
                if factory not in source and "PLUGIN_FACTORY" not in source:
                    continue  # nothing to export; discovery drops it as well
                names.add(sub.name)
                self._manifests[sub.name] = metadata
        if self._entry_point_plugins_exist():
            needs_import = True
        return names, needs_import

    @staticmethod
    def _read(entry_file: Path) -> str | None:
        try:
            return entry_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.debug("Cannot read %s: %s", entry_file, e)
            return None

    @staticmethod
    def _entry_point_plugins_exist(group: str | None = None) -> bool:
        try:
            from importlib.metadata import entry_points

            from .discovery import ENTRYPOINT_GROUP, LEGACY_ENTRYPOINT_GROUP
            groups = (group,) if group else (ENTRYPOINT_GROUP, LEGACY_ENTRYPOINT_GROUP)
            return any(list(entry_points(group=g)) for g in groups)
        except Exception as e:
            logger.debug("Entry point scan failed: %s", e)
            return False

    def _names_from_discovery(self) -> set[str]:
        from .discovery import discover_all_plugins
        logger.debug("Plugin catalog falls back to discovery for %s", [str(d) for d in self._dirs])
        return set(discover_all_plugins(dirs=list(self._dirs)))
