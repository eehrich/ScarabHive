"""
Settings loader using pydantic BaseSettings semantics.

This provides a single, typed configuration entrypoint that prefers
environment variables and falls back to YAML file values. It keeps
behavior explicit and testable.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional
from pathlib import Path
import os
import yaml
from agent_system.utils import yaml_io
import glob as glob_module

from .models import AgentSystemConfig, MCPConfig, strip_empty_yaml_keys

logger = logging.getLogger(__name__)

#: Loaded at most once per process — the file is read on every load_settings()
#: call otherwise (config reload, tests, every CLI subcommand).
_secrets_loaded: set[str] = set()


def _load_secrets_file(path: Path) -> None:
    """Read ``KEY=value`` lines into the environment, without overriding it.

    The real environment WINS on purpose: the server sets its credentials
    through systemd/CI, and a stale developer file on the same machine must
    not quietly replace them. That also makes the file optional — a deployment
    that has no secrets.env is fully configured through the environment.

    Never raises: an unreadable or malformed credentials file must degrade to
    "no credentials from here" (and say so), not stop the process from
    starting.
    """
    key = str(path.resolve()) if path.is_absolute() else str(path)
    if key in _secrets_loaded:
        return
    _secrets_loaded.add(key)
    if not path.is_file():
        return
    try:
        loaded = 0
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            if not sep:
                continue
            name = name.strip()
            if not name or name in os.environ:
                continue
            # Quotes are stripped so `KEY="v"` and `KEY=v` behave the same;
            # values are used verbatim otherwise (no escape processing —
            # an API key is an opaque string).
            os.environ[name] = value.strip().strip('"').strip("'")
            loaded += 1
        # Count only — never the names' values.
        logger.info("Loaded %d credential(s) from %s", loaded, path)
    except OSError as e:
        logger.warning("Could not read %s: %s", path, e)


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
    # A reload is a fresh verdict: what the operator just fixed must be able to
    # report again if it is still broken.
    _reported_stale_llm_params.clear()
    
    # Allow overriding default config file via env var
    env_cfg = os.environ.get("AGENT_CONFIG_PATH")
    cfg_path = Path(config_path or env_cfg or "config/config.yaml")

    # Credentials come from a file the repository never sees, so that the
    # YAML can be shared and versioned. Loaded BEFORE the ${VAR} expansion
    # below, which is what actually consumes them.
    _load_secrets_file(cfg_path.parent / "secrets.env")

    data: dict = {}

    # If the master config exists, load it and then load any included files
    if cfg_path.exists():
        try:
            master = yaml_io.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
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
                    part = yaml_io.safe_load(inc_path.read_text(encoding="utf-8")) or {}
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

                    # Global hook settings (plugins.yaml). Dropped here, they
                    # were re-read from config/plugins.yaml relative to the
                    # working directory -- ignoring --config.
                    if "hooks" in part:
                        section, current = part["hooks"], data.get("hooks")
                        if isinstance(section, dict):
                            # Before the merge: an empty key sets nothing, it
                            # must not overwrite an earlier file's value with
                            # null (which the model then turns into a default)
                            section = strip_empty_yaml_keys(section)
                        if section is None:
                            pass  # every line commented out: nothing set here
                        elif isinstance(section, dict) and isinstance(current, dict):
                            data["hooks"] = deep_merge(current, section)
                        elif current is None or isinstance(current, dict):
                            # The first section -- or a wrong shape (`hooks: []`),
                            # kept as it is so GlobalHooksConfig rejects it by
                            # name. deep_merge on it raised inside the per-file
                            # except and logged the whole file as skipped.
                            data["hooks"] = section
                    
                except yaml.YAMLError as e:
                    # Log YAML syntax errors and continue (allows other configs to load)
                    logger.error(f"YAML syntax error in included config '{inc_path}': {e}")
                    logger.warning(f"Skipping malformed config file: {inc_path}")
                except Exception as e:
                    # Log other parsing errors but continue loading
                    logger.error(f"Failed to load included config '{inc_path}': {e}", exc_info=True)
                    logger.warning(f"Skipping problematic config file: {inc_path}")

    # Apply simple env-variable expansion for ${VAR} patterns (keep existing loader behavior)
    _missing_vars: set[str] = set()

    def _expand_env(value):
        if isinstance(value, str):
            import re
            pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
            def repl(m):
                name = m.group(1)
                if name not in os.environ:
                    # Collected, not silently blanked. An unset key used to
                    # become "" and surfaced hours later as an opaque 401 from
                    # a provider; the operator needs the VARIABLE NAME.
                    _missing_vars.add(name)
                    return ""
                return os.environ[name]
            return pattern.sub(repl, value)
        if isinstance(value, dict):
            return {k: _expand_env(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_expand_env(v) for v in value]
        return value

    data = _expand_env(data)

    if _missing_vars:
        logger.warning(
            "Config references %d unset variable(s): %s — the values are empty. "
            "Set them in %s (template: secrets.env.example) or in the "
            "environment.",
            len(_missing_vars), ", ".join(sorted(_missing_vars)),
            cfg_path.parent / "secrets.env",
        )

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
    _report_dropped_llm_params(data, cfg)
    _report_unknown_llm_profiles(cfg)
    return cfg


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


#: Already-reported (agent, keys) pairs. get_mcp_config_by_name re-validates on
#: every agent creation; the operator needs the line once, not per spawn.
_reported_stale_llm_params: set = set()

#: Errors raised before any log handler existed. Every entry point loads the
#: config FIRST and configures logging after, so these would only ever reach
#: stderr — not the logfile the operator actually reads. setup_logging replays
#: them once a handler is there.
_deferred_config_errors: list = []


def flush_deferred_config_errors() -> int:
    """Re-emit config errors that were raised before logging was configured."""
    pending, _deferred_config_errors[:] = list(_deferred_config_errors), []
    for fmt, args in pending:
        logger.error(fmt, *args)
    return len(pending)


def _warn_stale_llm_params(name: str, raw: Any, agent_cfg: Any) -> None:
    """Name the agent whose profile-keyed llm_params were dropped.

    The validator sees the params but not which agent they belong to, so the
    comparison happens at the call sites, where both are at hand. Loud on
    purpose: the values silently stop applying, which is exactly what the
    strict check was built to prevent. It just must not cost the start.
    """
    if agent_cfg is None or not isinstance(raw, dict):
        return
    dropped = set(raw) - set(getattr(agent_cfg, "llm_params", None) or {})
    if not dropped:
        return
    marker = (name, frozenset(dropped))
    if marker in _reported_stale_llm_params:
        return
    _reported_stale_llm_params.add(marker)
    # available_llm_profiles instead of list(llm_profile): the field may be a
    # plain STRING, and list("turbo") spells the chain out letter by letter in
    # the error message the operator has to act on.
    chain = list(getattr(agent_cfg, "available_llm_profiles", None) or [])
    fmt = ("Agent '%s': llm_params for %s have NO effect — those profiles are in "
           "none of its LLM chains %s. Values: %s")
    args = (name, sorted(dropped), chain, {k: raw[k] for k in sorted(dropped)})
    logger.error(fmt, *args)
    if not logging.getLogger().handlers:
        _deferred_config_errors.append((fmt, args))


def _report_dropped_llm_params(data: dict, cfg: AgentSystemConfig) -> None:
    """Whole-config pass over the raw server dicts (load_settings)."""
    raw_servers = ((data.get("plugins") or {}).get("servers") or {})
    for name, server in (getattr(cfg.plugins, "servers", None) or {}).items():
        raw = ((raw_servers.get(name) or {}).get("agent_config") or {}).get("llm_params")
        _warn_stale_llm_params(name, raw, getattr(server, "agent_config", None))


def _report_unknown_llm_profiles(cfg: AgentSystemConfig) -> None:
    """Name chain members that do not exist as profiles.

    Only the PRIMARY profile is resolved at startup; fallback links are built
    lazily, in the failure path. A typo in one therefore stays invisible until
    the primary model rate-limits — the exact moment the fallback was
    configured for. Then it dies with "Profile 'x' not found" and the
    configured resilience turns out to never have existed.

    Loud, but not fatal — same rule as the stale llm_params next door: one
    typo in ONE agent must not keep every server down, and the primary
    profile of every OTHER agent still works.
    """
    profiles = set(getattr(cfg.llm_system, "profiles", None) or {})
    if not profiles:
        return  # nothing to compare against (partial config / tests)
    for name, server in (getattr(cfg.plugins, "servers", None) or {}).items():
        agent_cfg = getattr(server, "agent_config", None)
        if agent_cfg is None:
            continue
        chain = getattr(agent_cfg, "available_llm_profiles", None) or []
        unknown = [p for p in chain if p not in profiles]
        if not unknown:
            continue
        primary = getattr(agent_cfg, "default_llm_profile", None)
        fmt = ("Agent '%s': LLM profiles %s are in its chain but in no "
               "llm_system.profiles — %s. Chain: %s")
        args = (name, sorted(unknown),
                "THE AGENT WILL NOT START" if primary in unknown
                else "the fallback dies in the incident it exists for",
                list(chain))
        logger.error(fmt, *args)
        if not logging.getLogger().handlers:
            _deferred_config_errors.append((fmt, args))


# Cache for plugin discovery (avoid repeated calls)
_plugins_cache: frozenset[str] | None = None
# Cache for resolved server inheritance
_inheritance_cache: dict[str, tuple[str, dict]] = {}


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
        plugins = _known_plugin_types()
        if typ in plugins:
            # Type is a real plugin, return as-is
            result = (typ, server_dict)
            _inheritance_cache[server_name] = result
            return result
        else:
            # Self-reference but not a plugin - return as-is (will fail later in bootstrap)
            return (typ, server_dict)
    
    # Check if type is a known plugin (use cached plugins)
    plugins = _known_plugin_types()
    
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
    merged = _deep_merge_dict(parent_dict, server_dict, server_name)
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
    merged_config = _deep_merge_dict(merged_config, resolved_config_dict, server_name)
    
    # Create and return final MCPConfig instance. Same tolerance as the config
    # load: a stale profile key that only appears AFTER inheritance (child
    # overrides the chains, inherits the keyed params) would otherwise raise
    # here — moving the abort from startup to agent creation, not removing it.
    final = MCPConfig.model_validate(
        merged_config, context={"drop_stale_llm_params": True})
    _warn_stale_llm_params(
        server_name, (merged_config.get("agent_config") or {}).get("llm_params"),
        getattr(final, "agent_config", None))
    return final


def _deep_merge_dict(base: dict, override: dict, _path: str = "",
                     *, explicit_none: bool = False) -> dict:
    """Deep merge two dictionaries, with override values taking precedence.

    A list is either MERGED into the inherited one or REPLACES it, never both:

    - `+item` appends to the inherited list
    - `!pattern` removes matching items from it (supports wildcards)
    - a list with no prefixes at all replaces the inherited one wholesale

    Examples:
        # Replace the entire list (no +/! anywhere)
        tools:
          allowed: ["new_tool/*"]

        # Merge with the inherited list
        tools:
          allowed:
            - "+new_tool/*"     # add
            - "!old_tool/*"     # remove

    Mixing the two forms raises — see :func:`_merge_lists_with_syntax`.

    Args:
        base: Base dictionary (default values)
        override: Override dictionary (specific values that override base)
        _path: Dotted key path, used only to make error messages locatable.
        explicit_none: What ``key: null`` in the override means. Default False —
            "nothing said", the inherited value stays; that is what an agent
            yaml with an empty key needs. True means "no value", the inherited
            one is removed: a model entry that inherits ``max_tokens: 16384``
            has no other way to say it wants the provider default.

    Returns:
        New dictionary with merged values
    """
    result = base.copy()

    for key, value in override.items():
        path = f"{_path}.{key}" if _path else str(key)
        if (key in result and
            isinstance(result[key], dict) and
            isinstance(value, dict)):
            # Recursively merge nested dictionaries
            result[key] = _deep_merge_dict(result[key], value, path,
                                           explicit_none=explicit_none)
        elif isinstance(value, list):
            # Also for a key the parent does not have: without this, a "+x"
            # would survive into the value as a literal and match nothing.
            parent = result.get(key)
            base_list = parent if isinstance(parent, list) else []
            result[key] = _merge_lists_with_syntax(base_list, value, path)
        elif value is not None or explicit_none:
            result[key] = value

    return result


def _merge_lists_with_syntax(parent_list: list, child_list: list,
                             path: str = "list") -> list:
    """Merge a child list into the inherited one, or let it replace it.

    Two mutually exclusive intents:

    - **merge** — every string carries ``+`` (add) or ``!`` (remove pattern)
    - **replace** — no string carries a prefix; the child list wins wholesale

    Mixing them raises ``ValueError``. The mix is almost always a forgotten
    ``+``, and it is unrecoverable by guessing: read as "replace", the prefixed
    entries would be the only survivors; read as "merge", the bare one silently
    joins the inherited list. Both readings are defensible, which is exactly why
    the config must say which one it means.

    Args:
        parent_list: The inherited list
        child_list: The override list from the child config
        path: Dotted key path, for the error message

    Returns:
        Merged or replaced list

    Raises:
        ValueError: if the child list mixes prefixed and bare string entries.
    """
    strings = [item for item in child_list if isinstance(item, str)]
    prefixed = [s for s in strings if s.startswith(('+', '!'))]
    bare = [s for s in strings if not s.startswith(('+', '!'))]

    if prefixed and bare:
        raise ValueError(
            f"Config '{path}' mixes list merge syntax with replacement entries. "
            f"Prefixed: {prefixed} — these add to / remove from the inherited "
            f"list. Without a prefix: {bare} — a list of those REPLACES the "
            f"inherited list entirely. One list cannot mean both. "
            f"Most likely a '+' was forgotten on {bare}; add it, or drop every "
            f"prefix to replace the list instead."
        )

    if not prefixed:
        # No merge syntax - complete replacement (original behavior)
        return child_list

    # The inherited list may itself still carry prefixes: a server's own "+x" is
    # only stripped when it is merged against default_config, and that happens
    # AFTER inheritance is resolved. Normalise it against an empty base first —
    # otherwise the "+" rides along into the child's result and later looks like
    # an authoring error that nobody made.
    return _apply_list_ops(_apply_list_ops([], parent_list), child_list)


def _apply_list_ops(base: list, items: list) -> list:
    """Apply ``+``/``!``/bare entries of *items* onto *base*.

    Deliberately permissive — mixing is rejected by the caller, which is the
    only place that knows whether the list was author-written or already merged.
    """
    result = list(base)

    for item in items:
        if not isinstance(item, str):
            # Non-string items are added as-is
            if item not in result:
                result.append(item)
            continue

        if item.startswith('!'):
            # Remove pattern from result
            pattern = item[1:]  # Strip ! prefix
            result = [r for r in result if not _matches_pattern(r, pattern)]
        else:
            clean_item = item[1:] if item.startswith('+') else item
            if clean_item not in result:
                result.append(clean_item)

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
