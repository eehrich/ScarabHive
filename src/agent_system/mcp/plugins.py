from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
from typing import Callable, Dict, Iterable
import yaml

from .base import MCPServer

logger = logging.getLogger(__name__)


def discover_plugins(path: Path) -> Dict[str, Callable[..., MCPServer]]:
    """Discover plugin modules under `path` and return a mapping of
    plugin name -> factory function/class that constructs an MCPServer.

    Each plugin should expose a `register()` function that returns a
    tuple (name, factory) or directly expose `PLUGIN_NAME` and `PLUGIN_FACTORY`.
    """
    out: Dict[str, Callable[..., MCPServer]] = {}
    if not path or not path.exists() or not path.is_dir():
        return out

    # Support two layouts:
    # 1) plugin as a single python file: plugins/foo.py
    # 2) plugin as a package folder: plugins/foo/plugin.py (recommended)

    # First, discover plugin folders with plugin.py
    for d in path.iterdir():
        if d.is_dir():
            plugin_file = d / "plugin.py"
            if plugin_file.exists():
                spec = importlib.util.spec_from_file_location(f"{d.name}.plugin", str(plugin_file))
                if spec is None or spec.loader is None:
                    logger.debug("Skipping plugin %s: cannot create spec", plugin_file)
                    continue
                mod = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(mod)
                except Exception as e:
                    logger.warning("Failed to load plugin module %s: %s", plugin_file, e)
                    continue

                # same registration logic as single-file plugins
                try:
                    if hasattr(mod, "register") and inspect.isfunction(mod.register):
                        name, factory = mod.register()
                        out[name] = factory
                        continue
                except Exception as e:
                    logger.warning("Plugin %s register() failed: %s", plugin_file, e)
                    continue

                name = getattr(mod, "PLUGIN_NAME", None)
                factory = getattr(mod, "PLUGIN_FACTORY", None)
                # Load optional plugin metadata file (plugin.yaml) if present
                metadata = None
                meta_file = d / "plugin.yaml"
                if meta_file.exists():
                    try:
                        with open(meta_file, 'r', encoding='utf-8') as fh:
                            metadata = yaml.safe_load(fh) or {}
                    except Exception as e:
                        logger.warning("Failed to read metadata %s: %s", meta_file, e)

                if name and factory:
                    # attach metadata to factory for downstream use
                    try:
                        setattr(factory, '_plugin_metadata', metadata)
                    except Exception:
                        pass
                    out[name] = factory

    # Then, discover legacy single-file plugins for backward compatibility
    for p in path.glob("*.py"):
        spec = importlib.util.spec_from_file_location(p.stem, str(p))
        if spec is None or spec.loader is None:
            logger.debug("Skipping plugin %s: cannot create spec", p)
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.warning("Failed to load plugin module %s: %s", p, e)
            continue

        # Try register() function
        try:
            if hasattr(mod, "register") and inspect.isfunction(mod.register):
                name, factory = mod.register()
                out[name] = factory
                continue
        except Exception as e:
            logger.warning("Plugin %s register() failed: %s", p, e)
            continue

        # Try constants
        name = getattr(mod, "PLUGIN_NAME", None)
        factory = getattr(mod, "PLUGIN_FACTORY", None)
        if name and factory:
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
                if name and callable(factory):
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

    # Normalize and resolve dirs: accept Path-like objects; resolve relative
    # paths against the repository root so `plugins` in config points to
    # repo_root/plugins by default.
    repo_root = Path(__file__).resolve().parents[2]
    norm_dirs: list[Path] = []
    if dirs:
        for d in dirs:
            try:
                # Keep original semantics for Path-like inputs: resolve relative
                # paths against current working directory so callers that pass
                # a Path('plugins') expecting cwd-relative behavior continue to work.
                if isinstance(d, Path):
                    p = d
                    if not p.is_absolute():
                        try:
                            p = p.resolve()
                        except Exception:
                            p = Path.cwd().joinpath(p)
                else:
                    # for strings (likely from config), resolve against repo_root
                    p = Path(d)
                    if not p.is_absolute():
                        p = repo_root.joinpath(p)
            except Exception:
                continue
            norm_dirs.append(p)

    # If no directories are configured or none exist, fallback to repo/plugins
    if not norm_dirs or not any(p.exists() for p in norm_dirs):
        repo_plugins = repo_root.joinpath("plugins")
        if repo_plugins.exists():
            subdirs = [d for d in repo_plugins.iterdir() if d.is_dir()]
            norm_dirs = subdirs if subdirs else [repo_plugins]

    # filesystem dirs
    if norm_dirs:
        for d in norm_dirs:
            try:
                discovered = discover_plugins(Path(d))
                plugins.update(discovered)
            except Exception as e:
                logger.warning("Error discovering plugins in %s: %s", d, e)

    # entry point plugins
    try:
        eps = discover_entrypoint_plugins(group=group)
        plugins.update(eps)
    except Exception as e:
        logger.warning("Error discovering entrypoint plugins: %s", e)

    # Ensure metadata: for factories missing `_plugin_metadata`, try to load
    # a `plugin.yaml` from repo/plugins/<name>/plugin.yaml and attach it.
    try:
        for name, factory in list(plugins.items()):
            meta = getattr(factory, "_plugin_metadata", None)
            if not meta:
                meta_path = repo_root.joinpath("plugins", name, "plugin.yaml")
                if meta_path.exists():
                    try:
                        loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                        try:
                            setattr(factory, "_plugin_metadata", loaded)
                        except Exception:
                            pass
                    except Exception:
                        # ignore metadata load errors
                        pass
    except Exception:
        pass

    return plugins
