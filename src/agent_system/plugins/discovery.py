from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
import sys
import types
from typing import Any, Callable, Dict, Iterable, Set

from ..tools.base import ToolServer
from .plugin_manifest import load_plugin_metadata, parse_entrypoint

logger = logging.getLogger(__name__)

# Cache for already-registered shared modules per package
_registered_shared_modules: Dict[str, Set[str]] = {}


def _record(failures: dict[str, str] | None, name: str, error: BaseException) -> None:
    """Note in *failures* (plugin name -> why, one line) that a plugin did not load; logged already."""
    if failures is None:
        return
    text = str(error).strip().splitlines()[0] if str(error).strip() else ""
    failures[name] = f"{type(error).__name__}: {text}" if text else type(error).__name__


def _held_elsewhere(taken: dict[str, str] | None, name: str, root: str) -> bool:
    """Whether another root of the same package name claimed ``name`` earlier in
    this discovery (see _ModuleNames); warned about when so."""
    holder = taken.get(name, root) if taken is not None else root
    if holder != root:
        logger.warning(
            "'%s' in %s is not loaded: %s, an earlier plugin directory of the same name, "
            "has one, and both would be the same module", name, root, holder)
        return True
    return False


def _hold(taken: dict[str, str] | None, name: str, root: str) -> None:
    if taken is not None:
        taken.setdefault(name, root)


def _register_shared_modules(path: Path, pkg_name: str, taken: dict[str, str] | None = None) -> None:
    """Register shared modules (non-plugin directories with __init__.py) in the package.
    
    This allows relative imports like "from ..writer_core import X" to work
    when loading plugins via importlib.
    
    Args:
        path: The plugin directory path (e.g., src/plugins_extra)
        pkg_name: The package name (e.g., "plugins_extra")
    """
    global _registered_shared_modules
    
    # Don't skip if we've processed this package before - module state may be stale
    # (e.g., from previous test runs or partial imports)
    
    if pkg_name not in _registered_shared_modules:
        _registered_shared_modules[pkg_name] = set()
    root = str(path.resolve())

    for subdir in path.iterdir():
        if not subdir.is_dir():
            continue
        
        # Skip if it is a plugin, not a shared module. Recognized the same
        # way the loader does: plugin.py OR a plugin.toml. Plugins with
        # entrypoint = "server:..." (audio_ops,
        # comfyui, ...) have no plugin.py and were misclassified as shared
        # modules -- every one of their *.py files got imported eagerly,
        # regardless of whether the plugin was even enabled.
        if (subdir / "plugin.py").exists() or load_plugin_metadata(subdir):
            continue
        
        # Check if it has __init__.py (it's a Python package)
        init_file = subdir / "__init__.py"
        if not init_file.exists():
            continue
        if _held_elsewhere(taken, subdir.name, root):
            continue
        _hold(taken, subdir.name, root)
        
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
        
        sub_mod = None
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
                        try:
                            submod_spec.loader.exec_module(submod)
                        except BaseException:
                            # Same torso rule as in the plugin loops below: the
                            # `not in sys.modules` check above would skip a
                            # half-executed submodule for the rest of the
                            # process, so it must not stay behind.
                            sys.modules.pop(submod_name, None)
                            raise
            
            # Now create and execute the main module's __init__.py
            sub_mod = types.ModuleType(module_name)
            sub_mod.__path__ = [str(subdir.resolve())]
            sub_mod.__file__ = str(init_file)
            previous = sys.modules.get(module_name)
            sys.modules[module_name] = sub_mod
            
            # Execute the __init__.py to set up exports
            spec = importlib.util.spec_from_file_location(module_name, str(init_file))
            if spec and spec.loader:
                spec.loader.exec_module(sub_mod)
            
            _registered_shared_modules[pkg_name].add(subdir.name)
            logger.debug(f"Registered shared module: {module_name}")
        except Exception as e:
            # Take back only OUR half-executed module: a failure in the
            # submodule loop above happens before it exists, and dropping a
            # module somebody else imported properly would force a re-import
            # and hand out a second identity of every class in it. What it
            # replaced comes back -- another directory's plugin package, whose
            # modules would be left without their parent.
            if sub_mod is not None and sys.modules.get(module_name) is sub_mod:
                _put_back(module_name, previous)
            # A shared module that does not load takes every plugin importing
            # it with it -- that is a warning, not a debug line.
            logger.warning(f"Failed to register shared module {module_name}: {e}", exc_info=True)


def _put_back(module_name: str, previous: Any) -> None:
    """After a plugin module failed under ``module_name``: what was there before
    it -- another file's module, from an earlier discovery of another directory
    -- or nothing. Dropped, that module was executed afresh by the next discovery
    of its directory: a second identity of every class in it."""
    if previous is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = previous


def _already_loaded(module_name: str, plugin_file: Path):
    """The module for ``plugin_file`` if THIS file was executed under
    ``module_name`` before, else None.

    Discovery runs more than once per process (servers/bootstrap.py and
    config/settings._get_plugins_cached, again on every reload) and used to
    exec every plugin.py each time. Measured 2026-09-04: of 50 shared types
    only 20 factories were identical, and a class defined in a plugin.py
    (SubAgentManagerHybridPlugin) existed twice -- ``isinstance`` against it
    was wrong between the two runs. Keyed by file, not by name: tests build
    throwaway plugins under the same package names in different directories
    and must get a fresh module for a different file.
    """
    existing = sys.modules.get(module_name)
    if existing is None:
        return None
    try:
        if Path(existing.__file__).resolve() == plugin_file.resolve():
            return existing
    except (AttributeError, TypeError, OSError):
        pass
    return None


def discover_plugins(path: Path, taken: dict[str, str] | None = None,
                     failures: dict[str, str] | None = None) -> Dict[str, Callable[..., ToolServer]]:
    """Discover plugins in a directory.

    Rules:
    - Folder plugin: <dir>/<plugin>/plugin.py must export PLUGIN_FACTORY or register().
      Name = folder name.
    - Single file plugin: <dir>/<name>.py with PLUGIN_FACTORY or register(). Name = file stem.
    - If a PLUGIN_NAME constant is present it overrides the folder/file name (backwards compatibility).

    ``taken``: module name -> the root that claimed it, over the roots of one
    package name (see _ModuleNames). A name another root claimed is skipped
    here; the names tried here are added, loaded or not.
    """
    out: Dict[str, Callable[..., ToolServer]] = {}
    if not (path and path.exists() and path.is_dir()):
        return out

    # Package module + shared modules ONCE per path -- doing this inside the
    # per-plugin loop re-scanned (and re-executed) every shared module for
    # every plugin directory.
    pkg_name = path.name  # e.g., "plugins" or "plugins_extra"
    root = str(path.resolve())
    try:
        if pkg_name not in sys.modules:
            pkg_mod = types.ModuleType(pkg_name)
            pkg_mod.__path__ = [str(path.resolve())]
            sys.modules[pkg_name] = pkg_mod
        # Register shared modules (like writer_core) that plugins depend on.
        # This allows relative imports like "from ..writer_core import X".
        _register_shared_modules(path, pkg_name, taken)
    except Exception as e:
        logger.debug(f"Failed to prepare package structure for '{pkg_name}': {e}")

    for d in path.iterdir():
        if not d.is_dir():
            continue
        
        # Load the plugin manifest (plugin.toml)
        # once — reused below for the entrypoint AND the attached _plugin_metadata.
        metadata = load_plugin_metadata(d)
        entrypoint_module, entrypoint_factory = parse_entrypoint(metadata)
        
        plugin_file = d / f"{entrypoint_module}.py"
        if not plugin_file.exists():
            logger.debug(f"Skipping plugin {d.name}: entrypoint file {plugin_file} not found")
            continue
        
        # Asked only of a folder with an entrypoint: a shared module was asked
        # (and warned about) in _register_shared_modules already.
        if _held_elsewhere(taken, d.name, root):
            continue
        plugin_pkg = f"{pkg_name}.{d.name}"
        _hold(taken, d.name, root)
        try:
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
        mod = _already_loaded(spec.name, plugin_file)
        if mod is None:
            mod = importlib.util.module_from_spec(spec)
            previous = sys.modules.get(spec.name)
            sys.modules[spec.name] = mod
            try:
                spec.loader.exec_module(mod)
            except BaseException as e:
                # Drop the half-executed torso, exactly as importlib does on a
                # failed import: _already_loaded would otherwise hand it to the
                # next discovery as "loaded", the plugin would stay missing and
                # the warning would never be logged again. BaseException on
                # purpose -- a module-level sys.exit() raises SystemExit, and
                # in a pytest process the run continues afterwards.
                _put_back(spec.name, previous)
                if not isinstance(e, Exception):
                    raise
                logger.warning(f"Failed to load plugin module {plugin_file}: {e}", exc_info=True)
                _record(failures, d.name, e)
                continue

        try:
            if hasattr(mod, "register") and inspect.isfunction(mod.register):
                name, factory = mod.register()
                out[name] = factory
                continue
        except Exception as e:
            logger.warning(f"Plugin {plugin_file} register() failed: {e}", exc_info=True)
            _record(failures, d.name, e)
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
        if p.name in ("__init__.py", "conftest.py"):
            continue
        if _held_elsewhere(taken, p.stem, root):
            continue
        _hold(taken, p.stem, root)
        # The directory this was found in, as the directory loop above names it:
        # under the fixed name "plugins" a second discovery root would either
        # take that package over or inherit its path, and every relative import
        # of one root's plugins would then read the other root's files.
        pkg_name = path.name
        try:
            if pkg_name not in sys.modules:
                pkg_mod = types.ModuleType(pkg_name)
                pkg_mod.__path__ = [str(path.resolve())]
                sys.modules[pkg_name] = pkg_mod
        except Exception as e:
            logger.debug(f"Failed to create module structure for '{pkg_name}': {e}")
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{p.stem}", str(p))
        if spec is None or spec.loader is None:
            continue
        mod = _already_loaded(spec.name, p)
        if mod is None:
            mod = importlib.util.module_from_spec(spec)
            # Claim the name only when it is free or already a module: a
            # directory plugin of the same name owns "plugins.<name>" as a
            # synthetic package, and overwriting that would break its relative
            # imports for the rest of the process. Without the entry the
            # module still runs -- it is only not reusable next time.
            previous = sys.modules.get(spec.name)
            claims_name = not hasattr(previous, "__path__")
            if claims_name:
                sys.modules[spec.name] = mod
            try:
                spec.loader.exec_module(mod)
            except BaseException as e:
                if claims_name:
                    _put_back(spec.name, previous)  # see the dir-plugin loop above
                if not isinstance(e, Exception):
                    raise
                logger.warning(f"Failed to load single-file plugin module {p}: {e}", exc_info=True)
                _record(failures, p.stem, e)
                continue
        try:
            if hasattr(mod, "register") and inspect.isfunction(mod.register):
                name, factory = mod.register()
                out[name] = factory
                continue
        except Exception as e:
            logger.warning(f"Single-file plugin {p} register() failed: {e}", exc_info=True)
            _record(failures, p.stem, e)
            continue
        factory = getattr(mod, "PLUGIN_FACTORY", None)
        if factory:
            name = getattr(mod, "PLUGIN_NAME", None) or p.stem
            out[name] = factory
    return out


#: Entry point group a packaged plugin declares to be discovered.
ENTRYPOINT_GROUP = "agent_system.tool_plugins"
#: What it was called before the rename; still read, see discover_entrypoint_plugins.
LEGACY_ENTRYPOINT_GROUP = "agent_system.mcp_plugins"


def discover_entrypoint_plugins(group: str = ENTRYPOINT_GROUP,
                                failures: dict[str, str] | None = None) -> Dict[str, Callable[..., ToolServer]]:
    """Discover plugins exposed via Python entry points.

    Entry point group defaults to 'agent_system.tool_plugins'. Each
    entry point should return a callable factory that accepts
    (name, system_config, server_config) -- plus ``registry=`` when the factory
    carries ``_accepts_registry`` -- and returns a ToolServer
    (see ``runtime._construct``).
    """
    out: Dict[str, Callable[..., ToolServer]] = {}
    try:
        # importlib.metadata.entry_points API varies between Python versions
        from importlib import metadata
        eps = metadata.entry_points()
        # try to select by group if available
        # The group was called agent_system.mcp_plugins until 17.09.2026. A
        # plugin installed from outside this repo still declares that name, and
        # an entry point nobody looks for disappears without a word.
        groups = (group, LEGACY_ENTRYPOINT_GROUP) if group == ENTRYPOINT_GROUP else (group,)
        try:
            selected = [ep for g in groups for ep in eps.select(group=g)]
        except Exception as e:
            # older API returns a list-like; filter manually
            logger.debug(f"Failed to use entry_points.select(), falling back to manual filtering: {e}")
            selected = [ep for ep in eps if getattr(ep, "group", None) in groups]

        for ep in selected:
            try:
                factory = ep.load()
                name = getattr(ep, "name", None) or getattr(factory, "__name__", None)
                # Try to attach plugin metadata from the package where the
                # entry-point factory is defined. This allows packaged
                # plugins to include a `plugin.toml` alongside their code.
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
                _record(failures, getattr(ep, "name", None) or str(ep), e)
                continue
    except Exception as e:
        # importlib.metadata not available; silently return empty
        logger.debug(f"importlib.metadata not available for entrypoint discovery: {e}")
        return out

    return out


def default_plugin_dirs() -> list[Path]:
    """Where filesystem discovery looks when no dirs are given.

    The importable ``plugins`` package (editable installs put it under src/),
    else the synthetic ``plugins`` module a previous discovery left in
    sys.modules. NOTE this is ONE directory -- ``config.plugins.plugin_dirs``
    lists three, and callers that want all of them pass them explicitly.
    """
    source_dirs: list[Path] = []
    try:
        spec = importlib.util.find_spec("plugins")
        if spec is not None:
            for location in getattr(spec, "submodule_search_locations", None) or []:
                source_dirs.append(Path(location))
    except Exception:
        # ignore and continue — we'll still discover entrypoint plugins
        pass
    if not source_dirs:
        try:
            mod = sys.modules.get("plugins")
            if mod is not None and getattr(mod, "__path__", None):
                source_dirs.extend(Path(p) for p in mod.__path__)
        except Exception as e:
            logger.debug(f"Failed to reuse existing 'plugins' module paths: {e}")
    return source_dirs


class _ModuleNames:
    """discover_plugins over several roots, one after the other, without two of
    them loading the same module name.

    A root's package name is its directory's name, so two roots both called
    ``plugins`` with a plugin (folder or single file) or a shared module of the
    same name would both be the module ``plugins.<name>``: the second replaced
    the first in sys.modules (its relative imports running against the first
    root's files), a plugin's type was dropped as provided twice anyway
    (_add_plugins), and every later discovery executed the first root's plugin
    afresh -- a new factory each time. The first root that has the name claims
    it, whether its own module loads or not; the later one's is not loaded.
    Freeing the name when the first root's module fails would hand it to the
    later root in one discovery and back to the first in the next, which would
    then run against -- and drop -- what the later root left in sys.modules. By
    resolved root: the same directory listed twice loads nothing twice and
    warns about nothing.

    Only roots discovered together are kept apart. Two directories of the same
    name discovered one after the other (tests do) still share the module
    names -- a plugin package, its submodules -- as one package name must.
    """

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self._taken: dict[str, dict[str, str]] = {}
        self._failures = failures

    def discover(self, path: Path) -> dict[str, Callable[..., ToolServer]]:
        return discover_plugins(path, taken=self._taken.setdefault(path.name, {}), failures=self._failures)


def _add_plugins(plugins: Dict[str, Callable[..., ToolServer]],
                 found: Dict[str, Callable[..., ToolServer]], source: str) -> None:
    """Add *found* to *plugins*; a type already there keeps its first source.

    The plugin type is a plugin's id: ``type:`` in plugins.yaml names it, and
    a prompt asks for it in ``plugins``. Two sources with the same type used to
    resolve silently to whichever was discovered last.
    """
    for name, factory in found.items():
        existing = plugins.get(name)
        if existing is None:
            plugins[name] = factory
        elif existing is not factory:
            logger.warning(
                "Plugin type '%s' is provided twice; keeping the first, ignoring the one from %s",
                name, source)


def discover_all_plugins(dirs: Iterable[Path] | None = None, group: str = ENTRYPOINT_GROUP,
                         failures: dict[str, str] | None = None) -> Dict[str, Callable[..., ToolServer]]:
    """Discover plugins from filesystem directories and entry points.

    dirs: iterable of Path objects to search for filesystem plugins.
    group: entry point group to query for packaged plugins.
    failures: filled with the plugins of this discovery whose module did not load (name -> why, one
    line) -- not the ones another root or an entry point provided after all. The runtime keeps them
    for the System panel and for the "unknown type" of a server whose plugin they are.
    """
    plugins: Dict[str, Callable[..., ToolServer]] = {}
    collected: dict[str, str] = {}

    # Use only explicitly provided `dirs` for filesystem discovery when
    # available. If none are provided, attempt to locate an importable
    # `plugins` package (editable installs place `plugins/` under `src/`),
    # and use its filesystem paths for discovery. This avoids hardcoding
    # repository paths while ensuring discoverability in common dev
    # and editable-install setups.
    source_dirs = list(dirs) if dirs else default_plugin_dirs()
    module_names = _ModuleNames(collected)
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
            _add_plugins(plugins, module_names.discover(p), str(p))
        except Exception as e:
            logger.warning(f"Error discovering plugins in {raw}: {e}", exc_info=True)

    # entry point plugins
    try:
        eps = discover_entrypoint_plugins(group=group, failures=collected)
        _add_plugins(plugins, eps, f"entry point group '{group}'")
    except Exception as e:
        logger.warning(f"Error discovering entrypoint plugins from group '{group}': {e}", exc_info=True)

    # Ensure metadata: for factories missing `_plugin_metadata`, try to load
    # a `plugin.toml` from the configured filesystem plugin directories (if
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

    if failures is not None:
        failures.update({name: why for name, why in collected.items() if name not in plugins})
    return plugins


# Hook registration moved to agent_system.hooks.plugin_registration; the names
# stay importable from here for the plugins and tests that use them.
from ..hooks.plugin_registration import (  # noqa: E402,F401
    register_bootstrapped_plugin_hooks,
    register_plugin_hooks,
    warn_unknown_hook_overrides,
)
