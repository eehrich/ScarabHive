"""
Enhanced Agent Core - Agent extends MCPServer for direct agent-to-agent communication
Supports multiple tool calls per conversation turn for better efficiency
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from ...config.models import AgentSystemConfig, MCPConfig
from ...core.cancellation import get_cancellation_manager, configure_cancellation_manager, CancellationToken
from ...mcp.base import MCPRegistry, MCPServer
from ...utils.id import short_id
from ...llm.models import ChatMessage, LLMRateLimitError, LLMQuotaExhaustedError
from ...llm.text_sanitizer import sanitize_for_llm
from ...mcp.status import (
    status_scope,
    StatusScope,
    status_bus,
    current_request_id
)
from .components.mcp_integration import MCPIntegrationManager
from .components.tool_execution import ToolExecutionManager
from .components.status_forwarding import StatusEventForwarder
from .components.session_tracking import SessionTracker
from .components.request_manager import AgentRequestManager
from .prompt_strategies import PromptRenderer, PromptContext
from .loop_detection import ToolCallLoopDetector
from .tool_discovery import ToolDiscoveryService
from .tool_schema_builder import ToolSchemaBuilder


logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """Context for agent conversation execution."""
    messages: List[ChatMessage]
    available_tools: List[str]
    tools_schema: List[Dict[str, Any]]
    tool_name_mapping: Dict[str, str]
    max_steps: int
    main_token: CancellationToken
    context_reset_token: Any  # Token for resetting contextvars
    status_forwarder: 'StatusEventForwarder'  # Per-request forwarder instance
    session_id: Optional[str] = None  # Session ID for session-scoped operations


class Agent(MCPServer):
    """Enhanced Agent with dual interface: execution engine + callable tool.

    TOOL INTERFACE CLARITY:
    ----------------------
    Agent has TWO distinct tool interfaces that are easily confused:

    1. EXTERNAL (what this agent OFFERS to others):
       - list_tools() → List[MCPTool] - Returns this agent as a callable tool
       - MCPServer interface: What OTHER agents see when they query our tools
       - Used by: ToolSchemaBuilder when other agents discover available tools

    2. INTERNAL (what this agent CAN USE):
       - list_usable_tools() → List[str] - Tool names this agent can call
       - Filtered by agent_config.tools.allowed patterns
       - Used by: _run_events() to build LLM prompt with available tools
       - Example: ["datetime", "web_search", "other_agent"]

    3. UTILITY (detailed info for user-facing endpoints):
       - _list_usable_tools_with_details() → List[Dict] - Name + description
       - Used by: BasicAgent's list_available_tools tool
       - For debugging/introspection, not for execution

    REMEMBER:
    - list_tools() = what I OFFER (MCPServer standard)
    - list_usable_tools() = what I CAN USE (internal execution)
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig,
                 registry: MCPRegistry | None = None,
                 llm: object | None = None, llm_factory: object | None = None,
                 session_service: object | None = None) -> None:
        """
        Initialize Agent as both an executor and an MCP Server.

        Modern signature matching plugin pattern:
        - system_config: Complete system configuration
        - mcp_config: MCP configuration object (contains agent_config, type, enabled)
        - registry: MCP Registry with available tools (required for agents)
        - session_service: SessionService for managing agent sessions (optional, will be injected if available)

        Args:
            name: Name of this agent (used when serving as MCP Server)
            system_config: Complete system configuration (includes llm_system, network, context, etc.)
            mcp_config: MCP configuration object (MCPConfig with agent_config)
            registry: MCP Registry with available tools
            llm: Optional LLM client instance (for testing)
            llm_factory: Optional LLM factory for creating client (for testing)
            session_service: Optional SessionService for session management (injected by CLI/App)
        """
        # Initialize as MCPServer with MCPConfig object
        super().__init__(name, system_config, mcp_config)

        # Extract agent_config from MCPConfig (required, no fallbacks)
        if not mcp_config.agent_config:
            raise ValueError(f"Agent '{name}' requires agent_config in MCPConfig")
        self.agent_config = mcp_config.agent_config

        # Agent-specific initialization (registry required for agents)
        if registry is None:
            raise ValueError(f"Agent '{name}' requires MCPRegistry instance")
        self.registry = registry

        # Store session_service for tools that need session access (e.g., sub-agent manager)
        # This is optional - if None, tools that need it will fail gracefully
        self._session_service = session_service

        # Visibility flags control where the agent appears
        # _mcp_public: Show in UI agent dropdown (GET /agents endpoint)
        # _mcp_tool_visible: Available as tool for other agents
        # Default both to False for config agents, can be overridden based on metadata
        self._mcp_public = False
        self._mcp_tool_visible = False

        # Allow dependency injection of an LLM client or a factory that
        # creates one. This makes testing and runtime wiring explicit.
        self.llm = llm
        self._llm_factory = llm_factory

        # Initialize LLM if not provided
        # Store LLM profile information for status display
        self.llm_profile_info = None
        
        # Store timeout configuration from agent_config
        self.timeouts = self.agent_config.timeouts if self.agent_config else None
        
        # Track active fallback LLM (persistent across requests)
        # When rate limit/quota is exhausted, we switch to fallback and stay there
        # until fallback_recovery_seconds has elapsed, then we try original again
        self._active_fallback_llm: Optional[Any] = None
        self._active_fallback_profile: Optional[str] = None
        self._fallback_activated_at: Optional[float] = None  # Timestamp when fallback was activated
        self._jittered_recovery_seconds: Optional[float] = None  # Per-instance jittered recovery time

        # Extract profile info even if LLM is provided externally
        if self.llm is not None and self.agent_config and system_config.llm_system:
            try:
                # Build llm_kwargs from config for profile info extraction
                llm_kwargs = {
                    "profile_name": self.agent_config.llm_profile,
                    "provider": getattr(self.llm, "provider", "external"),
                    "model": getattr(self.llm, "model", "external-model")
                }
                self.llm_profile_info = self._extract_profile_info(system_config, name, llm_kwargs)
            except Exception as e:
                logger.debug(f"Could not extract profile info for external LLM: {e}")

        if self.llm is None:
            # If a factory is provided, use it to create the client.
            if self._llm_factory is not None:
                try:
                    self.llm = self._llm_factory.create()
                except Exception as e:
                    logger.warning("LLM factory creation failed: %s", e)
                    self.llm = None
            else:
                # Create LLM using the new profile-based system (now required)
                try:
                    # Lazy import to avoid circular imports when testing
                    from ...llm.factory import create_llm_from_profile, resolve_llm_config_for_agent

                    # Use new profile-based resolution with agent config
                    llm_kwargs = resolve_llm_config_for_agent(system_config, self.agent_config)

                    # Store profile information for status display
                    self.llm_profile_info = self._extract_profile_info(system_config, name, llm_kwargs)

                    # Get SSL verify setting
                    ssl_verify = getattr(system_config, "network").ssl_verify if getattr(system_config, "network", None) else None

                    # Use factory function that properly handles batch mode
                    self.llm = create_llm_from_profile(
                        config=system_config,
                        llm_profile=self.agent_config.default_llm_profile,
                        ssl_verify=ssl_verify,
                    )
                except Exception as e:
                    # Missing API key is an expected situation in test/dev
                    # environments; avoid noisy warnings for that case.
                    try:
                        msg = str(e)
                    except Exception as e2:
                        logger.debug(f"Failed to stringify exception: {e2}")
                        msg = "<exception>"
                    # Match the exact ValueError message emitted by make_llm
                    if isinstance(e, ValueError) and msg == "OPENAI_API_KEY is required when provider=openai":
                        logger.debug("LLM not initialized (no API key): %s", msg)
                    else:
                        logger.warning("LLM initialization failed: %s", msg)
                    self.llm = None

        # Context management now handled by hook plugins (context_optimizer, context_summarizer)

        # Configure cancellation system with agent config values
        if hasattr(system_config, 'cancellation') and system_config.cancellation:
            configure_cancellation_manager(
                cleanup_timeout=system_config.cancellation.cleanup_timeout,
                monitor_interval=system_config.cancellation.monitor_interval
            )
        else:
            configure_cancellation_manager()

        # Centralized internal tool-call counter (used to generate per-tool suffixes)
        self._internal_tool_counter = 0
        self._internal_tool_counter_lock = asyncio.Lock()

        # Cache for list_tools() to avoid creating new MCPTool objects on every call
        self._list_tools_cache: list | None = None

        # Track current conversation messages for debugging
        self._current_messages: List[ChatMessage] = []

        # Initialize component managers for better code organization
        # Create request manager first (owns _active_requests dict)
        self._request_manager = AgentRequestManager(self.name)
        # Session tracker shares the same _active_requests dict for coordination
        self._session_tracker = SessionTracker(self._request_manager._active_requests)
        self._mcp_integration_manager = MCPIntegrationManager(self.system_config, self.agent_config)
        # Note: StatusEventForwarder is now created per-request (not shared) to prevent race conditions
        self._tool_execution_manager = ToolExecutionManager(
            self.registry,
            self
        )

        # Context management now handled by hook plugins via HookIntegrationManager

        # Initialize hook integration manager
        from .components.hook_integration import HookIntegrationManager
        self._hook_manager = HookIntegrationManager(self)
        
        # Initialize tool call loop detector to prevent infinite tool loops
        # Especially important for Gemini which tends to get stuck
        # Read config from agent_config.loop_detection
        loop_config = self.agent_config.loop_detection if self.agent_config else None
        if loop_config and loop_config.enabled:
            self._loop_detector = ToolCallLoopDetector(
                history_size=loop_config.history_size,
                exact_match_threshold=loop_config.exact_match_threshold,
                sequence_threshold=loop_config.sequence_threshold,
                block_after_threshold=loop_config.block_after_threshold,
                auto_unblock_after_steps=loop_config.auto_unblock_after_steps
            )
        else:
            # Create a disabled detector (never triggers)
            self._loop_detector = ToolCallLoopDetector(
                exact_match_threshold=9999  # Effectively disabled
            )
            if loop_config and not loop_config.enabled:
                logger.debug(f"[{self.name}] Loop detection disabled via config")

        # Set agent reference in MCP integration for cancellation support
        self._set_agent_reference_in_mcp()

    def _extract_profile_info(self, config, agent_name: str, llm_kwargs: dict) -> str:
        """Extract profile information for status display."""
        model = llm_kwargs.get("model", "unknown")
        provider = llm_kwargs.get("provider", "unknown")

        # Get profile name - priority order:
        # 1. From llm_kwargs (directly resolved profile used for this LLM)
        # 2. From agent_config.llm_profile (agent's configured profile)
        # 3. From agent_llm_profiles mapping (agent-specific override)
        # 4. From default_profile (system default)
        profile_name = llm_kwargs.get("profile_name")

        if not profile_name and hasattr(self, 'agent_config') and self.agent_config:
            profile_name = getattr(self.agent_config, 'llm_profile', None)
            # If llm_profile is a list, use the first element (default profile)
            if isinstance(profile_name, list):
                profile_name = profile_name[0] if profile_name else None

        if not profile_name and config.llm_system and config.llm_system.profiles:
            # Check agent-specific assignment
            if agent_name and hasattr(config, 'agent_llm_profiles') and config.agent_llm_profiles:
                profile_name = config.agent_llm_profiles.get(agent_name)
            # Fall back to default profile
            if not profile_name:
                profile_name = getattr(config.llm_system, 'default_profile', None)

        # Return with profile name if available, otherwise just provider/model
        if profile_name:
            return f"{profile_name}:{provider}/{model}"
        return f"{provider}/{model}"

    def _create_fallback_llm(self, fallback_profile: str) -> Optional[Any]:
        """Create an LLM client for a fallback profile.
        
        Args:
            fallback_profile: Name of the fallback LLM profile to use
            
        Returns:
            LLM client instance or None if creation fails
        """
        try:
            from ...llm.factory import create_llm_from_profile
            
            ssl_verify = getattr(self.system_config, "network").ssl_verify if getattr(self.system_config, "network", None) else None
            
            fallback_llm = create_llm_from_profile(
                config=self.system_config,
                llm_profile=fallback_profile,
                ssl_verify=ssl_verify,
            )
            logger.info(f"[{self.name}] Created fallback LLM for profile: {fallback_profile}")
            return fallback_llm
        except Exception as e:
            logger.warning(f"[{self.name}] Failed to create fallback LLM for profile '{fallback_profile}': {e}")
            return None

    async def next_internal_tool_request_id(self, base_request_id: str) -> str:
        """Return the next internal tool request id with a 3-digit suffix.

        This method is async and protected by an internal lock to ensure
        unique, monotonic counters for the lifetime of the Agent instance.
        """
        async with self._internal_tool_counter_lock:
            self._internal_tool_counter += 1
            return f"{base_request_id}_{self._internal_tool_counter:03d}"

    # ------------------------------------------------------------------
    # Unified Server Resolution (Central Method)
    # ------------------------------------------------------------------
    def _get_server_from_any_registry(self, server_name: str) -> Optional[MCPServer]:
        """Get a server from either plugin_registry or self.registry.

        This is the CENTRAL method for resolving servers. All code that needs to
        find a server should use this method instead of accessing registries directly.

        Search order:
        1. self.registry (contains ALL servers: plugins + config agents)
        2. plugin_registry (fallback for plugin adapters)

        Args:
            server_name: Name of the server to find (e.g., 'basic_operations', 'meta_web_research_agent')

        Returns:
            The server instance or None if not found
        """
        # First, try local registry (contains all servers)
        if hasattr(self, 'registry') and self.registry:
            try:
                server = self.registry.get(server_name)
                if server:
                    return server
            except Exception as e:
                logger.debug(f"Failed to get server '{server_name}' from local registry: {e}")

        # Fallback: try plugin registry (for plugin adapters)
        if self._mcp_integration_manager.mcp_integration and self._mcp_integration_manager.mcp_integration.initialized:
            try:
                plugin_adapter = self._mcp_integration_manager.mcp_integration.plugin_registry.get_server(server_name)
                if plugin_adapter and hasattr(plugin_adapter, 'plugin_server'):
                    return plugin_adapter.plugin_server
            except Exception as e:
                logger.debug(f"Failed to get server '{server_name}' from plugin registry: {e}")

        return None

    # ------------------------------------------------------------------
    # Prompt customization hook
    # ------------------------------------------------------------------
    def get_custom_system_prompt(self, context: Dict[str, Any]) -> Optional[str]:  # pragma: no cover - default noop
        """Subclass hook: return a fully rendered system prompt string or None.

        If a subclass returns a non-empty string here, that prompt is used as the
        primary system prompt and configuration driven template / raw prompt
        logic is skipped (except tools prompt injection which still occurs).

        Args:
            context: Dict containing keys like 'tools', 'max_steps', plus any
                     auto datetime context if enabled.

        Returns:
            The custom system prompt string or None to fall back to config logic.
        """
        return None

    # ------------------------------------------------------------------
    # Central prompt rendering utilities (using strategy pattern)
    # ------------------------------------------------------------------
    def _render_prompts(
        self, 
        usable_tools: List[str], 
        max_steps: int, 
        current_step: int,
        session_id: Optional[str] = None
    ) -> tuple[str, Optional[str]]:
        """
        Render (system_prompt, tools_prompt) using strategy pattern.

        Args:
            usable_tools: List of tool names available to the agent
            max_steps: Maximum steps allowed for the agent
            current_step: Current step number (1-indexed, for dynamic per-step rendering)
            session_id: Optional session ID for session-scoped template vars

        Order of precedence:
          1. Subclass hook `get_custom_system_prompt`
          2. In-memory raw `agent_config.system_prompt`
          3. File/template based `agent_config.system_template`
          4. Default fallback

        Returns:
            (system_prompt, tools_prompt_or_None)
        """
        # Get session-scoped template vars if session_id provided
        session_template_vars = None
        if session_id and hasattr(self, '_session_tracker') and self._session_tracker:
            session_template_vars = self._session_tracker.get_session_template_vars(session_id)
        
        renderer = PromptRenderer()
        context = PromptContext(
            agent_name=self.name,
            agent_config=self.agent_config,
            system_config=self.system_config,
            available_tools=usable_tools,
            max_steps=max_steps,
            current_step=current_step,
            agent_instance=self,  # Pass self for hook access
            session_template_vars=session_template_vars  # Session-scoped vars (override agent_config)
        )
        return renderer.render(context)

    async def get_current_system_prompt(self) -> str:
        """Async: render current system prompt (diagnostics endpoint)."""
        try:
            usable_tools, _, _ = await self.list_usable_tools()  # Ignore patterns for prompt display
        except Exception as e:
            logger.warning(f"Failed to list usable tools for system prompt: {e}", exc_info=True)
            usable_tools = []
        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
        system_msg, _ = self._render_prompts(usable_tools, max_steps, current_step=0)
        return system_msg


    def _set_agent_reference_in_mcp(self) -> None:
        """Set agent reference in MCP integration for cancellation support."""
        try:
            # Set agent reference in MCP integration manager
            self._mcp_integration_manager._agent_ref = self

            # Try to set agent reference in MCP integration when it's available
            if hasattr(self._mcp_integration_manager, 'mcp_integration') and self._mcp_integration_manager.mcp_integration:
                self._mcp_integration_manager.mcp_integration.main_agent_ref = self
        except Exception as e:
            logger.debug("Failed to set agent reference in MCP integration: %s", e)

    @property
    def description(self) -> str:
        """Get the agent description."""
        # Try to get description from mcp_config
        if hasattr(self, 'mcp_config') and self.mcp_config:
            desc = getattr(self.mcp_config, 'description', None)
            if desc:
                return desc
        return f"Agent: {self.name}"

    async def cancel_request(self, request_id: str) -> bool:
        """
        Cancel an active request.

        Uses dual cancellation: global CancellationManager for tools +
        per-agent events for request loop. See docs/cancellation_architecture.md
        for design details.

        Args:
            request_id: The unique ID of the request to cancel

        Returns:
            True if the request was found and cancelled, False otherwise
        """
        return await self._request_manager.cancel_request(request_id)

    def _is_cancelled(self, request_id: Optional[str]) -> bool:
        """
        Check if a request has been cancelled.

        Args:
            request_id: The unique ID of the request to check

        Returns:
            True if the request has been cancelled, False otherwise
        """
        return self._request_manager.is_cancelled(request_id)
    
    def reset_fallback(self) -> None:
        """Reset persistent fallback LLM to use original LLM again.
        
        Call this when you want to try the original (e.g., batch) LLM again
        after rate limit/quota was exhausted and fallback was activated.
        """
        if self._active_fallback_llm is not None:
            logger.info(
                f"[{self.name}] Resetting persistent fallback {self._active_fallback_profile} "
                f"back to original LLM"
            )
            self._active_fallback_llm = None
            self._active_fallback_profile = None
            self._fallback_activated_at = None
            self._jittered_recovery_seconds = None  # Reset jitter for next fallback
            # Restore original profile info
            if self.llm_profile_info and ":fallback" in self.llm_profile_info:
                self.llm_profile_info = self.llm_profile_info.replace(":fallback", "")
    
    def _check_fallback_recovery(self) -> bool:
        """Check if fallback recovery period has elapsed and reset if so.
        
        Uses a per-instance jittered recovery time to prevent "thundering herd"
        where all agents try to recover simultaneously after a shared outage.
        
        Returns:
            True if fallback was reset (should try original LLM)
            False if still in fallback mode
        """
        if self._active_fallback_llm is None or self._fallback_activated_at is None:
            return False
        
        # Use cached jittered value, or compute it once per fallback activation
        if self._jittered_recovery_seconds is None:
            import random
            base_seconds = 3600  # Default 1 hour
            jitter_percent = 20.0  # Default ±20%
            if self.agent_config:
                base_seconds = self.agent_config.fallback_recovery_seconds
                jitter_percent = self.agent_config.fallback_recovery_jitter_percent
            
            # Apply random jitter: base ± (base * jitter_percent/100)
            jitter_range = base_seconds * (jitter_percent / 100.0)
            jitter = random.uniform(-jitter_range, jitter_range)
            self._jittered_recovery_seconds = base_seconds + jitter
            logger.debug(
                f"[{self.name}] Fallback recovery jitter: base={base_seconds}s, "
                f"jitter={jitter:+.1f}s, effective={self._jittered_recovery_seconds:.1f}s"
            )
        
        import time
        elapsed = time.time() - self._fallback_activated_at
        if elapsed >= self._jittered_recovery_seconds:
            logger.info(
                f"[{self.name}] Fallback recovery period ({self._jittered_recovery_seconds:.0f}s) elapsed. "
                f"Trying original LLM again after {int(elapsed)}s in fallback mode."
            )
            self.reset_fallback()
            return True
        
        remaining = int(self._jittered_recovery_seconds - elapsed)
        logger.debug(
            f"[{self.name}] Still in fallback mode. "
            f"Recovery in {remaining}s (elapsed: {int(elapsed)}s)"
        )
        return False

    async def append_user_message(self, request_id: str, content: str) -> bool:
        """
        Append a user message to an active request's conversation.
        Returns True if appended, False if request not found.
        """
        return await self._session_tracker.append_user_message(request_id, content)

    async def append_to_session(self, session_id: str, content: str) -> bool:
        """
        Append a user message directly to a persisted session.
        Returns True if appended, False if session not found.
        """
        return await self._session_tracker.append_to_session(session_id, content)

    async def _drain_appended_messages(self, request_id: str, messages: List[ChatMessage]) -> List[ChatMessage]:
        """
        Drain any appended messages for a request and add them to the conversation.
        Returns the updated messages list.
        """
        return await self._session_tracker.drain_appended_messages(request_id, messages)

    # ------------------------------------------------------------------
    # Tool filtering helpers
    # ------------------------------------------------------------------
    def _is_tool_allowed(self, tool_name: str, patterns: list[str]) -> bool:
        """Return True if tool_name matches any allowed pattern.

        Patterns may be:
          plugin            -> matches exact tool/plugin name
          plugin/*          -> matches all functions of plugin and plugin itself
          plugin/function   -> matches one function inside multi-tool plugin (also matches plugin server name for discovery)
          external.tool     -> exact external tool name
          external.*        -> all tools of an external server (dot form)
        Uses fnmatch for flexible wildcard support.
        """
        if not patterns:
            return True
        # Fast path: global wildcard grants all
        if '*' in patterns:
            return True
        from fnmatch import fnmatch
        for pat in patterns:
            # Normalize common shorthand
            if pat.endswith('/*'):
                base = pat[:-2]
                if tool_name == base or tool_name.startswith(base + '.'):
                    return True
            # Allow pattern "plugin" to match plugin and any function (added later) via startswith
            if '/' not in pat and '*' not in pat and '.' not in pat:
                if tool_name == pat or tool_name.startswith(pat + '.'):
                    return True
            # Support dot wildcards: external_server.*
            if pat.endswith('.*'):
                base = pat[:-2]
                if tool_name.startswith(base + '.'):
                    return True
            # Special case: If pattern is "server_name/tool_name" (specific tool pattern),
            # also match the server name itself. This allows server names to pass through
            # discovery so tools can be expanded later and filtered at the tool level.
            if '/' in pat and '*' not in pat:
                server_name = pat.split('/')[0]
                if tool_name == server_name:
                    return True
            # Direct fnmatch (covers explicit names and wildcards)
            if fnmatch(tool_name, pat):
                return True
        return False

    def _filter_usable_tools(self, tools: list[str], patterns: list[str]) -> list[str]:
        """Filter list of tools by allow patterns.

        Logs any pattern that matches nothing for visibility, but continues.
        """
        matched = []
        for t in tools:
            if self._is_tool_allowed(t, patterns):
                matched.append(t)
        # Log patterns with zero matches (diagnostic)
        unmatched = []
        if patterns and patterns != ['*'] and not (len(patterns) > 1 and '*' in patterns):
            for pat in patterns:
                if pat == '*':
                    continue
                if not any(self._is_tool_allowed(t, [pat]) for t in tools):
                    unmatched.append(pat)
        if unmatched:
            logger.debug("Agent %s tools.allowed patterns with no matches: %s", self.name, unmatched)
        return matched

    async def list_usable_tools(self) -> tuple[list[str], list[str] | None, list[str] | None]:
        """Return list of tool names this agent CAN USE (filtered by agent config).

        This is the INTERNAL interface - tools available for this agent's execution.
        Filtered by agent_config.tools.allowed patterns.

        Contrast with list_tools() which returns what this agent OFFERS to others.

        Returns:
            Tuple of:
            - List of tool server names this agent is allowed to use
            - List of allowed patterns (for fine-grained filtering after tool expansion)
            - List of blocked patterns (to be applied after tool expansion)
        """
        # Initialize MCP integration (idempotent)
        await self._mcp_integration_manager.setup_mcp_integration()

        # Use ToolDiscoveryService for clean tool filtering
        discovery_service = ToolDiscoveryService(
            agent_name=self.name,
            agent_config=self.agent_config,
            mcp_integration_manager=self._mcp_integration_manager,
            registry=self.registry if hasattr(self, 'registry') else None
        )

        return await discovery_service.discover_allowed_tools()

    async def _list_usable_tools_with_details(self, params: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Return detailed info about tools this agent CAN USE (name + description).

        Internal utility for agent subclasses (e.g., BasicAgent's list_available_tools).
        Like list_usable_tools() but includes descriptions for user-facing output.

        Args:
            params: Parameters including optional '_status' for progress reporting

        Returns:
            List of dicts with 'name' and 'description' keys
        """
        try:
            status = params.get("_status")
            all_tools: list[Dict[str, Any]] = []

            # Use agent's tools.allowed configuration for filtering
            allowed_patterns = self.agent_config.tools.allowed if self.agent_config.tools else None

            # Guard against non-iterable / MagicMock truthy values in tests
            if allowed_patterns and not isinstance(allowed_patterns, (list, tuple, set)):
                allowed_patterns = None

            if self.registry:
                server_names = list(self.registry.list())

                # Apply server-level filtering when allow patterns defined
                if allowed_patterns:
                    filtered_server_names = [s for s in server_names if self._is_tool_allowed(s, allowed_patterns)]
                else:
                    filtered_server_names = server_names

                for server_name in filtered_server_names:
                    try:
                        server = self.registry.get(server_name)
                        if not server or not hasattr(server, 'get_tools'):
                            continue
                        tools = server.get_tools()
                        for tool in tools:
                            name = tool.get("function", {}).get("name", "unknown")
                            description = tool.get("function", {}).get("description", "")
                            all_tools.append({"name": name, "description": description})
                    except Exception as e:
                        logger.debug(f"Could not get tools from server '{server_name}': {e}")

            if status:
                await status.end(f"Listed available tools ({len(all_tools)} tools)")

            return all_tools
        except Exception as e:
            logger.error(f"Failed to list tools: {e}")
            return []

    async def run_events(
        self,
        task: Union[str, ChatMessage],
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        llm_override: Optional[object] = None,
        llm_profile_info_override: Optional[str] = None,
        use_advanced_model: bool = False
    ):
        """Run the agent and yield structured events for UI streaming.

        Args:
            task: Either a string task description or a ChatMessage with multimodal content
            request_id: Optional request ID for tracking
            session_id: Optional session ID for conversation history
            llm_override: Optional LLM client to use instead of self.llm (for per-request profile overrides)
            llm_profile_info_override: Optional profile info string for status display (e.g., "turbo:openai_httpx/gpt-5-nano")
            use_advanced_model: If True and llm_override not set, use best available LLM profile
        """

        # Generate request ID if not provided
        if request_id is None:
            request_id = short_id()

        # Track if this is a newly generated session
        was_new_session = not session_id
        
        # If no session_id provided, generate one and persist empty history
        if not session_id:
            session_id = short_id()

        # CRITICAL: Set session metadata for newly generated sessions AND ensure it exists for existing ones
        # This ensures user_id is available for tool execution even in sub-agents
        if self._session_tracker:
            existing_metadata = self._session_tracker.get_session_metadata(session_id)
            if not existing_metadata:
                # Extract user_id from request_user_map (populated by API/tool execution)
                # Import at use-site to avoid circular dependency
                from agent_system.app import _request_user_map
                user_id = _request_user_map.get(request_id, "anonymous")
                
                # Use agent's default llm_profile for metadata
                effective_llm_profile = self.agent_config.default_llm_profile if self.agent_config else "normal"
                self._session_tracker.set_session_metadata(session_id, {
                    "user_id": user_id,
                    "agent_name": self.name,
                    "llm_profile": effective_llm_profile
                })
                if was_new_session:
                    logger.debug(f"Set session metadata for new session {session_id}: user_id={user_id}")
                else:
                    logger.debug(f"Set session metadata for existing session {session_id} (was missing): user_id={user_id}")

        # Handle Union[str, ChatMessage] input
        initial_message: Optional[ChatMessage] = None
        task_text: str = ""

        if isinstance(task, ChatMessage):
            # Extract task text from ChatMessage content for logging/tracking
            initial_message = task
            if isinstance(task.content, str):
                task_text = task.content
            elif isinstance(task.content, list):
                # Extract text from content items (Pydantic models, not dicts)
                text_parts = [getattr(item, "text", "") for item in task.content if hasattr(item, "type") and getattr(item, "type") == "text"]
                task_text = " ".join(text_parts) if text_parts else "[multimodal input]"
            else:
                task_text = "[multimodal input]"
        else:
            task_text = task

        # Create suffixed request IDs for coordinator and worker so their
        # status messages can be correlated separately while still linking
        # back to the base request_id. Use the Agent's centralized counter
        # to ensure monotonic, global numbering across components.
        coordinator_request_id = await self.next_internal_tool_request_id(request_id) if request_id else None
        worker_request_id = await self.next_internal_tool_request_id(request_id) if request_id else None

        # Handle use_advanced_model if no llm_override provided
        if use_advanced_model and not llm_override:
            from agent_system.llm.factory import create_llm_from_profile

            available_profiles = self.agent_config.available_llm_profiles if self.agent_config else []
            if available_profiles and len(available_profiles) > 1:
                # Use last profile (most capable)
                advanced_profile = available_profiles[-1]

                try:
                    # Get SSL verify setting
                    ssl_verify = getattr(self.system_config.network, 'ssl_verify', None)

                    # Create LLM client override using factory
                    llm_override = create_llm_from_profile(
                        config=self.system_config,
                        llm_profile=advanced_profile,
                        ssl_verify=ssl_verify,
                    )

                    # Create profile info for logging
                    profile = self.system_config.llm_system.profiles[advanced_profile]
                    model_ref = profile.model_ref
                    model_config = self.system_config.llm_system.models[model_ref]
                    llm_profile_info_override = f"{advanced_profile}:{model_config.provider}/{model_config.model}"

                    logger.info(f"use_advanced_model=True mapped to profile: {llm_profile_info_override}")

                except Exception as e:
                    logger.error(f"Failed to create LLM override for use_advanced_model: {e}")
                    # Continue with default LLM

        # Create and start status forwarder BEFORE entering status_scope context managers
        # This ensures the forwarder is subscribed to status_bus before any START events are generated
        # Fixes race condition where status_scope generates events before forwarder is ready
        status_forwarder = StatusEventForwarder()
        await status_forwarder.start_forwarding(request_id)
        
        try:
            # Pass status_scope parameters to _run_events which will open them AFTER
            # sending the 'start' event - this ensures frontend has currentRequestId
            # before any status events arrive
            async for event in self._run_events(
                task_text,
                request_id=request_id,
                session_id=session_id,
                coordinator_request_id=coordinator_request_id,
                worker_request_id=worker_request_id,
                initial_message=initial_message,
                llm_override=llm_override,
                llm_profile_info_override=llm_profile_info_override,
                status_forwarder=status_forwarder
            ):
                yield event
        except GeneratorExit:
            # Generator is being closed early - clean exit without error
            raise

    async def _initialize_request_and_conversation(
        self,
        task: str,
        request_id: str,
        session_id: str,
        initial_message: Optional[ChatMessage] = None,
        llm_override: Optional[object] = None,
        status_forwarder: Optional[StatusEventForwarder] = None
    ) -> ConversationContext:
        """Initialize request tracking and build initial conversation context.

        Phase 1 of agent execution: Setup all state needed for the LLM loop.

        Steps:
        1. Create cancellation token
        2. Register request for cancellation/appends
        3. Set context vars
        4. Initialize session storage
        5. Use pre-created status event forwarder (passed from caller)
        6. Validate LLM availability
        7. Initialize MCP integration
        8. Discover usable tools
        9. Render system prompts
        10. Load session history
        11. Execute session start hooks
        12. Build tool schemas

        Args:
            task: User task description
            request_id: Unique request identifier
            session_id: Session identifier for history
            initial_message: Optional multimodal message
            llm_override: Optional LLM client override
            status_forwarder: Pre-created status event forwarder (must be started before status_scope)

        Returns:
            ConversationContext with all initialized state

        Raises:
            RuntimeError: If no LLM is available
        """
        # Determine which LLM to use
        active_llm = llm_override if llm_override is not None else self.llm

        # Create cancellation token for the main request
        cancellation_manager = get_cancellation_manager()
        main_token = cancellation_manager.create_token(request_id)
        logger.debug("Created main cancellation token for request %s", request_id)

        # Set the ContextVar so any publish_status() calls without explicit request_id
        # will inherit the current request id. Store token for reset in finally.
        try:
            context_reset_token = current_request_id.set(request_id)
        except Exception as e:
            logger.debug(f"Failed to set current_request_id context var: {e}")
            context_reset_token = None

        # Status forwarder is passed in from caller (created before status_scope context managers)
        # This ensures forwarder is subscribed to status_bus before any START events are generated
        # Fallback to creating one here for backwards compatibility (though this defeats the purpose)
        if status_forwarder is None:
            logger.warning("status_forwarder not passed to _initialize_request_and_conversation - creating one here (may miss events)")
            status_forwarder = StatusEventForwarder()
            await status_forwarder.start_forwarding(request_id)

        # If no LLM is configured, raise error
        if active_llm is None:
            raise RuntimeError("No LLM available; agent requires an LLM to run")

        # Initialize MCP integration
        await self._mcp_integration_manager.setup_mcp_integration()

        # Get tools this agent can use (filtered by agent_config)
        # Returns tuple: (tools, allowed_patterns, blocked_patterns)
        usable_tools, allowed_patterns, blocked_patterns = await self.list_usable_tools()

        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))

        # Centralized prompt rendering (system + optional tools) using helper.
        # Initial render with step 0 (before loop starts)
        # Pass session_id to use session-scoped template vars
        system_msg, tools_msg = self._render_prompts(usable_tools, max_steps, current_step=0, session_id=session_id)

        # Initialize conversation from persisted session history
        session_msgs = self._session_tracker.get_session_messages(session_id)

        # Create initial system messages
        messages = [ChatMessage(role="system", content=system_msg)]
        if tools_msg:
            messages.append(ChatMessage(role="system", content=tools_msg))

        # Execute session start hooks for new sessions AFTER creating system messages
        # This allows hooks like markdown_formatter to inject additional system prompts
        # Check if session is empty (new session), not just if it exists (setdefault creates it above)
        is_new_session = len(session_msgs) == 0
        if is_new_session:
            # Reset loop detector for new sessions to avoid false positives
            # from previous sessions
            self._loop_detector.reset()
            logger.debug(f"Reset loop detector for new session {session_id}")
            
            modified_messages = await self._hook_manager.execute_session_start_hooks(
                session_id, request_id, messages=messages
            )
            if modified_messages is not None:
                messages = modified_messages
                logger.debug(f"Session start hooks modified messages: {len(messages)} total messages")

        # include persisted session messages
        if session_msgs:
            # Convert dicts to ChatMessage objects if needed
            for msg in session_msgs:
                if isinstance(msg, dict):
                    messages.append(ChatMessage(**msg))
                else:
                    messages.append(msg)

        # add the new user input as last message
        # Use initial_message if provided (for multimodal input), otherwise create from task
        if initial_message:
            messages.append(initial_message)
        else:
            messages.append(ChatMessage(role="user", content=sanitize_for_llm(task), timestamp=datetime.now(timezone.utc)))

        # Also include any appended messages already queued for this request
        messages = await self._session_tracker.drain_appended_messages(request_id, messages)

        # Track messages for debugging
        self._current_messages = messages.copy()

        # Build tool schemas using ToolSchemaBuilder
        # Pass allowed_patterns and blocked_patterns so they can be applied AFTER tools are expanded
        schema_builder = ToolSchemaBuilder(
            agent_name=self.name,
            mcp_integration_manager=self._mcp_integration_manager,
            server_getter_func=self._get_server_from_any_registry
        )

        tools_schema, tool_name_mapping, usable_tools, display_tools = await schema_builder.build_schemas(
            usable_tools,
            allowed_patterns=allowed_patterns,
            blocked_patterns=blocked_patterns
        )

        # Return initialized context
        return ConversationContext(
            messages=messages,
            available_tools=usable_tools,  # Use usable_tools for validation (includes both server names and tool names)
            tools_schema=tools_schema,
            tool_name_mapping=tool_name_mapping,
            max_steps=max_steps,
            main_token=main_token,
            context_reset_token=context_reset_token,
            status_forwarder=status_forwarder,
            session_id=session_id
        )

    async def _finalize_request(
        self,
        request_id: str,
        session_id: str,
        status_coordinator: StatusScope,
        status_worker: StatusScope,
        context: Optional[ConversationContext],
        messages: Optional[List[ChatMessage]],
        results: Dict[str, Any],
        step: int
    ) -> None:
        """Finalize request and clean up resources.

        Phase 3 of agent execution: Cleanup and persistence.

        Steps:
        1. Execute session end hooks
        2. Unregister cancellation token
        3. Clean up request tracking
        4. Persist session messages (conversation history only)
        5. Shutdown MCP integration
        6. Reset context vars
        7. Publish final status events
        8. Stop status forwarding
        9. Yield final pending status events

        Args:
            request_id: Request identifier
            session_id: Session identifier
            status_coordinator: Coordinator status scope
            status_worker: Worker status scope
            context: Conversation context (if initialized)
            messages: Final conversation messages
            results: Execution results dictionary
            step: Final step number
        """
        # Execute session end hooks
        try:
            await self._hook_manager.execute_session_end_hooks(
                session_id, request_id, messages=messages
            )
        except Exception as e:
            logger.warning(f"Session end hooks failed: {e}", exc_info=True)

        # Clean up cancellation token
        cancellation_manager = get_cancellation_manager()
        cancellation_manager.unregister_request(request_id)

        # Clean up request tracking but preserve session data
        self._request_manager.unregister_active_request(request_id)
        logger.debug("Cleaned up request tracking for %s", request_id)

        # Release session lock BEFORE persisting (allows other requests to proceed)
        # Note: unregister_request also releases the lock, but we do it explicitly here
        # for clarity and to ensure it happens before session persistence
        sid = self._session_tracker.get_session_for_request(request_id)
        if sid:
            await self._session_tracker.release_session_lock(sid, request_id)
            logger.debug("Released session lock for %s (request %s)", sid, request_id)

        # Persist session messages and keep the request->session mapping for a while
        if sid and messages:
            try:
                # Check if ANY tool modified the session messages during this request
                # Tools can call session_tracker.set_compacted_messages() to replace the history
                compacted_msgs = self._session_tracker.get_compacted_messages(sid)
                
                if compacted_msgs is not None:
                    # A tool replaced the message history - use those messages for persistence
                    logger.debug(
                        f"Using {len(compacted_msgs)} tool-modified messages for session {sid} "
                        f"(request had {len(messages)} messages)"
                    )
                    self._session_tracker.set_session_messages(sid, compacted_msgs)
                    self._session_tracker.clear_compacted_messages(sid)
                    logger.debug("Persisted session %s with %d modified messages", sid, len(compacted_msgs))
                else:
                    # Normal case: no tool modified messages, persist request's messages
                    # Filter out system messages - only persist conversation history
                    conversation_msgs = [msg for msg in messages if msg.role != "system"]
                    # Update the persistent session with conversation state (no system messages)
                    self._session_tracker.set_session_messages(sid, conversation_msgs.copy())
                    logger.debug("Persisted session %s with %d conversation messages", sid, len(conversation_msgs))
                
                # CRITICAL: Also save to disk at end of request
                # This ensures the session is saved even if no SSE client is connected
                # (e.g., browser disconnected during background job execution)
                if self._session_service:
                    session_meta = self._session_tracker.get_session_metadata(sid)
                    if session_meta:
                        save_user_id = session_meta.get("user_id", "anonymous")
                        save_agent_name = session_meta.get("agent_name", self.name)
                        save_llm_profile = session_meta.get("llm_profile", self.agent_config.default_llm_profile)
                        
                        await self._session_service.save_session(
                            agent=self,
                            user_id=save_user_id,
                            session_id=sid,
                            agent_name=save_agent_name,
                            llm_profile=save_llm_profile,
                            was_new_session=False
                        )
                        logger.debug(f"Saved session {sid} to disk at end of request")
                
                # Keep the request->session mapping (don't pop it immediately)
                # This allows append requests that arrive shortly after completion to find the session
            except Exception as e:
                logger.warning(f"Failed to persist session {sid}: {e}", exc_info=True)

        # Clean up MCP integration if we initialized it locally
        await self._mcp_integration_manager.shutdown()

        # Reset the current_request_id ContextVar so it doesn't leak to other tasks
        if context and context.context_reset_token is not None:
            try:
                current_request_id.reset(context.context_reset_token)
            except Exception:
                pass

        # Signal completion using status contexts
        # Note: step is already 1-indexed (extracted from events that use step+1)
        final_msg = "completed" if not results.get('errors') else "completed with errors"
        step_display = step if step > 0 else 1  # Ensure at least 1 step shown

        if results.get('errors'):
            await status_worker.error(final_msg, meta={"summary": results.get('summary')})
            await status_coordinator.error(f"{final_msg} ({step_display} steps)", meta={"summary": results.get('summary')})
        else:
            await status_worker.end(final_msg, meta={"summary": results.get('summary')})
            await status_coordinator.end(f"{final_msg} ({step_display} steps)", meta={"summary": results.get('summary')})

        # Give a small delay to allow final status events to be processed by the forwarding task
        await asyncio.sleep(0.01)

        # Clean up status forwarding task AFTER publishing final status
        if context and context.status_forwarder:
            await context.status_forwarder.stop_forwarding()

    async def _call_llm_with_streaming(
        self,
        llm: Any,
        messages: List[ChatMessage],
        tools_schema: List[Dict[str, Any]],
        cancellation_token: CancellationToken,
        step: int,
        yield_pending_status_fn,
        status_scope: Optional[StatusScope] = None
    ):
        """Call LLM with streaming support and interleaved status events.

        This method uses chat_tools_streaming() when available, yielding token deltas
        and checking status events between chunks. For non-streaming LLMs, falls back
        to regular chat_tools() with periodic status polling.

        Args:
            llm: LLM client instance
            messages: Conversation messages
            tools_schema: Available tools schema
            cancellation_token: Cancellation token for interruption
            step: Current step number
            yield_pending_status_fn: Function that yields pending status events
            status_scope: Optional status scope for LLM to report progress (batch status, etc.)

        Yields:
            - {"type": "thinking_delta", "step": int, "delta": str, "accumulated": str}
            - {"type": "status", ...}
            - {"type": "thinking_complete", "assistant": {...}}
        """
        if llm.supports_streaming():
            # Streaming LLM: zero-overhead real-time tokens
            accumulated_content = []
            final_assistant = None
            final_usage = None  # Store usage data from final chunk

            async for chunk in llm.chat_tools_streaming(
                messages, tools_schema, 
                cancellation_token=cancellation_token,
                status_scope=status_scope
            ):
                chunk_type = chunk.get("type")

                if chunk_type == "thinking_delta":
                    # Gemini reasoning/thinking tokens (not content)
                    yield {"type": "reasoning_delta", "step": step + 1, "delta": chunk["delta"]}
                    
                    # Check status events after each token (zero overhead)
                    for status_event in yield_pending_status_fn():
                        yield status_event

                elif chunk_type == "content_delta":
                    # Yield token delta for real-time display
                    yield {"type": "thinking_delta", "step": step + 1, "delta": chunk["delta"], "accumulated": chunk["accumulated"]}
                    accumulated_content.append(chunk["delta"])

                    # Check status events after each token (zero overhead)
                    for status_event in yield_pending_status_fn():
                        yield status_event

                elif chunk_type == "tool_call_delta":
                    # Tool calls are accumulated server-side, we can skip yielding deltas for now
                    # Future: could yield tool_call_delta events for UI to show "Calling get_weather..."
                    # Still yield status events to prevent delays
                    for status_event in yield_pending_status_fn():
                        yield status_event

                elif chunk_type == "final":
                    final_assistant = chunk["assistant"]
                    # Preserve usage data from final chunk
                    if "usage" in chunk:
                        final_usage = chunk["usage"]

            # Yield any remaining status events after streaming completes
            for status_event in yield_pending_status_fn():
                yield status_event

            # Yield final response with usage data
            if final_assistant:
                result = {"type": "thinking_complete", "step": step + 1, "assistant": final_assistant}
                if final_usage:
                    result["usage"] = final_usage
                yield result
            else:
                yield {"type": "thinking_complete", "step": step + 1, "assistant": {"role": "assistant", "content": "".join(accumulated_content)}}

        else:
            # Non-streaming LLM: Use polling with 100ms intervals
            # Create task for LLM call
            llm_task = asyncio.create_task(llm.chat_tools(
                messages, tools_schema, 
                cancellation_token=cancellation_token,
                status_scope=status_scope
            ))

            # Poll for status events while waiting
            # For batch mode: The batch queue manager has its own timeout (max_wait_hours in llm.yaml)
            # so we don't need an agent-side timeout. We use a very high limit (24h) as safety net.
            # For sync mode: The HTTPX client has its own request_timeout.
            # The agent-side limit is just a safety net for truly stuck calls.
            max_llm_iterations = self.timeouts.llm_task_max_iterations if self.timeouts else 864000  # 24h default
            llm_iteration_count = 0
            # Calculate heartbeat interval in iterations (config is in seconds, we poll every 0.1s)
            heartbeat_interval_seconds = self.system_config.status.llm_heartbeat_interval
            heartbeat_interval = int(heartbeat_interval_seconds / 0.1)  # Convert seconds to iterations
            
            while not llm_task.done():
                llm_iteration_count += 1
                if llm_iteration_count > max_llm_iterations:
                    timeout_seconds = max_llm_iterations * 0.1
                    logger.error(
                        "LLM task polling exceeded max iterations (%d = %.0fs), forcing exit",
                        max_llm_iterations, timeout_seconds
                    )
                    llm_task.cancel()
                    await asyncio.sleep(0.1)
                    # Raise timeout error so it can be caught and handled appropriately
                    # This is different from CancelledError (user cancellation)
                    raise asyncio.TimeoutError(
                        f"LLM task timed out after {timeout_seconds:.0f} seconds "
                        f"(max_iterations={max_llm_iterations})"
                    )

                # Check for status events
                for status_event in yield_pending_status_fn():
                    yield status_event

                # Send heartbeat event periodically to prevent SSE connection timeout
                # This keeps the connection alive during long LLM calls (30+ seconds)
                if llm_iteration_count % heartbeat_interval == 0:
                    yield {
                        "type": "heartbeat",
                        "step": step + 1,
                        "timestamp": asyncio.get_event_loop().time()
                    }

                # Wait 100ms before next poll
                try:
                    await asyncio.wait_for(asyncio.shield(llm_task), timeout=0.1)
                except asyncio.TimeoutError:
                    pass  # Continue polling

            # Get result
            llm_out = await llm_task
            result = {"type": "thinking_complete", "step": step + 1, "assistant": llm_out.get("assistant", {})}
            # Preserve usage data if present
            if "usage" in llm_out:
                result["usage"] = llm_out["usage"]
            yield result

    async def _execute_llm_loop(
        self,
        context: ConversationContext,
        request_id: str,
        session_id: str,
        status_coordinator: StatusScope,
        status_worker: StatusScope,
        llm_override: Optional[object] = None,
        llm_profile_info_override: Optional[str] = None
    ):
        """Execute the main LLM conversation loop with tool execution.

        Phase 2 of agent execution: Iterative LLM calls with tool execution.

        Loops up to max_steps:
        1. Check cancellation
        2. Drain appended messages
        3. Call LLM with tools
        4. Handle thinking/status events
        5. Execute tool calls (if any)
        6. Check loop guards (empty responses, no tool calls)
        7. Update messages with results

        Args:
            context: Conversation context with messages and tools
            request_id: Request identifier
            session_id: Session identifier
            status_coordinator: Coordinator status scope
            status_worker: Worker status scope
            llm_override: Optional LLM client override
            llm_profile_info_override: Optional profile info for status

        Yields:
            Dict events: heartbeat, thinking, status, tool_*, final, error, cancelled

        Returns:
            Tuple of (messages, results, step) after loop completion
        """
        # Determine which LLM to use
        active_llm = llm_override if llm_override is not None else self.llm

        # Extract from context
        messages = context.messages
        tools_schema = context.tools_schema
        tool_name_mapping = context.tool_name_mapping
        max_steps = context.max_steps
        main_token = context.main_token

        # Initialize results
        results: Dict[str, Any] = {"task": "", "calls": []}

        # Add safeguards against infinite loops
        consecutive_no_tool_calls = 0
        consecutive_empty_responses = 0
        max_consecutive_no_tools = 3  # Break after 3 consecutive responses without tool calls
        max_consecutive_empty = 2    # Break after 2 consecutive empty responses

        # Helper function to yield any pending status events from per-request forwarder
        def yield_pending_status_events():
            for event in context.status_forwarder.get_pending_events():
                yield event

        for step in range(max_steps):
            # Drain any appended user messages before each step
            messages = await self._drain_appended_messages(request_id, messages)
            # Sync context.messages after draining
            context.messages = messages

            # Check for cancellation at the start of each step
            if self._is_cancelled(request_id):
                logger.info("Request %s cancelled at step %d", request_id, step + 1)
                # Signal cancellation using status contexts FIRST (so events are queued)
                await status_worker.error(f"cancelled at step {step + 1}",
                                      meta={"step": step + 1, "reason": "cancelled"})
                await status_coordinator.error(f"cancelled at step {step + 1}",
                                            meta={"step": step + 1, "reason": "cancelled"})
                # Give status events a moment to be captured by forwarder
                await asyncio.sleep(0.01)
                # Yield all pending status events before cancelled event
                for status_event in context.status_forwarder.get_pending_events():
                    yield status_event
                # Now yield the cancelled event
                yield {"type": "cancelled", "request_id": request_id, "step": step + 1}
                return

            # Progress heartbeat using status_coordinator
            await status_coordinator.progress(
                f"step {step + 1}/{max_steps}",
                meta={"step": step + 1, "max_steps": max_steps}
            )

            # Yield a heartbeat for UI responsiveness (non-blocking)
            yield {"type": "heartbeat", "step": step + 1, "max_steps": max_steps}

            # Yield pending status events before LLM call
            for status_event in yield_pending_status_events():
                yield status_event

            # Signal LLM call start
            if llm_profile_info_override:
                llm_display = f" ({llm_profile_info_override})"
            else:
                llm_display = f" ({self.llm_profile_info})" if self.llm_profile_info else " (unknown LLM)"
            await status_worker.progress(f"Calling LLM{llm_display}", meta={"step": step + 1})

            # Update system message with current step number
            # Pass session_id to use session-scoped template vars
            updated_system_msg, _ = self._render_prompts(
                context.available_tools, max_steps, current_step=step + 1, session_id=context.session_id
            )
            messages[0] = ChatMessage(role="system", content=updated_system_msg)

            # Emit thinking event before LLM call (for UI step display)
            yield {"type": "thinking", "step": step + 1}

            # Execute pre-LLM hooks with real-time status streaming
            # NOTE: Hooks execute synchronously from this generator's perspective,
            # so we use asyncio.create_task() + polling to stream status events
            # during hook execution. This pattern is only needed for hooks that
            # emit status messages (currently only context_summarizer).
            # Other hook types (post_llm, session_start, session_end) don't need
            # this pattern as they don't emit status events.
            try:
                # Create async task for hook execution
                hook_task = asyncio.create_task(
                    self._hook_manager.execute_pre_llm_hooks(
                        messages=messages,
                        step=step,
                        request_id=request_id,
                        session_id=session_id,
                        llm=active_llm,
                        cancellation_token=main_token
                    )
                )

                # Stream status events while hook is running
                # NOTE: No hard timeout - hooks can run as long as needed (e.g., context_summarizer may take 10+ minutes)
                # Hooks are expected to implement their own timeouts if needed
                while not hook_task.done():
                    for status_event in yield_pending_status_events():
                        yield status_event
                    await asyncio.sleep(0.1)  # Poll every 100ms

                # Get hook result
                modified_messages = await hook_task

                # Yield any final status events from hook execution
                for status_event in yield_pending_status_events():
                    yield status_event

                # Check if hook set compacted_messages (e.g., context_summarizer)
                compacted_messages = self._session_tracker.get_compacted_messages(session_id)
                
                if compacted_messages is not None:
                    # Hook used compaction mechanism - reconstruct message list
                    logger.debug(f"Pre-LLM hook set compacted_messages with {len(compacted_messages)} messages")
                    
                    # Build: [system] + compacted + [current_step_messages]
                    reconstructed = []
                    # Check first message for system role (handle both dict and ChatMessage)
                    if messages:
                        first_msg = messages[0]
                        first_role = first_msg.get("role") if isinstance(first_msg, dict) else getattr(first_msg, "role", None)
                        if first_role == "system":
                            reconstructed.append(first_msg)
                    
                    reconstructed.extend(compacted_messages)
                    
                    messages = reconstructed
                    context.messages = reconstructed
                    
                    # Clear compacted_messages for next iteration
                    self._session_tracker.clear_compacted_messages(session_id)
                    
                elif modified_messages is not None:
                    # Hook returned modified messages directly (old mechanism)
                    messages = modified_messages
                    context.messages = modified_messages
            except Exception as e:
                logger.warning(f"Pre-LLM hooks failed: {e}", exc_info=True)

            # LLM call with streaming support and fallback handling
            llm_out = None
            
            # Check if fallback recovery period has elapsed - try original LLM again
            self._check_fallback_recovery()
            
            # Check if we have an active persistent fallback (from previous rate limit/quota exhaustion)
            if self._active_fallback_llm is not None:
                logger.info(f"[{self.name}] Using persistent fallback LLM: {self._active_fallback_profile}")
                current_llm = self._active_fallback_llm
                fallback_index = 0  # Already at fallback, no further fallbacks available
                fallback_profiles = []  # No more fallbacks to try
            else:
                current_llm = active_llm
                fallback_index = 0
                fallback_profiles = self.agent_config.fallback_profiles if self.agent_config else []
            
            while True:  # Retry loop for fallbacks
                try:
                    async for event in self._call_llm_with_streaming(
                        llm=current_llm,
                        messages=messages,
                        tools_schema=tools_schema,
                        cancellation_token=main_token,
                        step=step,
                        yield_pending_status_fn=yield_pending_status_events,
                        status_scope=status_worker
                    ):
                        event_type = event.get("type")

                        if event_type == "reasoning_delta":
                            # Yield Gemini reasoning/thinking tokens to WebUI
                            yield event
                        elif event_type == "thinking_delta":
                            # Yield real-time token deltas to WebUI
                            yield event
                        elif event_type == "status":
                            # Yield interleaved status events
                            yield event
                        elif event_type == "thinking_complete":
                            # CRITICAL: Make a deep copy of assistant dict to prevent
                            # format_output hooks in app.py from modifying the stored message!
                            # app.py formats events for display, but we need raw Markdown in messages
                            import copy
                            llm_out = {"assistant": copy.deepcopy(event["assistant"])}
                            # Preserve usage data if present in event
                            if "usage" in event:
                                llm_out["usage"] = event["usage"]
                            # Yield thinking_complete to WebUI for final formatting
                            yield event
                    # Success - exit retry loop
                    break
                    
                except (LLMRateLimitError, LLMQuotaExhaustedError) as e:
                    is_quota_exhausted = isinstance(e, LLMQuotaExhaustedError)
                    
                    # Try fallback profiles
                    if fallback_index < len(fallback_profiles):
                        fallback_profile = fallback_profiles[fallback_index]
                        fallback_index += 1
                        
                        logger.warning(
                            f"[{self.name}] {e.__class__.__name__}: {e}. "
                            f"Switching to fallback profile: {fallback_profile}"
                        )
                        await status_worker.progress(
                            f"{'Quota exhausted' if is_quota_exhausted else 'Rate limit hit'}, switching to {fallback_profile}",
                            meta={"step": step + 1, "fallback": fallback_profile}
                        )
                        
                        fallback_llm = self._create_fallback_llm(fallback_profile)
                        if fallback_llm:
                            current_llm = fallback_llm
                            # Update profile info for status display
                            self.llm_profile_info = f"{fallback_profile}:fallback"
                            
                            # Make fallback PERSISTENT for both rate limit and quota exhausted
                            # Rate limit: temporary, will try original again after recovery period
                            # Quota exhausted: permanent until recovery period (usually longer)
                            import time
                            self._active_fallback_llm = fallback_llm
                            self._active_fallback_profile = fallback_profile
                            self._fallback_activated_at = time.time()
                            
                            recovery_seconds = 3600  # Default
                            if self.agent_config:
                                recovery_seconds = self.agent_config.fallback_recovery_seconds
                            
                            reason = "quota exhausted" if is_quota_exhausted else "rate limit hit"
                            logger.info(
                                f"[{self.name}] {reason.title()} - fallback to {fallback_profile} "
                                f"is now PERSISTENT. Will try original again in {recovery_seconds}s"
                            )
                            await status_worker.progress(
                                f"Switched to {fallback_profile} ({reason}, retry in {recovery_seconds//60}min)",
                                meta={"step": step + 1, "fallback": fallback_profile, "persistent": True, "recovery_seconds": recovery_seconds}
                            )
                            
                            continue  # Retry with fallback
                        else:
                            logger.error(f"[{self.name}] Failed to create fallback LLM, giving up")
                            raise
                    else:
                        # No more fallbacks available
                        logger.error(f"[{self.name}] No fallback profiles available, rate limit exceeded")
                        raise
                
                except asyncio.CancelledError:
                    # Streaming was cancelled - send proper status events and cancelled event
                    logger.info(f"Request {request_id} cancelled during LLM call at step {step + 1}")
                    await status_worker.error(f"cancelled at step {step + 1}",
                                          meta={"step": step + 1, "reason": "cancelled"})
                    await status_coordinator.error(f"cancelled at step {step + 1}",
                                                meta={"step": step + 1, "reason": "cancelled"})
                    await asyncio.sleep(0.01)
                    for status_event in context.status_forwarder.get_pending_events():
                        yield status_event
                    yield {"type": "cancelled", "request_id": request_id, "step": step + 1}
                    return
                
                except asyncio.TimeoutError as e:
                    # LLM task timed out (e.g., batch job taking too long)
                    # This is different from user cancellation - report as timeout error
                    timeout_msg = str(e) if str(e) else "LLM request timed out"
                    logger.error(f"Request {request_id} timed out during LLM call at step {step + 1}: {timeout_msg}")
                    await status_worker.error(f"timeout at step {step + 1}: {timeout_msg}",
                                          meta={"step": step + 1, "reason": "timeout"})
                    await status_coordinator.error(f"timeout at step {step + 1}",
                                                meta={"step": step + 1, "reason": "timeout"})
                    await asyncio.sleep(0.01)
                    for status_event in context.status_forwarder.get_pending_events():
                        yield status_event
                    yield {"type": "error", "request_id": request_id, "step": step + 1, 
                           "message": timeout_msg, "error_type": "timeout"}
                    return

            # Signal LLM call completion
            await status_worker.progress("LLM (chat) response received", meta={"step": step + 1})

            assistant = llm_out.get("assistant", {}) if llm_out else {}

            # Check if LLM returned an error response
            if "error" in assistant:
                error_info = assistant["error"]
                error_msg = error_info.get("message", "Unknown LLM error")
                error_type = error_info.get("type", "unknown")
                logger.warning(f"LLM returned error: {error_type} - {error_msg}")
                yield {"type": "error", "message": error_msg, "error_type": error_type}
                return

            content = assistant.get("content")
            tool_calls = assistant.get("tool_calls", [])

            # Create assistant message and add it BEFORE post_llm hooks
            # so message debugger can capture the complete conversation
            assistant_msg = ChatMessage(
                role="assistant",
                content=content or "",
                tool_calls=tool_calls if tool_calls else None,
                timestamp=datetime.now(timezone.utc)
            )
            messages.append(assistant_msg)
            # Only append to context.messages if it's a different list
            if context.messages is not messages:
                context.messages.append(assistant_msg)

            # Execute post-LLM hooks to transform the response
            # NOTE: Using same polling pattern as pre_llm_hooks to support
            # future hooks that may emit status messages during execution.
            try:
                # Create async task for hook execution
                hook_task = asyncio.create_task(
                    self._hook_manager.execute_post_llm_hooks(
                        messages=messages,
                        llm_response=llm_out,  # Pass full LLM response including usage data
                        step=step,
                        request_id=request_id,
                        session_id=session_id,
                        llm=active_llm
                    )
                )

                # Stream status events while hook is running
                # NOTE: No hard timeout - hooks can run as long as needed
                # Hooks are expected to implement their own timeouts if needed
                while not hook_task.done():
                    for status_event in yield_pending_status_events():
                        yield status_event
                    await asyncio.sleep(0.1)  # Poll every 100ms

                # Get hook result
                modified_response, hook_metadata = await hook_task

                # Yield any final status events
                for status_event in yield_pending_status_events():
                    yield status_event

                if modified_response is not None:
                    # Extract assistant data from modified response
                    modified_assistant = modified_response.get("assistant", {})
                    new_content = modified_assistant.get("content")
                    new_tool_calls = modified_assistant.get("tool_calls")

                    # Update content and tool_calls if hooks modified them
                    if new_content is not None:
                        content = new_content
                        assistant_msg.content = content or ""
                    if new_tool_calls is not None:
                        tool_calls = new_tool_calls
                        assistant_msg.tool_calls = tool_calls if tool_calls else None

                    # Set content_format from hook metadata (e.g., 'html', 'markdown', 'text')
                    if "content_format" in hook_metadata:
                        assistant_msg.content_format = hook_metadata["content_format"]
            except Exception as e:
                logger.warning(f"Post-LLM hooks failed: {e}", exc_info=True)

            # Format content for display (markdown -> HTML for web UI)
            formatted_content = content
            content_format = getattr(assistant_msg, 'content_format', 'text')  # Default to 'text' if not set by hooks
            try:
                if content and self._hook_manager:
                    formatted_content, content_format = await self._hook_manager.execute_format_output_hooks(
                        output=content,
                        request_id=request_id or "unknown",
                        session_id=session_id or "unknown",
                        output_format='html'
                    )
            except Exception as e:
                logger.warning(f"Failed to format content for display: {e}", exc_info=True)
                # Keep original content on error
                formatted_content = content

            # Emit thinking event with LLM response (for UI to show assistant reasoning)
            yield {"type": "thinking", "step": step + 1, "assistant": {"content": formatted_content, "tool_calls": tool_calls, "content_format": content_format}}

            # Also emit simplified thinking event if we have content and no tool calls (final answer)
            if content and not tool_calls:
                yield {"type": "thinking", "content": formatted_content, "content_format": content_format}

            # Yield pending status events after LLM response
            for status_event in yield_pending_status_events():
                yield status_event

            # Infinite loop guard: Track consecutive empty responses FIRST
            # (before checking tool calls, to catch completely empty responses)
            if not content and not tool_calls:
                consecutive_empty_responses += 1
                
                # Remove the empty assistant message we just added - it serves no purpose
                # and will just accumulate in the session causing validation issues
                if messages and messages[-1].role == "assistant" and not messages[-1].content and not messages[-1].tool_calls:
                    messages.pop()
                    if context.messages is not messages and context.messages and context.messages[-1].role == "assistant":
                        context.messages.pop()
                    logger.debug("Removed empty assistant message from conversation history")
                
                if consecutive_empty_responses >= max_consecutive_empty:
                    logger.warning(f"Empty response #{consecutive_empty_responses}: Injecting 'Continue' user message to prompt LLM")
                    # Instead of breaking, inject a "Continue" user message to nudge the LLM
                    # This mimics the user typing "weiter" or "continue" manually
                    continue_message = ChatMessage(role="user", content="Continue with your task.")
                    messages.append(continue_message)
                    # Don't reset counter - if we get another empty response after this, we'll inject again
                    # But cap at a reasonable limit to prevent truly infinite loops
                    if consecutive_empty_responses >= max_consecutive_empty + 3:
                        logger.warning(f"Breaking loop: {consecutive_empty_responses} consecutive empty responses even after 'Continue' prompts")
                        error_msg = "LLM returned empty responses repeatedly despite continue prompts"
                        results.setdefault("errors", []).append(error_msg)
                        yield {"type": "error", "message": error_msg}
                        return
                    # Continue to next iteration with the injected message
                    continue
            else:
                consecutive_empty_responses = 0  # Reset counter
                
                # Persist session after each valid (non-empty) LLM response to preserve progress on cancellation
                # NOTE: We only persist non-empty responses to avoid accumulating useless empty messages
                try:
                    conversation_msgs = [msg for msg in messages if msg.role != "system"]
                    self._session_tracker.set_session_messages(session_id, conversation_msgs.copy())
                    logger.debug(f"Persisted session {session_id} after LLM response (step {step}) with {len(conversation_msgs)} messages")
                except Exception as e:
                    logger.warning(f"Failed to persist session {session_id} after LLM response: {e}", exc_info=True)

            # Check if we have tool calls to execute
            if tool_calls:
                # Reset no-tool-calls counter
                consecutive_no_tool_calls = 0
                
                # ===== TOOL CALL LOOP DETECTION =====
                # Check for repeated tool call patterns that indicate the agent is stuck
                loop_result = self._loop_detector.record_batch_and_check(tool_calls, step)
                
                pending_intervention_msg: Optional[ChatMessage] = None

                if loop_result.is_loop:
                    # Log the detection
                    logger.warning(
                        f"[{self.name}] Tool loop detected at step {step}: "
                        f"type={loop_result.loop_type}, tool={loop_result.tool_name}, "
                        f"count={loop_result.repetition_count}"
                    )
                    
                    # Prepare intervention message to nudge the LLM
                    pending_intervention_msg = ChatMessage(
                        role="user",
                        content=loop_result.intervention,
                        timestamp=datetime.now(timezone.utc)
                    )
                    
                    # Emit status event for visibility
                    await status_worker.progress(
                        f"Loop detected: {loop_result.tool_name} ({loop_result.repetition_count}x)",
                        meta={"step": step + 1, "loop_type": loop_result.loop_type}
                    )
                    
                    # If tool should be blocked, filter it out
                    if loop_result.should_block_tool:
                        blocked_tools = loop_result.blocked_tools
                        original_count = len(tool_calls)
                        tool_calls = [
                            tc for tc in tool_calls 
                            if tc.get("function", {}).get("name") not in blocked_tools
                        ]
                        if len(tool_calls) < original_count:
                            logger.warning(
                                f"[{self.name}] Blocked {original_count - len(tool_calls)} tool calls "
                                f"due to loop detection. Blocked tools: {blocked_tools}"
                            )
                            # If all tools were blocked, continue to next iteration
                            # The intervention message will prompt the LLM to try something else
                            if not tool_calls:
                                # No tool results will follow, so inject now to keep the loop warning.
                                messages.append(pending_intervention_msg)
                                context.messages = messages
                                continue
                
                # Signal tool execution start
                await status_worker.progress(f"Executing Tools ({len(tool_calls)} total)", meta={"step": step + 1})

                # Assistant message with tool calls was already added above before post_llm hooks

                # Update tracked messages
                self._current_messages = messages.copy()

                # Execute all tools using streaming to get real-time status events from sub-agents
                tool_messages = []
                tool_results = []

                # Extract user_id from session metadata for multi-user tool isolation
                user_id: Optional[str] = None
                if self._session_tracker:
                    session_meta = self._session_tracker.get_session_metadata(session_id)
                    if session_meta:
                        user_id = session_meta.get("user_id")
                        logger.debug(f"[TOOL_EXEC] Extracted user_id='{user_id}' from session_metadata for session {session_id}")
                    else:
                        logger.warning(f"[TOOL_EXEC] No session_metadata found for session {session_id}")
                else:
                    logger.warning("[TOOL_EXEC] No _session_tracker available")

                # CRITICAL: Pass per-request status_forwarder as parameter to avoid race conditions
                # when multiple requests share the same agent instance (e.g., parent + sub-agent)
                # DO NOT set self._tool_execution_manager._status_forwarder - that causes race conditions!

                async for item in self._tool_execution_manager.execute_tools_streaming(
                    tool_calls=tool_calls,
                    tool_name_mapping=tool_name_mapping,
                    available_tools=context.available_tools,
                    step=step,
                    request_id=request_id,
                    session_id=session_id,
                    user_id=user_id,
                    status_forwarder=context.status_forwarder
                ):
                    if item.get("type") == "status":
                        # Yield status events in real-time during tool execution
                        yield item["event"]
                    elif item.get("type") == "tool_events":
                        # Yield tool execution events
                        for event in item["events"]:
                            yield event
                    elif item.get("type") == "complete":
                        # Store final results
                        tool_messages = item["messages"]
                        tool_results = item["results"]

                # Add tool results to the results dictionary
                results["calls"].extend(tool_results)

                # CRITICAL: Check if ANY tool modified the session messages during execution.
                # Tools can set modified messages via session_tracker.set_compacted_messages()
                # This is a generic mechanism - any tool can use it to replace the message history.
                #
                # Examples of tools that use this:
                # - context_engineer.compact() - Compresses messages to save tokens
                # - context_summarizer - Summarizes old conversation history
                # - Any custom tool that wants to modify conversation state
                #
                # The session tracker stores conversation messages WITHOUT system message.
                compacted_messages = self._session_tracker.get_compacted_messages(session_id)
                
                if compacted_messages is not None:
                    # A tool replaced the message history - use the new messages
                    local_conversation = [m for m in messages if m.role != "system"]
                    logger.info(
                        f"Tool modified session: {len(compacted_messages)} new msgs replacing "
                        f"{len(local_conversation)} old msgs. Reconstructing conversation."
                    )
                    
                    # Reconstruct messages: system + modified conversation + tool results
                    system_msg = messages[0] if messages and messages[0].role == "system" else None
                    if system_msg:
                        messages = [system_msg] + list(compacted_messages) + tool_messages
                    else:
                        messages = list(compacted_messages) + tool_messages
                    
                    # Clear compacted messages - they've been applied
                    self._session_tracker.clear_compacted_messages(session_id)
                    
                    # Update session storage to match
                    self._session_tracker.set_session_messages(session_id, list(compacted_messages))
                else:
                    # Normal case: no tool modified messages, just extend with tool results
                    messages.extend(tool_messages)

                if pending_intervention_msg is not None:
                    messages.append(pending_intervention_msg)

                # Sync context.messages with the updated messages list
                context.messages = messages

                # Update tracked messages after tool execution
                self._current_messages = messages.copy()

                # Persist session after complete turn (tool calls + results processed)
                # This avoids orphaned tool calls that would occur if we saved after each tool execution
                try:
                    conversation_msgs = [msg for msg in messages if msg.role != "system"]
                    self._session_tracker.set_session_messages(session_id, conversation_msgs.copy())
                    logger.debug(f"Persisted session {session_id} to in-memory tracker after completing turn (step {step}) with {len(conversation_msgs)} messages")
                    
                    # CRITICAL: Also save to disk via SessionService after complete turn
                    # This ensures progress is preserved after all tools from one LLM request are processed
                    # Avoids orphaned tool calls (assistant calls tool, but response not yet processed)
                    if self._session_service:
                        session_meta = self._session_tracker.get_session_metadata(session_id)
                        if session_meta:
                            save_user_id = session_meta.get("user_id", "anonymous")
                            save_agent_name = session_meta.get("agent_name", self.name)
                            save_llm_profile = session_meta.get("llm_profile", self.agent_config.default_llm_profile)
                            
                            await self._session_service.save_session(
                                agent=self,
                                user_id=save_user_id,
                                session_id=session_id,
                                agent_name=save_agent_name,
                                llm_profile=save_llm_profile,
                                was_new_session=False  # Always update for intermediate saves
                            )
                            logger.debug(f"Saved session {session_id} to disk after completing turn with all tool results")
                        else:
                            logger.warning(f"No session metadata found for {session_id}, skipping disk save")
                    else:
                        logger.debug("No session_service available, skipping disk save")
                except Exception as e:
                    logger.warning(f"Failed to persist session {session_id} after completing turn: {e}", exc_info=True)

                # Yield pending status events after tool execution
                for status_event in yield_pending_status_events():
                    yield status_event

                # Continue to next iteration to let LLM respond to tool results
                continue

            # No tool calls - check if we should treat this as the final answer
            # Track consecutive responses without tool calls
            consecutive_no_tool_calls += 1

            # === CONTINUATION HOOK SIGNAL ===
            # A post_llm_call hook (e.g. agent_continuation) may set
            # metadata["continue"] = True to prevent treating a text-only
            # response as the final answer.  This allows autonomous agents
            # to keep working when they emit intermediate status reports.
            if hook_metadata.get("continue") and content and content.strip():
                cont_count = hook_metadata.get("continuation_count", "?")
                cont_reason = hook_metadata.get("continuation_reason", "hook signal")
                logger.info(
                    f"[{self.name}] Continuation #{cont_count} at step {step}: {cont_reason}"
                )
                continuation_msg = ChatMessage(
                    role="user",
                    content=hook_metadata.get(
                        "continue_message",
                        "Continue with your task.",
                    ),
                    timestamp=datetime.now(timezone.utc),
                )
                messages.append(continuation_msg)
                context.messages = messages
                await status_worker.progress(
                    f"Auto-continue #{cont_count}: {cont_reason}",
                    meta={"step": step + 1, "continuation": True},
                )
                consecutive_no_tool_calls = 0  # Reset — hook evaluated this
                continue
            
            # If we have content AND it's not just whitespace, treat as final answer
            if content and content.strip():
                # Assistant message was already added above before post_llm hooks
                results["summary"] = content
                # Update tracked messages with final response
                self._current_messages = messages.copy()

                # Use the already formatted content from above
                final_event = {"type": "final", "summary": formatted_content, "content_format": content_format}
                # Include usage data if available from last LLM call
                if llm_out and "usage" in llm_out:
                    final_event["usage"] = llm_out["usage"]
                yield final_event
                return
            
            # No tool calls AND (no content OR empty content)
            # Check consecutive no-tool-calls limit to avoid infinite loop
            if consecutive_no_tool_calls >= max_consecutive_no_tools:
                logger.warning(f"Breaking loop: {consecutive_no_tool_calls} consecutive responses without tool calls (empty or no content)")
                # Treat whatever content we have as final (even if empty)
                results["summary"] = content or ""
                self._current_messages = messages.copy()
                final_event = {"type": "final", "summary": formatted_content or "", "content_format": content_format}
                if llm_out and "usage" in llm_out:
                    final_event["usage"] = llm_out["usage"]
                yield final_event
                return

            # Update tracked messages at end of each step
            self._current_messages = messages.copy()

            # Drain any final appended messages before next step
            messages = await self._drain_appended_messages(request_id, messages)
            # Sync context.messages after draining
            context.messages = messages

        # Max steps reached - warning and try to get final answer
        logger.warning(
            f"Max steps ({max_steps}) reached. Agent may not have completed the task. "
            f"Making one final LLM call to attempt completion."
        )

        # Add explicit user message requesting final answer WITHOUT tools
        final_user_message = ChatMessage(
            role="user",
            content=(
                f"You have reached the maximum number of steps ({max_steps}). "
                "Please provide your final answer NOW based on the information you have gathered. "
                "Do NOT use any tools in this response - just give me your best answer or summary of what you've accomplished."
            ),
            timestamp=datetime.now(timezone.utc)
        )
        messages.append(final_user_message)

        # Try final call with tools still available (but instructed not to use them)
        try:
            final_llm_out = await active_llm.chat_tools(messages, tools_schema, cancellation_token=main_token)
            final_assistant = final_llm_out.get("assistant", {})
            final_content = final_assistant.get("content")
            final_tool_calls = final_assistant.get("tool_calls", [])

            if final_tool_calls:
                # Agent still wants to use tools after max_steps!
                logger.error(
                    f"Agent returned tool calls after max_steps limit! "
                    f"Tools: {[tc.get('function', {}).get('name') for tc in final_tool_calls]}. "
                    f"Increase max_steps or simplify the task."
                )
                results.setdefault("errors", []).append(
                    f"Agent needs more steps to complete task (wanted to call: "
                    f"{', '.join([tc.get('function', {}).get('name', '?') for tc in final_tool_calls])})"
                )
                yield {"type": "error", "message": f"Agent incomplete: max steps ({max_steps}) reached but still has work to do."}
                return

            if final_content:
                # Append final assistant message to conversation history
                assistant_msg = ChatMessage(role="assistant", content=final_content or "", timestamp=datetime.now(timezone.utc))
                messages.append(assistant_msg)
                context.messages.append(assistant_msg)  # Also append to context.messages
                results["summary"] = final_content
                # Update tracked messages and emit final event
                self._current_messages = messages.copy()

                # Format content for display
                formatted_final = final_content
                final_format = 'text'  # Default to 'text' if not set by hooks
                try:
                    if self._hook_manager:
                        formatted_final, final_format = await self._hook_manager.execute_format_output_hooks(
                            output=final_content,
                            request_id=request_id or "unknown",
                            session_id=session_id or "unknown",
                            output_format='html'
                        )
                except Exception as e:
                    logger.warning(f"Failed to format final content: {e}", exc_info=True)

                final_event = {"type": "final", "summary": formatted_final, "content_format": final_format}
                # Include usage data if available from final LLM call
                if final_llm_out and "usage" in final_llm_out:
                    final_event["usage"] = final_llm_out["usage"]
                yield final_event
            else:
                results.setdefault("errors", []).append("LLM planner reached max steps without final answer.")
                yield {"type": "error", "message": "LLM planner reached max steps without final answer."}
        except Exception as e:
            # Check if this is a cancellation exception in final answer
            final_error_str = str(e).lower()
            if "cancelled" in final_error_str or "timeout" in final_error_str:
                logger.info(f"Request {request_id} cancelled during final LLM call: {e}")
                # Signal cancellation using status contexts
                await status_worker.error(f"cancelled during final LLM call: {e}",
                                        meta={"step": step + 1, "reason": "cancelled"})
                await status_coordinator.error("cancelled during final LLM call",
                                             meta={"step": step + 1, "reason": "cancelled"})
                yield {"type": "cancelled", "request_id": request_id, "step": step + 1, "reason": str(e)}
                return

            logger.exception("Failed to get final answer: %s", e)
            results.setdefault("errors", []).append(f"Failed to get final answer: {e}")
            yield {"type": "error", "message": f"Failed to get final answer: {e}"}

        return

    async def _run_events(
        self,
        task: str,
        request_id: str,
        session_id: str,
        coordinator_request_id: str,
        worker_request_id: str,
        initial_message: Optional[ChatMessage] = None,
        llm_override: Optional[object] = None,
        llm_profile_info_override: Optional[str] = None,
        status_forwarder: Optional[StatusEventForwarder] = None
    ):
        """
        Core agent execution loop - orchestrates LLM conversation with tool usage.

        Now refactored into three focused phases (see REFACTORING_PLAN.md):
        1. Initialize: _initialize_request_and_conversation() - Setup and context building
        2. Execute: _execute_llm_loop() - Main LLM interaction loop with tool execution
        3. Finalize: _finalize_request() - Cleanup and persistence

        This orchestration method is now <150 LOC, delegating complex logic to focused helpers.

        Args:
            task: Text task description (may be empty if initial_message is provided)
            request_id: Request ID for tracking and cancellation
            session_id: Session ID for conversation history persistence
            coordinator_request_id: Request ID for coordinator status scope
            worker_request_id: Request ID for worker status scope
            initial_message: Optional ChatMessage with multimodal content
            llm_override: Optional LLM client override
            llm_profile_info_override: Optional profile info for status display
            status_forwarder: Pre-created status event forwarder (created before status_scope)

        Yields:
            Dict events: start, heartbeat, thinking, status, tool_*, final, error, cancelled, end
        """
        # Initialize state variables for access in finally block
        step = 0
        context = None
        messages = None
        results: Dict[str, Any] = {"task": task, "calls": []}

        # Register this request BEFORE emitting start event so appends work immediately
        request_entry = {
            "cancel": asyncio.Event(),
            "message_event": asyncio.Event(),
            "appended": []
        }
        self._request_manager.register_active_request(request_id, request_entry)
        self._session_tracker.register_request(request_id, session_id, request_entry)

        # Try to acquire session lock to prevent parallel requests on same session
        # This prevents race conditions when multiple browser tabs access the same session
        session_lock_timeout = self.timeouts.session_lock_timeout if self.timeouts else 5.0
        lock_acquired = await self._session_tracker.acquire_session_lock(session_id, request_id, timeout=session_lock_timeout)
        if not lock_acquired:
            # Another request is already processing this session
            is_locked, owner = self._session_tracker.check_session_locked(session_id)
            error_msg = f"Session {session_id} is currently locked by another request ({owner}). Please wait for that request to complete."
            logger.warning("Request %s failed to acquire lock for session %s (owner: %s)", 
                         request_id, session_id, owner)
            
            # CRITICAL: Clean up the request registration since we're aborting
            # Without this, the request stays registered as "active" forever
            self._request_manager.unregister_active_request(request_id)
            self._session_tracker.unregister_request(request_id)
            
            yield {"type": "error", "message": error_msg, "request_id": request_id}
            yield {"type": "end"}
            return

        # Emit start event BEFORE opening status_scope contexts
        # This ensures frontend has currentRequestId set before any status events arrive
        yield {"type": "start", "task": task, "request_id": request_id, "session_id": session_id}

        # Now open status_scope contexts - their START events will arrive AFTER the start event
        async with status_scope(status_bus, f"{self.name}_coordinator", coordinator_request_id) as status_coordinator, \
                   status_scope(status_bus, f"{self.name}_worker", worker_request_id) as status_worker:
            try:
                # Phase 1: Initialize request and build conversation context
                try:
                    context = await self._initialize_request_and_conversation(
                        task=task,
                        request_id=request_id,
                        session_id=session_id,
                        initial_message=initial_message,
                        llm_override=llm_override,
                        status_forwarder=status_forwarder
                    )
                except RuntimeError as e:
                    # LLM not available - emit error and end stream
                    yield {"type": "error", "message": str(e), "request_id": request_id}
                    yield {"type": "end"}
                    return

                # Helper function to yield any pending status events from per-request forwarder
                def yield_pending_status_events():
                    if context and context.status_forwarder:
                        events = context.status_forwarder.get_pending_events()
                        for event in events:
                            yield event

                # CRITICAL: Give the forwarder task CPU time to process queued events
                # During Phase 1 (synchronous initialization), the forwarder task may not
                # have had a chance to read events from its queue. This sleep allows it to catch up.
                await asyncio.sleep(0)
                
                # CRITICAL: Yield any pending status events from Phase 1 initialization
                # This ensures START events from status_scope are delivered before LLM loop
                for status_event in yield_pending_status_events():
                    yield status_event

                # Track messages from context for updates during loop
                messages = context.messages

                # Phase 2: Execute main LLM loop with tool execution
                loop_generator = self._execute_llm_loop(
                    context=context,
                    request_id=request_id,
                    session_id=session_id,
                    status_coordinator=status_coordinator,
                    status_worker=status_worker,
                    llm_override=llm_override,
                    llm_profile_info_override=llm_profile_info_override
                )

                async for event in loop_generator:
                    yield event

                    # Yield any pending status events after each main event
                    # This ensures status messages are delivered in real-time, not batched at the end
                    for status_event in yield_pending_status_events():
                        yield status_event

                    # Track messages updates from context during loop execution
                    if context:
                        messages = context.messages

                    # Track step from events that contain step info
                    # This ensures we report accurate step count in completion message
                    if "step" in event:
                        step = event.get("step", step)
                    
                    # Capture summary and errors from events
                    if event.get("type") == "final" and "summary" in event:
                        results["summary"] = event["summary"]
                    elif event.get("type") == "error":
                        results.setdefault("errors", []).append(event.get("message", "Unknown error"))

            except Exception as e:
                logger.exception("Agent execution failed with exception:")
                yield {"type": "error", "message": f"Agent execution failed: {e}"}
                
                # CRITICAL: Cancel all sub-requests when parent agent fails
                # This ensures sub-agents don't continue running when the parent has an error
                # Uses prefix matching: request_id "abc123" will cancel "abc123_sub_xxx" etc.
                cancellation_manager = get_cancellation_manager()
                cancelled_count = cancellation_manager.cancel_request(request_id)
                if cancelled_count:
                    logger.info(f"Cancelled {cancelled_count} sub-request(s) due to parent agent error")
            finally:
                # Phase 3: Finalize and cleanup
                # Note: This runs even if generator is closed early, but we can't yield in that case
                await self._finalize_request(
                    request_id=request_id,
                    session_id=session_id,
                    status_coordinator=status_coordinator,
                    status_worker=status_worker,
                    context=context,
                    messages=messages if messages else (context.messages if context else None),
                    results=results,
                    step=step
                )

            # Yield final status events and end marker
            # These won't execute if generator was closed early (GeneratorExit), which is fine
            if 'yield_pending_status_events' in locals():
                for status_event in yield_pending_status_events():
                    yield status_event

            yield {"type": "end"}


    async def shutdown(self) -> None:
        """Shutdown the agent and clean up resources"""
        logger.info("Agent shutdown initiated")

        # Shutdown MCP integration to close external server connections
        if hasattr(self, '_mcp_integration_manager') and self._mcp_integration_manager:
            try:
                if self._mcp_integration_manager.mcp_integration:
                    await self._mcp_integration_manager.mcp_integration.shutdown()
                    logger.debug("MCP integration shutdown completed")
            except Exception as e:
                logger.warning(f"Error during MCP integration shutdown: {e}")

        # Clear sessions and request mappings
        self._session_tracker.clear()

        logger.info("Agent shutdown completed")

    # MCPServer interface implementation
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """
        MCPServer interface: Handle tool calls from other agents.

        An agent is a tool that executes tasks. No special "actions" needed.

        Args:
            tool: The tool name (should be agent's name when called via MCP)
            params: Parameters including task/query/prompt (required)

        Returns:
            The agent's execution result
        """
        # Extract task from parameters
        task = params.get("task") or params.get("query") or params.get("prompt")
        if not task:
            return {
                "status": "error",
                "error": "Missing required parameter: 'task', 'query', or 'prompt'"
            }

        # Extract session context from injected params (populated by ToolExecutionManager)
        request_id = params.get("request_id") or params.get("_request_id")
        session_id = params.get("session_id") or params.get("_session_id")

        try:
            # Execute the task using this agent
            logger.info("Agent %s executing task: %s", self.name, task[:100])
            from .result_utils import collect_final_result, extract_summary
            result = await collect_final_result(
                self, 
                str(task),
                request_id=request_id,
                session_id=session_id
            )

            # Wrap result with agent metadata
            return {
                "status": "success",
                "agent": self.name,
                "task": task,
                "result": result,
                "summary": extract_summary(result)
            }

        except Exception as e:
            logger.error("Agent %s failed to execute task: %s", self.name, e)
            return {
                "status": "error",
                "agent": self.name,
                "task": task,
                "error": str(e)
            }

    def get_schema(self) -> dict[str, Any]:
        """
        MCPServer interface: Return the OpenAI function schema for this agent.

        Returns:
            OpenAI function schema dict
        """
        # Try to get description from: mcp_config.description -> fallback to agent name
        description = None
        if hasattr(self, 'mcp_config') and self.mcp_config:
            description = getattr(self.mcp_config, 'description', None)
        if not description:
            description = getattr(self, '_agent_description', f"Agent: {self.name}")

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The task/query/prompt to execute"
                        }
                    },
                    "required": ["task"],
                },
            },
        }

    async def list_tools(self) -> List:
        """Return tools this agent OFFERS to other agents (MCPServer interface).

        EXTERNAL INTERFACE - What this agent exposes as callable tools.
        When other agents query available tools, they get this agent's schema.

        Contrast with list_usable_tools() which returns tools this agent CAN USE.

        Returns:
            List[MCPTool] - Single MCPTool representing this agent
        """
        # Return cached tools to avoid creating new objects on every call
        if self._list_tools_cache is not None:
            return self._list_tools_cache

        from agent_system.mcp.core import MCPTool

        # Get the agent's schema (what it offers as a callable tool)
        schema = self.get_schema()
        func = schema.get("function", {})

        # Convert to MCPTool format
        tool = MCPTool(
            name=func.get("name", self.name),
            description=func.get("description", f"Agent: {self.name}"),
            input_schema=func.get("parameters", {})
        )

        self._list_tools_cache = [tool]
        return self._list_tools_cache

    async def call_tool(self, tool_name: str, params: Dict[str, Any]) -> Any:
        """Call a tool by name with parameters.
        
        This is a convenience method that allows calling tools directly
        without going through the full agent run loop. Useful for:
        - Precondition checks (task_switch plugin)
        - Direct tool invocations from sub-agents
        - Testing individual tools
        
        Args:
            tool_name: The full tool name (e.g., "writer_content_production_status")
            params: Parameters to pass to the tool
            
        Returns:
            The tool's result
        """
        return await self._tool_execution_manager._invoke_tool(tool_name, params)
