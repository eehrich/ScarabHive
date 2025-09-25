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
                if isinstance(server_cfg, dict) and 'parent_llm' not in server_cfg:
                    parent_config = {}
                    # Only include modern LLM system configuration
                    if getattr(config, 'llm_system', None):
                        parent_config['llm_system'] = config.llm_system.model_dump()
                    if getattr(config, 'agent_llm_profiles', None):
                        parent_config['agent_llm_profiles'] = config.agent_llm_profiles
                    # Include minimal LLM config for backwards compatibility (empty dict)
                    parent_config['llm'] = {}
                    if parent_config:
                        server_cfg['parent_llm'] = parent_config
            except Exception:
                pass
            try:
                inst = factory(key, server_cfg, ssl_verify=config.network.ssl_verify)
                registry.register(key, inst)
            except Exception as e:
                logger.exception("Failed to instantiate/register plugin '%s' for server '%s': %s", typ, key, e)
                # For debugging, re-raise in tests
                if "test" in str(Path.cwd()):
                    raise
                # continue to try other servers
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
            agent_cfg = AgentConfig()
            # copy top-level LLM config
            agent_cfg.llm = config.llm
            # apply server-level overrides if present
            if server_cfg.get("default_provider") or server_cfg.get("provider") or server_cfg.get("model"):
                sc = server_cfg
                llm_kwargs = {}
                if sc.get("default_provider"):
                    llm_kwargs["provider"] = sc.get("default_provider")
                elif sc.get("provider"):
                    llm_kwargs["provider"] = sc.get("provider")
                if sc.get("model"):
                    llm_kwargs["model"] = sc.get("model")
                if sc.get("openai_api_key"):
                    llm_kwargs["openai_api_key"] = sc.get("openai_api_key")
                if sc.get("ollama_url"):
                    llm_kwargs["ollama_url"] = sc.get("ollama_url")
                if sc.get("ollama_mode"):
                    llm_kwargs["ollama_mode"] = sc.get("ollama_mode")
                if sc.get("request_timeout") is not None:
                    llm_kwargs["request_timeout"] = sc.get("request_timeout")
                # merge overrides
                for k, v in llm_kwargs.items():
                    setattr(agent_cfg.llm, k, v)

            agent_registry = MCPRegistry()  # Empty registry for this agent
            registry.register(key, Agent(key, agent_cfg, agent_registry, server_cfg, ssl_verify=config.network.ssl_verify))
        else:
            # ignore unknown for now
            continue

    # After registration, log all registered servers for visibility
    try:
        logger.info("Registered MCP servers: %s", ", ".join(registry.list()))
    except Exception:
        logger.debug("Could not list registered servers after bootstrap")
