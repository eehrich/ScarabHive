"""The composition root: one place that turns a config into running servers.

Five entry points used to assemble the process each in their own way
(``build_app``, ``InitializationService``, ``MCPIntegration.initialize`` and
four writer tools), and each of them repeated the same twelve steps:
discover, merge the server config, call the factory with the right signature,
register in BOTH registries, then post-process an agent instance (shared
registry, metadata, description, visibility, self tool descriptions).

``Runtime`` owns those steps once. ``ServerDecl`` is what is known about a
server BEFORE it is built -- the merged config, its factory, its manifest --
so callers can ask about a server without instantiating it, which is what
makes lazy construction possible later.

Behaviour is deliberately identical to the old ``bootstrap_servers``:
same order, same log lines, same error policy (log and continue -- except
under a test working directory, where a failed instantiation re-raises).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .config.models import AgentSystemConfig, MCPConfig
from .config.settings import get_mcp_config_by_name
from .mcp.base import MCPRegistry

logger = logging.getLogger(__name__)

VISIBILITIES = ("ui", "tool", "both", "private")


@dataclass
class ServerDecl:
    """What is known about a configured server before anything is built."""

    name: str
    type: str
    mcp_config: MCPConfig
    factory: Optional[Callable[..., Any]] = None      # None: the direct "agent" type
    plugin_metadata: Optional[dict] = field(default=None, repr=False)

    @property
    def is_declared_agent(self) -> bool:
        """True when this declaration builds an Agent for sure.

        Only the direct ``type: agent`` is certain without building; a plugin
        factory may return an agent or a plain server (writer_issues does both,
        by ``config.mode``).
        """
        return self.factory is None and self.type == "agent"

    @property
    def visibility(self) -> str:
        """ui | tool | both | private -- priority as bootstrap always had it:
        the instance's own MCPConfig metadata first, then the plugin manifest,
        else private (not visible; secure by default)."""
        metadata = self.mcp_config.metadata
        if metadata and metadata.visibility:
            return metadata.visibility
        if self.plugin_metadata:
            declared = self.plugin_metadata.get("visibility")
            if declared in VISIBILITIES:
                return declared
        return "private"

    def apply_to(self, instance: Any, registry: MCPRegistry) -> None:
        """The post-processing every agent instance got in bootstrap."""
        from .servers.agent.server import Agent

        if not isinstance(instance, Agent):
            return

        instance.registry = registry
        logger.debug("Updated agent %s to use shared registry with %d servers",
                     self.name, len(registry._servers))

        if self.mcp_config.metadata:
            instance._metadata = self.mcp_config.metadata.model_dump()
            logger.debug("Applied instance metadata to agent '%s': %s", self.name, instance._metadata)

        if self.mcp_config.description:
            instance._description = self.mcp_config.description
            logger.debug("Applied instance description to agent '%s': %s",
                         self.name, self.mcp_config.description)

        if not hasattr(instance, "_visibility_set_explicitly"):
            visibility = self.visibility
            instance._mcp_public = visibility in ("ui", "both")
            instance._mcp_tool_visible = visibility in ("tool", "both")
            logger.debug("Plugin agent '%s' visibility set: %s (ui=%s, tool=%s)",
                         self.name, visibility, instance._mcp_public, instance._mcp_tool_visible)

        if self.mcp_config.self_tool_descriptions:
            instance._self_tool_descriptions = self.mcp_config.self_tool_descriptions
            logger.debug("Applied self_tool_descriptions to agent '%s': %d overrides",
                         self.name, len(instance._self_tool_descriptions))


class Runtime:
    """Config in, running servers out -- and the declarations in between."""

    def __init__(self, config: AgentSystemConfig, *,
                 registry: Optional[MCPRegistry] = None,
                 session_service: Any = None):
        self.config = config
        self.registry = registry if registry is not None else MCPRegistry()
        self._session_service = session_service
        self._decls: dict[str, ServerDecl] = {}
        self._plugins: dict[str, Callable[..., Any]] = {}
        self._declare_all()

    # ------------------------------------------------------------------
    # declarations
    # ------------------------------------------------------------------
    def _declare_all(self) -> None:
        if not self.config.plugins:
            logger.warning("No plugins configuration found, skipping bootstrap")
            return

        configured = self.config.plugins.plugin_dirs or []
        dirs = [Path(p) for p in configured if p]
        from .plugins import discover_all_plugins
        self._plugins = discover_all_plugins(dirs=dirs if dirs else None)

        if self._plugins:
            logger.info("Discovered MCP plugins: %s", ", ".join(sorted(self._plugins.keys())))
        else:
            logger.debug("No external MCP plugins discovered")

        for name, server in self.config.plugins.servers.items():
            if not server.enabled:
                continue
            merged = get_mcp_config_by_name(name, self.config)
            if not merged:
                logger.warning("Failed to resolve MCP config for server '%s', skipping", name)
                continue
            logger.debug(f"Bootstrap server '{name}': type={merged.type}, enabled={merged.enabled}")

            factory = self._plugins.get(merged.type)
            if factory is not None:
                metadata = getattr(factory, "_plugin_metadata", None)
                if metadata:
                    logger.info("Using plugin '%s' (version=%s) for server '%s': %s",
                                merged.type, metadata.get("version") or "",
                                name, metadata.get("description") or metadata.get("summary") or "")
            elif merged.type != "agent":
                logger.warning("Unknown server type '%s' for server '%s'", merged.type, name)
                continue

            self._decls[name] = ServerDecl(
                name=name, type=merged.type, mcp_config=merged, factory=factory,
                plugin_metadata=getattr(factory, "_plugin_metadata", None),
            )

    def declarations(self) -> dict[str, ServerDecl]:
        return dict(self._decls)

    def describe(self, name: str) -> Optional[ServerDecl]:
        """What is known about a server without building it."""
        return self._decls.get(name)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def start(self) -> "Runtime":
        """Build every declared server, in config order."""
        for name in list(self._decls):
            self._build_logged(name)
        try:
            logger.info("Registered MCP servers: %s", ", ".join(self.registry.list()))
        except Exception:
            logger.debug("Could not list registered servers after bootstrap")
        return self

    def _build_logged(self, name: str) -> Any:
        """Today's start-up error policy: log and carry on, so one broken
        server does not take the process with it -- but re-raise under a test
        working directory, where a swallowed failure is a green lie."""
        decl = self._decls[name]
        try:
            return self.materialize(name)
        except Exception as e:
            if decl.factory is not None:
                logger.exception("Failed to instantiate plugin '%s' for server '%s': %s",
                                 decl.type, name, e)
            else:
                logger.exception("Failed to instantiate agent '%s': %s", name, e)
            if "test" in str(Path.cwd()):
                raise
            return None

    def materialize(self, name: str) -> Any:
        """THE one build path: construct, register in both registries, post-process."""
        existing = self.registry._servers.get(name)
        if existing is not None:
            return existing

        decl = self._decls.get(name)
        if decl is None:
            raise KeyError(name)

        instance = self._construct(decl)
        self.registry.register(name, instance)

        if decl.factory is not None:
            # Asymmetric on purpose, exactly as bootstrap always was: the
            # direct ``type: agent`` branch registered the instance in the
            # MCPRegistry and did nothing else. Doing the two steps below for
            # it as well would publish it as an MCP plugin server and give it
            # a visibility flag it never had -- and the default, "private",
            # would take it OUT of GET /agents, which shows agents that carry
            # no _mcp_public at all.
            from .plugins.mcp_adapter import plugin_mcp_registry
            plugin_mcp_registry.register_existing_plugin_instance(
                name, instance, self.config, decl.mcp_config)
            logger.debug(f"Registered plugin '{name}' in both registries (MCPRegistry + PluginMCPRegistry)")

            decl.apply_to(instance, self.registry)
        if self._session_service is not None:
            from .servers.agent.server import Agent
            if isinstance(instance, Agent):
                instance._session_service = self._session_service
        return instance

    def _construct(self, decl: ServerDecl) -> Any:
        if decl.factory is not None:
            # All plugins take (name, system_config, mcp_config); agent
            # factories additionally take the shared registry, and it must
            # reach __init__ -- see plugins/factory_utils.
            if getattr(decl.factory, "_accepts_registry", False):
                return decl.factory(decl.name, self.config, decl.mcp_config, registry=self.registry)
            return decl.factory(decl.name, self.config, decl.mcp_config)

        from .servers.agent.server import Agent
        return Agent(decl.name, self.config, decl.mcp_config, self.registry)


def configure_process_singletons(config: AgentSystemConfig) -> None:
    """Process-wide setup that must happen ONCE, before any server is built.

    (Historically the cancellation manager was configured in every
    Agent.__init__, which REPLACED the global manager per instantiation and
    orphaned all registered tokens/tasks -- cancellation of in-flight requests
    silently stopped working. The old code also read a non-existent top-level
    `config.cancellation`, so the YAML values were never honored;
    CancellationConfig lives under `logging.cancellation`.)
    """
    from .core.cancellation import configure_cancellation_manager
    cancellation = getattr(getattr(config, "logging", None), "cancellation", None)
    if cancellation:
        configure_cancellation_manager(
            cleanup_timeout=cancellation.cleanup_timeout,
            monitor_interval=cancellation.monitor_interval,
        )
