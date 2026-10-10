"""
Settings loader using pydantic BaseSettings semantics.

This provides a single, typed configuration entrypoint that prefers
environment variables and falls back to YAML file values. It keeps
behavior explicit and testable.

The entry points are here -- load_settings, get_tool_server_config,
master_data_dir -- and the steps they take are in the modules beside it:

- environment.py  -- the secrets files read into the environment; ${VAR} expansion
- layers.py       -- the master config, its includes and the local layer: which file
                     may set what, and how their sections stack
- merging.py      -- deep_merge for stacking files, _deep_merge_dict and the +/! list
                     syntax for inheritance
- inheritance.py  -- ``extends`` between model entries, ``type`` between server entries
- llm_checks.py   -- the loud but not fatal reports on agents' llm_params and chains

What other modules and tests import from here stays importable from here (__all__).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from agent_system.paths import relocate_data_paths, set_config_data_dir

from .environment import (LOCAL_SECRETS, SECRETS_FROM_FILE_ENV, _ENV_PLACEHOLDER, _load_secrets_file,
                          _read_secrets_file, _secrets_file_entries, environment_at_restart, environment_takes,
                          expand_env, secrets_files, set_by_the_environment, take_secret)
from .inheritance import (_forget_known_plugin_types, _known_plugin_types, _resolve_model_inheritance,
                          _resolve_server_inheritance)
from .layers import (AUTH_RULE_SECTIONS, LOCAL_CONFIG, MASTER_ONLY_SECTIONS, _expand_includes, config_files,
                     local_text, master_path, master_section, read_layers, resolve_discovery_dirs)
from .llm_checks import (_report_dropped_llm_params, _report_unknown_llm_profiles, _reported_stale_llm_params,
                         _warn_stale_llm_params, flush_deferred_config_errors)
from .merging import _deep_merge_dict
from .models import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

__all__ = [
    # the loader's own
    "load_settings", "get_tool_server_config", "master_data_dir", "AUTH_REQUIRED_ENV", "AUTH_REQUIRED_BY_WAKER",
    # environment.py
    "LOCAL_SECRETS", "SECRETS_FROM_FILE_ENV", "_ENV_PLACEHOLDER", "_load_secrets_file", "_read_secrets_file",
    "_secrets_file_entries", "environment_at_restart", "environment_takes", "secrets_files",
    "set_by_the_environment", "take_secret",
    # layers.py
    "AUTH_RULE_SECTIONS", "LOCAL_CONFIG", "MASTER_ONLY_SECTIONS", "_expand_includes", "config_files", "local_text",
    "master_section",
    # merging.py, inheritance.py, llm_checks.py
    "_deep_merge_dict", "_known_plugin_types", "_resolve_model_inheritance", "_resolve_server_inheritance",
    "_report_unknown_llm_profiles", "_reported_stale_llm_params", "flush_deferred_config_errors",
    # models
    "AgentSystemConfig",
]


def master_data_dir(config_path: Optional[str] = None) -> Optional[str]:
    """``paths.data_dir`` as a start takes it, for a process that never loads settings.

    Only the master and the local layer can set it: the loader takes this
    section from those alone. Credentials are loaded and ``${VAR}`` expanded
    first, so the value is what load_settings would see.
    """
    cfg_path = master_path(config_path)
    if not cfg_path.exists():
        return None
    for path in secrets_files(cfg_path):
        _load_secrets_file(path)
    try:
        section = master_section(cfg_path, "paths")
    except ValueError:
        return None
    value = section.get("data_dir") if isinstance(section, dict) else None
    return expand_env(str(value)) if value is not None else None


#: In the environment of a run a process that enforces authentication woke (core/session_presence.spawn_wake), and
#: of every run that run wakes in turn. The run loads its config with auth on, whatever the disk says by now: it acts
#: for a user of that API and judges them as the API does -- which enforces what it started with until a restart.
#: Tighten only: whoever sets it makes a process stricter, never looser.
AUTH_REQUIRED_ENV = "HIVE_AUTH_REQUIRED"
#: Whether this process is such a run. Taken out of the environment on import: what the run starts otherwise -- a
#: terminal command, a test run, an API of its own -- loads its config as the disk says; its wakes get it back.
AUTH_REQUIRED_BY_WAKER = os.environ.pop(AUTH_REQUIRED_ENV, None) == "1"


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
    _forget_known_plugin_types()
    # A reload is a fresh verdict: what the operator just fixed must be able to
    # report again if it is still broken.
    _reported_stale_llm_params.clear()
    
    # Allow overriding default config file via env var
    cfg_path = master_path(config_path)

    # Credentials come from a file the repository never sees, so that the
    # YAML can be shared and versioned. Loaded BEFORE the ${VAR} expansion
    # below, which is what actually consumes them.
    for secrets_path in secrets_files(cfg_path):
        _load_secrets_file(secrets_path)

    data: dict = {}

    # If the master config exists, load it and then load any included files
    if cfg_path.exists():
        data = read_layers(cfg_path)

    # Apply simple env-variable expansion for ${VAR} patterns (keep existing loader behavior)
    _missing_vars: set[str] = set()
    data = expand_env(data, _missing_vars)

    if _missing_vars:
        logger.warning(
            "Config references %d unset variable(s): %s — the values are empty. "
            "Enter them in the Setup panel, or in %s (template: secrets.env.example), "
            "or set them in the environment.",
            len(_missing_vars), ", ".join(sorted(_missing_vars)),
            cfg_path.parent / LOCAL_SECRETS,
        )

    # One data directory for every path the configuration names under data/
    # (paths.py). Recorded first, since the move resolves against it. The
    # paths section itself stays as written: `data_dir: data/x` would
    # otherwise be read as a path inside itself.
    section = data.get("paths")
    set_config_data_dir(section.get("data_dir") if isinstance(section, dict) else None)
    data = {key: value if key == "paths" else relocate_data_paths(value)
            for key, value in data.items()}

    # Resolve plugin_dirs to absolute paths
    # Paths are resolved relative to the configuration file directory first,
    # then relative to repository root if the config-relative path doesn't exist.
    # This allows entries like `src/plugins` in included files to refer to the
    # repository `src` tree while keeping resolution predictable.
    if cfg_path.exists():
        resolve_discovery_dirs(data, cfg_path)

    _resolve_model_inheritance(data)

    # Validate configuration with Pydantic
    try:
        cfg = AgentSystemConfig.model_validate(
            data, context={"drop_stale_llm_params": True})
    except Exception as e:
        logger.error(f"Configuration validation failed: {e}")
        # Log the data structure that failed validation for debugging
        logger.debug(f"Failed configuration data: {data}")
        raise
    cfg._source_path = str(cfg_path)
    if AUTH_REQUIRED_BY_WAKER:
        cfg.auth.enabled = True  # a woken run judges its user as the API that woke it does
    _report_dropped_llm_params(data, cfg)
    _report_unknown_llm_profiles(cfg)
    return cfg


def get_tool_server_config(server_name: str, config: Optional[AgentSystemConfig] = None) -> Optional[ToolServerConfig]:
    """Get a ToolServerConfig by name with inheritance from default_config and parent servers.
    
    This function creates a final ToolServerConfig by:
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
        ToolServerConfig instance with inherited values, or None if server not found
    """
    from .models import ToolServerConfig
    
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
    # ...except its metadata: with the defaults dumped, visibility "private" would read
    # as named on every server and hide the plugin manifest's (ServerDecl.visibility)
    if config.plugins.default_config.metadata is not None:
        default_config_dict["metadata"] = config.plugins.default_config.metadata.model_dump(exclude_unset=True)
    
    # Resolve inheritance chain (type: writer_agent -> type: basic_agent)
    try:
        _, resolved_config_dict = _resolve_server_inheritance(server_name, config)
    except ValueError as e:
        logger.error("Failed to resolve inheritance for '%s': %s", server_name, e)
        # Fall back to direct config without inheritance (what it sets: the defaults come next)
        resolved_config_dict = server_config.model_dump(exclude_unset=True)
    
    # Start with default config, then merge resolved (inherited) config
    merged_config = default_config_dict.copy()
    merged_config = _deep_merge_dict(merged_config, resolved_config_dict, server_name)
    
    # Create and return final ToolServerConfig instance. Same tolerance as the config
    # load: a stale profile key that only appears AFTER inheritance (child
    # overrides the chains, inherits the keyed params) would otherwise raise
    # here — moving the abort from startup to agent creation, not removing it.
    final = ToolServerConfig.model_validate(
        merged_config, context={"drop_stale_llm_params": True})
    _warn_stale_llm_params(
        server_name, (merged_config.get("agent_config") or {}).get("llm_params"),
        getattr(final, "agent_config", None))
    return final
