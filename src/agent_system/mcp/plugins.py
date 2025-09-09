from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
import sys
import types
from typing import Callable, Dict, Iterable
import yaml

from .base import MCPServer

logger = logging.getLogger(__name__)


def discover_plugins(path: Path) -> Dict[str, Callable[..., MCPServer]]:
    """Discover plugins in a directory.

    Rules:
    - Folder plugin: <dir>/<plugin>/plugin.py must export PLUGIN_FACTORY or register().
      Name = folder name.
    - Single file plugin: <dir>/<name>.py with PLUGIN_FACTORY or register(). Name = file stem.
    - If a PLUGIN_NAME constant is present it overrides the folder/file name (backwards compatibility).
    """
    out: Dict[str, Callable[..., MCPServer]] = {}
    if not (path and path.exists() and path.is_dir()):
        return out

    for d in path.iterdir():
        if not d.is_dir():
            continue
        plugin_file = d / "plugin.py"
        if not plugin_file.exists():
            continue
        pkg_name = "plugins"
        plugin_pkg = f"{pkg_name}.{d.name}"
        try:
            if pkg_name not in sys.modules:
                pkg_mod = types.ModuleType(pkg_name)
                pkg_mod.__path__ = [str(path.resolve())]
                sys.modules[pkg_name] = pkg_mod
            if plugin_pkg not in sys.modules:
                sub_mod = types.ModuleType(plugin_pkg)
                sub_mod.__path__ = [str(d.resolve())]
                sys.modules[plugin_pkg] = sub_mod
        except Exception:
            pass

        spec = importlib.util.spec_from_file_location(f"{plugin_pkg}.plugin", str(plugin_file))
        if spec is None or spec.loader is None:
            logger.debug("Skipping plugin %s: cannot create spec", plugin_file)
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.warning("Failed to load plugin module %s: %s", plugin_file, e)
            continue

        try:
            if hasattr(mod, "register") and inspect.isfunction(mod.register):
                name, factory = mod.register()
                out[name] = factory
                continue
        except Exception as e:
            logger.warning("Plugin %s register() failed: %s", plugin_file, e)
            continue

        # Determine plugin name: prefer PLUGIN_NAME constant if present for backwards compatibility
        name = getattr(mod, "PLUGIN_NAME", None) or d.name
        factory = getattr(mod, "PLUGIN_FACTORY", None)
        metadata = None
        meta_file = d / "plugin.yaml"
        if meta_file.exists():
            try:
                with meta_file.open('r', encoding='utf-8') as fh:
                    metadata = yaml.safe_load(fh) or {}
            except Exception as e:
                logger.warning("Failed to read metadata %s: %s", meta_file, e)
        if factory:
            try:
                setattr(factory, '_plugin_metadata', metadata)
            except Exception:
                pass
            out[name] = factory

    for p in path.glob("*.py"):
        if p.name == "__init__.py":
            continue
        pkg_name = "plugins"
        try:
            if pkg_name not in sys.modules:
                pkg_mod = types.ModuleType(pkg_name)
                pkg_mod.__path__ = [str(path.resolve())]
                sys.modules[pkg_name] = pkg_mod
        except Exception:
            pass
        spec = importlib.util.spec_from_file_location(f"plugins.{p.stem}", str(p))
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.warning("Failed to load plugin module %s: %s", p, e)
            continue
        try:
            if hasattr(mod, "register") and inspect.isfunction(mod.register):
                name, factory = mod.register()
                out[name] = factory
                continue
        except Exception as e:
            logger.warning("Plugin %s register() failed: %s", p, e)
            continue
        factory = getattr(mod, "PLUGIN_FACTORY", None)
        if factory:
            name = getattr(mod, "PLUGIN_NAME", None) or p.stem
            out[name] = factory
    return out


def discover_entrypoint_plugins(group: str = "agent_system.mcp_plugins") -> Dict[str, Callable[..., MCPServer]]:
    """Discover plugins exposed via Python entry points.

    Entry point group defaults to 'agent_system.mcp_plugins'. Each
    entry point should return a callable factory that accepts
    (name, config, ssl_verify=True) and returns an MCPServer.
    """
    out: Dict[str, Callable[..., MCPServer]] = {}
    try:
        # importlib.metadata.entry_points API varies between Python versions
        from importlib import metadata
        eps = metadata.entry_points()
        # try to select by group if available
        try:
            selected = eps.select(group=group)
        except Exception:
            # older API returns a list-like; filter manually
            selected = [ep for ep in eps if getattr(ep, "group", None) == group]

        for ep in selected:
            try:
                factory = ep.load()
                name = getattr(ep, "name", None) or getattr(factory, "__name__", None)
                # Try to attach plugin metadata from the package where the
                # entry-point factory is defined. This allows packaged
                # plugins to include a `plugin.yaml` alongside their code.
                metadata_obj = None
                try:
                    mod_name = getattr(factory, "__module__", None)
                    if mod_name:
                        # Top-level package containing the factory
                        pkg = mod_name.split('.')[0]
                        try:
                            spec = importlib.util.find_spec(pkg)
                            if spec is not None:
                                # package (has submodule_search_locations)
                                if getattr(spec, "submodule_search_locations", None):
                                    pkg_path = Path(spec.submodule_search_locations[0])
                                    meta_path = pkg_path / "plugin.yaml"
                                    if meta_path.exists():
                                        metadata_obj = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                                else:
                                    # single-module distribution: check module file's parent
                                    origin = getattr(spec, "origin", None)
                                    if origin:
                                        mod_path = Path(origin).parent
                                        meta_path = mod_path / "plugin.yaml"
                                        if meta_path.exists():
                                            metadata_obj = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                        except Exception:
                            # best-effort; don't fail discovery on metadata lookup
                            metadata_obj = None
                except Exception:
                    metadata_obj = None

                if name and callable(factory):
                    if metadata_obj:
                        try:
                            setattr(factory, "_plugin_metadata", metadata_obj)
                        except Exception:
                            pass
                    out[name] = factory
            except Exception as e:
                logger.warning("Failed to load entrypoint plugin %s: %s", ep, e)
                continue
    except Exception:
        # importlib.metadata not available; silently return empty
        return out

    return out


def discover_all_plugins(dirs: Iterable[Path] | None = None, group: str = "agent_system.mcp_plugins") -> Dict[str, Callable[..., MCPServer]]:
    """Discover plugins from filesystem directories and entry points.

    dirs: iterable of Path objects to search for filesystem plugins.
    group: entry point group to query for packaged plugins.
    """
    plugins: Dict[str, Callable[..., MCPServer]] = {}

    # Use only explicitly provided `dirs` for filesystem discovery when
    # available. If none are provided, attempt to locate an importable
    # `plugins` package (editable installs place `plugins/` under `src/`),
    # and use its filesystem paths for discovery. This avoids hardcoding
    # repository paths while ensuring discoverability in common dev
    # and editable-install setups.
    source_dirs = list(dirs) if dirs else []
    if not source_dirs:
        try:
            import importlib
            spec = importlib.util.find_spec("plugins")
            if spec is not None and getattr(spec, "submodule_search_locations", None):
                for p in spec.submodule_search_locations:
                    source_dirs.append(Path(p))
        except Exception:
            # ignore and continue — we'll still discover entrypoint plugins
            pass
        # If importlib couldn't find a spec, it's possible a previous
        # discovery run created a synthetic 'plugins' module in
        # sys.modules (we do this to load filesystem plugins under the
        # 'plugins.<name>' namespace). In that case reuse its __path__
        # entries so subsequent discovery (e.g. nested bootstraps) can
        # still locate the filesystem plugins.
        if not source_dirs:
            try:
                mod = sys.modules.get("plugins")
                if mod is not None and getattr(mod, "__path__", None):
                    for p in mod.__path__:
                        source_dirs.append(Path(p))
            except Exception:
                pass
    # Normalize each source dir to an absolute Path and discover plugins there.
    for raw in source_dirs:
        try:
            if isinstance(raw, Path):
                p = raw
                if not p.is_absolute():
                    # Path objects should be resolved against current working dir
                    try:
                        p = p.resolve()
                    except Exception:
                        p = Path.cwd().joinpath(p)
            else:
                # strings (likely from config or env) resolve relative to repo_root
                p = Path(raw)
                if not p.is_absolute():
                    # resolve relative to current working dir when caller
                    # supplied a relative path
                    try:
                        p = p.resolve()
                    except Exception:
                        p = Path.cwd().joinpath(p)

            if not p.exists():
                # skip non-existing dirs silently
                continue
            discovered = discover_plugins(p)
            plugins.update(discovered)
        except Exception as e:
            logger.warning("Error discovering plugins in %s: %s", raw, e)

    # entry point plugins
    try:
        eps = discover_entrypoint_plugins(group=group)
        plugins.update(eps)
    except Exception as e:
        logger.warning("Error discovering entrypoint plugins: %s", e)

    # Ensure metadata: for factories missing `_plugin_metadata`, try to load
    # a `plugin.yaml` from the configured filesystem plugin directories (if
    # any were provided). This avoids hardcoding repository-level paths.
    if source_dirs:
        try:
            for name, factory in list(plugins.items()):
                meta = getattr(factory, "_plugin_metadata", None)
                if not meta:
                    for d in source_dirs:
                        try:
                            dp = Path(d)
                            meta_path = dp.joinpath(name, "plugin.yaml")
                            if meta_path.exists():
                                loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                                try:
                                    setattr(factory, "_plugin_metadata", loaded)
                                except Exception:
                                    pass
                                break
                        except Exception:
                            continue
        except Exception:
            pass

    return plugins
