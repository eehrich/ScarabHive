from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
import sys
import types
from typing import Any, Callable, Dict, Iterable, List, Set

from ..mcp.base import MCPServer
from .plugin_manifest import load_plugin_metadata

logger = logging.getLogger(__name__)

# Cache for already-registered shared modules per package
_registered_shared_modules: Dict[str, Set[str]] = {}


def _register_shared_modules(path: Path, pkg_name: str) -> None:
    """Register shared modules (non-plugin directories with __init__.py) in the package.
    
    This allows relative imports like "from ..writer_core import X" to work
    when loading plugins via importlib.
    
    Args:
        path: The plugin directory path (e.g., src/plugins_writer)
        pkg_name: The package name (e.g., "plugins_writer")
    """
    global _registered_shared_modules
    
    # Don't skip if we've processed this package before - module state may be stale
    # (e.g., from previous test runs or partial imports)
    
    if pkg_name not in _registered_shared_modules:
        _registered_shared_modules[pkg_name] = set()
    
    for subdir in path.iterdir():
        if not subdir.is_dir():
            continue
        
        # Skip if it has plugin.py (it's a plugin, not a shared module)
        if (subdir / "plugin.py").exists():
            continue
        
        # Check if it has __init__.py (it's a Python package)
        init_file = subdir / "__init__.py"
        if not init_file.exists():
            continue
        
        # This is a shared module (like writer_core)
        module_name = f"{pkg_name}.{subdir.name}"
        
        # Check if module is already properly loaded (has the key exports)
        existing_mod = sys.modules.get(module_name)
        if existing_mod is not None:
            # If module was loaded normally (e.g., by pytest imports), it should work
            # Only reload if it looks like an incomplete placeholder
            if hasattr(existing_mod, 'ensure_database') or hasattr(existing_mod, 'WriteKeyMixin'):
                _registered_shared_modules[pkg_name].add(subdir.name)
                continue  # Already properly loaded
        
        try:
            # First, register all Python files in the shared module as submodules
            # This is needed for relative imports within __init__.py to work
            for py_file in subdir.glob("*.py"):
                if py_file.name == "__init__.py":
                    continue
                submod_name = f"{module_name}.{py_file.stem}"
                if submod_name not in sys.modules:
                    submod_spec = importlib.util.spec_from_file_location(
                        submod_name, str(py_file)
                    )
                    if submod_spec and submod_spec.loader:
                        submod = importlib.util.module_from_spec(submod_spec)
                        sys.modules[submod_name] = submod
                        submod_spec.loader.exec_module(submod)
            
            # Now create and execute the main module's __init__.py
            sub_mod = types.ModuleType(module_name)
            sub_mod.__path__ = [str(subdir.resolve())]
            sub_mod.__file__ = str(init_file)
            sys.modules[module_name] = sub_mod
            
            # Execute the __init__.py to set up exports
            spec = importlib.util.spec_from_file_location(module_name, str(init_file))
            if spec and spec.loader:
                spec.loader.exec_module(sub_mod)
            
            _registered_shared_modules[pkg_name].add(subdir.name)
            logger.debug(f"Registered shared module: {module_name}")
        except Exception as e:
            logger.debug(f"Failed to register shared module {module_name}: {e}")


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
        
        # Load the plugin manifest (plugin.toml preferred, plugin.yaml fallback)
        # once — reused below for the entrypoint AND the attached _plugin_metadata.
        metadata = load_plugin_metadata(d)
        entrypoint_module = "plugin"
        entrypoint_factory = "PLUGIN_FACTORY"
        entrypoint = metadata.get("entrypoint", "plugin:PLUGIN_FACTORY")
        if isinstance(entrypoint, str) and ":" in entrypoint:
            entrypoint_module, entrypoint_factory = entrypoint.split(":", 1)
        
        plugin_file = d / f"{entrypoint_module}.py"
        if not plugin_file.exists():
            logger.debug(f"Skipping plugin {d.name}: entrypoint file {plugin_file} not found")
            continue
        
        # Use the parent directory name as the package name
        # This supports both src/plugins/ and src/plugins_writer/
        pkg_name = path.name  # e.g., "plugins" or "plugins_writer"
        plugin_pkg = f"{pkg_name}.{d.name}"
        try:
            if pkg_name not in sys.modules:
                pkg_mod = types.ModuleType(pkg_name)
                pkg_mod.__path__ = [str(path.resolve())]
                sys.modules[pkg_name] = pkg_mod
            
            # Also register shared modules (like writer_core) that plugins depend on
            # This allows relative imports like "from ..writer_core import X" to work
            _register_shared_modules(path, pkg_name)
            
            if plugin_pkg not in sys.modules:
                sub_mod = types.ModuleType(plugin_pkg)
                sub_mod.__path__ = [str(d.resolve())]
                sys.modules[plugin_pkg] = sub_mod
        except Exception as e:
            logger.debug(f"Failed to create module structure for plugin '{d.name}': {e}")

        spec = importlib.util.spec_from_file_location(f"{plugin_pkg}.{entrypoint_module}", str(plugin_file))
        if spec is None or spec.loader is None:
            logger.debug("Skipping plugin %s: cannot create spec", plugin_file)
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
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

        # Try to get factory using the configured factory name from entrypoint
        factory = getattr(mod, entrypoint_factory, None)
        
        # Fallback to PLUGIN_FACTORY if configured name not found
        if factory is None:
            factory = getattr(mod, "PLUGIN_FACTORY", None)
        
        if factory is None:
            logger.debug(f"Plugin {plugin_file} has no {entrypoint_factory} or PLUGIN_FACTORY")
            continue
        
        # Determine plugin name: prefer PLUGIN_NAME constant if present for backwards compatibility
        name = getattr(mod, "PLUGIN_NAME", None) or d.name
        
        # Attach the manifest loaded at the top of the loop (None if absent,
        # preserving the prior contract that _plugin_metadata is None w/o manifest).
        try:
            setattr(factory, '_plugin_metadata', metadata or None)
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
                                    metadata_obj = load_plugin_metadata(pkg_path) or None
                                else:
                                    # single-module distribution: check module file's parent
                                    origin = getattr(spec, "origin", None)
                                    if origin:
                                        mod_path = Path(origin).parent
                                        metadata_obj = load_plugin_metadata(mod_path) or None
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
            # Convert to Path object if string (config.plugins.plugin_dirs returns strings)
            p = Path(raw) if isinstance(raw, str) else raw

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
                            loaded = load_plugin_metadata(dp / name)
                            if loaded:
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
    
    # Verify plugin implements PluginHook interface OR has the required hook methods
    # Duck-typing: Plugin doesn't need to inherit from PluginHook if it has the hook methods
    if not isinstance(plugin_instance, PluginHook):
        # Check if plugin has hook methods (duck typing for hybrid plugins)
        hook_method_name = f"on_{hooks_list[0].get('type', '').lower()}"
        if not hasattr(plugin_instance, hook_method_name):
            raise TypeError(
                f"Plugin '{plugin_name}' declares hooks but does not implement PluginHook interface "
                f"and is missing hook method '{hook_method_name}'. "
                "The plugin class must either inherit from PluginHook or implement async hook methods."
            )
        logger.debug(f"Plugin '{plugin_name}' uses duck-typed hooks (has {hook_method_name} method)")

    
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
            category = hook_metadata.get('category', None)  # Optional category/tag
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
            
            # Skip if hook already registered (e.g., HTTP server registered
            # it via MCPIntegration AND CLI subprocess calls
            # ``register_bootstrapped_plugin_hooks`` in the same process — or
            # any other re-entry path). Without this guard the second pass
            # raises ValueError per hook and floods the log with tracebacks.
            existing_hooks = registry.list_hooks(hook_type).get(hook_type.value, [])
            if full_hook_name in existing_hooks:
                logger.debug(
                    f"Hook '{full_hook_name}' already registered for "
                    f"{hook_type.value} — skipping duplicate registration"
                )
                registered_hooks.append(full_hook_name)
                continue

            # Register with HookRegistry (it expects a PluginHook instance)
            await registry.register_hook(
                hook_type=hook_type,
                hook_name=full_hook_name,  # Use full name with plugin prefix
                hook=plugin_instance,  # Pass the PluginHook instance
                order_spec=order,
                enabled=enabled,
                timeout=timeout,
                description=description,
                category=category,  # Pass category for grouping
                # Additional metadata for tracking
                plugin=plugin_name,
                source="plugin_discovery"
            )

            registered_hooks.append(full_hook_name)  # Track full name
            logger.debug(
                f"Registered hook '{full_hook_name}' from plugin '{plugin_name}' "
                f"(type={hook_type.value}, enabled={enabled}, description='{description}')"
            )

        except Exception as e:
            logger.error(
                f"Failed to register hook '{hook_metadata.get('name', 'unknown')}' "
                f"from plugin '{plugin_name}': {e}",
                exc_info=True
            )
            continue

    return registered_hooks


_BOOTSTRAPPED_HOOKS_REGISTERED = False


async def register_bootstrapped_plugin_hooks() -> List[str]:
    """Register hooks for all plugins already loaded into the global plugin registry.

    The HTTP agent server registers plugin hooks via
    ``MCPIntegration._register_plugin_hooks``. CLI subprocesses (e.g.
    ``writer_audio produce``) only call ``bootstrap_servers`` and never get
    hooks wired up, so listeners like the message debugger silently miss
    every LLM call made from the CLI.

    Call this once after ``bootstrap_servers`` to mirror the HTTP-server
    behaviour. Subsequent calls are no-ops.

    Returns:
        List of all registered hook names (full ``plugin.hook`` form).
    """
    global _BOOTSTRAPPED_HOOKS_REGISTERED
    if _BOOTSTRAPPED_HOOKS_REGISTERED:
        return []

    from ..plugins.mcp_adapter import plugin_mcp_registry
    from ..hooks import load_hooks_config

    hooks_config = load_hooks_config()
    all_registered: List[str] = []

    for server_name in plugin_mcp_registry.list_servers():
        server = plugin_mcp_registry.get_server(server_name)
        if not server or not hasattr(server, 'plugin_schema') or not server.plugin_schema:
            continue
        plugin_schema = server.plugin_schema
        if 'hooks' not in plugin_schema:
            continue

        plugin_instance = server.plugin_server if hasattr(server, 'plugin_server') else server
        if hasattr(plugin_instance, 'hooks_plugin'):
            plugin_instance = plugin_instance.hooks_plugin

        try:
            registered = await register_plugin_hooks(
                plugin_name=server_name,
                plugin_instance=plugin_instance,
                metadata=plugin_schema,
                hooks_config=hooks_config,
            )
            all_registered.extend(registered)
        except Exception as e:
            logger.warning(
                f"register_bootstrapped_plugin_hooks: failed for '{server_name}': {e}"
            )

    _BOOTSTRAPPED_HOOKS_REGISTERED = True
    if all_registered:
        logger.info(
            "register_bootstrapped_plugin_hooks: wired %d plugin hooks", len(all_registered)
        )
    return all_registered
