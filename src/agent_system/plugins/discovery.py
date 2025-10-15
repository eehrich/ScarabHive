from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
import sys
import types
from typing import Any, Callable, Dict, Iterable, List
import yaml

from ..mcp.base import MCPServer

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
        except Exception as e:
            logger.debug(f"Failed to create module structure for plugin '{d.name}': {e}")

        spec = importlib.util.spec_from_file_location(f"{plugin_pkg}.plugin", str(plugin_file))
        if spec is None or spec.loader is None:
            logger.debug("Skipping plugin %s: cannot create spec", plugin_file)
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.warning(f"Failed to load plugin module {plugin_file}: {e}", exc_info=True)
            continue

        try:
            if hasattr(mod, "register") and inspect.isfunction(mod.register):
                name, factory = mod.register()
                out[name] = factory
                continue
        except Exception as e:
            logger.warning(f"Plugin {plugin_file} register() failed: {e}", exc_info=True)
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
                logger.warning(f"Failed to read plugin metadata {meta_file}: {e}", exc_info=True)
        if factory:
            try:
                setattr(factory, '_plugin_metadata', metadata)
            except Exception as e:
                logger.debug(f"Failed to attach metadata to plugin factory: {e}")
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
        except Exception as e:
            logger.debug(f"Failed to create plugins module structure: {e}")
        spec = importlib.util.spec_from_file_location(f"plugins.{p.stem}", str(p))
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.warning(f"Failed to load single-file plugin module {p}: {e}", exc_info=True)
            continue
        try:
            if hasattr(mod, "register") and inspect.isfunction(mod.register):
                name, factory = mod.register()
                out[name] = factory
                continue
        except Exception as e:
            logger.warning(f"Single-file plugin {p} register() failed: {e}", exc_info=True)
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
            selected = list(eps.select(group=group))
        except Exception as e:
            # older API returns a list-like; filter manually
            logger.debug(f"Failed to use entry_points.select(), falling back to manual filtering: {e}")
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
                                submodule_locations = getattr(spec, "submodule_search_locations", None)
                                if submodule_locations:
                                    pkg_path = Path(submodule_locations[0])
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
                        except Exception as e:
                            # best-effort; don't fail discovery on metadata lookup
                            logger.debug(f"Failed to load metadata for packaged plugin '{name}': {e}")
                            metadata_obj = None
                except Exception as e:
                    logger.debug(f"Failed to locate module for metadata lookup: {e}")
                    metadata_obj = None

                if name and callable(factory):
                    if metadata_obj:
                        try:
                            setattr(factory, "_plugin_metadata", metadata_obj)
                        except Exception as e:
                            logger.debug(f"Failed to attach metadata to entrypoint plugin factory: {e}")
                    out[name] = factory
            except Exception as e:
                logger.warning(f"Failed to load entrypoint plugin {ep}: {e}", exc_info=True)
                continue
    except Exception as e:
        # importlib.metadata not available; silently return empty
        logger.debug(f"importlib.metadata not available for entrypoint discovery: {e}")
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
            if spec is not None:
                submodule_locations = getattr(spec, "submodule_search_locations", None)
                if submodule_locations:
                    for p in submodule_locations:
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
            except Exception as e:
                logger.debug(f"Failed to reuse existing 'plugins' module paths: {e}")
    # Normalize each source dir to an absolute Path and discover plugins there.
    for raw in source_dirs:
        try:
            # All items in source_dirs are Path objects
            p = raw

            # Resolve relative paths
            if not p.is_absolute():
                try:
                    p = p.resolve()
                except Exception as e:
                    logger.debug(f"Failed to resolve relative plugin path '{raw}': {e}")
                    p = Path.cwd().joinpath(p)

            if not p.exists():
                # skip non-existing dirs silently
                continue
            discovered = discover_plugins(p)
            plugins.update(discovered)
        except Exception as e:
            logger.warning(f"Error discovering plugins in {raw}: {e}", exc_info=True)

    # entry point plugins
    try:
        eps = discover_entrypoint_plugins(group=group)
        plugins.update(eps)
    except Exception as e:
        logger.warning(f"Error discovering entrypoint plugins from group '{group}': {e}", exc_info=True)

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
                                except Exception as e:
                                    logger.debug(f"Failed to attach metadata to plugin '{name}': {e}")
                                break
                        except Exception as e:
                            logger.debug(f"Failed to load metadata for plugin '{name}' from '{d}': {e}")
                            continue
        except Exception:
            pass

    return plugins


async def register_plugin_hooks(
    plugin_name: str,
    plugin_instance: Any,
    metadata: Dict[str, Any] | None = None,
    registry: Any | None = None,
    hooks_config: Any | None = None
) -> List[str]:
    """Register hooks declared in plugin metadata.
    
    Args:
        plugin_name: Name of the plugin
        plugin_instance: Instantiated plugin server (should implement PluginHook for hook-enabled plugins)
        metadata: Plugin metadata from plugin.yaml (optional, will use _plugin_metadata attribute if not provided)
        registry: Hook registry to use (optional, will use global registry if not provided)
        hooks_config: Global hooks configuration (optional, will load from config/plugins.yaml if not provided)
    
    Returns:
        List of registered hook names
    
    Raises:
        ValueError: If hook configuration is invalid
        TypeError: If plugin doesn't implement PluginHook interface
    """
    from ..hooks import get_hook_registry, PluginHook, HookType, load_hooks_config
    
    registered_hooks = []
    
    # Get metadata from parameter or plugin instance attribute
    if metadata is None:
        metadata = getattr(plugin_instance, '_plugin_metadata', None)
    
    if not metadata:
        logger.debug(f"Plugin '{plugin_name}' has no metadata, skipping hook registration")
        return registered_hooks
    
    # Load global hooks configuration if not provided
    if hooks_config is None:
        hooks_config = load_hooks_config()
    
    hooks_list = metadata.get('hooks', [])
    if not hooks_list:
        logger.debug(f"Plugin '{plugin_name}' declares no hooks in metadata")
        return registered_hooks
    
    # Verify plugin implements PluginHook interface
    if not isinstance(plugin_instance, PluginHook):
        raise TypeError(
            f"Plugin '{plugin_name}' declares hooks but does not implement PluginHook interface. "
            "The plugin class must inherit from PluginHook and implement async hook methods."
        )
    
    # Use provided registry or get global one
    if registry is None:
        registry = get_hook_registry()
    
    for hook_metadata in hooks_list:
        try:
            # Validate required fields
            hook_name = hook_metadata.get('name')
            hook_type_str = hook_metadata.get('type')
            
            if not hook_name:
                logger.warning(f"Plugin '{plugin_name}' has hook with missing 'name', skipping")
                continue
            
            if not hook_type_str:
                logger.warning(f"Plugin '{plugin_name}' hook '{hook_name}' missing 'type', skipping")
                continue
            
            # Parse hook type
            try:
                hook_type = HookType[hook_type_str.upper()]
            except KeyError:
                logger.warning(
                    f"Plugin '{plugin_name}' hook '{hook_name}' has invalid type '{hook_type_str}'. "
                    f"Valid types: {[t.name.lower() for t in HookType]}"
                )
                continue
            
            # Get configuration from plugin metadata
            enabled = hook_metadata.get('enabled', True)
            timeout = hook_metadata.get('timeout', 30.0)
            description = hook_metadata.get('description', '')
            order_spec = hook_metadata.get('order', {})
            
            # Apply global hooks configuration overrides
            # Build full hook name for lookup (plugin.hook_name)
            full_hook_name = f"{plugin_name}.{hook_name}"
            
            # Global config overrides plugin defaults
            if full_hook_name in hooks_config.overrides:
                override = hooks_config.overrides[full_hook_name]
                if 'enabled' in override:
                    enabled = override['enabled']
                if 'timeout' in override:
                    timeout = override['timeout']
                if 'order' in override:
                    order_spec = override['order']
            
            # Apply global enabled flag if hooks are globally disabled
            if not hooks_config.enabled:
                enabled = False
            
            # Validate order specification
            before_list = order_spec.get('before', [])
            after_list = order_spec.get('after', [])
            
            if not isinstance(before_list, list):
                logger.warning(f"Hook '{hook_name}' has invalid 'before' specification (must be list)")
                before_list = []
            
            if not isinstance(after_list, list):
                logger.warning(f"Hook '{hook_name}' has invalid 'after' specification (must be list)")
                after_list = []
            
            # Check for self-reference
            if hook_name in before_list or hook_name in after_list:
                logger.warning(f"Hook '{hook_name}' references itself in ordering, removing self-reference")
                before_list = [h for h in before_list if h != hook_name]
                after_list = [h for h in after_list if h != hook_name]
            
            order = {
                'before': before_list,
                'after': after_list
            }
            
            # Register with HookRegistry (it expects a PluginHook instance)
            await registry.register_hook(
                hook_type=hook_type,
                hook_name=hook_name,
                hook=plugin_instance,  # Pass the PluginHook instance
                order_spec=order,
                enabled=enabled,
                timeout=timeout,
                description=description,
                # Additional metadata for tracking
                plugin=plugin_name,
                source="plugin_discovery"
            )
            
            registered_hooks.append(hook_name)
            logger.info(
                f"Registered hook '{hook_name}' from plugin '{plugin_name}' "
                f"(type={hook_type.name.lower()}, enabled={enabled}, description='{description}')"
            )
            
        except Exception as e:
            logger.error(
                f"Failed to register hook '{hook_metadata.get('name', 'unknown')}' "
                f"from plugin '{plugin_name}': {e}",
                exc_info=True
            )
            continue
    
    return registered_hooks
