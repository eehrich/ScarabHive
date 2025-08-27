from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
from typing import Callable, Dict, Iterable

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
    # filesystem dirs
    if dirs:
        for d in dirs:
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

    return plugins
