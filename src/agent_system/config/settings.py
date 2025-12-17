"""
Settings loader using pydantic BaseSettings semantics.

This provides a single, typed configuration entrypoint that prefers
environment variables and falls back to YAML file values. It keeps
behavior explicit and testable.
"""
from __future__ import annotations

import logging
from typing import Optional
from pathlib import Path
import os
import yaml
import glob as glob_module

from .models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


def deep_merge(base: dict, overlay: dict) -> dict:
    """Deep merge two dictionaries. Overlay values override base values.
    
    For nested dicts, merges recursively. For lists and other types, overlay replaces base.
    
    Args:
        base: Base dictionary
        overlay: Dictionary to merge on top of base
        
    Returns:
        Merged dictionary
    """
    result = dict(base)
    
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Recursively merge nested dicts
            result[key] = deep_merge(result[key], value)
        else:
            # Replace value (including lists, primitives, etc.)
            result[key] = value
    
    return result


def load_settings(config_path: Optional[str] = None) -> AgentSystemConfig:
    """Load and return an `AgentSystemConfig` using environment variables and
    optional YAML config file. Environment variables take precedence for
    any ${VAR} placeholders inside the YAML but do not override explicit
    keys unless the YAML uses that placeholder.

    Args:
        config_path: optional path to YAML config; if omitted, uses
                     the `config/config.yaml` file if it exists or the
                     path from the AGENT_CONFIG_PATH environment variable.
    """
    # Clear caches when reloading settings
    global _plugins_cache, _inheritance_cache
    _plugins_cache = None
    _inheritance_cache.clear()
    
    # Allow overriding default config file via env var
    env_cfg = os.environ.get("AGENT_CONFIG_PATH")
    cfg_path = Path(config_path or env_cfg or "config/config.yaml")
    data: dict = {}

    # If the master config exists, load it and then load any included files
    if cfg_path.exists():
        master = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        # Determine includes: accept either `includes` (list) or `files`
        includes = master.get("includes") or master.get("files") or []
        # If includes is a single string, make it a list
        if isinstance(includes, str):
            includes = [includes]

        # Start with the master config as base
        data = dict(master)

        # Expand wildcards in includes list
        expanded_includes = []
        for inc in includes:
            # Check if pattern contains wildcards
            if '*' in inc or '?' in inc or '[' in inc:
                # Resolve relative to config file directory
                if not Path(inc).is_absolute():
                    pattern = str(cfg_path.parent / inc)
                else:
                    pattern = inc
                
                # Expand glob pattern
                matched_files = sorted(glob_module.glob(pattern))
                if matched_files:
                    # Make paths relative to config dir for consistency
                    for matched in matched_files:
                        rel_path = Path(matched).relative_to(cfg_path.parent)
                        expanded_includes.append(str(rel_path))
                else:
                    # No matches - keep original pattern for error reporting
                    expanded_includes.append(inc)
            else:
                # No wildcards - keep as is
                expanded_includes.append(inc)
        
        includes = expanded_includes

        # Load each included file and merge into specific sections
        for inc in includes:
            inc_path = Path(inc)
            if not inc_path.is_absolute():
                inc_path = cfg_path.parent.joinpath(inc_path)
            if inc_path.exists():
                try:
                    part = yaml.safe_load(inc_path.read_text(encoding="utf-8")) or {}
                    
                    # Merge based on included file structure
                    if "llm_system" in part:
                        data["llm_system"] = part["llm_system"]
                    elif inc.endswith("llm.yaml"):
                        # If llm.yaml contains the configuration directly
                        data["llm_system"] = part
                    
                    # New split structure (Epic 0044)
                    # Deep merge plugins to allow multiple files to contribute agents/servers
                    if "plugins" in part:
                        if "plugins" in data:
                            data["plugins"] = deep_merge(data["plugins"], part["plugins"])
                        else:
                            data["plugins"] = part["plugins"]
                    
                    if "external_servers" in part:
                        # mcp_servers.yaml uses "external_servers" key
                        data["external_servers"] = part["external_servers"]
                    
                    if "server_mode" in part:
                        # mcp_server_mode.yaml uses "server_mode" key
                        data["server_mode"] = part["server_mode"]
                    
                    if "agents" in part:
                        data["agents"] = part["agents"]
                        
                except Exception as e:
                    # Log parsing errors but continue loading
                    print(f"Warning: Failed to parse {inc_path}: {e}")
                    pass

    # Apply simple env-variable expansion for ${VAR} patterns (keep existing loader behavior)
    def _expand_env(value):
        if isinstance(value, str):
            import re
            pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
            def repl(m):
                return os.environ.get(m.group(1), "")
            return pattern.sub(repl, value)
        if isinstance(value, dict):
            return {k: _expand_env(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_expand_env(v) for v in value]
        return value

    data = _expand_env(data)

    # Resolve plugin_dirs to absolute paths
    # Paths are resolved relative to the configuration file directory first,
    # then relative to repository root if the config-relative path doesn't exist.
    # This allows entries like `src/plugins` in included files to refer to the
    # repository `src` tree while keeping resolution predictable.
    if cfg_path.exists():
        base_dir = cfg_path.parent
        # repository root is assumed to be parent of the config dir
        repo_root = cfg_path.parent.parent if cfg_path.parent.parent.exists() else base_dir
        try:
            # New structure: plugins.plugin_dirs
            plugins_block = data.get("plugins") if isinstance(data, dict) else None
            if isinstance(plugins_block, dict):
                pdirs = plugins_block.get("plugin_dirs")
                if isinstance(pdirs, list):
                    resolved = []
                    for p in pdirs:
                        if isinstance(p, str) and p:
                            ppath = Path(p)
                            if not ppath.is_absolute():
                                # Try config-folder-relative first
                                try:
                                    candidate = (base_dir.joinpath(ppath)).resolve()
                                except Exception:
                                    candidate = base_dir.joinpath(ppath)
                                # If that candidate doesn't exist but an equivalent
                                # path exists relative to the repo root, prefer the
                                # repo-root-relative path (handles `src/...`).
                                if not candidate.exists():
                                    try:
                                        repo_candidate = (repo_root.joinpath(ppath)).resolve()
                                    except Exception:
                                        repo_candidate = repo_root.joinpath(ppath)
                                    if repo_candidate.exists():
                                        p = str(repo_candidate)
                                    else:
                                        p = str(candidate)
                                else:
                                    p = str(candidate)
                        resolved.append(p)
                    data["plugins"]["plugin_dirs"] = resolved
        except Exception:
            # Conservative: if resolution fails for any reason, keep original values
            pass

    return AgentSystemConfig.model_validate(data)


# Cache for plugin discovery (avoid repeated calls)
_plugins_cache: dict[str, type] | None = None
# Cache for resolved server inheritance
_inheritance_cache: dict[str, tuple[str, dict]] = {}


def _get_plugins_cached() -> dict[str, type]:
    """Get plugins with caching to avoid repeated discovery."""
    global _plugins_cache
    if _plugins_cache is None:
        from ..plugins import discover_all_plugins
        _plugins_cache = discover_all_plugins()
    return _plugins_cache


def _resolve_server_inheritance(
    server_name: str,
    config: "AgentSystemConfig",
    visited: set[str] | None = None
) -> tuple[str, dict]:
    """Resolve server config inheritance chain with caching.
    
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
    
    # Check cache for top-level calls only (not during recursion)
    if visited is None and server_name in _inheritance_cache:
        return _inheritance_cache[server_name]
    
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
        plugins = _get_plugins_cached()
        if typ in plugins:
            # Type is a real plugin, return as-is
            result = (typ, server_dict)
            _inheritance_cache[server_name] = result
            return result
        else:
            # Self-reference but not a plugin - return as-is (will fail later in bootstrap)
            return (typ, server_dict)
    
    # Check if type is a known plugin (use cached plugins)
    plugins = _get_plugins_cached()
    
    if typ in plugins:
        # Type is a real plugin, no further inheritance needed
        result = (typ, server_dict)
        _inheritance_cache[server_name] = result
        return result
    
    # Type might be another server - check if it exists
    parent_server = config.plugins.servers.get(typ)
    if parent_server is None:
        # Not a server either, return as-is (will fail later in bootstrap)
        return (typ, server_dict)
    
    # Recursively resolve parent
    parent_type, parent_dict = _resolve_server_inheritance(typ, config, visited)
    
    # Merge: parent config first, then child overrides
    merged = _deep_merge_dict(parent_dict, server_dict)
    # The final type comes from the resolved parent chain
    merged["type"] = parent_type
    
    result = (parent_type, merged)
    
    # Cache result for this server_name (at any recursion level, since visited contains it)
    # This way sub_agent_character_designer gets cached even though it goes through writer_agent
    _inheritance_cache[server_name] = result
    
    return result


def get_mcp_config_by_name(server_name: str, config: Optional[AgentSystemConfig] = None) -> Optional[MCPConfig]:
    """Get an MCPConfig by name with inheritance from default_config and parent servers.
    
    This function creates a final MCPConfig by:
    1. Starting with the default_config (from plugins or legacy mcp_system)
    2. Resolving inheritance if type refers to another server (e.g., type: writer_agent)
    3. Overlaying/merging the specific server configuration
    
    Server inheritance example:
        writer_agent:
          type: basic_agent
          agent_config:
            hooks: {enabled: true}
            
        character_designer:
          type: writer_agent  # Inherits from writer_agent, resolves to basic_agent
          agent_config:
            max_steps: 50     # Override specific values
    
    Args:
        server_name: Name of the server configuration to retrieve
        config: Optional AgentSystemConfig instance. If None, loads from default path.
        
    Returns:
        MCPConfig instance with inherited values, or None if server not found
    """
    from .models import MCPConfig
    
    if config is None:
        config = load_settings()
    
    # Use new structure (plugins)
    if not config.plugins:
        return None
    
    server_config = config.plugins.servers.get(server_name)
    if server_config is None:
        return None
    
    # Use exclude_unset for default_config too - but this one we want WITH defaults
    # because it's the base layer. So use regular model_dump() here.
    default_config_dict = config.plugins.default_config.model_dump()
    
    # Resolve inheritance chain (type: writer_agent -> type: basic_agent)
    try:
        final_type, resolved_config_dict = _resolve_server_inheritance(server_name, config)
        logger.debug(
            "Resolved server '%s': type '%s' -> '%s'",
            server_name, server_config.type, final_type
        )
    except ValueError as e:
        logger.error("Failed to resolve inheritance for '%s': %s", server_name, e)
        # Fall back to direct config without inheritance
        resolved_config_dict = server_config.model_dump()
    
    # Start with default config, then merge resolved (inherited) config
    merged_config = default_config_dict.copy()
    merged_config = _deep_merge_dict(merged_config, resolved_config_dict)
    
    # Create and return final MCPConfig instance
    return MCPConfig.model_validate(merged_config)


def _deep_merge_dict(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries, with override values taking precedence.
    
    Args:
        base: Base dictionary (default values)
        override: Override dictionary (specific values that override base)
        
    Returns:
        New dictionary with merged values
    """
    result = base.copy()
    
    for key, value in override.items():
        if (key in result and 
            isinstance(result[key], dict) and 
            isinstance(value, dict)):
            # Recursively merge nested dictionaries
            result[key] = _deep_merge_dict(result[key], value)
        elif value is not None:
            # Override with non-None values
            result[key] = value
        # Skip None values to preserve defaults
    
    return result
