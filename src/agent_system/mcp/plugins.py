from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
import os
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

    # Simplified directory handling:
    # - Allow environment override via AGENT_PLUGIN_DIR or AGENT_PLUGIN_DIRS
    # - Prefer explicitly passed `dirs` from caller (typically from config)
    # - If none provided, fall back to repo_root/plugins
    repo_root = Path(__file__).resolve().parents[2]

    env_dirs = []
    env_single = os.environ.get("AGENT_PLUGIN_DIR")
    env_multi = os.environ.get("AGENT_PLUGIN_DIRS")
    if env_single:
        env_dirs = [p.strip() for p in env_single.split(";") if p.strip()]
    elif env_multi:
        env_dirs = [p.strip() for p in env_multi.split(";") if p.strip()]

    # choose source dirs: explicit dirs -> env dirs -> default repo/plugins
    source_dirs = []
    if dirs:
        source_dirs = list(dirs)
    elif env_dirs:
        source_dirs = env_dirs
    else:
        source_dirs = [repo_root.joinpath("plugins")]

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
                    p = (repo_root / p).resolve()

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
