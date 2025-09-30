from __future__ import annotations

from typing import Any

from ..config.models import AgentConfig
from ..mcp.base import MCPRegistry
from ..plugins import discover_all_plugins
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def bootstrap_servers(config: AgentConfig, registry: MCPRegistry) -> None:
    # Discover plugins from configured plugin directories only. The
    # `config.mcp.plugin_dirs` entries are expected to be resolved by the
    # settings loader; here we convert them to Path objects and forward them
    # to the discovery routine. If no plugin_dirs are configured, skip
    # filesystem discovery and rely only on entrypoint plugins.
    configured = config.mcp.plugin_dirs or []
    dirs = [Path(p) for p in configured if p]
    # If no plugin_dirs configured, allow discovery from ./plugins in the
    # current working directory. Tests create a `plugins/` folder in a
    # temporary cwd and expect bootstrap to pick it up; this is a minimal
    # local fallback that does not introduce repository-specific paths.
    if not dirs:
        cwd_plugins = Path.cwd() / "plugins"
        if cwd_plugins.exists() and cwd_plugins.is_dir():
            dirs = [cwd_plugins]
        else:
            # Fallback for development: check src/plugins
            src_plugins = Path.cwd() / "src" / "plugins"
            if src_plugins.exists() and src_plugins.is_dir():
                dirs = [src_plugins]
    # Always include src/plugins as a fallback for development
    src_plugins = Path.cwd() / "src" / "plugins"
    if src_plugins.exists() and src_plugins.is_dir() and src_plugins not in dirs:
        dirs.append(src_plugins)

    plugins = discover_all_plugins(dirs=dirs if dirs else None)

    # Log discovered plugins for visibility at startup
    if plugins:
        logger.info("Discovered MCP plugins: %s", ", ".join(sorted(plugins.keys())))
    else:
        logger.debug("No external MCP plugins discovered in %s", dirs)

    for key in config.mcp.enabled_servers:
        server_cfg: dict[str, Any] = config.servers.get(key, {})
        logger.debug(f"Bootstrap server '{key}': server_cfg={server_cfg}")
        typ = server_cfg.get("type", key)
        # If a plugin provides this type, use it
        if typ in plugins:
            factory = plugins[typ]
            # Attach metadata-aware logging if plugin provides metadata
            meta = getattr(factory, '_plugin_metadata', None)
            if meta:
                desc = meta.get('description') or meta.get('summary') or ''
                ver = meta.get('version') or ''
                logger.info("Using plugin '%s' (version=%s) for server '%s': %s", typ, ver, key, desc)
            # Provide inheritance hook: if plugin server config lacks explicit LLM
            # fields, inject a parent_llm snapshot so specialized plugins (e.g.
            # web_research_agent) can inherit without duplicating credentials.
            # Now includes full config for new LLM system support.
            try:  # pragma: no cover - defensive
                if isinstance(server_cfg, dict):
                    # Volle Parent AgentConfig für Plugins bereitstellen, damit keine Teil-Rekonstruktion nötig ist
                    if 'parent_agent_config' not in server_cfg:
                        server_cfg['parent_agent_config'] = config
                    # Behalte parent_llm als Legacy-Fallback für bestehende Plugins
                    if 'parent_llm' not in server_cfg:
                        parent_config = {}
                        if getattr(config, 'llm_system', None):
                            parent_config['llm_system'] = config.llm_system.model_dump()
                        if getattr(config, 'agent_llm_profiles', None):
                            parent_config['agent_llm_profiles'] = config.agent_llm_profiles
                        parent_config['llm'] = {}
                        server_cfg['parent_llm'] = parent_config
            except Exception:
                pass
            try:
                # MODERN: Pass complete AgentConfig to factory
                # If server_cfg has parent_agent_config, use it; otherwise create one
                if 'parent_agent_config' in server_cfg:
                    inst = factory(key, server_cfg['parent_agent_config'])
                else:
                    # Legacy fallback - try to extract AgentConfig or use old signature
                    try:
                        inst = factory(key, config)  # Pass complete config
                    except TypeError:
                        # Last resort for very old factories
                        inst = factory(key, server_cfg, ssl_verify=config.network.ssl_verify)
                registry.register(key, inst)
                # CRITICAL FIX: Update agent plugins to use the shared registry instead of their isolated one
                # Agent plugins need access to all other plugins (like duckduckgo_search, web_scraper) but the
                # factory creates them with empty MCPRegistry() instances
                try:  # pragma: no cover - defensive
                    from agent_system.servers.agent.server import Agent as _Agent
                    if isinstance(inst, _Agent):
                        # Fix isolated registry - point agent to the shared registry containing all plugins
                        inst.registry = registry
                        logger.debug("Updated agent %s to use shared registry with %d servers", key, len(registry._servers))
                except Exception:
                    logger.debug("Failed to update registry for agent %s", key)
                # Apply per-server agent_config overrides (allowed/blocked tools) without mutating the shared
                # global AgentConfig reference. We deep-copy only when overrides are present so most plugins stay cheap.
                try:  # pragma: no cover - defensive
                    from agent_system.servers.agent.server import Agent as _Agent
                    overrides = server_cfg.get('agent_config', {}) if isinstance(server_cfg, dict) else {}
                    logger.debug("Checking agent_config overrides for %s: inst_type=%s, overrides=%s", key, type(inst).__name__, overrides)
                    is_agent = isinstance(inst, _Agent)
                    is_dict = isinstance(overrides, dict)
                    has_overrides = bool(overrides)
                    logger.debug("Override checks for %s: is_agent=%s, is_dict=%s, has_overrides=%s", key, is_agent, is_dict, has_overrides)
                    if isinstance(inst, _Agent) and isinstance(overrides, dict) and overrides:
                        needs_copy = any(k in overrides for k in ('allowed_tools', 'blocked_tools', 'max_steps'))
                        logger.debug("Override application for %s: needs_copy=%s, keys=%s", key, needs_copy, list(overrides.keys()))
                        if needs_copy:
                            updates = {}
                            if 'allowed_tools' in overrides and overrides.get('allowed_tools') is not None:
                                try:
                                    updates['allowed_tools'] = list(overrides.get('allowed_tools') or [])
                                except Exception:
                                    pass
                            if 'blocked_tools' in overrides and overrides.get('blocked_tools') is not None:
                                try:
                                    updates['blocked_tools'] = list(overrides.get('blocked_tools') or [])
                                except Exception:
                                    pass
                            # Optional: allow per-server max_steps override (outside of strict AgentConfig.agent_config block)
                            if 'max_steps' in server_cfg and isinstance(server_cfg.get('max_steps'), int):
                                try:
                                    updates['max_steps'] = int(server_cfg.get('max_steps'))
                                except Exception:
                                    pass
                            if updates:
                                logger.debug("Applying agent_config updates to %s: %s", key, updates)
                                old_id = id(inst.agent_config)
                                inst.agent_config = inst.agent_config.model_copy(update=updates)  # type: ignore[attr-defined]
                                new_id = id(inst.agent_config)
                                logger.debug("Applied per-server agent_config overrides to %s: old_id=%s, new_id=%s, new allowed_tools=%s", key, old_id, new_id, getattr(inst.agent_config, 'allowed_tools', None))
                            else:
                                logger.debug("No updates to apply for %s", key)
                except Exception:
                    logger.debug("Failed to apply per-server overrides for %s", key)
                # Nach erfolgreicher Instanzierung: Schwergewichtige Vererbungs-Hilfsfelder entfernen,
                # damit spätere Dumps (API Payload / Logging) keine rekursiven oder extrem großen
                # Strukturen erzeugen.
                try:
                    if isinstance(server_cfg, dict):
                        server_cfg.pop('parent_agent_config', None)
                        server_cfg.pop('parent_llm', None)
                except Exception:  # pragma: no cover - defensiv
                    pass
            except Exception as e:
                logger.exception("Failed to instantiate/register plugin '%s' for server '%s': %s", typ, key, e)
                if "test" in str(Path.cwd()):  # Re-raise in Testumgebung für schnellere Fehlerdiagnose
                    raise
            continue

        # google_search migrated to plugins; discovery will provide the factory.
        # yahoo_finance migrated to plugin package; filesystem or entrypoint discovery
        # will provide the factory when configured via `mcp.plugin_dirs` or entrypoints.
        # llm_router migrated to plugins; discovery will provide the factory.
        # web_scraper migrated to plugins; discovery will provide the factory.
        # legacy weather server removed; prefer plugin discovery above. If a
        # non-plugin implementation is required in future, add it here.
        elif typ == "agent":
            # Direct agent type - Agent extends MCPServer so can be used directly
            from .agent.server import Agent
            # Create agent with basic config and empty registry (no recursion)
            # Inherit top-level LLM config unless server explicitly overrides
            llm_system = config.llm_system

            # Apply server-level overrides if present
            if any(server_cfg.get(k) for k in ["default_provider", "provider", "model", "openai_api_key", "ollama_url", "ollama_mode", "request_timeout"]):
                from ..config.models import LLMSystemConfig, LLMModelConfig

                # Deep copy the existing LLM system to avoid modifying the original
                llm_system_dict = config.llm_system.model_dump()

                # Create override model configuration
                override_model_name = server_cfg.get("model", "override-model")
                provider = server_cfg.get("default_provider") or server_cfg.get("provider", "openai")

                override_model = LLMModelConfig(
                    provider=provider,
                    model=override_model_name,
                    openai_api_key=server_cfg.get("openai_api_key"),
                    ollama_url=server_cfg.get("ollama_url"),
                    ollama_mode=server_cfg.get("ollama_mode", "openai_compat"),
                    request_timeout=server_cfg.get("request_timeout", 120)
                )

                # Add or update the override model in the models dict
                llm_system_dict["models"][override_model_name] = override_model.model_dump()

                # Create or update a profile to use this override model
                override_profile_name = f"{key}_override"
                llm_system_dict["profiles"][override_profile_name] = {
                    "model_ref": override_model_name,
                    "description": f"Server override profile for {key}"
                }

                # Set this as the default profile for this agent
                llm_system_dict["default_profile"] = override_profile_name

                # Create new LLMSystemConfig with overrides
                llm_system = LLMSystemConfig.model_validate(llm_system_dict)

            # Create agent config by copying all fields from parent config, then override llm_system
            agent_cfg = config.model_copy(update={"llm_system": llm_system}, deep=True)

            agent_registry = MCPRegistry()  # Empty registry for this agent
            registry.register(key, Agent(key, agent_cfg, agent_registry))
        else:
            # ignore unknown for now
            continue

    # After registration, log all registered servers for visibility
    try:
        logger.info("Registered MCP servers: %s", ", ".join(registry.list()))
    except Exception:
        logger.debug("Could not list registered servers after bootstrap")
