"""Inheritance between entries of the merged configuration: a model entry that ``extends`` another
(llm_system.models, resolved by load_settings before validation) and a server entry whose ``type``
names another server instead of a plugin (plugins.servers, resolved when get_tool_server_config
builds one). Both merge with merging._deep_merge_dict: field-wise deep, lists via +item/!pattern.
"""
from __future__ import annotations

import copy

from .merging import _deep_merge_dict
from .models import AgentSystemConfig


def _resolve_model_inheritance(data: dict) -> None:
    """Resolve ``extends`` on llm_system.models, in place.

    A model entry inherits from another model entry — variants (``-unlimited``,
    ``-nostream``) from theirs, family members from the one that carries the
    shared knobs. Same idea as the agent ``type:`` chains, and the same merge:
    field-wise deep, lists via +item/!pattern.

    Runs before validation: LLMModelConfig has no ``extends`` field, and with
    extra="forbid" an unresolved one is a loud error instead of a silent
    fallback to the defaults.
    """
    llm_system = data.get("llm_system")
    if not isinstance(llm_system, dict):
        return
    models = llm_system.get("models")
    if not isinstance(models, dict) or not models:
        return

    sources = {k: v for k, v in models.items() if isinstance(v, dict)}
    resolved: dict = {}

    def resolve(name: str, chain: tuple) -> dict:
        if name in resolved:
            return resolved[name]
        if name in chain:
            raise ValueError(
                f"llm_system.models: extends cycle {' -> '.join(chain + (name,))}")
        src = sources.get(name)
        if src is None:
            raise ValueError(
                f"llm_system.models: '{chain[-1] if chain else name}' extends "
                f"'{name}', which is not a model entry")
        own = {k: v for k, v in src.items() if k != "extends"}
        parent = src.get("extends")
        out = (_deep_merge_dict(copy.deepcopy(resolve(parent, chain + (name,))),
                                own, name, explicit_none=True)
               if parent else copy.deepcopy(own))
        resolved[name] = out
        return out

    for name in list(sources):
        models[name] = copy.deepcopy(resolve(name, ()))


# Cache for plugin discovery (avoid repeated calls)
_plugins_cache: frozenset[str] | None = None
# No cache for the resolved inheritance: it was keyed by server name alone, so a second config object (a fresh load
# from disk next to the live one) got the first one's result. Resolving all 215 servers takes 8 ms without it
# (measured 2026-09-16), the same as with it.


def _forget_known_plugin_types() -> None:
    """Read the manifests again at the next question: load_settings starts every load with this."""
    global _plugins_cache
    _plugins_cache = None


def _known_plugin_types() -> frozenset[str]:
    """The plugin type names, for the membership tests below.

    Read from the manifests, not by discovering: this used to import every
    plugin module in the process just to answer "is this type a plugin?"
    (measured 2026-09-04 in a fresh process: 0.85 s, 187 plugin modules in
    sys.modules). The catalog looks in the same directory discovery would use
    without arguments -- ONE directory, the importable ``plugins`` package,
    not the three in config.plugins.plugin_dirs. Widening that here is not a
    free cleanup: `repair_pipeline` (type: writer_issues) inherits its
    ``write_key`` from the sibling server because ``writer_issues`` is NOT in
    this set, and would lose it (measured over all 219 servers: that one).
    """
    global _plugins_cache
    if _plugins_cache is None:
        from ..plugins.catalog import PluginCatalog
        from ..plugins.discovery import default_plugin_dirs
        _plugins_cache = PluginCatalog(default_plugin_dirs()).types()
    return _plugins_cache


def _resolve_server_inheritance(
    server_name: str,
    config: "AgentSystemConfig",
    visited: set[str] | None = None
) -> tuple[str, dict]:
    """Resolve server config inheritance chain.
    
    If a server's type refers to another server (not a plugin), this function
    resolves the inheritance chain and merges configurations.
    
    Args:
        server_name: Name of the server to resolve
        config: AgentSystemConfig instance
        visited: Set of already visited servers (for cycle detection)
        
    Returns:
        Tuple of (final_plugin_type, merged_config_dict)
        
    Raises:
        ValueError: If circular inheritance detected
    """
    
    if visited is None:
        visited = set()
    
    if server_name in visited:
        raise ValueError(f"Circular inheritance detected: {' -> '.join(visited)} -> {server_name}")
    
    visited.add(server_name)
    
    # Get server config
    server_config = config.plugins.servers.get(server_name)
    if server_config is None:
        return (server_name, {})  # Not found, return as-is
    
    # Use exclude_unset=True to only export explicitly set values, not Pydantic defaults
    # This prevents defaults from overriding parent values during inheritance
    server_dict = server_config.model_dump(exclude_unset=True)
    typ = server_dict.get("type", "basic_agent")
    
    # Check if server references itself (e.g., writer_content with type: writer_content)
    # This is not real inheritance, treat it as if type is a plugin
    if typ == server_name:
        # Check if it's a known plugin
        plugins = _known_plugin_types()
        if typ in plugins:
            # Type is a real plugin, return as-is
            return (typ, server_dict)
        else:
            # Self-reference but not a plugin - return as-is (will fail later in bootstrap)
            return (typ, server_dict)
    
    # Check if type is a known plugin (use cached plugins)
    plugins = _known_plugin_types()
    
    if typ in plugins:
        # Type is a real plugin, no further inheritance needed
        return (typ, server_dict)
    
    # Type might be another server - check if it exists
    parent_server = config.plugins.servers.get(typ)
    if parent_server is None:
        # Not a server either, return as-is (will fail later in bootstrap)
        return (typ, server_dict)
    
    # Recursively resolve parent
    parent_type, parent_dict = _resolve_server_inheritance(typ, config, visited)
    
    # Merge: parent config first, then child overrides
    merged = _deep_merge_dict(parent_dict, server_dict, server_name)
    # The final type comes from the resolved parent chain
    merged["type"] = parent_type
    
    return (parent_type, merged)
