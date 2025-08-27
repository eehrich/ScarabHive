"""
Enhanced Agent Core - Agent extends MCPServer for direct agent-to-agent communication
Supports multiple tool calls per conversation turn for better efficiency
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, AsyncIterator

from ...config.models import AgentConfig
from ...mcp.base import MCPRegistry, MCPServer
from ...llm.clients import ChatMessage, make_llm
from ...utils.prompt_renderer import render_prompts
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
                        )
                except Exception as e:
                    logger.warning("LLM initialization failed: %s", e)
                    self.llm = None

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
            available_tools = self.registry.list()
            logger = logging.getLogger(__name__)

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

            # Build tool schemas
            tools_schema: List[Dict] = []
            for tool_name in available_tools:
                server = self.registry.get(tool_name)
                tools_schema.append(server.get_schema())

            max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
            
            executor = Executor(self.registry)
            executor = Executor(self.registry)
            for step in range(max_steps):
                # Debug: Log message count and estimated token count
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
                        tool_name = func.get("name")
                        raw_args = func.get("arguments")
                        
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
                            logger.warning("Unknown tool requested: %s", tool_name)
                            # Add error message for this specific tool call
                            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                            messages.append(ChatMessage(
                                role="tool", 
                                tool_call_id=tool_call_id,
                                name=tool_name or "unknown",
                                content=json.dumps({"error": f"Tool '{tool_name}' is not available."})
                            ))
                            continue
                        
                        # Get action name and validate
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
                            messages.append(ChatMessage(
                                role="tool", 
                                tool_call_id=tool_call_id, 
                                name=tool_name, 
                                content=tool_msg_content
                            ))
                            
                        except Exception as e:
                            logger.exception("Tool %s invocation failed: %s", tool_name, e)
                            results.setdefault("errors", []).append(str(e))
                            # Add error result for this specific tool call
                            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                            messages.append(ChatMessage(
                                role="tool", 
                                tool_call_id=tool_call_id,
                                name=tool_name,
                                content=json.dumps({"error": str(e)})
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
            available_tools = self.registry.list()
            logger = logging.getLogger(__name__)

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
            messages.append(ChatMessage(role="user", content=task))

            # Build tool schemas
            tools_schema: List[Dict] = []
            for tool_name in available_tools:
                server = self.registry.get(tool_name)
                tools_schema.append(server.get_schema())

            max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
            results: Dict[str, Any] = {"task": task, "calls": []}
            executor = Executor(self.registry)
            
            for step in range(max_steps):
                # Debug: Log message count and estimated token count
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
                        tool_name = func.get("name")
                        raw_args = func.get("arguments")
                        
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
                            logger.warning("Unknown tool requested: %s", tool_name)
                            yield {"type": "error", "message": f"Unknown tool: {tool_name}"}
                            # Add error result for this specific tool call
                            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                            messages.append(ChatMessage(
                                role="tool", 
                                tool_call_id=tool_call_id,
                                name=tool_name or "unknown",
                                content=json.dumps({"error": f"Tool '{tool_name}' is not available."})
                            ))
                            continue
                        
                        # Get action name and validate
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
                            messages.append(ChatMessage(
                                role="tool", 
                                tool_call_id=tool_call_id, 
                                name=tool_name, 
                                content=tool_msg_content
                            ))
                            
                        except Exception as e:
                            logger.exception("Tool %s invocation failed: %s", tool_name, e)
                            results.setdefault("errors", []).append(str(e))
                            yield {"type": "error", "message": f"Tool {tool_name} failed: {e}"}
                            # Add error result for this specific tool call
                            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                            messages.append(ChatMessage(
                                role="tool", 
                                tool_call_id=tool_call_id,
                                name=tool_name,
                                content=json.dumps({"error": str(e)})
                            ))
                            messages.append(ChatMessage(
                                role="assistant", 
                                content=f"Error calling tool {tool_name}: {e}"
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
