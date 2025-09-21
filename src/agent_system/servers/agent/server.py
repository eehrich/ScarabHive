"""
Enhanced Agent Core - Agent extends MCPServer for direct agent-to-agent communication
Supports multiple tool calls per conversation turn for better efficiency
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from ...config.models import AgentConfig
from ...mcp.base import MCPRegistry, MCPServer
from ...llm.clients import ChatMessage
from ...utils.prompt_renderer import render_prompts
from ...utils.text_sanitizer import sanitize_for_llm, sanitize_json_content
from ...context import ContextManager, ConversationSummarizer, TokenOptimizer
from ...context.agent_tracker import register_agent_for_tracking, update_agent_context_usage
from ...mcp.status import publish_status, PHASE_START, PHASE_PROGRESS, PHASE_END, PHASE_ERROR
from .planner import Planner
from .executor import Executor


logger = logging.getLogger(__name__)


class Agent(MCPServer):
    """
    Enhanced Agent that executes ALL tool calls per LLM conversation turn.
    Also serves as an MCP Server that can be used by other agents as a tool.
    This enables direct agent-to-agent communication without wrapper classes.
    """

    def __init__(self, name: str, config: AgentConfig, registry: MCPRegistry,
                 agent_config: dict | None = None, ssl_verify: bool = True,
                 llm: object | None = None, llm_factory: object | None = None) -> None:
        """
        Initialize Agent as both an executor and an MCP Server.

        Args:
            name: Name of this agent (used when serving as MCP Server)
            config: Agent configuration
            registry: MCP Registry with available tools
            agent_config: Optional agent-specific config (description, etc.)
            ssl_verify: SSL verification setting
        """
        # Initialize as MCPServer
        super().__init__(name, agent_config, ssl_verify)

        # Agent-specific initialization
        self.agent_config = config
        self.registry = registry
        # Mark this Agent as internal by default so it doesn't show up in UI lists
        # Consumers who want it visible can set `agent._mcp_public = True` after construction.
        self._mcp_public = False
        # Allow dependency injection of an LLM client or a factory that
        # creates one. This makes testing and runtime wiring explicit.
        self.llm = llm
        self._llm_factory = llm_factory

        # Set default description
        if "description" not in self.config:
            self.config["description"] = f"Agent: {name}"

        # Initialize LLM if not provided. Prefer an explicitly passed `llm`.
        if self.llm is None:
            # If a factory is provided, use it to create the client.
            if self._llm_factory is not None:
                try:
                    self.llm = self._llm_factory.create()
                except Exception as e:
                    logger.warning("LLM factory creation failed: %s", e)
                    self.llm = None
            else:
                # Fallback: attempt to create LLM directly from config if available.
                try:
                    # Lazy import to avoid circular imports when testing
                    from ...llm.clients import make_llm
                    if getattr(config, "llm", None):
                        self.llm = make_llm(
                            config.llm.provider,
                            config.llm.model,
                            config.llm.openai_api_key,
                            config.llm.ollama_url,
                            config.llm.context_window,
                            getattr(config.llm, "ollama_mode", None),
                            getattr(config.llm, "request_timeout", None),
                            ssl_verify=getattr(config, "network").ssl_verify if getattr(config, "network", None) else None,
                        )
                except Exception as e:
                    # Missing API key is an expected situation in test/dev
                    # environments; avoid noisy warnings for that case.
                    try:
                        msg = str(e)
                    except Exception:
                        msg = "<exception>"
                    # Match the exact ValueError message emitted by make_llm
                    if isinstance(e, ValueError) and msg == "OPENAI_API_KEY is required when provider=openai":
                        logger.debug("LLM not initialized (no API key): %s", msg)
                    else:
                        logger.warning("LLM initialization failed: %s", msg)
                    self.llm = None

        # Initialize context management system
        self._init_context_management()

        # Register agent with context tracker
        if hasattr(self, 'context_manager') and self.context_manager:
            register_agent_for_tracking(name, name, self.context_manager.config.context_window)
        else:
            # Use default context window if no context manager
            register_agent_for_tracking(name, name, 32768)

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

    def _init_context_management(self):
        """Initialize the context management system."""
        try:
            # Read context window from LLM config if present
            context_window = self.agent_config.llm.context_window if getattr(self.agent_config, 'llm', None) else 32768

            # Read user-provided context management settings (may be None)
            context_mgmt = getattr(self.agent_config, 'context_management', None) or {}

            # Normalize to dict if it's a pydantic model
            if hasattr(context_mgmt, '__dict__'):
                context_mgmt_settings = vars(context_mgmt)
            else:
                context_mgmt_settings = dict(context_mgmt)

            # Create ContextConfig with proper parameters from context_mgmt_settings
            from ...context.config import ContextConfig, ContextStrategy

            # Convert strategy string to enum if needed
            strategy_str = context_mgmt_settings.get("strategy", "SUMMARIZE_OLDEST")
            if isinstance(strategy_str, str):
                strategy = ContextStrategy(strategy_str)
            else:
                strategy = strategy_str

            # summarization_threshold may be provided as float (percentage) or int (absolute tokens)
            summarization_threshold = context_mgmt_settings.get("summarization_threshold", 0.80)
            # If percentage (0..1) leave as-is; ContextConfig.from_dict handles translation to tokens

            self.context_config = ContextConfig(
                context_window=int(context_window),
                summarization_threshold=summarization_threshold,
                prediction_threshold=context_mgmt_settings.get("prediction_threshold", 0.90),
                preserve_recent_messages=context_mgmt_settings.get("preserve_recent_messages", 10),
                strategy=strategy,
                max_summary_words=context_mgmt_settings.get("max_summary_words", 500),
                tool_result_preview_chars=context_mgmt_settings.get("tool_result_preview_chars", 200),
                enable_compression=context_mgmt_settings.get("optimization", {}).get("enabled", True),
                compress_tool_results=context_mgmt_settings.get("optimization", {}).get("compress_tool_results", True)
            )

            # Initialize context manager
            self.context_manager = ContextManager(self.context_config)

            # Initialize and set summarizer with dedicated LLM client
            # Create a separate LLM client for summarization to prevent recursive context management
            summarizer_llm = None
            if self.llm and getattr(self.agent_config, "llm", None):
                try:
                    # Create a direct LLM client that bypasses context management
                    from ...llm.clients import make_llm
                    summarizer_llm = make_llm(
                        self.agent_config.llm.provider,
                        self.agent_config.llm.model,
                        self.agent_config.llm.openai_api_key,
                        self.agent_config.llm.ollama_url,
                        self.agent_config.llm.context_window,
                        getattr(self.agent_config.llm, "ollama_mode", None),
                        getattr(self.agent_config.llm, "request_timeout", None),
                        ssl_verify=getattr(self.agent_config, "network").ssl_verify if getattr(self.agent_config, "network", None) else None,
                    )
                except Exception as e:
                    logger.warning("Failed to create dedicated summarizer LLM client: %s", e)
                    summarizer_llm = None

            summarizer = ConversationSummarizer(summarizer_llm)
            self.context_manager.set_summarizer(summarizer)

            # Initialize optimizer
            self.token_optimizer = TokenOptimizer()

            # Optimizer run guard: avoid repeated optimizer runs when token usage
            # hasn't increased significantly since the last run.
            self._last_optimizer_tokens_snapshot = 0
            self._last_optimizer_run_time = 0.0
            # Cooldown in seconds between optimizer attempts when insufficient growth
            self._optimizer_cooldown_seconds = 10.0  # Increased from 1.0 to 10.0 seconds
            # Minimum token increase required to trigger optimizer again
            self._optimizer_min_increase_tokens = max(200, int(self.context_config.context_window * 0.05))  # Increased threshold
            # Skip optimizer for N steps after context management
            self._skip_optimizer_steps_after_context_mgmt = 0

            logger.info("Context management initialized - window: %d, summarization threshold: %d, prediction threshold: %.1f%%",
                      self.context_config.context_window,
                      int(self.context_config.context_window * self.context_config.summarization_threshold),
                      self.context_config.prediction_threshold * 100)

        except Exception as e:
            logger.warning("Context management initialization failed: %s", e)
            self.context_manager = None
            self.token_optimizer = None


    @property
    def description(self) -> str:
        """Get the agent description."""
        return self.config.get("description", f"Agent: {self.name}")

    async def cancel_request(self, request_id: str) -> bool:
        """
        Cancel an active request by setting its cancellation event.

        Args:
            request_id: The unique ID of the request to cancel

        Returns:
            True if the request was found and cancelled, False otherwise
        """
        async with self._request_lock:
            if request_id in self._active_requests:
                logger.info("Cancelling request %s", request_id)
                try:
                    self._active_requests[request_id]["cancel"].set()
                except Exception:
                    # Defensive: if structure unexpected, try old-style event
                    if isinstance(self._active_requests[request_id], asyncio.Event):
                        self._active_requests[request_id].set()
                return True
            else:
                logger.warning("Request %s not found for cancellation", request_id)
                return False

    def _is_cancelled(self, request_id: Optional[str]) -> bool:
        """
        Check if a request has been cancelled.

        Args:
            request_id: The unique ID of the request to check

        Returns:
            True if the request has been cancelled, False otherwise
        """
        if request_id and request_id in self._active_requests:
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
                        except Exception:
                            pass
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
                    except Exception:
                        pass
        return messages

    async def run(self, task: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Run the agent task with enhanced tool calling logic.
        Executes ALL tool calls from LLM per turn for better efficiency.
        """
        results: Dict[str, Any] = {"task": task, "calls": []}

        if self.llm is None:
            results["errors"] = ["No LLM available; agent requires an LLM to plan tool usage."]
            return results

        # Use module-level logger instead of redefining it
        try:
            # Generate a transient request_id for run() to correlate status messages
            if request_id is None:
                request_id = str(uuid.uuid4())

            # Initialize MCP integration tracking
            mcp_integration = None
            mcp_initialized_locally = False

            # Get tools from the local registry (plugins)
            available_tools = self.registry.list()

            # Also include tools from external MCP servers
            try:
                from ...mcp.integration import get_mcp_integration
                mcp_integration = get_mcp_integration()
                if not mcp_integration.initialized:
                    # Initialize with the same configuration as the agent
                    if hasattr(self.agent_config, 'mcp'):
                        mcp_config = {"mcp": self.agent_config.mcp.model_dump() if hasattr(self.agent_config.mcp, "model_dump") else getattr(self.agent_config.mcp, "__dict__", {})}
                        await mcp_integration.initialize(mcp_config)
                        mcp_initialized_locally = True
                        logger.debug("Initialized MCP integration for agent")

                if mcp_integration and mcp_integration.initialized:
                    all_tools = await mcp_integration.list_all_tools()
                    # Add external server tools to available tools
                    for server_name, tools in all_tools.get("external_servers", {}).items():
                        for tool in tools:
                            tool_name = f"{server_name}.{tool['name']}"
                            available_tools.append(tool_name)
                            logger.debug("Added external tool: %s", tool_name)
            except Exception as e:
                logger.debug("Failed to get external MCP tools: %s", e)

            max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))

            # Render prompts
            rendered = render_prompts(
                self.agent_config.prompts.system_template,
                {"tools": available_tools, "max_steps": max_steps},
                auto_datetime=self.agent_config.context.auto_datetime,
                timezone=self.agent_config.context.timezone,
                location=self.agent_config.context.location
            )
            system_msg = rendered.get("system_prompt") or "You are an assistant agent."
            tools_msg = rendered.get("tools_prompt")

            # Use Planner to build initial messages and call the LLM
            planner = Planner(self.llm, system_msg, tools_msg)
            messages = planner.initial_messages(task)

            # Track messages for debugging
            self._current_messages = messages.copy()

            # Build tool schemas and maintain mapping for external tools
            tools_schema: List[Dict] = []
            tool_name_mapping = {}  # Maps OpenAI-compatible names to original names

            for tool_name in available_tools:
                # Check if it's an external tool (contains a dot)
                if "." in tool_name:
                    server_name, actual_tool_name = tool_name.split(".", 1)
                    # Create OpenAI-compatible name (replace dots with underscores)
                    openai_tool_name = tool_name.replace(".", "_")
                    tool_name_mapping[openai_tool_name] = tool_name

                    # Create a schema for external tools
                    try:
                        from ...mcp.integration import get_mcp_integration
                        mcp_integration = get_mcp_integration()
                        if mcp_integration and mcp_integration.initialized:
                            all_tools = await mcp_integration.list_all_tools()
                            external_tools = all_tools.get("external_servers", {}).get(server_name, [])
                            for tool in external_tools:
                                if tool["name"] == actual_tool_name:
                                    schema = {
                                        "type": "function",
                                        "function": {
                                            "name": openai_tool_name,
                                            "description": f"[{server_name}] {tool['description']}",
                                            "parameters": tool.get("input_schema", {})
                                        }
                                    }
                                    tools_schema.append(schema)
                                    break
                    except Exception as e:
                        logger.debug("Failed to build schema for external tool %s: %s", tool_name, e)
                else:
                    # Regular plugin tool - support multiple tools per server
                    server = self.registry.get(tool_name)
                    if hasattr(server, 'get_tools'):
                        # New multi-tool interface
                        server_tools = server.get_tools()
                        tools_schema.extend(server_tools)
                    else:
                        # Fallback to legacy single-tool interface
                        tools_schema.append(server.get_schema())

            max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))

            executor = Executor(self.registry)

            # Add safeguards against infinite loops
            consecutive_no_tool_calls = 0
            consecutive_empty_responses = 0
            max_consecutive_no_tools = 3  # Break after 3 consecutive responses without tool calls
            max_consecutive_empty = 2    # Break after 2 consecutive empty responses

            for step in range(max_steps):
                # Enhanced context management and token tracking
                if self.context_manager:
                    # Apply token optimization with centralized guard to avoid repeated runs
                    if self.token_optimizer:
                        # Skip optimizer for a few steps after context management
                        if getattr(self, '_skip_optimizer_steps_after_context_mgmt', 0) > 0:
                            self._skip_optimizer_steps_after_context_mgmt -= 1
                            logger.debug("Skipping token optimizer: %d steps remaining after context mgmt",
                                       self._skip_optimizer_steps_after_context_mgmt)
                        else:
                            try:
                                import time
                                now = time.time()
                                # Estimate current tokens
                                estimated_tokens_now = self.context_manager.estimate_token_count(messages)

                                tokens_growth = estimated_tokens_now - getattr(self, '_last_optimizer_tokens_snapshot', 0)
                                time_since_last = now - getattr(self, '_last_optimizer_run_time', 0.0)

                                should_run_optimizer = False
                                # Run if tokens grew sufficiently since last run
                                if tokens_growth >= getattr(self, '_optimizer_min_increase_tokens', 200):
                                    should_run_optimizer = True
                                # Or if enough time passed since last run (cooldown)
                                elif time_since_last >= getattr(self, '_optimizer_cooldown_seconds', 10.0):
                                    should_run_optimizer = True

                                if should_run_optimizer:
                                    messages = await self.token_optimizer.optimize_messages(messages)
                                    # update snapshot
                                    self._last_optimizer_tokens_snapshot = self.context_manager.estimate_token_count(messages)
                                    self._last_optimizer_run_time = now
                                else:
                                    logger.debug("Skipping token optimizer: growth=%d, time_since_last=%.2fs", tokens_growth, time_since_last)
                            except Exception as e:
                                logger.debug("Token optimizer guard check failed: %s", e)

                    # Check token count and issue appropriate warnings
                    estimated_tokens, warning_level = self.context_manager.check_and_warn(messages, step)

                    # Apply context management if needed
                    if self.context_manager.should_manage_context(estimated_tokens, warning_level):
                        logger.info("Applying context management at step %d", step + 1)
                        messages = await self.context_manager.manage_context(messages)
                        # Skip optimizer for next 2 steps after context management
                        self._skip_optimizer_steps_after_context_mgmt = 2
                        # Re-check after management
                        estimated_tokens, _ = self.context_manager.check_and_warn(messages, step)

                    # Update context stats for this agent
                    try:
                        update_agent_context_usage(
                            self.name,
                            current_tokens=estimated_tokens,
                            predicted_tokens=estimated_tokens,
                            message_count=len(messages)
                        )
                    except Exception as e:
                        logger.debug("Failed to update agent context stats: %s", e)
                else:
                    # Fallback to legacy token warning
                    message_count = len(messages)
                    estimated_tokens = self._estimate_token_count(messages)
                    context_window = getattr(self.agent_config, "context_window", 32768)
                    logger.debug("LLM input (step %d): %d messages, ~%d tokens (context: %d)",
                               step + 1, message_count, estimated_tokens, context_window)

                    if estimated_tokens > context_window * 0.9:  # 90% threshold
                        logger.warning("Token count approaching context window limit: %d/%d tokens",
                                     estimated_tokens, context_window)

                logger.debug("LLM messages: %s", [m.model_dump() for m in messages])

                # Best-effort: publish a PHASE_START for the whole run when first entering run()
                if step == 0:
                    # High-level operation status using PHASE_PROGRESS so it doesn't conflict with LLM PHASE_START
                    await publish_status(
                        server=f"{self.name}_coordinator",
                        message=f"Processing: {task[:50]}{'...' if len(task) > 50 else ''}", 
                        request_id=request_id, 
                        phase=PHASE_PROGRESS
                    )
                    await asyncio.sleep(0)

                # Emit status: calling LLM (planner) - technical detail level
                await publish_status(
                    server=self.name, 
                    message="Calling LLM (planner)", 
                    request_id=request_id, 
                    phase=PHASE_PROGRESS, 
                    meta={"step": step + 1}
                )
                await asyncio.sleep(0)

                # Get LLM response via Planner
                llm_out = await planner.chat(messages, tools_schema)

                # Emit status: LLM call complete
                await publish_status(
                    erver=self.name, 
                    message="LLM (planner) response received", 
                    request_id=request_id, 
                    phase=PHASE_PROGRESS, 
                    meta={"step": step + 1}
                )
                assistant = llm_out.get("assistant", {})
                logger.debug("LLM assistant message (step %d): %s", step + 1, assistant)

                # Track actual token usage if available and context manager is active
                if self.context_manager and 'usage' in llm_out:
                    usage_data = llm_out['usage']
                    self.context_manager.update_token_usage(usage_data)
                    logger.debug("Updated token usage from LLM response: %s", usage_data)

                    # Update agent context tracker with actual LLM tokens.
                    # Always call the tracker so that LLM call counts are incremented
                    # even when the token count is zero or missing.
                    try:
                        # Extract total tokens from LLM response and coerce to int safely
                        raw_total = usage_data.get('total_tokens', 0)
                        try:
                            actual_tokens = int(raw_total or 0)
                        except Exception:
                            try:
                                actual_tokens = int(float(str(raw_total)))
                            except Exception:
                                actual_tokens = 0

                        update_agent_context_usage(
                            self.name,
                            current_tokens=estimated_tokens,
                            predicted_tokens=estimated_tokens,
                            message_count=len(messages),
                            actual_tokens=actual_tokens
                        )
                    except Exception as e:
                        logger.debug("Failed to update agent context with LLM tokens: %s", e)

                tool_calls = assistant.get("tool_calls") or []
                content = assistant.get("content")

                # Track consecutive responses without progress to prevent infinite loops
                if not tool_calls and not content:
                    consecutive_empty_responses += 1
                    consecutive_no_tool_calls += 1
                    logger.warning("LLM returned empty response (step %d), consecutive empty: %d", 
                                 step + 1, consecutive_empty_responses)
                elif not tool_calls:
                    consecutive_no_tool_calls += 1
                    consecutive_empty_responses = 0  # Reset empty counter if we have content
                    logger.debug("LLM returned content without tool calls (step %d), consecutive no-tools: %d", 
                               step + 1, consecutive_no_tool_calls)
                else:
                    # Reset counters when we get tool calls (making progress)
                    consecutive_no_tool_calls = 0
                    consecutive_empty_responses = 0

                # Emergency break conditions to prevent infinite loops
                if consecutive_empty_responses >= max_consecutive_empty:
                    logger.warning("Breaking agent loop: %d consecutive empty responses", consecutive_empty_responses)
                    results.setdefault("errors", []).append(f"Agent stopped due to {consecutive_empty_responses} consecutive empty LLM responses")
                    # Best-effort publish terminal error status
                    await publish_status(
                        server=self.name,
                        message=f"{self.name}: stopped due to {consecutive_empty_responses} empty responses",
                        request_id=request_id,
                        phase=PHASE_ERROR,
                        meta={"consecutive_empty": consecutive_empty_responses}
                    )
                    break
                
                if consecutive_no_tool_calls >= max_consecutive_no_tools:
                    logger.warning("Breaking agent loop: %d consecutive responses without tool calls", consecutive_no_tool_calls)
                    # If we have content in the last response, treat it as final
                    if content:
                        messages.append(ChatMessage(role="assistant", content=content or ""))
                        results["summary"] = content
                    else:
                        results.setdefault("errors", []).append(f"Agent stopped due to {consecutive_no_tool_calls} consecutive responses without tool calls")
                    # Best-effort: publish terminal status
                    await publish_status(
                        server=self.name,
                        message=f"{self.name}: stopped after {consecutive_no_tool_calls} responses without tool calls",
                        request_id=request_id,
                        phase=PHASE_PROGRESS if content else PHASE_ERROR,
                        meta={"consecutive_no_tool_calls": consecutive_no_tool_calls}
                    )
                    break

                # Execute ALL tool calls in parallel for better performance
                if tool_calls:
                    # Add assistant message with ALL tool calls to conversation
                    messages.append(ChatMessage(role="assistant", content=content or "", tool_calls=tool_calls))

                    async def execute_single_tool_call(tc: Dict[str, Any], index: int) -> Dict[str, Any]:
                        """Execute a single tool call and return structured result."""
                        func = tc.get("function", {})
                        openai_tool_name = func.get("name")  # This is the OpenAI-compatible name
                        raw_args = func.get("arguments")

                        # Map back to original tool name if it was converted
                        tool_name = tool_name_mapping.get(openai_tool_name, openai_tool_name)

                        # Parse arguments
                        params: Dict[str, Any] = {}
                        if isinstance(raw_args, str) and raw_args:
                            try:
                                params = json.loads(raw_args)
                            except json.JSONDecodeError:
                                logger.warning("Failed to parse tool arguments: %s", raw_args)
                                params = {}
                        elif isinstance(raw_args, dict):
                            params = raw_args

                        if not tool_name or tool_name not in available_tools:
                            logger.warning("Unknown tool requested: %s (OpenAI name: %s)", tool_name, openai_tool_name)
                            # Return error result
                            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}-{index}"
                            error_content = json.dumps({"error": f"Tool '{tool_name}' is not available."})
                            return {
                                "success": False,
                                "message": ChatMessage(
                                    role="tool",
                                    tool_call_id=tool_call_id,
                                    name=sanitize_for_llm(openai_tool_name or "unknown"),
                                    content=sanitize_json_content(error_content)
                                ),
                                "call_info": None,
                                "error": f"Tool '{tool_name}' is not available."
                            }

                        # Execute tool call
                        if "." in tool_name:
                            # External tool - call via MCP integration
                            server_name, actual_tool_name = tool_name.split(".", 1)
                            try:
                                logger.info("Invoking external tool %s on server %s with params %s", actual_tool_name, server_name, params)
                                from ...mcp.integration import get_mcp_integration
                                mcp_integration = get_mcp_integration()
                                tool_result = await mcp_integration.call_tool(server_name, actual_tool_name, params, "external")
                                logger.info("External tool %s returned: %s", tool_name, str(tool_result)[:500])

                                call_info = {
                                    "server": tool_name,
                                    "action": actual_tool_name,
                                    "params": params,
                                    "result": tool_result
                                }

                                # Create tool result message
                                tool_call_id = tc.get("id") or f"{tool_name}-call-{int(time.time()*1000)}"
                                tool_msg_content = json.dumps(tool_result, ensure_ascii=False)
                                tool_msg_content = sanitize_json_content(tool_msg_content)
                                
                                return {
                                    "success": True,
                                    "message": ChatMessage(
                                        role="tool",
                                        tool_call_id=tool_call_id,
                                        name=sanitize_for_llm(openai_tool_name),
                                        content=tool_msg_content
                                    ),
                                    "call_info": call_info,
                                    "error": None
                                }
                            except Exception as e:
                                logger.exception("External tool %s invocation failed: %s", tool_name, e)
                                tool_call_id = tc.get("id") or f"{tool_name}-error-{int(time.time()*1000)}"
                                error_content = json.dumps({"error": f"Tool invocation failed: {str(e)}"})
                                return {
                                    "success": False,
                                    "message": ChatMessage(
                                        role="tool",
                                        tool_call_id=tool_call_id,
                                        name=sanitize_for_llm(openai_tool_name),
                                        content=sanitize_json_content(error_content)
                                    ),
                                    "call_info": None,
                                    "error": str(e)
                                }
                        else:
                            # Plugin tool - use existing logic
                            server = self.registry.get(tool_name)
                            action_name = params.get("action") or params.get("tool") or server.get_default_action()

                            # Validate action against server schema
                            schema = server.get_schema()
                            valid_actions = []
                            if "function" in schema and "parameters" in schema["function"]:
                                action_prop = schema["function"]["parameters"].get("properties", {}).get("action", {})
                                valid_actions = action_prop.get("enum", [])

                            if valid_actions and action_name not in valid_actions:
                                logger.warning("Invalid action '%s' for tool %s, valid actions: %s. Using default action.",
                                             action_name, tool_name, valid_actions)
                                action_name = server.get_default_action()
                                params["action"] = action_name

                            try:
                                logger.info("Invoking tool %s action %s with params %s", tool_name, action_name, params)
                                tool_result = await executor.invoke(tool_name, params)
                                logger.info("Tool %s returned: %s", tool_name, str(tool_result)[:500])

                                call_info = {
                                    "server": tool_name,
                                    "action": action_name,
                                    "params": params,
                                    "result": tool_result
                                }

                                # Create tool result message
                                tool_call_id = tc.get("id") or f"{tool_name}-call-{int(time.time()*1000)}"
                                tool_msg_content = json.dumps(tool_result, ensure_ascii=False)
                                tool_msg_content = sanitize_json_content(tool_msg_content)
                                
                                return {
                                    "success": True,
                                    "message": ChatMessage(
                                        role="tool",
                                        tool_call_id=tool_call_id,
                                        name=openai_tool_name,
                                        content=tool_msg_content
                                    ),
                                    "call_info": call_info,
                                    "error": None
                                }

                            except Exception as e:
                                logger.exception("Tool %s invocation failed: %s", tool_name, e)
                                tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                                error_content = json.dumps({"error": sanitize_for_llm(str(e))})
                                return {
                                    "success": False,
                                    "message": ChatMessage(
                                        role="tool",
                                        tool_call_id=tool_call_id,
                                        name=sanitize_for_llm(openai_tool_name),
                                        content=sanitize_json_content(error_content)
                                    ),
                                    "call_info": None,
                                    "error": str(e)
                                }

                    # Execute all tool calls in parallel using asyncio.gather()
                    import time
                    start_time = time.time()
                    logger.info("Executing %d tool calls in parallel", len(tool_calls))
                    
                    tool_results = await asyncio.gather(
                        *[execute_single_tool_call(tc, i) for i, tc in enumerate(tool_calls)],
                        return_exceptions=True
                    )
                    
                    execution_time = time.time() - start_time
                    logger.info("Parallel tool execution completed in %.2f seconds", execution_time)

                    # Process results and add messages to conversation
                    for i, result in enumerate(tool_results):
                        if isinstance(result, Exception):
                            logger.exception("Tool call %d failed with exception: %s", i, result)
                            results.setdefault("errors", []).append(f"Tool call {i} failed: {str(result)}")
                            # Create error message for failed tool call
                            tc = tool_calls[i]
                            tool_call_id = tc.get("id") or f"exception-call-{int(time.time()*1000)}-{i}"
                            error_content = json.dumps({"error": f"Tool execution failed: {str(result)}"})
                            messages.append(ChatMessage(
                                role="tool",
                                tool_call_id=tool_call_id,
                                name=sanitize_for_llm(tc.get("function", {}).get("name", "unknown")),
                                content=sanitize_json_content(error_content)
                            ))
                        else:
                            # Add successful result message to conversation
                            messages.append(result["message"])
                            
                            # Add call info to results if successful
                            if result["success"] and result["call_info"]:
                                results["calls"].append(result["call_info"])
                            elif result["error"]:
                                results.setdefault("errors", []).append(result["error"])

                # Check for final content
                elif content:
                    # Append assistant final message to conversation history
                    messages.append(ChatMessage(role="assistant", content=content or ""))
                    results["summary"] = content
                    # Update tracked messages with final response
                    self._current_messages = messages.copy()
                    break
                # If we had tool calls, continue to next iteration to let LLM respond to tool results
                # Don't add extra assistant messages here as it creates invalid conversation flow

                # Update tracked messages at end of each step
                self._current_messages = messages.copy()

            else:
                # Max steps reached - get final answer
                try:
                    final_llm_out = await self.llm.chat_tools(messages, [])
                    final_assistant = final_llm_out.get("assistant", {})
                    final_content = final_assistant.get("content")
                    if final_content:
                        # Append final assistant message to conversation history
                        messages.append(ChatMessage(role="assistant", content=final_content or ""))
                        results["summary"] = final_content
                    else:
                        results.setdefault("errors", []).append("LLM planner reached max steps without final answer.")
                except Exception as e:
                    logger.exception("Failed to get final answer: %s", e)
                    results.setdefault("errors", []).append(f"Failed to get final answer: {e}")

        except Exception as e:
            logger.exception("LLM planning failed: %s", e)
            results.setdefault("errors", []).append(f"LLM planning failed: {e}")
        finally:
            # Clean up MCP integration if we initialized it locally
            if 'mcp_initialized_locally' in locals() and mcp_initialized_locally and 'mcp_integration' in locals() and mcp_integration:
                try:
                    await mcp_integration.shutdown()
                    logger.debug("Shut down MCP integration after agent execution")
                except Exception as e:
                    logger.debug("Error shutting down MCP integration: %s", e)

            # Best-effort: publish a terminal status event for CLI/run path so subscribers see completion
            # Let exceptions surface here to reveal publish issues rather than silently hiding them.
            final_phase = PHASE_ERROR if results.get('errors') else PHASE_END
            
            # Complete the high-level operation status first using PHASE_PROGRESS 
            operation_final_msg = f"Completed: {task[:50]}..." if final_phase == PHASE_END else f"Failed: {task[:50]}..."
            await publish_status(
                server=self.name, 
                message=operation_final_msg, 
                request_id=request_id, 
                phase=PHASE_PROGRESS
            )
            await asyncio.sleep(0)  # Yield to allow event loop to publish status
            
            # Then complete the technical status
            final_msg = f"{self.name}: completed" if final_phase == PHASE_END else f"{self.name}: completed with errors"
            await publish_status(
                server=self.name, 
                message=final_msg, 
                request_id=request_id, 
                phase=final_phase, 
                meta={"summary": results.get('summary') if results else None}
            )

        return results

    def _estimate_token_count(self, messages: List[ChatMessage]) -> int:
        """Rough token count estimation for debugging context window usage."""
        total_chars = 0
        for msg in messages:
            # Count content characters
            if msg.content:
                total_chars += len(str(msg.content))

            # Count tool calls
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tc in msg.tool_calls:
                    func = tc.get("function", {})
                    total_chars += len(str(func.get("name", "")))
                    total_chars += len(str(func.get("arguments", "")))

            # Add role and structure overhead
            total_chars += 50  # rough overhead per message

        # Very rough token estimation: ~4 chars per token for most languages
        # This is conservative and language-dependent
        estimated_tokens = total_chars // 4
        return estimated_tokens

    async def run_events(self, task: str, request_id: Optional[str] = None, session_id: Optional[str] = None):
        """Run the agent and yield structured events for UI streaming."""
        # Generate request ID if not provided
        if request_id is None:
            request_id = str(uuid.uuid4())

        # Register this request for potential cancellation and appended messages
        async with self._request_lock:
            self._active_requests[request_id] = {
                "cancel": asyncio.Event(),
                "message_event": asyncio.Event(),
                "appended": []
            }

        try:
            # If no session_id provided, generate one and persist empty history
            if not session_id:
                session_id = str(uuid.uuid4())
            async with self._request_lock:
                # ensure session exists
                self._sessions.setdefault(session_id, [])
                # map request to session
                self._request_to_session[request_id] = session_id

            yield {"type": "start", "task": task, "request_id": request_id, "session_id": session_id}

            # Subscribe to status events for this request to forward them through SSE
            from ...mcp.status import status_bus
            status_queue = await status_bus.subscribe(server=self.name, request_id=request_id)

            # Set up status event forwarding task
            status_events_to_forward = []
            forwarding_done = asyncio.Event()
            forwarding_ready = asyncio.Event()
            first_get_started = asyncio.Event()

            async def forward_status_events():
                """Forward status events from status_bus to SSE stream"""
                try:
                    logger.debug("Status forwarding task starting...")
                    while not forwarding_done.is_set():
                        try:
                            # Signal that we're ready to receive events right before we start waiting
                            if not forwarding_ready.is_set():
                                forwarding_ready.set()
                                logger.debug("Status forwarding task ready, waiting for events...")
                            
                            # Signal that the first get() call is about to start
                            if not first_get_started.is_set():
                                first_get_started.set()
                            
                            # Wait for status event with timeout
                            status_event = await asyncio.wait_for(status_queue.get(), timeout=0.1)
                            logger.debug("Forwarding received status event: %s [%s]: %s (seq: %s)", 
                                       status_event.server, status_event.phase, status_event.message, 
                                       status_event.meta.get('_seq') if status_event.meta else 'no-seq')
                            logger.debug("Forwarding status event: %s [%s]: %s", status_event.server, status_event.phase, status_event.message)
                            # Convert status event to SSE format
                            status_sse_event = {
                                "type": "status",
                                "server": status_event.server,
                                "request_id": status_event.request_id,
                                "message": status_event.message,
                                "phase": status_event.phase,
                                "level": status_event.level,
                                "timestamp": status_event.timestamp.isoformat(),
                                "meta": status_event.meta or {}
                            }
                            status_events_to_forward.append(status_sse_event)
                        except asyncio.TimeoutError:
                            continue
                        except asyncio.CancelledError:
                            break
                except Exception as e:
                    logger.debug("Error in status event forwarding: %s", e)

            # Start the status forwarding task
            forwarding_task = asyncio.create_task(forward_status_events())

            # Helper function to yield any pending status events
            def yield_pending_status_events():
                while status_events_to_forward:
                    yield status_events_to_forward.pop(0)

            # Wait for the forwarding task to be ready AND for the first get() to start
            await forwarding_ready.wait()
            await first_get_started.wait()
            # Give the forwarding task a moment to actually reach the status_queue.get() call
            await asyncio.sleep(0.01)
            logger.debug("Status forwarding task is ready and listening")

            # If no LLM is configured, emit an immediate error event and end the stream
            if self.llm is None:
                yield {"type": "error", "message": "No LLM available; agent requires an LLM to run", "request_id": request_id}
                yield {"type": "end"}
                return

            # Initialize MCP integration tracking
            mcp_integration = None
            mcp_initialized_locally = False

            # Subscribe to status events for this request to forward them through SSE
            from ...mcp.status import status_bus
            status_queue = await status_bus.subscribe(server=self.name, request_id=request_id)

            # Set up status event forwarding task
            status_events_to_forward = []
            forwarding_done = asyncio.Event()

            async def forward_status_events():
                """Forward status events from status_bus to SSE stream"""
                try:
                    while not forwarding_done.is_set():
                        try:
                            # Wait for status event with timeout
                            status_event = await asyncio.wait_for(status_queue.get(), timeout=0.1)
                            # Convert status event to SSE format
                            status_sse_event = {
                                "type": "status",
                                "server": status_event.server,
                                "request_id": status_event.request_id,
                                "message": status_event.message,
                                "phase": status_event.phase,
                                "level": status_event.level,
                                "timestamp": status_event.timestamp.isoformat(),
                                "meta": status_event.meta or {}
                            }
                            status_events_to_forward.append(status_sse_event)
                        except asyncio.TimeoutError:
                            continue
                        except asyncio.CancelledError:
                            break
                except Exception as e:
                    logger.debug("Error in status event forwarding: %s", e)

            # Start the status forwarding task
            forwarding_task = asyncio.create_task(forward_status_events())

            # Get tools from the local registry (plugins)
            available_tools = self.registry.list()

            # Also include tools from external MCP servers
            try:
                from ...mcp.integration import get_mcp_integration
                mcp_integration = get_mcp_integration()
                if not mcp_integration.initialized:
                    # Initialize with the same configuration as the agent
                    if hasattr(self.agent_config, 'mcp'):
                        mcp_config = {"mcp": self.agent_config.mcp.model_dump() if hasattr(self.agent_config.mcp, "model_dump") else getattr(self.agent_config.mcp, "__dict__", {})}
                        await mcp_integration.initialize(mcp_config)
                        mcp_initialized_locally = True
                        logger.debug("Initialized MCP integration for agent in run_events")

                if mcp_integration and mcp_integration.initialized:
                    all_tools = await mcp_integration.list_all_tools()
                    # Add external server tools to available tools
                    for server_name, tools in all_tools.get("external_servers", {}).items():
                        for tool in tools:
                            tool_name = f"{server_name}.{tool['name']}"
                            available_tools.append(tool_name)
                            logger.debug("Added external tool to run_events: %s", tool_name)
            except Exception as e:
                logger.debug("Failed to get external MCP tools in run_events: %s", e)

            max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))

            # Render prompts
            rendered = render_prompts(
                self.agent_config.prompts.system_template,
                {"tools": available_tools, "max_steps": max_steps-1},
                auto_datetime=self.agent_config.context.auto_datetime,
                timezone=self.agent_config.context.timezone,
                location=self.agent_config.context.location
            )
            system_msg = rendered.get("system_prompt") or "You are an assistant agent."
            tools_msg = rendered.get("tools_prompt")

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

            for tool_name in available_tools:
                # Check if it's an external tool (contains a dot)
                if "." in tool_name:
                    server_name, actual_tool_name = tool_name.split(".", 1)
                    # Create OpenAI-compatible name (replace dots with underscores)
                    openai_tool_name = tool_name.replace(".", "_")
                    tool_name_mapping[openai_tool_name] = tool_name

                    # Create a schema for external tools
                    try:
                        from ...mcp.integration import get_mcp_integration
                        mcp_integration = get_mcp_integration()
                        if mcp_integration and mcp_integration.initialized:
                            all_tools = await mcp_integration.list_all_tools()
                            external_tools = all_tools.get("external_servers", {}).get(server_name, [])
                            for tool in external_tools:
                                if tool["name"] == actual_tool_name:
                                    schema = {
                                        "type": "function",
                                        "function": {
                                            "name": openai_tool_name,
                                            "description": f"[{server_name}] {tool['description']}",
                                            "parameters": tool.get("input_schema", {})
                                        }
                                    }
                                    tools_schema.append(schema)
                                    break
                    except Exception as e:
                        logger.debug("Failed to build schema for external tool %s: %s", tool_name, e)
                else:
                    # Regular plugin tool - support multiple tools per server
                    server = self.registry.get(tool_name)
                    if hasattr(server, 'get_tools'):
                        # New multi-tool interface
                        server_tools = server.get_tools()
                        tools_schema.extend(server_tools)
                    else:
                        # Fallback to legacy single-tool interface
                        tools_schema.append(server.get_schema())

            max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
            results: Dict[str, Any] = {"task": task, "calls": []}
            executor = Executor(self.registry)

            # Now that everything is set up and the forwarding task is definitely running,
            # publish the PHASE_START event so it gets captured
            logger.debug("Publishing PHASE_START at execution start for server=%s request_id=%s", self.name, request_id)
            await publish_status(
                server=f"{self.name}_coordinator",
                message=f"{self.name}: started",
                request_id=request_id,
                phase=PHASE_START,
                meta={"session_id": session_id},
            )
            await asyncio.sleep(0)  # yield to event loop


            # Add safeguards against infinite loops
            consecutive_no_tool_calls = 0
            consecutive_empty_responses = 0
            max_consecutive_no_tools = 3  # Break after 3 consecutive responses without tool calls
            max_consecutive_empty = 2    # Break after 2 consecutive empty responses

            for step in range(max_steps):
                # Drain any appended user messages before each step
                messages = await self._drain_appended_messages(request_id, messages)
                
                # Check for cancellation at the start of each step
                if self._is_cancelled(request_id):
                    logger.info("Request %s cancelled at step %d", request_id, step + 1)
                    yield {"type": "cancelled", "request_id": request_id, "step": step + 1}
                    # Best-effort: publish terminal status so clients see completion
                    await publish_status(
                        server=self.name,
                        message=f"{self.name}: cancelled at step {step + 1}",
                        request_id=request_id,
                        phase=PHASE_END,
                        meta={"step": step + 1, "reason": "cancelled"}
                    )
                    asyncio.sleep(0)
                    await publish_status(
                        server=f"{self.name}_coordinator",
                        message=f"{self.name}: cancelled at step {step + 1}",
                        request_id=request_id,
                        phase=PHASE_END,
                        meta={"step": step + 1, "reason": "cancelled"}
                    )                    
                    asyncio.sleep(0)
                    yield {"type": "end"}
                    return

                # Publish heartbeat status for overall agent progress (best-effort)
                await publish_status(
                        server=f"{self.name}_coordinator",
                        message=f"{self.name}: running step {step + 1}/{max_steps}",
                        request_id=request_id,
                        phase=PHASE_PROGRESS,
                        meta={"step": step + 1, "max_steps": max_steps}
                )
                await asyncio.sleep(0)
                # Also yield a heartbeat event in the run_events stream for direct consumers
                try:
                    yield {
                        "type": "heartbeat",
                        "message": f"{self.name}: running step {step + 1}/{max_steps}",
                        "step": step + 1,
                        "max_steps": max_steps,
                        "request_id": request_id,
                    }
                except Exception:
                    # If the consumer isn't expecting heartbeat, ignore
                    pass

                # Yield any pending status events
                for status_event in yield_pending_status_events():
                    yield status_event

                # Enhanced context management and token tracking
                if self.context_manager:
                    # Apply token optimization with centralized guard to avoid repeated runs (events loop)
                    if self.token_optimizer:
                        # Skip optimizer for a few steps after context management
                        if getattr(self, '_skip_optimizer_steps_after_context_mgmt', 0) > 0:
                            self._skip_optimizer_steps_after_context_mgmt -= 1
                            logger.debug("Skipping token optimizer (events): %d steps remaining after context mgmt",
                                       self._skip_optimizer_steps_after_context_mgmt)
                        else:
                            try:
                                import time
                                now = time.time()
                                estimated_tokens_now = self.context_manager.estimate_token_count(messages)

                                tokens_growth = estimated_tokens_now - getattr(self, '_last_optimizer_tokens_snapshot', 0)
                                time_since_last = now - getattr(self, '_last_optimizer_run_time', 0.0)

                                should_run_optimizer = False
                                if tokens_growth >= getattr(self, '_optimizer_min_increase_tokens', 200):
                                    should_run_optimizer = True
                                elif time_since_last >= getattr(self, '_optimizer_cooldown_seconds', 10.0):
                                    should_run_optimizer = True

                                if should_run_optimizer:
                                    messages = await self.token_optimizer.optimize_messages(messages)
                                    self._last_optimizer_tokens_snapshot = self.context_manager.estimate_token_count(messages)
                                    self._last_optimizer_run_time = now
                                else:
                                    logger.debug("Skipping token optimizer (events): growth=%d, time_since_last=%.2fs", tokens_growth, time_since_last)
                            except Exception as e:
                                logger.debug("Token optimizer guard check failed (events): %s", e)

                    # Check token count and issue appropriate warnings
                    estimated_tokens, warning_level = self.context_manager.check_and_warn(messages, step)

                    # Apply context management if needed
                    if self.context_manager.should_manage_context(estimated_tokens, warning_level):
                        logger.info("Applying context management at step %d", step + 1)
                        messages = await self.context_manager.manage_context(messages)
                        # Skip optimizer for next 2 steps after context management
                        self._skip_optimizer_steps_after_context_mgmt = 2
                        # Re-check after management
                        estimated_tokens, _ = self.context_manager.check_and_warn(messages, step)
                else:
                    # Fallback to legacy token warning
                    message_count = len(messages)
                    estimated_tokens = self._estimate_token_count(messages)
                    context_window = getattr(self.agent_config, "context_window", 32768)
                    logger.debug("LLM input (step %d): %d messages, ~%d tokens (context: %d)",
                               step + 1, message_count, estimated_tokens, context_window)

                    if estimated_tokens > context_window * 0.9:  # 90% threshold
                        logger.warning("Token count approaching context window limit: %d/%d tokens",
                                     estimated_tokens, context_window)

                logger.debug("LLM messages: %s", [m.model_dump() for m in messages])

                # Emit thinking event before LLM call
                yield {"type": "thinking", "step": step + 1}

                # Emit status: calling LLM (chat_tools)
                await publish_status(
                    server=self.name, 
                    message="Calling LLM (chat)", 
                    request_id=request_id, 
                    phase=PHASE_PROGRESS, 
                    meta={"step": step + 1}
                )
                await asyncio.sleep(0)

                # Get LLM response
                llm_out = await self.llm.chat_tools(messages, tools_schema)
                
                # Drain any messages that arrived during LLM call
                messages = await self._drain_appended_messages(request_id, messages)

                # Emit status: LLM call complete            
                await publish_status(
                    server=self.name, 
                    message="LLM (chat) response received", 
                    request_id=request_id, 
                    phase=PHASE_PROGRESS, 
                    meta={"step": step + 1}
                )
                await asyncio.sleep(0)

                assistant = llm_out.get("assistant", {})
                logger.debug("LLM assistant message (step %d): %s", step + 1, assistant)

                # Track actual token usage for streamed events if available
                if self.context_manager and isinstance(llm_out, dict) and 'usage' in llm_out:
                    try:
                        usage_data = llm_out['usage']
                        self.context_manager.update_token_usage(usage_data)
                        logger.debug("Updated token usage from LLM response (events): %s", usage_data)

                        # Coerce token count to int safely and always update tracker
                        raw_total = usage_data.get('total_tokens', 0)
                        try:
                            actual_tokens = int(raw_total or 0)
                        except Exception:
                            try:
                                actual_tokens = int(float(str(raw_total)))
                            except Exception:
                                actual_tokens = 0

                        try:
                            update_agent_context_usage(
                                self.name,
                                current_tokens=estimated_tokens,
                                predicted_tokens=estimated_tokens,
                                message_count=len(messages),
                                actual_tokens=actual_tokens
                            )
                        except Exception as e:
                            logger.debug("Failed to update agent context (events) with LLM tokens: %s", e)
                    except Exception as e:
                        logger.debug("Failed to handle LLM usage in run_events: %s", e)

                # Emit thinking event with LLM response content
                yield {"type": "thinking", "step": step + 1, "assistant": assistant}

                tool_calls = assistant.get("tool_calls") or []
                content = assistant.get("content")

                # Track consecutive responses without progress to prevent infinite loops
                if not tool_calls and not content:
                    consecutive_empty_responses += 1
                    consecutive_no_tool_calls += 1
                    logger.warning("LLM returned empty response (step %d), consecutive empty: %d", 
                                 step + 1, consecutive_empty_responses)
                elif not tool_calls:
                    consecutive_no_tool_calls += 1
                    consecutive_empty_responses = 0  # Reset empty counter if we have content
                    logger.debug("LLM returned content without tool calls (step %d), consecutive no-tools: %d", 
                               step + 1, consecutive_no_tool_calls)
                else:
                    # Reset counters when we get tool calls (making progress)
                    consecutive_no_tool_calls = 0
                    consecutive_empty_responses = 0

                # Emergency break conditions to prevent infinite loops
                if consecutive_empty_responses >= max_consecutive_empty:
                    logger.warning("Breaking agent loop: %d consecutive empty responses", consecutive_empty_responses)
                    results.setdefault("errors", []).append(f"Agent stopped due to {consecutive_empty_responses} consecutive empty LLM responses")
                    yield {"type": "error", "message": f"Agent stopped due to {consecutive_empty_responses} consecutive empty LLM responses"}
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
                    # Add assistant message with ALL tool calls to conversation
                    messages.append(ChatMessage(role="assistant", content=content or "", tool_calls=tool_calls))

                    # Execute ALL tool calls with immediate streaming
                    for i, tc in enumerate(tool_calls):
                        func = tc.get("function", {})
                        openai_tool_name = func.get("name")  # This is the OpenAI-compatible name
                        raw_args = func.get("arguments")

                        # Map back to original tool name if it was converted
                        tool_name = tool_name_mapping.get(openai_tool_name, openai_tool_name)

                        # Parse arguments
                        params: Dict[str, Any] = {}
                        if isinstance(raw_args, str) and raw_args:
                            try:
                                params = json.loads(raw_args)
                            except json.JSONDecodeError:
                                logger.warning("Failed to parse tool arguments: %s", raw_args)
                                params = {}
                        elif isinstance(raw_args, dict):
                            params = raw_args

                        if not tool_name or tool_name not in available_tools:
                            logger.warning("Unknown tool requested: %s (OpenAI name: %s)", tool_name, openai_tool_name)
                            yield {"type": "error", "message": f"Unknown tool: {tool_name}"}
                            # Add error result for this specific tool call
                            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                            messages.append(ChatMessage(
                                role="tool",
                                tool_call_id=tool_call_id,
                                name=openai_tool_name or "unknown",
                                content=json.dumps({"error": f"Tool '{tool_name}' is not available."})
                            ))
                            continue

                        # Get action name and validate
                        if "." in tool_name:
                            # External tool - call via MCP integration
                            server_name, actual_tool_name = tool_name.split(".", 1)

                            # Emit MCP call event immediately
                            yield {"type": "mcp_call", "step": step + 1, "server": tool_name, "action": actual_tool_name, "params": params}
                            # Best-effort: publish tool-level START so frontend shows per-tool slot
                            await publish_status(
                                server_name,
                                f"{server_name}: Starting {actual_tool_name}",
                                request_id=request_id,
                                phase=PHASE_PROGRESS,
                            )



                            try:
                                logger.info("Invoking external tool %s on server %s with params %s", actual_tool_name, server_name, params)
                                from ...mcp.integration import get_mcp_integration
                                mcp_integration = get_mcp_integration()
                                tool_result = await mcp_integration.call_tool(server_name, actual_tool_name, params, "external")
                                logger.info("External tool %s returned: %s", tool_name, str(tool_result)[:500])

                                results["calls"].append({
                                    "server": tool_name,
                                    "action": actual_tool_name,
                                    "params": params,
                                    "result": tool_result
                                })

                                # Emit MCP result event immediately
                                yield {"type": "mcp_result", "step": step + 1, "server": tool_name, "action": actual_tool_name, "result": tool_result}
                                # Best-effort: publish tool-level END to close per-tool slot
                                await publish_status(
                                    server_name,
                                    f"{server_name}: Completed {actual_tool_name}",
                                    request_id=request_id,
                                    phase=PHASE_PROGRESS,
                                )

                                # Add tool result to conversation
                                tool_call_id = tc.get("id") or f"{tool_name}-call-{int(time.time()*1000)}"
                                tool_msg_content = json.dumps(tool_result, ensure_ascii=False)
                                # Sanitize tool result content before adding to messages
                                tool_msg_content = sanitize_json_content(tool_msg_content)
                                messages.append(ChatMessage(
                                    role="tool",
                                    tool_call_id=tool_call_id,
                                    name=sanitize_for_llm(openai_tool_name),
                                    content=tool_msg_content
                                ))
                            except Exception as e:
                                logger.exception("External tool %s invocation failed: %s", tool_name, e)
                                yield {"type": "error", "message": f"External tool {tool_name} failed: {str(e)}"}
                                tool_call_id = tc.get("id") or f"{tool_name}-error-{int(time.time()*1000)}"
                                error_content = json.dumps({"error": f"Tool invocation failed: {str(e)}"})
                                messages.append(ChatMessage(
                                    role="tool",
                                    tool_call_id=tool_call_id,
                                    name=sanitize_for_llm(openai_tool_name),
                                    content=sanitize_json_content(error_content)
                                ))
                        else:
                            # Plugin tool - use existing logic
                            server = self.registry.get(tool_name)
                            action_name = params.get("action") or params.get("tool") or server.get_default_action()

                            # Validate action against server schema
                            schema = server.get_schema()
                            valid_actions = []
                            if "function" in schema and "parameters" in schema["function"]:
                                action_prop = schema["function"]["parameters"].get("properties", {}).get("action", {})
                                valid_actions = action_prop.get("enum", [])

                            if valid_actions and action_name not in valid_actions:
                                logger.warning("Invalid action '%s' for tool %s, valid actions: %s. Using default action.",
                                             action_name, tool_name, valid_actions)
                                action_name = server.get_default_action()
                                params["action"] = action_name

                            # Emit MCP call event immediately
                            yield {"type": "mcp_call", "step": step + 1, "server": tool_name, "action": action_name, "params": params}

                            try:
                                logger.info("Invoking tool %s action %s with params %s", tool_name, action_name, params)
                                tool_result = await executor.invoke(tool_name, params)
                                logger.info("Tool %s returned: %s", tool_name, str(tool_result)[:500])

                                results["calls"].append({
                                    "server": tool_name,
                                    "action": action_name,
                                    "params": params,
                                    "result": tool_result
                                })

                                # Emit MCP result event immediately
                                yield {"type": "mcp_result", "step": step + 1, "server": tool_name, "action": action_name, "result": tool_result}


                                # Add tool result to conversation
                                tool_call_id = tc.get("id") or f"{tool_name}-call-{int(time.time()*1000)}"
                                tool_msg_content = json.dumps(tool_result, ensure_ascii=False)
                                # Sanitize tool result content before adding to messages
                                tool_msg_content = sanitize_json_content(tool_msg_content)
                                messages.append(ChatMessage(
                                    role="tool",
                                    tool_call_id=tool_call_id,
                                    name=openai_tool_name,
                                    content=tool_msg_content
                                ))

                            except Exception as e:
                                logger.exception("Tool %s invocation failed: %s", tool_name, e)
                                results.setdefault("errors", []).append(str(e))
                                yield {"type": "error", "message": f"Tool {tool_name} failed: {e}"}
                                # Add error result for this specific tool call
                                tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                                error_content = json.dumps({"error": sanitize_for_llm(str(e))})
                                messages.append(ChatMessage(
                                    role="tool",
                                    tool_call_id=tool_call_id,
                                    name=sanitize_for_llm(openai_tool_name),
                                    content=sanitize_json_content(error_content)
                                ))

                # Check for final content
                elif content:
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
                    final_llm_out = await self.llm.chat_tools(messages, [])
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
                    logger.exception("Failed to get final answer: %s", e)
                    results.setdefault("errors", []).append(f"Failed to get final answer: {e}")
                    yield {"type": "error", "message": f"Failed to get final answer: {e}"}

        except Exception as e:
            yield {"type": "error", "message": f"Agent execution failed: {e}"}
        finally:
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
                        logger.debug("Failed to persist session %s: %s", sid, e)

            # Clean up MCP integration if we initialized it locally
            if 'mcp_initialized_locally' in locals() and mcp_initialized_locally and 'mcp_integration' in locals() and mcp_integration:
                try:
                    await mcp_integration.shutdown()
                    logger.debug("Shut down MCP integration after agent run_events")
                except Exception as e:
                    logger.debug("Error shutting down MCP integration in run_events: %s", e)

            # Clean up status forwarding task
            if 'forwarding_task' in locals() and 'forwarding_done' in locals():
                try:
                    forwarding_done.set()
                    forwarding_task.cancel()
                    try:
                        await forwarding_task
                    except asyncio.CancelledError:
                        pass
                    logger.debug("Cleaned up status forwarding task")
                except Exception as e:
                    logger.debug("Error cleaning up status forwarding task: %s", e)

        # Best-effort: publish a terminal status event so SSE subscribers see completion

        # If results contains errors, publish PHASE_ERROR, else PHASE_END
        final_phase = PHASE_ERROR if ('results' in locals() and results.get('errors')) else PHASE_END
        
        # Then complete the technical status
        final_msg = f"{self.name}: completed" if final_phase == PHASE_END else f"{self.name}: completed with errors"
        await publish_status(
            server=self.name, 
            message=final_msg, 
            request_id=request_id, 
            phase=final_phase, 
            meta={"summary": results.get('summary') if 'results' in locals() else None}
        )
        await asyncio.sleep(0)       
        await publish_status(
            server= f"{self.name}_coordinator", 
            message=f" {final_msg} ({step+1} steps)",
            request_id=request_id, 
            phase=final_phase, 
            meta={"summary": results.get('summary') if 'results' in locals() else None}
        )
        await asyncio.sleep(0)

        # Yield any final pending status events before ending
        if 'yield_pending_status_events' in locals():
            for status_event in yield_pending_status_events():
                yield status_event
        
        yield {"type": "end"}

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
            result = await self.run(str(task))

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
                "description": self.config.get("description", f"Agent: {self.name}"),
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
