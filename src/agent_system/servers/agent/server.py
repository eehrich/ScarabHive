"""
Enhanced Agent Core - Agent extends MCPServer for direct agent-to-agent communication
Supports multiple tool calls per conversation turn for better efficiency
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Union

from ...config.models import AgentSystemConfig, MCPConfig
from ...utils.cancellation import get_cancellation_manager, configure_cancellation_manager
from ...mcp.base import MCPRegistry, MCPServer
from ...utils.id import short_id
from ...llm.models import ChatMessage
from ...utils.prompt_renderer import render_prompts, get_datetime_context
from jinja2 import Template
from ...llm.text_sanitizer import sanitize_for_llm
from ...context import ContextManager, ConversationSummarizer, TokenOptimizer
from ...context.agent_tracker import register_agent_for_tracking
from ...mcp.status import (
    status_scope,
    StatusScope,
    status_bus,
    current_request_id
)
from .components.mcp_integration import MCPIntegrationManager
from .components.tool_execution import ToolExecutionManager
from .components.status_forwarding import StatusEventForwarder
from .components.context_management import ContextManagementHandler


logger = logging.getLogger(__name__)


class Agent(MCPServer):
    """
    Enhanced Agent that executes ALL tool calls per LLM conversation turn.
    Also serves as an MCP Server that can be used by other agents as a tool.
    This enables direct agent-to-agent communication without wrapper classes.
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
        
        # Mark this Agent as internal by default so it doesn't show up in UI lists
        # Consumers who want it visible can set `agent._mcp_public = True` after construction.
        self._mcp_public = False
        
        # Allow dependency injection of an LLM client or a factory that
        # creates one. This makes testing and runtime wiring explicit.
        self.llm = llm
        self._llm_factory = llm_factory

        # Initialize LLM if not provided
        # Store LLM profile information for status display
        self.llm_profile_info = None
        
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

        # Initialize context management system
        self._init_context_management()

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

        # Register agent with context tracker (use context_window from context_manager)
        if self.context_manager:
            register_agent_for_tracking(name, name, self.context_manager.context_window)

        # Track current conversation messages for debugging
        self._current_messages: List[ChatMessage] = []

        # Initialize request tracking for cancellation and per-request state
        # _active_requests maps request_id -> { 'cancel': Event(), 'message_event': Event(), 'appended': List[ChatMessage] }
        self._active_requests: Dict[str, Dict[str, Any]] = {}
        self._request_lock = asyncio.Lock()
        # Persisted sessions (conversation history) keyed by session_id
        self._sessions: Dict[str, List[ChatMessage]] = {}
        # Map active request_id -> session_id for runs
        self._request_to_session: Dict[str, str] = {}

        # Initialize component managers for better code organization
        self._mcp_integration_manager = MCPIntegrationManager(self.system_config, self.agent_config)
        self._tool_execution_manager = ToolExecutionManager(self.registry, self)
        self._status_event_forwarder = StatusEventForwarder()
                    
        self._context_management_handler = ContextManagementHandler(
            self.context_manager, 
            self.token_optimizer, 
            self,
            self.agent_config.context_management.token_optimization
        )
        
        # Set agent reference in MCP integration for cancellation support
        self._set_agent_reference_in_mcp()

        # Track emergency context management attempts to prevent loops
        self._emergency_context_attempts = 0
        self._max_emergency_attempts = 2  # Maximum emergency attempts per conversation

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

    def _init_context_management(self):
        """Initialize the context management system using new config system."""
        try:
            # Initialize context manager with Agent instance (no config passing)
            self.context_manager = ContextManager(self)

            # Initialize and set summarizer with dedicated LLM client
            summarizer_llm = None
            if self.llm:
                try:
                    # Use profile-based resolution for summarizer LLM
                    from ...llm.factory import resolve_llm_config_for_agent
                    from ...llm.clients import make_llm
                    from ...config.models import AgentConfig
                    
                    # Create agent config for summarizer using summarizer_llm_profile from context config
                    summarizer_profile = self.agent_config.context_management.summarizer_llm_profile
                    summarizer_agent_config = AgentConfig(llm_profile=summarizer_profile)
                    summarizer_kwargs = resolve_llm_config_for_agent(self.system_config, summarizer_agent_config)
                    
                    summarizer_llm = make_llm(
                        summarizer_kwargs["provider"],
                        summarizer_kwargs["model"],
                        summarizer_kwargs["openai_api_key"],
                        summarizer_kwargs["ollama_url"],
                        summarizer_kwargs["context_window"],
                        summarizer_kwargs["ollama_mode"],
                        summarizer_kwargs["request_timeout"],
                        ssl_verify=self.system_config.network.ssl_verify if self.system_config.network else None,
                        httpx_timeouts=summarizer_kwargs.get("httpx_timeouts"),
                    )
                    
                    # Store profile info for summarizer
                    self.summarizer_profile_info = f"{summarizer_profile}:{summarizer_kwargs.get('provider')}/{summarizer_kwargs.get('model')}"
                    
                except Exception as e:
                    logger.warning("Failed to create dedicated summarizer LLM client: %s", e)
                    summarizer_llm = None

            # Pass profile info to summarizer for status display
            summarizer_profile = getattr(self, 'summarizer_profile_info', None)
            summarizer = ConversationSummarizer(summarizer_llm, profile_info=summarizer_profile)
            self.context_manager.set_summarizer(summarizer)

            # Initialize optimizer only when compression/optimization is enabled
            if self.mcp_config.agent_config.context_management.token_optimization.enable_compression:
                self.token_optimizer = TokenOptimizer()
            else:
                self.token_optimizer = None

            logger.info("Context management initialized - window: %d, strategy: %s",
                      self.context_manager.context_window,
                      self.mcp_config.agent_config.context_management.strategy)

        except Exception as e:
            logger.warning("Context management initialization failed: %s", e)
            self.context_manager = None
            self.token_optimizer = None

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
    # Central prompt rendering utilities (restored)
    # ------------------------------------------------------------------
    def _render_prompts(self, available_tools: List[str], max_steps: int) -> tuple[str, Optional[str]]:
        """Render (system_prompt, tools_prompt) applying hook, raw prompt, or template.

        Order of precedence:
          1. Subclass hook `get_custom_system_prompt`
          2. In-memory raw `agent_config.system_prompt`
          3. File/template based `agent_config.system_template` (render_prompts)

        Returns:
            (system_prompt, tools_prompt_or_None)
        """
        context_vals = {"tools": available_tools, "max_steps": max_steps-1}

        # Get datetime context from system_config.context, not agent_config
        if hasattr(self.system_config, 'context') and self.system_config.context.auto_datetime:
            dt_ctx = get_datetime_context(self.system_config.context.timezone, self.system_config.context.location)
            context_vals.update(dt_ctx)

        # Subclass custom hook
        try:
            custom_prompt = self.get_custom_system_prompt(context_vals)
        except Exception as e:  # pragma: no cover
            logger.warning("Custom system prompt hook failed for agent %s: %s", self.name, e)
            custom_prompt = None

        if custom_prompt:
            logger.debug("Agent %s using subclass custom system prompt (len=%d)", self.name, len(custom_prompt))
            return custom_prompt, None

        # Check for an in-memory raw prompt
        system_prompt_raw = getattr(self.agent_config, 'system_prompt', None)
        if system_prompt_raw:
            logger.debug("Agent %s using in-memory system_prompt (length=%s)", self.name, len(system_prompt_raw or ''))
            try:
                rendered_system = Template(system_prompt_raw).render(**context_vals)
            except Exception as e:
                logger.warning(f"Failed to render system prompt template: {e}", exc_info=True)
                rendered_system = "You are an assistant agent."
            return rendered_system, None

        # Template based
        system_template_path = getattr(self.agent_config, 'system_template', None)
        if not system_template_path:
            logger.debug("Agent %s has no prompts config, using default system prompt", self.name)
            return "You are an assistant agent.", None

        logger.debug(
            "Agent %s rendering system_template from path: %s",
            self.name, system_template_path)
        rendered = render_prompts(
            system_template_path,
            context_vals,
            auto_datetime=self.system_config.context.auto_datetime if hasattr(self.system_config, 'context') else False,
            timezone=self.system_config.context.timezone if hasattr(self.system_config, 'context') else None,
            location=self.system_config.context.location if hasattr(self.system_config, 'context') else None
        )
        system_msg = rendered.get("system_prompt") or "You are an assistant agent."
        tools_msg = rendered.get("tools_prompt")
        return system_msg, tools_msg

    async def get_current_system_prompt(self) -> str:
        """Async: render current system prompt (diagnostics endpoint)."""
        try:
            available_tools = await self.list_allowed_tool_servers()
        except Exception as e:
            logger.warning(f"Failed to list available tools for system prompt: {e}", exc_info=True)
            available_tools = []
        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
        system_msg, _ = self._render_prompts(available_tools, max_steps)
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
        # No description field in AgentConfig, use agent name
        return f"Agent: {self.name}"

    async def cancel_request(self, request_id: str) -> bool:
        """
        Cancel an active request by setting its cancellation event.
        Also uses the new cancellation manager for graceful/forced tool cancellation.
        Uses prefix matching to cancel all related tool requests.

        Args:
            request_id: The unique ID of the request to cancel

        Returns:
            True if the request was found and cancelled, False otherwise
        """
        logger.info("Cancelling request %s", request_id)
        
        # Use new cancellation manager for tool-level cancellation with prefix matching
        cancellation_manager = get_cancellation_manager()
        tool_cancelled = cancellation_manager.cancel_request(request_id)
        
        # Don't force-cancel tasks immediately - let timeout monitor handle it
        # Only check if we have matching tasks for logging
        task_cancelled_count = 0
        for task_id in cancellation_manager._tasks.keys():
            if task_id == request_id or task_id.startswith(request_id + "_"):
                task_cancelled_count += 1
        
        # Also cancel in the legacy agent system
        async with self._request_lock:
            agent_cancelled = False
            if request_id in self._active_requests:
                try:
                    self._active_requests[request_id]["cancel"].set()
                    agent_cancelled = True
                except Exception as e:
                    # Defensive: if structure unexpected, try old-style event
                    logger.debug(f"Failed to cancel via cancel event, trying old-style: {e}")
                    if isinstance(self._active_requests[request_id], asyncio.Event):
                        self._active_requests[request_id].set()
                        agent_cancelled = True
            
            if not agent_cancelled:
                logger.warning("Request %s not found in active requests", request_id)
            
            # Return True if any system found and cancelled something
            return tool_cancelled or agent_cancelled or (task_cancelled_count > 0)

    def _is_cancelled(self, request_id: Optional[str]) -> bool:
        """
        Check if a request has been cancelled.

        Args:
            request_id: The unique ID of the request to check

        Returns:
            True if the request has been cancelled, False otherwise
        """
        if request_id:
            # Check the cancellation token first
            cancellation_manager = get_cancellation_manager()
            token = cancellation_manager.get_token(request_id)
            if token and token.is_cancelled:
                logger.debug("Request %s is cancelled (cancellation token)", request_id)
                return True
            elif token:
                logger.debug("Request %s has token but not cancelled", request_id)
            else:
                logger.debug("Request %s has no cancellation token", request_id)
                
            # Also check legacy internal cancellation event
            if request_id in self._active_requests:
                entry = self._active_requests[request_id]
                if isinstance(entry, dict) and 'cancel' in entry:
                    return bool(entry['cancel'].is_set())
                if isinstance(entry, asyncio.Event):
                    return entry.is_set()
        return False

    async def append_user_message(self, request_id: str, content: str) -> bool:
        """
        Append a user message to an active request's conversation.
        Returns True if appended, False if request not found.
        """
        logger.debug("Append request received for request_id=%s: %s", request_id, content[:50])
        async with self._request_lock:
            if request_id in self._active_requests:
                entry = self._active_requests[request_id]
                if isinstance(entry, dict):
                    try:
                        msg = ChatMessage(role="user", content=sanitize_for_llm(content))
                        entry.setdefault('appended', []).append(msg)
                        # notify run_events if it's waiting
                        try:
                            entry['message_event'].set()
                        except Exception as e:
                            logger.debug(f"Failed to set message event: {e}")
                        logger.debug("Message appended to active request %s", request_id)
                        return True
                    except Exception as e:
                        logger.debug("Failed to append message to request %s: %s", request_id, e)
                        return False
        logger.debug("Request %s not found for append", request_id)
        return False

    async def append_to_session(self, session_id: str, content: str) -> bool:
        """
        Append a user message directly to a persisted session.
        Returns True if appended, False if session not found.
        """
        logger.debug("Session append request for session_id=%s: %s", session_id, content[:50])
        async with self._request_lock:
            if session_id in self._sessions:
                try:
                    msg = ChatMessage(role="user", content=sanitize_for_llm(content))
                    self._sessions[session_id].append(msg)
                    logger.debug("Message appended to session %s", session_id)
                    return True
                except Exception as e:
                    logger.debug("Failed to append message to session %s: %s", session_id, e)
                    return False
        logger.debug("Session %s not found for append", session_id)
        return False

    async def _drain_appended_messages(self, request_id: str, messages: List[ChatMessage]) -> List[ChatMessage]:
        """
        Drain any appended messages for a request and add them to the conversation.
        Returns the updated messages list.
        """
        async with self._request_lock:
            entry = self._active_requests.get(request_id)
            if isinstance(entry, dict):
                appended = entry.get('appended', [])
                if appended:
                    messages.extend(appended)
                    entry['appended'] = []
                    logger.debug("Drained %d appended messages for request %s", len(appended), request_id)
                    # clear message_event
                    try:
                        entry['message_event'].clear()
                    except Exception as e:
                        logger.debug(f"Failed to clear message event: {e}")
        return messages

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

    def _filter_available_tools(self, tools: list[str], patterns: list[str]) -> list[str]:
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

    async def list_allowed_tool_servers(self) -> list[str]:
        """Collect all available tool server names (plugins + external + registry) applying per-agent allow list.

        This centralizes tool discovery so that both the LLM prompt construction and any
        user-facing listing endpoints / plugin helper tools obtain a consistent filtered
        view. Previously the collection logic lived inline in `_run_events`; extracting
        it here avoids divergence.
        """
        # Initialize MCP integration (idempotent)
        await self._mcp_integration_manager.setup_mcp_integration()

        # Determine patterns first (deny-all baseline if not configured)
        try:
            allowed_patterns = self.agent_config.tools.allowed if self.agent_config.tools else None
        except Exception as e:
            logger.warning(f"Failed to get allowed_patterns from agent config: {e}", exc_info=True)
            allowed_patterns = None
        # If no allow list -> deny all (explicit policy change)
        if not allowed_patterns:
            logger.debug("Agent %s: no tools.allowed configured -> deny-all (0 tools)", self.name)
            return []

        # Gather plugin provided tool servers
        plugin_tools: list[str] = []
        if self._mcp_integration_manager.mcp_integration and self._mcp_integration_manager.mcp_integration.initialized:
            plugin_tools = self._mcp_integration_manager.mcp_integration.plugin_registry.list_servers()

        # External + plugin + adapter tools via integration manager helper
        available_tools = await self._mcp_integration_manager.get_available_tools(plugin_tools)

        # Local registry (directly registered mock/test servers)
        if hasattr(self, 'registry') and self.registry:
            for tool_name in self.registry.list():
                if tool_name not in available_tools:
                    available_tools.append(tool_name)

        # Apply allow-list (guaranteed non-empty here)
        try:
            blocked_patterns = getattr(self.agent_config, 'blocked_tools', None)
        except Exception as e:
            logger.warning(f"Failed to get blocked_patterns from agent config: {e}", exc_info=True)
            blocked_patterns = None

        available_tools = self._filter_available_tools(available_tools, allowed_patterns)
        logger.debug("Filtered available tools for agent %s (allow list) -> %s", self.name, available_tools)
        if not available_tools:
            logger.warning("Agent %s allow list patterns produced an empty tool set", self.name)
            # Fallback: if a global wildcard '*' was specified but nothing matched (e.g. discovery timing)
            # attempt a second pass pulling plugin server names directly from the MCP plugin registry.
            try:
                if any(p == '*' for p in allowed_patterns):
                    if (self._mcp_integration_manager.mcp_integration and
                            self._mcp_integration_manager.mcp_integration.initialized):
                        plugin_registry = self._mcp_integration_manager.mcp_integration.plugin_registry
                        plugin_names = []
                        try:
                            plugin_names = list(plugin_registry.list_servers())
                        except Exception as e:
                            logger.debug(f"Failed to list plugin servers: {e}")
                            plugin_names = []
                        if plugin_names:
                            logger.debug("Wildcard fallback adding plugin servers for agent %s: %s", self.name, plugin_names)
                            available_tools = plugin_names
            except Exception as e:
                logger.debug(f"Wildcard plugin fallback failed: {e}")

        if blocked_patterns:
            before_block = list(available_tools)
            available_tools = [t for t in available_tools if not self._is_tool_allowed(t, blocked_patterns)]
            removed = set(before_block) - set(available_tools)
            if removed:
                logger.debug("Agent %s blocked_tools removed: %s", self.name, sorted(removed))
            if not available_tools:
                logger.warning("Agent %s blocked_tools removed all tools", self.name)
        return available_tools

    async def _list_available_tools(self, params: Dict[str, Any]) -> list[Dict[str, Any]]:
        """List all available tools that the agent can access (simplified: only names and descriptions).
        
        This is a utility method for agent subclasses that provide tool listing functionality.
        Returns a list of tool dictionaries with 'name' and 'description' keys.
        
        Args:
            params: Parameters including optional '_status' for progress reporting
            
        Returns:
            List of tool dictionaries with 'name' and 'description' keys
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
        """Internal implementation of run_events with optional multimodal message.
        
        Args:
            task: Text task description (may be empty if initial_message is provided)
            request_id: Request ID for tracking
            session_id: Session ID for conversation history
            status_coordinator: Status scope for coordinator
            status_worker: Status scope for worker
            initial_message: Optional ChatMessage with multimodal content to use instead of task
            llm_override: Optional LLM client to use instead of self.llm
            llm_profile_info_override: Optional profile info string for status display
        """
        # Determine which LLM to use for this request
        active_llm = llm_override if llm_override is not None else self.llm
        
        # Initialize step counter at function level so it's accessible in finally blocks and cleanup
        step = 0
        
        # Create cancellation token for the main request
        cancellation_manager = get_cancellation_manager()
        main_token = cancellation_manager.create_token(request_id)
        logger.debug("Created main cancellation token for request %s", request_id)
        
        # Register this request for potential cancellation and appended messages
        async with self._request_lock:
            self._active_requests[request_id] = {
                "cancel": asyncio.Event(),
                "message_event": asyncio.Event(),
                "appended": []
            }

        # Set the ContextVar so any publish_status() calls without explicit request_id
        # will inherit the current request id. Store token for reset in finally.
        token = None
        try:
            try:
                token = current_request_id.set(request_id)
            except Exception as e:
                logger.debug(f"Failed to set current_request_id context var: {e}")
                token = None
            async with self._request_lock:
                # ensure session exists
                self._sessions.setdefault(session_id, [])
                # map request to session
                self._request_to_session[request_id] = session_id
                
                # Reset emergency context management counter for new conversations
                # Only reset if this is a new session (empty history)
                if not self._sessions[session_id]:
                    self._emergency_context_attempts = 0
                    logger.debug("Reset emergency context counter for new session")

            yield {"type": "start", "task": task, "request_id": request_id, "session_id": session_id}

            # Start status event forwarding
            await self._status_event_forwarder.start_forwarding(request_id)

            # Helper function to yield any pending status events
            def yield_pending_status_events():
                for event in self._status_event_forwarder.get_pending_events():
                    yield event

            # If no LLM is configured, emit an immediate error event and end the stream
            if active_llm is None:
                yield {"type": "error", "message": "No LLM available; agent requires an LLM to run", "request_id": request_id}
                yield {"type": "end"}
                return

            # Initialize MCP integration tracking
            mcp_integration = None
            mcp_initialized_locally = False

            # Initialize MCP integration
            await self._mcp_integration_manager.setup_mcp_integration()

            # Unified tool server discovery (filtered)
            available_tools = await self.list_allowed_tool_servers()

            max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))

            # Centralized prompt rendering (system + optional tools) using helper.
            system_msg, tools_msg = self._render_prompts(available_tools, max_steps)

            # Initialize conversation from persisted session history
            async with self._request_lock:
                session_msgs = list(self._sessions.get(session_id, []))

            messages = [ChatMessage(role="system", content=system_msg)]
            if tools_msg:
                messages.append(ChatMessage(role="system", content=tools_msg))
            # include persisted session messages
            if session_msgs:
                messages.extend(session_msgs)
            # add the new user input as last message
            # Use initial_message if provided (for multimodal input), otherwise create from task
            if initial_message:
                messages.append(initial_message)
            else:
                messages.append(ChatMessage(role="user", content=sanitize_for_llm(task)))

            # Also include any appended messages already queued for this request
            async with self._request_lock:
                entry = self._active_requests.get(request_id)
                if isinstance(entry, dict):
                    appended = entry.get('appended', [])
                    if appended:
                        messages.extend(appended)
                        entry['appended'] = []

            # Track messages for debugging
            self._current_messages = messages.copy()

            # Build tool schemas and maintain mapping for external tools
            tools_schema: List[Dict] = []
            tool_name_mapping = {}  # Maps OpenAI-compatible names to original names

            # Build schemas for external MCP tools
            external_schemas, external_mapping = await self._mcp_integration_manager.build_tool_schemas(available_tools)
            tools_schema.extend(external_schemas)
            tool_name_mapping.update(external_mapping)

            # Build schemas for internal plugin tools and update available_tools for multi-tool plugins
            plugin_tools_to_add: List[str] = []  # Individual tool names to add to available_tools
            if self._mcp_integration_manager.mcp_integration and self._mcp_integration_manager.mcp_integration.initialized:
                plugin_registry = self._mcp_integration_manager.mcp_integration.plugin_registry
                for tool_name in available_tools.copy():  # Use copy to avoid modifying during iteration
                    if "." not in tool_name:  # Internal plugin tool
                        # Get plugin server from MCP integration plugin registry
                        plugin_adapter = plugin_registry.get_server(tool_name)
                        if plugin_adapter and hasattr(plugin_adapter, 'plugin_server'):
                            server = plugin_adapter.plugin_server
                            if hasattr(server, 'get_tools'):
                                # New multi-tool interface
                                server_tools = server.get_tools()
                                tools_schema.extend(server_tools)
                                # For multi-tool plugins, map individual tool names back to the registry name
                                for tool_schema in server_tools:
                                    if tool_schema.get("type") == "function" and "function" in tool_schema:
                                        individual_tool_name = tool_schema["function"].get("name")
                                        if individual_tool_name:
                                            tool_name_mapping[individual_tool_name] = tool_name
                                            plugin_tools_to_add.append(individual_tool_name)
                            else:
                                # Fallback to legacy single-tool interface
                                tools_schema.append(server.get_schema())
            
            # Add individual tool names to available_tools for multi-tool plugins
            available_tools.extend(plugin_tools_to_add)

            max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
            results: Dict[str, Any] = {"task": task, "calls": []}

            # Now that everything is set up and the forwarding task is definitely running,


            # Add safeguards against infinite loops
            consecutive_no_tool_calls = 0
            consecutive_empty_responses = 0
            max_consecutive_no_tools = 3  # Break after 3 consecutive responses without tool calls
            max_consecutive_empty = 2    # Break after 2 consecutive empty responses

            for step in range(max_steps):
                # Drain any appended user messages before each step
                messages = await self._drain_appended_messages(request_id, messages)
                
                # Reset context manager step state to prevent duplicate management
                if self.context_manager:
                    self.context_manager.reset_step_state()
                
                # Check for cancellation at the start of each step
                if self._is_cancelled(request_id):
                    logger.info("Request %s cancelled at step %d", request_id, step + 1)
                    yield {"type": "cancelled", "request_id": request_id, "step": step + 1}
                    # Signal cancellation using status contexts
                    await status_worker.error(f"cancelled at step {step + 1}", 
                                          meta={"step": step + 1, "reason": "cancelled"})
                    await status_coordinator.error(f"cancelled at step {step + 1}",
                                                meta={"step": step + 1, "reason": "cancelled"})
                    yield {"type": "end"}
                    return

                # Progress heartbeat using status_coordinator
                await status_coordinator.progress(
                    f"running step {step + 1}/{max_steps}",
                    meta={"step": step + 1, "max_steps": max_steps}
                )

                try:
                    yield {
                        "type": "heartbeat",
                        "message": f"{self.name}: running step {step + 1}/{max_steps}",
                        "step": step + 1,
                        "max_steps": max_steps,
                        "request_id": request_id,
                    }
                except Exception as e:
                    # If the consumer isn't expecting heartbeat, ignore
                    logger.debug(f"Failed to yield heartbeat: {e}")

                # Yield any pending status events
                for status_event in yield_pending_status_events():
                    yield status_event

                # Enhanced context management and token tracking
                messages, estimated_tokens = await self._context_management_handler.handle_context_management(messages, step, request_id)

                logger.debug("LLM messages: %s", [m.model_dump() for m in messages])

                # Emit thinking event before LLM call
                yield {"type": "thinking", "step": step + 1}

                # Signal LLM call using status_worker with profile info
                # Use profile info override if provided (from API-level LLM override)
                if llm_profile_info_override:
                    llm_display = f" ({llm_profile_info_override})"
                else:
                    llm_display = f" ({self.llm_profile_info})" if self.llm_profile_info else " (unknown LLM)"
                await status_worker.progress(f"Calling LLM{llm_display}", meta={"step": step + 1})

                # Validate messages before LLM call to ensure API compliance
                from agent_system.llm.message_validator import validate_messages_before_llm
                messages = validate_messages_before_llm(messages, context=f"agent_server_step_{step + 1}")

                # Get LLM response - handle context length exceeded errors
                try:
                    llm_out = await active_llm.chat_tools(messages, tools_schema, cancellation_token=main_token)
                except asyncio.CancelledError:  # pragma: no cover - explicit cancellation path
                    # Treat as graceful cancellation (user cancel or upstream timeout cancellation)
                    logger.info("Request %s received asyncio.CancelledError during LLM call at step %d", request_id, step + 1)
                    # Signal cancellation using status contexts
                    await status_worker.error("cancelled during LLM call (asyncio.CancelledError)", meta={"step": step + 1, "reason": "cancelled"})
                    await status_coordinator.error("cancelled during LLM call", meta={"step": step + 1, "reason": "cancelled"})
                    yield {"type": "cancelled", "request_id": request_id, "step": step + 1, "reason": "asyncio.CancelledError"}
                    yield {"type": "end"}
                    return
                except Exception as e:
                    # Check if this is a context length exceeded error
                    from agent_system.context.exceptions import ContextLengthExceededError
                    if isinstance(e, ContextLengthExceededError):
                        logger.warning("Context length exceeded, triggering emergency context management")
                        
                        # Check if we've already tried emergency context management too many times
                        if self._emergency_context_attempts >= self._max_emergency_attempts:
                            logger.error(f"Maximum emergency context attempts ({self._max_emergency_attempts}) exceeded, stopping agent")
                            llm_out = {"assistant": {"role": "assistant", "content": ""}}
                        elif self._context_management_handler.context_manager:
                            self._emergency_context_attempts += 1
                            logger.info(f"Applying emergency context management due to token limit (attempt {self._emergency_attempts}/{self._max_emergency_attempts})")
                            
                            try:
                                # First, try intelligent summarization if available
                                context_manager = self._context_management_handler.context_manager
                                
                                # Force summarization by temporarily changing strategy and lowering thresholds
                                original_strategy = context_manager.config.strategy
                                original_window = context_manager.context_window
                                original_threshold = context_manager.config.summarization_threshold
                                
                                # Set emergency summarization parameters
                                context_manager.config.strategy = "SUMMARIZE_OLDEST"
                                # Set a very low window to force aggressive summarization
                                context_manager.context_window = min(50000, original_window // 4)
                                context_manager.config.summarization_threshold = 1000  # Very low threshold
                                
                                logger.info("Attempting emergency summarization to preserve context")

                                
                                # Apply context management (will use summarization)
                                # Generate unique request ID for emergency context management
                                emergency_request_id = f"{request_id}_emergency" if request_id else None
                                summarized_messages = await context_manager.manage_context(messages, request_id=emergency_request_id)
                                
                                # Restore original settings
                                context_manager.config.strategy = original_strategy
                                context_manager.context_window = original_window
                                context_manager.config.summarization_threshold = original_threshold
                                
                                if len(summarized_messages) < len(messages):
                                    messages = summarized_messages
                                    logger.info(f"Emergency summarization successful: {len(messages)} messages after summarization")
                                else:
                                    # Summarization didn't reduce message count enough, fall back to truncation
                                    logger.warning("Summarization didn't reduce messages enough, falling back to truncation")
                                    raise ValueError("Summarization insufficient")
                                    
                            except Exception as summary_e:
                                logger.warning(f"Emergency summarization failed ({summary_e}), falling back to truncation")
                                
                                # Fallback to aggressive truncation - keep only last 5 messages + system
                                emergency_messages = []
                                if messages and getattr(messages[0], 'role', None) == 'system':
                                    emergency_messages.append(messages[0])
                                # Keep only the last 5 messages
                                emergency_messages.extend(messages[-5:])
                                
                                # If that's still not enough, keep only the last 3
                                if len(emergency_messages) > 6:  # system + 5 messages
                                    emergency_messages = [emergency_messages[0]] + emergency_messages[-3:]
                                
                                messages = emergency_messages
                                logger.info(f"Emergency truncation fallback: now have {len(messages)} messages")
                            
                            # Try the LLM call again with reduced context
                            try:
                                llm_out = await active_llm.chat_tools(messages, tools_schema, cancellation_token=main_token)
                                logger.info("LLM call successful after emergency context management")
                            except Exception as retry_e:
                                # Check if this is a cancellation exception in the retry
                                retry_error_str = str(retry_e).lower()
                                if "cancelled" in retry_error_str or "timeout" in retry_error_str:
                                    logger.info(f"Request {request_id} cancelled during retry LLM call: {retry_e}")
                                    # Signal cancellation using status contexts
                                    await status_worker.error(f"cancelled during retry LLM call: {retry_e}", 
                                                            meta={"step": step + 1, "reason": "cancelled"})
                                    await status_coordinator.error("cancelled during retry LLM call", 
                                                                 meta={"step": step + 1, "reason": "cancelled"})
                                    yield {"type": "cancelled", "request_id": request_id, "step": step + 1, "reason": str(retry_e)}
                                    yield {"type": "end"}
                                    return
                                
                                logger.error(f"LLM call failed even after emergency context management: {retry_e}")
                                # Return empty response to trigger agent stop
                                llm_out = {"assistant": {"role": "assistant", "content": ""}}
                        else:
                            logger.error("No context manager available for emergency context reduction")
                            # Return empty response to trigger agent stop
                            llm_out = {"assistant": {"role": "assistant", "content": ""}}
                    else:
                        # Check if this is a cancellation exception
                        error_str = str(e).lower()
                        if "cancelled" in error_str or "timeout" in error_str:
                            logger.info(f"Request {request_id} cancelled during LLM call: {e}")
                            # Signal cancellation using status contexts
                            await status_worker.error(f"cancelled during LLM call: {e}", 
                                                    meta={"step": step + 1, "reason": "cancelled"})
                            await status_coordinator.error("cancelled during LLM call", 
                                                         meta={"step": step + 1, "reason": "cancelled"})
                            yield {"type": "cancelled", "request_id": request_id, "step": step + 1, "reason": str(e)}
                            yield {"type": "end"}
                            return
                        
                        # Re-raise other exceptions
                        raise
                
                # Drain any messages that arrived during LLM call
                messages = await self._drain_appended_messages(request_id, messages)

                # Signal LLM call completion using status_worker
                await status_worker.progress("LLM (chat) response received", meta={"step": step + 1})

                assistant = llm_out.get("assistant", {})
                logger.debug("LLM assistant message (step %d): %s", step + 1, assistant)

                # Track actual token usage for streamed events if available
                await self._context_management_handler.update_token_usage(llm_out, estimated_tokens, len(messages))

                # Emit thinking event with LLM response content
                yield {"type": "thinking", "step": step + 1, "assistant": assistant}

                tool_calls = assistant.get("tool_calls") or []
                assistant_error = assistant.get("error") if isinstance(assistant, dict) else None
                content = assistant.get("content")

                # Normalize content variants for empty detection
                def _is_effectively_empty(c) -> tuple[bool, str]:  # (empty?, reason)
                    if c is None:
                        return True, "content=None"
                    # Text string
                    if isinstance(c, str):
                        if c.strip() == "":
                            return True, "content=empty-string"
                        return False, "non-empty-string"
                    # OpenAI style list of segments
                    if isinstance(c, list):
                        if len(c) == 0:
                            return True, "content=list-empty"
                        # Collect any non-empty text segments
                        has_text = False
                        for seg in c:
                            try:
                                if isinstance(seg, dict):
                                    # Accept either {type: 'text', text: '...'} or {type:'...','content': '...'}
                                    txt = seg.get("text") or seg.get("content")
                                    if isinstance(txt, str) and txt.strip():
                                        has_text = True
                                        break
                                elif isinstance(seg, str) and seg.strip():
                                    has_text = True
                                    break
                            except Exception as e:
                                logger.debug(f"Failed to check content segment: {e}")
                                continue
                        if has_text:
                            return False, "list-has-text"
                        return True, "list-only-empty-segments"
                    # Fallback for unexpected structure
                    return False, f"unexpected-type-{type(c).__name__}"

                is_empty_content, empty_reason = _is_effectively_empty(content)

                # Track consecutive responses without progress to prevent infinite loops
                # If LLM client surfaced a structured error, emit error event and break immediately
                if assistant_error:
                    # Structured error propagated by LLM client (e.g. invalid tool schema, 400 request error).
                    # Treat as hard failure instead of counting toward empty-response heuristic.
                    consecutive_empty_responses = 0
                    consecutive_no_tool_calls = 0
                    logger.error("LLM returned error payload (step %d): %s", step + 1, assistant_error)
                    results.setdefault("errors", []).append(f"LLM error: {assistant_error.get('message')}")
                    
                    # Check if this is a cancellation error and send status messages
                    error_message = assistant_error.get('message', '')
                    if 'cancelled' in error_message.lower() or 'timeout' in error_message.lower():
                        logger.info(f"Request {request_id} cancelled (from LLM error payload): {error_message}")
                        # Signal cancellation using status contexts
                        await status_worker.error(f"cancelled during LLM call: {error_message}", 
                                                meta={"step": step + 1, "reason": "cancelled"})
                        await status_coordinator.error("cancelled during LLM call", 
                                                     meta={"step": step + 1, "reason": "cancelled"})
                        yield {"type": "cancelled", "request_id": request_id, "step": step + 1, "reason": error_message}
                    else:
                        yield {"type": "error", "message": assistant_error.get("message", "LLM error"), "llm_error": assistant_error}
                    break

                if not tool_calls and is_empty_content:
                    consecutive_empty_responses += 1
                    consecutive_no_tool_calls += 1
                    logger.warning(
                        "LLM returned empty response (step %d), consecutive empty: %d (reason=%s)",
                        step + 1,
                        consecutive_empty_responses,
                        empty_reason,
                    )
                elif not tool_calls:
                    consecutive_no_tool_calls += 1
                    consecutive_empty_responses = 0  # Reset empty counter if we have content
                    logger.debug(
                        "LLM returned content without tool calls (step %d), consecutive no-tools: %d (reason=%s)",
                        step + 1,
                        consecutive_no_tool_calls,
                        empty_reason,
                    )
                else:
                    # Reset counters when we get tool calls (making progress)
                    consecutive_no_tool_calls = 0
                    consecutive_empty_responses = 0

                # Emergency break conditions to prevent infinite loops
                if consecutive_empty_responses >= max_consecutive_empty:
                    logger.warning(
                        "Breaking agent loop: %d consecutive empty responses (last_empty_reason=%s)",
                        consecutive_empty_responses,
                        empty_reason,
                    )
                    diagnostic = {
                        "empty_reason": empty_reason,
                        "assistant_keys": list(assistant.keys()) if isinstance(assistant, dict) else None,
                        "has_tool_calls": bool(tool_calls),
                        "raw_content_repr": repr(content)[:400],
                    }
                    # Keep legacy phrasing 'consecutive empty' for test compatibility while adding reason detail
                    results.setdefault("errors", []).append(
                        f"Agent stopped due to {consecutive_empty_responses} consecutive empty LLM responses (reason={empty_reason})"
                    )
                    yield {
                        "type": "error",
                        "message": f"Agent stopped due to {consecutive_empty_responses} consecutive empty LLM responses (reason={empty_reason})",
                        "diagnostic": diagnostic,
                    }
                    break
                
                if consecutive_no_tool_calls >= max_consecutive_no_tools:
                    logger.warning("Breaking agent loop: %d consecutive responses without tool calls", consecutive_no_tool_calls)
                    # If we have content in the last response, treat it as final
                    if content:
                        messages.append(ChatMessage(role="assistant", content=content or ""))
                        results["summary"] = content
                        self._current_messages = messages.copy()
                        yield {"type": "final", "summary": content}
                    else:
                        results.setdefault("errors", []).append(f"Agent stopped due to {consecutive_no_tool_calls} consecutive responses without tool calls")
                        yield {"type": "error", "message": f"Agent stopped due to {consecutive_no_tool_calls} consecutive responses without tool calls"}
                    break

                # Execute ALL tool calls with immediate streaming
                if tool_calls:
                    # Check for cancellation before executing tools
                    if self._is_cancelled(request_id):
                        logger.info("Request %s cancelled before tool execution at step %d", request_id, step + 1)
                        yield {"type": "cancelled", "request_id": request_id, "step": step + 1}
                        await status_worker.error(f"cancelled before tool execution at step {step + 1}", 
                                              meta={"step": step + 1, "reason": "cancelled"})
                        await status_coordinator.error(f"cancelled before tool execution at step {step + 1}",
                                                    meta={"step": step + 1, "reason": "cancelled"})
                        yield {"type": "end"}
                        return

                    await status_worker.progress(f"Executing Tools ({len(tool_calls)} total)", meta={"step": step + 1})

                    # Add assistant message with ALL tool calls to conversation
                    messages.append(ChatMessage(role="assistant", content=content or "", tool_calls=tool_calls))

                    # Execute all tools using the component
                    tool_messages, tool_events, tool_results = await self._tool_execution_manager.execute_tools(
                        tool_calls, tool_name_mapping, available_tools, step, request_id=request_id
                    )
                    
                    # Yield the tool events
                    for event in tool_events:
                        yield event
                    
                    # Add tool results to the results dictionary
                    results["calls"].extend(tool_results)
                    
                    # Add tool messages to conversation
                    messages.extend(tool_messages)

                # Check for final content
                elif content:
                    # Filter out OpenAI's meta-messages that should be ignored
                    content_str = content.strip() if isinstance(content, str) else str(content)
                    if content_str.startswith("(Note: last assistant message duplicated"):
                        # OpenAI detected a duplicate message - this typically means the LLM
                        # has nothing new to add. We should prompt it to summarize the tool results.
                        logger.debug("OpenAI duplicate message warning at step %d - prompting for summary", step + 1)
                        consecutive_no_tool_calls += 1
                        
                        # Add a user message to explicitly ask for a summary
                        messages.append(ChatMessage(
                            role="user",
                            content="Please provide a brief summary of the results from the tool execution above."
                        ))
                        # Continue to next iteration to let LLM respond
                    else:
                        # Append assistant final message to conversation history
                        messages.append(ChatMessage(role="assistant", content=content or ""))
                        results["summary"] = content
                        # Update tracked messages with final response
                        self._current_messages = messages.copy()
                        yield {"type": "final", "summary": content}
                        break
                # If we had tool calls, continue to next iteration to let LLM respond to tool results
                # Don't add extra assistant messages here as it creates invalid conversation flow

                # Update tracked messages at end of each step
                self._current_messages = messages.copy()
                
                # Drain any final appended messages before next step  
                messages = await self._drain_appended_messages(request_id, messages)

            else:
                # Max steps reached - get final answer
                try:
                    # Validate messages before final LLM call
                    from agent_system.llm.message_validator import validate_messages_before_llm
                    messages = validate_messages_before_llm(messages, context="agent_server_final")
                    
                    final_llm_out = await active_llm.chat_tools(messages, [], cancellation_token=main_token)
                    final_assistant = final_llm_out.get("assistant", {})
                    final_content = final_assistant.get("content")
                    if final_content:
                        # Append final assistant message to conversation history
                        messages.append(ChatMessage(role="assistant", content=final_content or ""))
                        results["summary"] = final_content
                        # Update tracked messages and emit final event
                        self._current_messages = messages.copy()
                        yield {"type": "final", "summary": final_content}
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
                        yield {"type": "end"}
                        return
                    
                    logger.exception("Failed to get final answer: %s", e)
                    results.setdefault("errors", []).append(f"Failed to get final answer: {e}")
                    yield {"type": "error", "message": f"Failed to get final answer: {e}"}

        except Exception as e:
            logger.exception("Agent execution failed with exception:")
            yield {"type": "error", "message": f"Agent execution failed: {e}"}
        finally:
            # Clean up cancellation token
            cancellation_manager = get_cancellation_manager()
            cancellation_manager.unregister_request(request_id)
            
            # Clean up request tracking but preserve session data
            async with self._request_lock:
                if request_id in self._active_requests:
                    del self._active_requests[request_id]
                    logger.debug("Cleaned up request tracking for %s", request_id)
                
                # Persist session messages and keep the request->session mapping for a while
                sid = self._request_to_session.get(request_id)
                if sid and 'messages' in locals() and messages:
                    try:
                        # Filter out system messages - only persist conversation history
                        conversation_msgs = [msg for msg in messages if msg.role != "system"]
                        # Update the persistent session with conversation state (no system messages)
                        self._sessions[sid] = conversation_msgs.copy()
                        logger.debug("Persisted session %s with %d conversation messages", sid, len(conversation_msgs))
                        # Keep the request->session mapping (don't pop it immediately)
                        # This allows append requests that arrive shortly after completion to find the session
                    except Exception as e:
                        logger.warning(f"Failed to persist session {sid}: {e}", exc_info=True)

            # Clean up MCP integration if we initialized it locally
            await self._mcp_integration_manager.shutdown()
            # Reset the current_request_id ContextVar so it doesn't leak to other tasks
            try:
                if token is not None:
                    current_request_id.reset(token)
            except Exception:
                pass
        # Signal completion using status contexts
        final_msg = "completed" if not ('results' in locals() and results.get('errors')) else "completed with errors"
        
        if 'results' in locals() and results.get('errors'):
            await status_worker.error(final_msg, 
                                    meta={"summary": results.get('summary') if 'results' in locals() else None})
            await status_coordinator.error(f"{final_msg} ({step+1} steps)",
                                         meta={"summary": results.get('summary') if 'results' in locals() else None})
        else:
            await status_worker.end(final_msg,
                                  meta={"summary": results.get('summary') if 'results' in locals() else None})
            await status_coordinator.end(f"{final_msg} ({step+1} steps)",
                                       meta={"summary": results.get('summary') if 'results' in locals() else None})

        # Give a small delay to allow final status events to be processed by the forwarding task
        await asyncio.sleep(0.01)

        # Clean up status forwarding task AFTER publishing final status
        await self._status_event_forwarder.stop_forwarding()

        # Yield any final pending status events before ending
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
        self._sessions.clear()
        self._request_to_session.clear()
        
        logger.info("Agent shutdown completed")

    # MCPServer interface implementation
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """
        MCPServer interface: Handle tool calls from other agents.

        Args:
            tool: The tool/action to execute (should be "run" or "execute")
            params: Parameters including the task to execute

        Returns:
            The agent's execution result
        """
        # Validate action
        if tool not in ["run", "execute", "ask"]:
            return {
                "status": "error",
                "error": f"Unknown action '{tool}'. Available actions: run, execute, ask"
            }

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
            from .result_utils import collect_final_result
            result = await collect_final_result(self, str(task))

            # Wrap result with agent metadata
            return {
                "status": "success",
                "agent": self.name,
                "task": task,
                "result": result,
                "summary": self._extract_summary(result)
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
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": getattr(self.agent_config, 'description', None) or getattr(self, '_agent_description', f"Agent: {self.name}"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["run", "execute", "ask"],
                            "description": "Action to perform (run/execute/ask the agent)"
                        },
                        "task": {
                            "type": "string",
                            "description": "The task/query/prompt to execute"
                        }
                    },
                    "required": ["task"],
                },
            },
        }

    def get_default_action(self) -> str:
        """MCPServer interface: Return the default action for this agent."""
        return "run"

    def _extract_summary(self, result: Dict[str, Any]) -> str:
        """
        Extract a summary from the agent result for easier consumption.

        Args:
            result: The agent execution result

        Returns:
            A summary string
        """
        if isinstance(result, dict):
            # Look for summary in result
            if "summary" in result:
                return str(result["summary"])

            # If there are successful tool calls, summarize them
            calls = result.get("calls", [])
            if calls:
                successful_calls = [c for c in calls if "error" not in str(c.get("result", ""))]
                if successful_calls:
                    return f"Executed {len(successful_calls)} tool(s) successfully"

            # Check for errors
            errors = result.get("errors", [])
            if errors:
                return f"Failed with {len(errors)} error(s): {errors[0]}"

            return "Task completed"

        return str(result)[:200]  # Fallback to string representation
