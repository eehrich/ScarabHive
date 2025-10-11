"""
Settings loader using pydantic BaseSettings semantics.

This provides a single, typed configuration entrypoint that prefers
environment variables and falls back to YAML file values. It keeps
behavior explicit and testable.
"""
from __future__ import annotations

from typing import Optional
from pathlib import Path
import os
import yaml

from .models import AgentSystemConfig, MCPConfig


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
                    if "plugins" in part:
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


def get_mcp_config_by_name(server_name: str, config: Optional[AgentSystemConfig] = None) -> Optional[MCPConfig]:
    """Get an MCPConfig by name with inheritance from default_config.    This function creates a final MCPConfig by:
    1. Starting with the default_config (from plugins or legacy mcp_system)
    2. Overlaying/merging the specific server configuration from servers[server_name]
    
    Supports both new structure (config.plugins) and legacy (config.mcp_system).
    
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
    
    default_config_dict = config.plugins.default_config.model_dump()
    server_config = config.plugins.servers.get(server_name)
    
    if server_config is None:
        return None
    
    server_config_dict = server_config.model_dump()
    
    # Start with a complete copy of default config
    merged_config = default_config_dict.copy()
    
    # Deep merge server-specific overrides
    merged_config = _deep_merge_dict(merged_config, server_config_dict)
    
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
