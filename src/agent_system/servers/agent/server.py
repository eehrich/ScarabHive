"""
Enhanced Agent Core - Agent extends MCPServer for direct agent-to-agent communication
Supports multiple tool calls per conversation turn for better efficiency
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from ...config.models import AgentSystemConfig, MCPConfig
from ...core.cancellation import get_cancellation_manager, configure_cancellation_manager, CancellationToken
from ...mcp.base import MCPRegistry, MCPServer
from ...utils.id import short_id
from ...llm.models import ChatMessage
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
                 llm: object | None = None, llm_factory: object | None = None) -> None:
        """
        Initialize Agent as both an executor and an MCP Server.
        
        Modern signature matching plugin pattern:
        - system_config: Complete system configuration
        - mcp_config: MCP configuration object (contains agent_config, type, enabled)
        - registry: MCP Registry with available tools (required for agents)

        Args:
            name: Name of this agent (used when serving as MCP Server)
            system_config: Complete system configuration (includes llm_system, network, context, etc.)
            mcp_config: MCP configuration object (MCPConfig with agent_config)
            registry: MCP Registry with available tools
            llm: Optional LLM client instance (for testing)
            llm_factory: Optional LLM factory for creating client (for testing)
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
                    from ...llm.factory import resolve_llm_config_for_agent
                    
                    # Use new profile-based resolution with agent config
                    llm_kwargs = resolve_llm_config_for_agent(system_config, self.agent_config)
                    
                    # Store profile information for status display
                    self.llm_profile_info = self._extract_profile_info(system_config, name, llm_kwargs)
                    
                    from ...llm.clients import make_llm
                    self.llm = make_llm(
                        llm_kwargs["provider"],
                        llm_kwargs["model"], 
                        llm_kwargs["openai_api_key"],
                        llm_kwargs["ollama_url"],
                        llm_kwargs["context_window"],
                        llm_kwargs["ollama_mode"],
                        llm_kwargs["request_timeout"],
                        ssl_verify=getattr(system_config, "network").ssl_verify if getattr(system_config, "network", None) else None,
                        httpx_timeouts=llm_kwargs.get("httpx_timeouts"),
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

        # Track current conversation messages for debugging
        self._current_messages: List[ChatMessage] = []

        # Initialize component managers for better code organization
        # Create request manager first (owns _active_requests dict)
        self._request_manager = AgentRequestManager(self.name)
        # Session tracker shares the same _active_requests dict for coordination
        self._session_tracker = SessionTracker(self._request_manager._active_requests)
        self._mcp_integration_manager = MCPIntegrationManager(self.system_config, self.agent_config)
        self._status_event_forwarder = StatusEventForwarder()
        self._tool_execution_manager = ToolExecutionManager(
            self.registry, 
            self, 
            status_forwarder=self._status_event_forwarder
        )
        
        # Context management now handled by hook plugins via HookIntegrationManager
        
        # Initialize hook integration manager
        from .components.hook_integration import HookIntegrationManager
        self._hook_manager = HookIntegrationManager(self)
        
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
    def _render_prompts(self, usable_tools: List[str], max_steps: int, current_step: int) -> tuple[str, Optional[str]]:
        """
        Render (system_prompt, tools_prompt) using strategy pattern.

        Args:
            usable_tools: List of tool names available to the agent
            max_steps: Maximum steps allowed for the agent
            current_step: Current step number (1-indexed, for dynamic per-step rendering)

        Order of precedence:
          1. Subclass hook `get_custom_system_prompt`
          2. In-memory raw `agent_config.system_prompt`
          3. File/template based `agent_config.system_template`
          4. Default fallback

        Returns:
            (system_prompt, tools_prompt_or_None)
        """
        renderer = PromptRenderer()
        context = PromptContext(
            agent_name=self.name,
            agent_config=self.agent_config,
            system_config=self.system_config,
            available_tools=usable_tools,
            max_steps=max_steps,
            current_step=current_step,
            agent_instance=self  # Pass self for hook access
        )
        return renderer.render(context)

    async def get_current_system_prompt(self) -> str:
        """Async: render current system prompt (diagnostics endpoint)."""
        try:
            usable_tools = await self.list_usable_tools()
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
          plugin/function   -> matches one function inside multi-tool plugin
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

    async def list_usable_tools(self) -> list[str]:
        """Return list of tool names this agent CAN USE (filtered by agent config).
        
        This is the INTERNAL interface - tools available for this agent's execution.
        Filtered by agent_config.tools.allowed patterns.
        
        Contrast with list_tools() which returns what this agent OFFERS to others.
        
        Returns:
            List of tool server names this agent is allowed to use
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
        llm_profile_info_override: Optional[str] = None
    ):
        """Run the agent and yield structured events for UI streaming.
        
        Args:
            task: Either a string task description or a ChatMessage with multimodal content
            request_id: Optional request ID for tracking
            session_id: Optional session ID for conversation history
            llm_override: Optional LLM client to use instead of self.llm (for per-request profile overrides)
            llm_profile_info_override: Optional profile info string for status display (e.g., "turbo:openai_httpx/gpt-5-nano")
        """

        # Generate request ID if not provided
        if request_id is None:
            request_id = short_id()

        # If no session_id provided, generate one and persist empty history
        if not session_id:
            session_id = short_id()

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

        try:
            async with status_scope(status_bus, f"{self.name}_coordinator", coordinator_request_id) as status_coordinator, \
                       status_scope(status_bus, f"{self.name}_worker", worker_request_id) as status_worker:
                async for event in self._run_events(
                    task_text, 
                    request_id=request_id, 
                    session_id=session_id, 
                    status_coordinator=status_coordinator, 
                    status_worker=status_worker,
                    initial_message=initial_message,
                    llm_override=llm_override,
                    llm_profile_info_override=llm_profile_info_override
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
        llm_override: Optional[object] = None
    ) -> ConversationContext:
        """Initialize request tracking and build initial conversation context.
        
        Phase 1 of agent execution: Setup all state needed for the LLM loop.
        
        Steps:
        1. Create cancellation token
        2. Register request for cancellation/appends
        3. Set context vars
        4. Initialize session storage
        5. Start status event forwarding
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
            
        # Start status event forwarding
        await self._status_event_forwarder.start_forwarding(request_id)

        # If no LLM is configured, raise error
        if active_llm is None:
            raise RuntimeError("No LLM available; agent requires an LLM to run")

        # Initialize MCP integration
        await self._mcp_integration_manager.setup_mcp_integration()

        # Get tools this agent can use (filtered by agent_config)
        usable_tools = await self.list_usable_tools()

        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))

        # Centralized prompt rendering (system + optional tools) using helper.
        # Initial render with step 0 (before loop starts)
        system_msg, tools_msg = self._render_prompts(usable_tools, max_steps, current_step=0)

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
            messages.append(ChatMessage(role="user", content=sanitize_for_llm(task)))

        # Also include any appended messages already queued for this request
        messages = await self._session_tracker.drain_appended_messages(request_id, messages)

        # Track messages for debugging
        self._current_messages = messages.copy()

        # Build tool schemas using ToolSchemaBuilder
        schema_builder = ToolSchemaBuilder(
            agent_name=self.name,
            mcp_integration_manager=self._mcp_integration_manager,
            server_getter_func=self._get_server_from_any_registry
        )
        
        tools_schema, tool_name_mapping, usable_tools, display_tools = await schema_builder.build_schemas(
            usable_tools
        )

        # Return initialized context
        return ConversationContext(
            messages=messages,
            available_tools=usable_tools,  # Use usable_tools for validation (includes both server names and tool names)
            tools_schema=tools_schema,
            tool_name_mapping=tool_name_mapping,
            max_steps=max_steps,
            main_token=main_token,
            context_reset_token=context_reset_token
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
        
        # Persist session messages and keep the request->session mapping for a while
        sid = self._session_tracker.get_session_for_request(request_id)
        if sid and messages:
            try:
                # Filter out system messages - only persist conversation history
                conversation_msgs = [msg for msg in messages if msg.role != "system"]
                # Update the persistent session with conversation state (no system messages)
                self._session_tracker.set_session_messages(sid, conversation_msgs.copy())
                logger.debug("Persisted session %s with %d conversation messages", sid, len(conversation_msgs))
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
        final_msg = "completed" if not results.get('errors') else "completed with errors"
        
        if results.get('errors'):
            await status_worker.error(final_msg, meta={"summary": results.get('summary')})
            await status_coordinator.error(f"{final_msg} ({step+1} steps)", meta={"summary": results.get('summary')})
        else:
            await status_worker.end(final_msg, meta={"summary": results.get('summary')})
            await status_coordinator.end(f"{final_msg} ({step+1} steps)", meta={"summary": results.get('summary')})

        # Give a small delay to allow final status events to be processed by the forwarding task
        await asyncio.sleep(0.01)

        # Clean up status forwarding task AFTER publishing final status
        await self._status_event_forwarder.stop_forwarding()

    async def _call_llm_with_streaming(
        self,
        llm: Any,
        messages: List[ChatMessage],
        tools_schema: List[Dict[str, Any]],
        cancellation_token: CancellationToken,
        step: int,
        yield_pending_status_fn
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
            
        Yields:
            - {"type": "thinking_delta", "step": int, "delta": str, "accumulated": str}
            - {"type": "status", ...}
            - {"type": "thinking_complete", "assistant": {...}}
        """
        if llm.supports_streaming():
            # Streaming LLM: zero-overhead real-time tokens
            accumulated_content = []
            final_assistant = None
            
            async for chunk in llm.chat_tools_streaming(messages, tools_schema, cancellation_token=cancellation_token):
                chunk_type = chunk.get("type")
                
                if chunk_type == "content_delta":
                    # Yield token delta for real-time display
                    yield {"type": "thinking_delta", "step": step + 1, "delta": chunk["delta"], "accumulated": chunk["accumulated"]}
                    accumulated_content.append(chunk["delta"])
                    
                    # Check status events after each token (zero overhead)
                    for status_event in yield_pending_status_fn():
                        yield status_event
                
                elif chunk_type == "tool_call_delta":
                    # Tool calls are accumulated server-side, we can skip yielding deltas for now
                    # Future: could yield tool_call_delta events for UI to show "Calling get_weather..."
                    pass
                
                elif chunk_type == "final":
                    final_assistant = chunk["assistant"]
            
            # Yield final response
            if final_assistant:
                yield {"type": "thinking_complete", "assistant": final_assistant}
            else:
                yield {"type": "thinking_complete", "assistant": {"role": "assistant", "content": "".join(accumulated_content)}}
        
        else:
            # Non-streaming LLM: Use polling with 100ms intervals
            # Create task for LLM call
            llm_task = asyncio.create_task(llm.chat_tools(messages, tools_schema, cancellation_token=cancellation_token))
            
            # Poll for status events while waiting
            while not llm_task.done():
                # Check for status events
                for status_event in yield_pending_status_fn():
                    yield status_event
                
                # Wait 100ms before next poll
                try:
                    await asyncio.wait_for(asyncio.shield(llm_task), timeout=0.1)
                except asyncio.TimeoutError:
                    pass  # Continue polling
            
            # Get result
            llm_out = await llm_task
            yield {"type": "thinking_complete", "assistant": llm_out.get("assistant", {})}

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
        
        # Helper function to yield any pending status events
        def yield_pending_status_events():
            for event in self._status_event_forwarder.get_pending_events():
                yield event

        for step in range(max_steps):
            # Drain any appended user messages before each step
            messages = await self._drain_appended_messages(request_id, messages)
            
            # Check for cancellation at the start of each step
            if self._is_cancelled(request_id):
                logger.info("Request %s cancelled at step %d", request_id, step + 1)
                yield {"type": "cancelled", "request_id": request_id, "step": step + 1}
                # Signal cancellation using status contexts
                await status_worker.error(f"cancelled at step {step + 1}", 
                                      meta={"step": step + 1, "reason": "cancelled"})
                await status_coordinator.error(f"cancelled at step {step + 1}",
                                            meta={"step": step + 1, "reason": "cancelled"})
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
            updated_system_msg, _ = self._render_prompts(context.available_tools, max_steps, current_step=step + 1)
            messages[0] = ChatMessage(role="system", content=updated_system_msg)

            # Emit thinking event before LLM call (for UI step display)
            yield {"type": "thinking", "step": step + 1}

            # Execute pre-LLM hooks to transform messages
            try:
                modified_messages = await self._hook_manager.execute_pre_llm_hooks(
                    messages=messages,
                    step=step,
                    request_id=request_id,
                    session_id=session_id,
                    llm=active_llm
                )
                if modified_messages is not None:
                    messages = modified_messages
            except Exception as e:
                logger.warning(f"Pre-LLM hooks failed: {e}", exc_info=True)

            # LLM call with streaming support
            llm_out = None
            try:
                async for event in self._call_llm_with_streaming(
                    llm=active_llm,
                    messages=messages,
                    tools_schema=tools_schema,
                    cancellation_token=main_token,
                    step=step,
                    yield_pending_status_fn=yield_pending_status_events
                ):
                    event_type = event.get("type")
                    
                    if event_type == "thinking_delta":
                        # Yield real-time token deltas to WebUI
                        yield event
                    elif event_type == "status":
                        # Yield interleaved status events
                        yield event
                    elif event_type == "thinking_complete":
                        llm_out = {"assistant": event["assistant"]}
                        
            except Exception as e:
                # Check if this is a cancellation exception
                error_str = str(e).lower()
                if "cancelled" in error_str or "timeout" in error_str:
                    logger.info(f"Request {request_id} cancelled during LLM call: {e}")
                    # Signal cancellation using status contexts
                    await status_worker.error(f"cancelled at step {step + 1}: {e}", 
                                          meta={"step": step + 1, "reason": "cancelled"})
                    await status_coordinator.error(f"cancelled at step {step + 1}",
                                                meta={"step": step + 1, "reason": "cancelled"})
                    yield {"type": "cancelled", "request_id": request_id, "step": step + 1, "reason": str(e)}
                    return
                
                # Propagate other exceptions
                raise

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
                tool_calls=tool_calls if tool_calls else None
            )
            messages.append(assistant_msg)
            context.messages.append(assistant_msg)

            # Execute post-LLM hooks to transform the response
            try:
                modified_response, hook_metadata = await self._hook_manager.execute_post_llm_hooks(
                    messages=messages,
                    llm_response=llm_out,  # Pass full LLM response including usage data
                    step=step,
                    request_id=request_id,
                    session_id=session_id,
                    llm=active_llm
                )
                if modified_response is not None:
                    # Extract assistant data from modified response
                    modified_assistant = modified_response.get("assistant", {})
                    content = modified_assistant.get("content", content)
                    tool_calls = modified_assistant.get("tool_calls", tool_calls)
                    # Update the assistant message if hooks modified the response
                    assistant_msg.content = content or ""
                    assistant_msg.tool_calls = tool_calls if tool_calls else None
                    # Set content_format from hook metadata (e.g., 'html', 'markdown', 'text')
                    if "content_format" in hook_metadata:
                        assistant_msg.content_format = hook_metadata["content_format"]
            except Exception as e:
                logger.warning(f"Post-LLM hooks failed: {e}", exc_info=True)

            # Emit thinking event with LLM response (for UI to show assistant reasoning)
            yield {"type": "thinking", "step": step + 1, "assistant": {"content": content, "tool_calls": tool_calls}}

            # Also emit simplified thinking event if we have content and no tool calls (final answer)
            if content and not tool_calls:
                yield {"type": "thinking", "content": content}

            # Yield pending status events after LLM response
            for status_event in yield_pending_status_events():
                yield status_event

            # Infinite loop guard: Track consecutive responses without tool calls
            if not tool_calls:
                consecutive_no_tool_calls += 1
                if consecutive_no_tool_calls >= max_consecutive_no_tools:
                    logger.warning(f"Breaking loop: {consecutive_no_tool_calls} consecutive responses without tool calls")
                    # Treat final content as answer (assistant_msg already added above)
                    results["summary"] = content
                    self._current_messages = messages.copy()
                    yield {"type": "final", "summary": content, "content_format": "markdown"}
                    return
            else:
                consecutive_no_tool_calls = 0  # Reset counter when we get tool calls

            # Infinite loop guard: Track consecutive empty responses
            if not content and not tool_calls:
                consecutive_empty_responses += 1
                if consecutive_empty_responses >= max_consecutive_empty:
                    logger.warning(f"Breaking loop: {consecutive_empty_responses} consecutive empty responses")
                    error_msg = "LLM returned empty responses repeatedly"
                    results.setdefault("errors", []).append(error_msg)
                    yield {"type": "error", "message": error_msg}
                    return
            else:
                consecutive_empty_responses = 0  # Reset counter

            # If we have tool calls, execute them
            if tool_calls:
                # Signal tool execution start
                await status_worker.progress(f"Executing Tools ({len(tool_calls)} total)", meta={"step": step + 1})
                
                # Assistant message with tool calls was already added above before post_llm hooks
                
                # Update tracked messages
                self._current_messages = messages.copy()
                
                # Execute all tools using streaming to get real-time status events from sub-agents
                tool_messages = []
                tool_results = []
                async for item in self._tool_execution_manager.execute_tools_streaming(
                    tool_calls=tool_calls,
                    tool_name_mapping=tool_name_mapping,
                    available_tools=context.available_tools,
                    step=step,
                    request_id=request_id,
                    session_id=session_id
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
                
                # Add tool messages to conversation
                messages.extend(tool_messages)
                context.messages.extend(tool_messages)  # FIX: Also extend context.messages
                
                # Update tracked messages after tool execution
                self._current_messages = messages.copy()
                
                # Yield pending status events after tool execution
                for status_event in yield_pending_status_events():
                    yield status_event
                
                # Continue to next iteration to let LLM respond to tool results
                continue
            
            # No tool calls - this is the final answer
            if content:
                # Assistant message was already added above before post_llm hooks
                results["summary"] = content
                # Update tracked messages with final response
                self._current_messages = messages.copy()
                
                # Return raw markdown - formatting happens in API/CLI layer
                yield {"type": "final", "summary": content, "content_format": "markdown"}
                return
            
            # Update tracked messages at end of each step
            self._current_messages = messages.copy()
            
            # Drain any final appended messages before next step  
            messages = await self._drain_appended_messages(request_id, messages)

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
            )
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
                assistant_msg = ChatMessage(role="assistant", content=final_content or "")
                messages.append(assistant_msg)
                context.messages.append(assistant_msg)  # Also append to context.messages
                results["summary"] = final_content
                # Update tracked messages and emit final event
                self._current_messages = messages.copy()
                
                # Return raw markdown - formatting happens in API/CLI layer
                yield {"type": "final", "summary": final_content, "content_format": "markdown"}
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
        status_coordinator: StatusScope, 
        status_worker: StatusScope,
        initial_message: Optional[ChatMessage] = None,
        llm_override: Optional[object] = None,
        llm_profile_info_override: Optional[str] = None
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
            status_coordinator: Status scope for coordinator-level events
            status_worker: Status scope for worker-level events
            initial_message: Optional ChatMessage with multimodal content
            llm_override: Optional LLM client override
            llm_profile_info_override: Optional profile info for status display
            
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
        
        # Emit start event (even if initialization fails later)
        yield {"type": "start", "task": task, "request_id": request_id, "session_id": session_id}
        
        try:
            # Phase 1: Initialize request and build conversation context
            try:
                context = await self._initialize_request_and_conversation(
                    task=task,
                    request_id=request_id,
                    session_id=session_id,
                    initial_message=initial_message,
                    llm_override=llm_override
                )
            except RuntimeError as e:
                # LLM not available - emit error and end stream
                yield {"type": "error", "message": str(e), "request_id": request_id}
                yield {"type": "end"}
                return
            
            # Helper function to yield any pending status events
            def yield_pending_status_events():
                for event in self._status_event_forwarder.get_pending_events():
                    yield event
            
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
                
                # Capture summary and errors from events
                if event.get("type") == "final" and "summary" in event:
                    results["summary"] = event["summary"]
                elif event.get("type") == "error":
                    results.setdefault("errors", []).append(event.get("message", "Unknown error"))
                elif event.get("type") == "cancelled":
                    # Loop was cancelled, update step from event
                    step = event.get("step", 0) - 1  # Convert to 0-indexed
            
        except Exception as e:
            logger.exception("Agent execution failed with exception:")
            yield {"type": "error", "message": f"Agent execution failed: {e}"}
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

        try:
            # Execute the task using this agent
            logger.info("Agent %s executing task: %s", self.name, task[:100])
            from .result_utils import collect_final_result, extract_summary
            result = await collect_final_result(self, str(task))

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
        
        return [tool]
