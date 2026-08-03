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


def _resolve_relative_paths(data: dict, base_dir: Path) -> dict:
    """Resolve relative paths (starting with ./) in agent config values.

    Walks the config tree and resolves ``system_template`` values that
    start with ``./`` relative to *base_dir* (the directory of the YAML
    file that declared them).  This allows agent configs co-located with
    plugins to use ``system_template: ./prompts/foo.md`` instead of
    hard-coding a project-relative path.
    """
    PATH_KEYS = {"system_template"}

    def _walk(obj: object) -> object:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in PATH_KEYS and isinstance(value, str) and (value.startswith("./") or value.startswith("../")):
                    resolved = str((base_dir / value).resolve())
                    obj[key] = resolved
                else:
                    _walk(value)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
        return obj

    return _walk(data)  # type: ignore[return-value]


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
        try:
            master = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            logger.error(f"YAML syntax error in config file '{cfg_path}': {e}")
            raise ValueError(f"Failed to parse configuration file '{cfg_path}': {e}") from e
        except Exception as e:
            logger.error(f"Failed to read config file '{cfg_path}': {e}")
            raise
        
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
        logger.debug(f"Config includes {len(includes)} files from glob patterns")

        # Load each included file and merge into specific sections
        for inc in includes:
            inc_path = Path(inc)
            if not inc_path.is_absolute():
                inc_path = cfg_path.parent.joinpath(inc_path)
            if inc_path.exists():
                try:
                    part = yaml.safe_load(inc_path.read_text(encoding="utf-8")) or {}
                    logger.debug(f"Loaded included config: {inc_path.name}")
                    
                    # Resolve relative paths (./prompts/...) relative to include file dir
                    _resolve_relative_paths(part, inc_path.parent)
                    
                    # Merge based on included file structure
                    # Deep merge llm_system to allow multiple files to contribute models/profiles
                    if "llm_system" in part:
                        if "llm_system" in data:
                            data["llm_system"] = deep_merge(data["llm_system"], part["llm_system"])
                        else:
                            data["llm_system"] = part["llm_system"]
                        # Log models being added
                        if "models" in part.get("llm_system", {}):
                            model_names = list(part["llm_system"]["models"].keys())
                            logger.debug(f"Added LLM models from {inc_path.name}: {model_names}")
                    elif inc.endswith("llm.yaml"):
                        # If llm.yaml contains the configuration directly (legacy format)
                        data["llm_system"] = part
                    
                    # New split structure (Epic 0044)
                    # Deep merge plugins to allow multiple files to contribute agents/servers
                    if "plugins" in part:
                        if "plugins" in data:
                            data["plugins"] = deep_merge(data["plugins"], part["plugins"])
                        else:
                            data["plugins"] = part["plugins"]
                        # Log servers being added
                        if "servers" in part.get("plugins", {}):
                            server_names = list(part["plugins"]["servers"].keys())
                            logger.debug(f"Added servers from {inc_path.name}: {server_names}")
                    
                    if "external_servers" in part:
                        # mcp_servers.yaml uses "external_servers" key
                        data["external_servers"] = part["external_servers"]
                    
                    if "server_mode" in part:
                        # mcp_server_mode.yaml uses "server_mode" key
                        data["server_mode"] = part["server_mode"]
                    
                    if "agents" in part:
                        data["agents"] = part["agents"]
                        
                except yaml.YAMLError as e:
                    # Log YAML syntax errors and continue (allows other configs to load)
                    logger.error(f"YAML syntax error in included config '{inc_path}': {e}")
                    logger.warning(f"Skipping malformed config file: {inc_path}")
                except Exception as e:
                    # Log other parsing errors but continue loading
                    logger.error(f"Failed to load included config '{inc_path}': {e}", exc_info=True)
                    logger.warning(f"Skipping problematic config file: {inc_path}")

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

        def _resolve_dir_list(block_key: str, list_key: str) -> None:
            """Resolve <block_key>.<list_key> entries to absolute paths in place.

            Shared by plugin_dirs and skill_dirs so both behave identically —
            the alternative was duplicating this resolution per discovery root.
            """
            block = data.get(block_key) if isinstance(data, dict) else None
            if not isinstance(block, dict):
                return
            entries = block.get(list_key)
            if not isinstance(entries, list):
                return
            resolved = []
            for p in entries:
                if isinstance(p, str) and p:
                    ppath = Path(p)
                    if not ppath.is_absolute() and any(ch in p for ch in "*?["):
                        # A wildcard entry (e.g. "skills/*/") cannot be tested
                        # with exists() — the literal path never exists, so the
                        # plain branch below would silently root it at the
                        # config folder. Ask glob which base actually matches;
                        # expansion itself happens at discovery.
                        # Resolve the BASE, not the pattern: Path.resolve() on
                        # "skills/*" would keep the star as a literal component.
                        # Absolute matters here — a relative pattern would move
                        # with the working directory at discovery time.
                        base_pattern = str(base_dir.resolve().joinpath(ppath))
                        repo_pattern = str(repo_root.resolve().joinpath(ppath))
                        if glob_module.glob(base_pattern, recursive=True):
                            p = base_pattern
                        elif glob_module.glob(repo_pattern, recursive=True):
                            p = repo_pattern
                        else:
                            # No match either way: keep it repo-rooted, which is
                            # where a skills/... pattern is meant, so the
                            # "matched nothing" warning names a sane path.
                            p = repo_pattern
                        resolved.append(p)
                        continue
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
            data[block_key][list_key] = resolved

        try:
            _resolve_dir_list("plugins", "plugin_dirs")   # New structure: plugins.plugin_dirs
            _resolve_dir_list("skills", "skill_dirs")     # skills.skill_dirs (docs/skills_design.md)
        except Exception:
            # Conservative: if resolution fails for any reason, keep original values
            pass

    # Validate configuration with Pydantic
    try:
        return AgentSystemConfig.model_validate(data)
    except Exception as e:
        logger.error(f"Configuration validation failed: {e}")
        # Log the data structure that failed validation for debugging
        logger.debug(f"Failed configuration data: {data}")
        raise


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
    
    Supports explicit list merge syntax:
    - `+item`: Append item to parent list
    - `!pattern`: Remove matching items from parent list (supports wildcards)
    - Items without prefix: If any +/! exists, also appended; otherwise list is replaced
    
    Examples:
        # Replace entire list (no +/! prefix)
        tools:
          allowed: ["new_tool/*"]  # Replaces parent's list
        
        # Merge with parent list
        tools:
          allowed:
            - "+new_tool/*"     # Add to parent
            - "!old_tool/*"     # Remove from parent
            - "another_tool/*"  # Also added (merge mode active)
    
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
        elif (key in result and 
              isinstance(result[key], list) and 
              isinstance(value, list)):
            # Check if list uses explicit merge syntax (+/!)
            result[key] = _merge_lists_with_syntax(result[key], value)
        elif value is not None:
            # Override with non-None values
            result[key] = value
        # Skip None values to preserve defaults
    
    return result


def _merge_lists_with_syntax(parent_list: list, child_list: list) -> list:
    """Merge two lists using explicit +/! syntax.
    
    If the child list contains any items with + or ! prefix, merge mode is activated:
    - +item: Add item (without prefix) to result
    - !pattern: Remove matching items from parent (supports * wildcard)
    - item (no prefix): Also added in merge mode
    
    If no +/! prefixes found, the child list completely replaces the parent.
    
    Args:
        parent_list: The base list from parent config
        child_list: The override list from child config
        
    Returns:
        Merged or replaced list
    """
    # Check if any item uses merge syntax
    has_merge_syntax = any(
        isinstance(item, str) and (item.startswith('+') or item.startswith('!'))
        for item in child_list
    )
    
    if not has_merge_syntax:
        # No merge syntax - complete replacement (original behavior)
        return child_list
    
    # Merge mode: start with parent list
    result = list(parent_list)
    
    for item in child_list:
        if not isinstance(item, str):
            # Non-string items are added as-is
            if item not in result:
                result.append(item)
            continue
            
        if item.startswith('!'):
            # Remove pattern from result
            pattern = item[1:]  # Strip ! prefix
            result = [r for r in result if not _matches_pattern(r, pattern)]
        elif item.startswith('+'):
            # Add item (strip + prefix)
            clean_item = item[1:]
            if clean_item not in result:
                result.append(clean_item)
        else:
            # Regular item in merge mode - also add
            if item not in result:
                result.append(item)
    
    return result


def _matches_pattern(value: str, pattern: str) -> bool:
    """Check if a value matches a pattern (supports * wildcard).
    
    Args:
        value: The value to check
        pattern: The pattern (e.g., "w_sam/*" or "exact_match")
        
    Returns:
        True if value matches pattern
    """
    if not isinstance(value, str):
        return False
    
    if pattern == value:
        return True
    
    if '*' in pattern:
        # Simple wildcard matching
        if pattern.endswith('/*'):
            # "plugin/*" matches "plugin/tool" and "plugin"
            prefix = pattern[:-2]
            return value == prefix or value.startswith(prefix + '/')
        elif pattern.endswith('*'):
            # "prefix*" matches anything starting with "prefix"
            prefix = pattern[:-1]
            return value.startswith(prefix)
        elif pattern.startswith('*'):
            # "*suffix" matches anything ending with "suffix"
            suffix = pattern[1:]
            return value.endswith(suffix)
    
    return False
