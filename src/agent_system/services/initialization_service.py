"""Centralized initialization service for AgentSystem.

This module consolidates all plugin/agent bootstrap and dependency injection logic
that was previously duplicated across app.py, agent_cli.py, and agent_run.py.

Key responsibilities:
- MCP server/plugin bootstrap and registration
- SessionManager and SessionService initialization
- Dependency injection (session_service) into all agents
- Support for both MCPRegistry (CLI/agent_run) and PluginMCPRegistry (API)
- Work in both sync and async contexts
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from ..config.models import AgentSystemConfig
from ..mcp.base import MCPRegistry
from .session_manager import SessionManager
from .session_service import SessionService

logger = logging.getLogger(__name__)


class InitializationService:
    """Centralized initialization for all AgentSystem entry points.
    
    Provides consistent bootstrap and injection logic for:
    - FastAPI app (app.py)
    - Rich CLI (agent_cli.py)
    - Lightweight runner (agent_run.py)
    
    Eliminates code duplication while maintaining flexibility for each context.
    """

    def __init__(self, config: AgentSystemConfig):
        """
        Initialize the service with system configuration.
        
        Args:
            config: Fully loaded AgentSystemConfig with merged settings
        """
        self.config = config
        self._session_manager: Optional[SessionManager] = None
        self._session_service: Optional[SessionService] = None
        self._runtime = None  # agent_system.runtime.Runtime, created on bootstrap
        self._initialized = False

    @property
    def session_manager(self) -> SessionManager:
        """Get or create the SessionManager."""
        if self._session_manager is None:
            # Same rule as the API (app.py): a woken run (core/session_presence)
            # is an agent-cli process that inherits this variable, and reading
            # sessions from somewhere else than the process that woke it means
            # continuing a session it cannot find.
            storage_path = Path(os.getenv("AGENT_SESSION_STORAGE_PATH")
                                or Path(__file__).parents[3] / "data" / "sessions")
            self._session_manager = SessionManager(storage_path=str(storage_path))
            logger.debug("SessionManager initialized at %s", storage_path)
        return self._session_manager

    @property
    def session_service(self) -> SessionService:
        """Get or create the SessionService."""
        if self._session_service is None:
            self._session_service = SessionService(self.session_manager)
            logger.debug("SessionService initialized")
        return self._session_service

    def bootstrap_and_inject(
        self,
        registry: Optional[MCPRegistry] = None,
        inject_sessions: bool = True
    ) -> MCPRegistry:
        """
        Bootstrap all MCP servers and inject dependencies.
        
        This method:
        1. Creates a new MCPRegistry if not provided
        2. Calls bootstrap_servers() to discover and register all plugins/agents
        3. Injects session_service into all agents (optional)
        4. Returns the fully initialized registry
        
        Args:
            registry: Existing registry to use, or None to create new one
            inject_sessions: Whether to inject session_service into agents (default: True)
        
        Returns:
            MCPRegistry with all servers bootstrapped and dependencies injected
        """
        from ..runtime import Runtime, configure_process_singletons

        # Create registry if not provided
        if registry is None:
            registry = MCPRegistry()
            logger.debug("Created new MCPRegistry")

        # Bootstrap all configured servers/plugins. The Runtime is KEPT: it
        # holds the declaration of every configured server, so a caller can
        # later ask about one -- or build it -- without a second discovery.
        logger.info("Bootstrapping MCP servers from config")
        configure_process_singletons(self.config)
        self._runtime = Runtime(
            self.config, registry=registry,
            session_service=self.session_service if inject_sessions else None,
        )
        self._runtime.start()
        logger.info("Servers registered: %s", ", ".join(registry.list()))

        # Inject session_service into all agents
        if inject_sessions:
            from .agent_injection import inject_session_service_into_agents
            logger.debug("Injecting session_service into all agents")
            inject_session_service_into_agents(registry, self.session_service)
            logger.debug("Session injection completed")

        self._initialized = True
        return registry

    def initialize_for_cli(self) -> tuple[MCPRegistry, SessionService]:
        """
        Full initialization for CLI context.
        
        This is the simplest pattern used by agent_cli.py and agent_run.py:
        - Create registry
        - Bootstrap servers
        - Inject session_service
        - Return both registry and session_service
        
        Returns:
            Tuple of (registry, session_service) ready for CLI use
        """
        logger.info("[InitializationService] Initializing for CLI context")
        
        # Bootstrap and inject
        registry = self.bootstrap_and_inject(inject_sessions=True)
        
        logger.info("[InitializationService] CLI initialization complete")
        return registry, self.session_service

    def initialize_for_api(
        self,
        plugin_registry=None,
    ) -> SessionService:
        """
        Initialization for API context (FastAPI app).
        
        API context is more complex because:
        - PluginMCPRegistry (singleton) is used instead of MCPRegistry
        - Agents might be created via build_mcp_app() which does its own bootstrap
        - We need to inject into the global plugin_registry
        
        Args:
            plugin_registry: The global PluginMCPRegistry instance (singleton)
        
        Returns:
            SessionService ready for use in API context
        """
        logger.info("[InitializationService] Initializing for API context")
        
        # The injection is this method's whole point. It used to hang off a
        # `skip_bootstrap` flag although the method never bootstrapped
        # anything -- the sole caller passed True and turned the call into a
        # silent no-op while still logging "initialization complete".
        if plugin_registry is not None:
            from .agent_injection import inject_session_service_into_agents
            logger.debug("Injecting session_service into API plugin_registry")
            inject_session_service_into_agents(plugin_registry, self.session_service)
            logger.debug("API session injection completed")
        
        self._initialized = True
        logger.info("[InitializationService] API initialization complete")
        return self.session_service

    @property
    def runtime(self):
        """The Runtime this service bootstrapped with, or None before bootstrap.

        Whoever holds it can ask what a server IS (``describe``) and build one
        on demand (``materialize``) instead of re-running discovery.
        """
        return self._runtime

    @property
    def initialized(self) -> bool:
        """Check if initialization has been completed."""
        return self._initialized
