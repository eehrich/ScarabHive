"""
Enhanced Agent Core - Agent extends MCPServer for direct agent-to-agent communication
Supports multiple tool calls per conversation turn for better efficiency
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from ...config.models import AgentConfig
from ...mcp.base import MCPRegistry, MCPServer
from ...llm.clients import ChatMessage
from ...utils.prompt_renderer import render_prompts
from ...utils.text_sanitizer import sanitize_for_llm, sanitize_json_content
from ...context import ContextConfig, ContextManager, ConversationSummarizer, TokenOptimizer
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

    def _init_context_management(self):
        """Initialize the context management system."""
        try:
            # Create context configuration from agent config
            context_window = getattr(self.agent_config, "context_window", 32768)
            
            # Get additional context settings from agent config if available
            context_attr = getattr(self.agent_config, "context", None)
            if context_attr and hasattr(context_attr, '__dict__'):
                # If it's an object, convert to dict
                context_settings = vars(context_attr)
            elif isinstance(context_attr, dict):
                context_settings = context_attr
            else:
                context_settings = {}
            
            self.context_config = ContextConfig(
                context_window=context_window,
                summarization_threshold=context_settings.get("summarization_threshold", context_window // 3),
                preserve_recent_messages=context_settings.get("preserve_recent_messages", 10),
                enable_compression=context_settings.get("enable_compression", True)
            )
            
            # Initialize context manager
            self.context_manager = ContextManager(self.context_config)
            
            # Initialize and set summarizer
            summarizer = ConversationSummarizer(self.llm)
            self.context_manager.set_summarizer(summarizer)
            
            # Initialize optimizer
            self.token_optimizer = TokenOptimizer()
            
            logger.info("Context management initialized - window: %d, summarization threshold: %d", 
                       context_window, self.context_config.summarization_threshold)
                       
        except Exception as e:
            logger.error("Failed to initialize context management: %s", e)
            # Fallback to None - will use legacy behavior
            self.context_manager = None
            self.token_optimizer = None

    @property
    def description(self) -> str:
        """Get the agent description."""
        return self.config.get("description", f"Agent: {self.name}")

    async def run(self, task: str) -> Dict[str, Any]:
        """
        Run the agent task with enhanced tool calling logic.
        Executes ALL tool calls from LLM per turn for better efficiency.
        """
        results: Dict[str, Any] = {"task": task, "calls": []}

        if self.llm is None:
            results["errors"] = ["No LLM available; agent requires an LLM to plan tool usage."]
            return results

        try:
            logger = logging.getLogger(__name__)
            
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
            executor = Executor(self.registry)
            for step in range(max_steps):
                # Enhanced context management and token tracking
                if self.context_manager:
                    # Apply token optimization
                    if self.token_optimizer:
                        messages = self.token_optimizer.optimize_messages(messages)
                    
                    # Check token count and issue appropriate warnings
                    estimated_tokens, warning_level = self.context_manager.check_and_warn(messages, step)
                    
                    # Apply context management if needed
                    if self.context_manager.should_manage_context(estimated_tokens, warning_level):
                        logger.info("Applying context management at step %d", step + 1)
                        messages = self.context_manager.manage_context(messages)
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

                # Get LLM response via Planner
                llm_out = await planner.chat(messages, tools_schema)
                assistant = llm_out.get("assistant", {})
                logger.debug("LLM assistant message (step %d): %s", step + 1, assistant)
                
                tool_calls = assistant.get("tool_calls") or []
                content = assistant.get("content")

                # SIMPLE RULE: Execute only the FIRST tool call per turn
                if tool_calls:
                    # Add assistant message with ALL tool calls to conversation
                    messages.append(ChatMessage(role="assistant", content=content or "", tool_calls=tool_calls))
                    
                    # Execute ALL tool calls
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
                            # Add error message for this specific tool call
                            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                            error_content = json.dumps({"error": f"Tool '{tool_name}' is not available."})
                            messages.append(ChatMessage(
                                role="tool", 
                                tool_call_id=tool_call_id,
                                name=sanitize_for_llm(openai_tool_name or "unknown"),
                                content=sanitize_json_content(error_content)
                            ))
                            continue
                        
                        # Get action name and validate
                        if "." in tool_name:
                            # External tool - call via MCP integration
                            server_name, actual_tool_name = tool_name.split(".", 1)
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
                    results["summary"] = content
                    break
                # If we had tool calls, continue to next iteration to let LLM respond to tool results
                # Don't add extra assistant messages here as it creates invalid conversation flow

            else:
                # Max steps reached - get final answer
                try:
                    final_llm_out = await self.llm.chat_tools(messages, [])
                    final_assistant = final_llm_out.get("assistant", {})
                    final_content = final_assistant.get("content")
                    if final_content:
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

    async def run_events(self, task: str):
        """Run the agent and yield structured events for UI streaming."""
        yield {"type": "start", "task": task}

        if self.llm is None:
            yield {"type": "error", "message": "No LLM available; agent requires an LLM to plan tool usage."}
            yield {"type": "end"}
            return

        try:
            logger = logging.getLogger(__name__)
            
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

            # Initialize conversation
            messages = [ChatMessage(role="system", content=system_msg)]
            if tools_msg:
                messages.append(ChatMessage(role="system", content=tools_msg))
            messages.append(ChatMessage(role="user", content=sanitize_for_llm(task)))

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
            
            for step in range(max_steps):
                # Enhanced context management and token tracking
                if self.context_manager:
                    # Apply token optimization
                    if self.token_optimizer:
                        messages = self.token_optimizer.optimize_messages(messages)
                    
                    # Check token count and issue appropriate warnings
                    estimated_tokens, warning_level = self.context_manager.check_and_warn(messages, step)
                    
                    # Apply context management if needed
                    if self.context_manager.should_manage_context(estimated_tokens, warning_level):
                        logger.info("Applying context management at step %d", step + 1)
                        messages = self.context_manager.manage_context(messages)
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

                # Get LLM response
                llm_out = await self.llm.chat_tools(messages, tools_schema)
                assistant = llm_out.get("assistant", {})
                logger.debug("LLM assistant message (step %d): %s", step + 1, assistant)
                
                # Emit thinking event with LLM response content
                yield {"type": "thinking", "step": step + 1, "assistant": assistant}
                
                tool_calls = assistant.get("tool_calls") or []
                content = assistant.get("content")

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
                    results["summary"] = content
                    yield {"type": "final", "summary": content}
                    break
                # If we had tool calls, continue to next iteration to let LLM respond to tool results
                # Don't add extra assistant messages here as it creates invalid conversation flow

            else:
                # Max steps reached - get final answer
                try:
                    final_llm_out = await self.llm.chat_tools(messages, [])
                    final_assistant = final_llm_out.get("assistant", {})
                    final_content = final_assistant.get("content")
                    if final_content:
                        results["summary"] = final_content
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
            # Clean up MCP integration if we initialized it locally
            if 'mcp_initialized_locally' in locals() and mcp_initialized_locally and 'mcp_integration' in locals() and mcp_integration:
                try:
                    await mcp_integration.shutdown()
                    logger.debug("Shut down MCP integration after agent run_events")
                except Exception as e:
                    logger.debug("Error shutting down MCP integration in run_events: %s", e)
            
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
