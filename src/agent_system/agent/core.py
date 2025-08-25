"""
Simplified Agent Core - One tool call per conversation turn
Focuses on clarity and prevents infinite tool call loops
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, AsyncIterator

from ..config.models import AgentConfig
from ..mcp.base import MCPRegistry
from ..llm.clients import ChatMessage, make_llm
from ..utils.prompt_renderer import render_prompts


logger = logging.getLogger(__name__)


class Agent:
    """
    Simplified Agent that executes ONE tool call per LLM conversation turn.
    This prevents infinite loops and makes the conversation flow clearer.
    """

    def __init__(self, config: AgentConfig, registry: MCPRegistry) -> None:
        self.config = config
        self.registry = registry
        self.llm = None
        
        # Initialize LLM if not provided
        if self.llm is None:
            try:
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

    async def run(self, task: str) -> Dict[str, Any]:
        """
        Run the agent task with simplified tool calling logic.
        Execute at most ONE tool call per LLM turn to prevent loops.
        """
        results: Dict[str, Any] = {"task": task, "calls": []}

        if self.llm is None:
            results["errors"] = ["No LLM available; agent requires an LLM to plan tool usage."]
            return results

        try:
            available_tools = self.registry.list()
            logger = logging.getLogger(__name__)

            # Render prompts
            rendered = render_prompts(
                self.config.prompts.system_template, 
                {"tools": available_tools},
                auto_datetime=self.config.context.auto_datetime,
                timezone=self.config.context.timezone,
                location=self.config.context.location
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

            max_steps = max(1, int(getattr(self.config, "max_steps", 6)))
            
            for step in range(max_steps):
                # Debug: Log message count and estimated token count
                message_count = len(messages)
                estimated_tokens = self._estimate_token_count(messages)
                context_window = getattr(self.config, "context_window", 32768)
                logger.debug("LLM input (step %d): %d messages, ~%d tokens (context: %d)", 
                           step + 1, message_count, estimated_tokens, context_window)
                
                if estimated_tokens > context_window * 0.9:  # 90% threshold
                    logger.warning("Token count approaching context window limit: %d/%d tokens", 
                                 estimated_tokens, context_window)
                
                logger.debug("LLM messages: %s", [m.model_dump() for m in messages])

                # Get LLM response
                llm_out = await self.llm.chat_tools(messages, tools_schema)
                assistant = llm_out.get("assistant", {})
                logger.debug("LLM assistant message (step %d): %s", step + 1, assistant)
                
                tool_calls = assistant.get("tool_calls") or []
                content = assistant.get("content")

                # SIMPLE RULE: Execute only the FIRST tool call per turn
                if tool_calls:
                    # Add assistant message with ALL tool calls to conversation
                    messages.append(ChatMessage(role="assistant", content=content or "", tool_calls=tool_calls))
                    
                    # Execute ONLY the first tool call
                    tc = tool_calls[0]
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
                        messages.append(ChatMessage(
                            role="assistant", 
                            content=f"Error: Tool '{tool_name}' is not available."
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
                        tool_result = await server.call(action_name, params)
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
                        
                        # If there were multiple tool calls, inform about skipping others
                        if len(tool_calls) > 1:
                            skipped_calls = [tc.get("function", {}).get("name") for tc in tool_calls[1:]]
                            messages.append(ChatMessage(
                                role="assistant", 
                                content=f"Note: I executed {tool_name} and skipped {len(tool_calls)-1} other tool calls: {', '.join(skipped_calls)}. I'll make one tool call at a time for clarity."
                            ))
                            
                    except Exception as e:
                        logger.exception("Tool %s invocation failed: %s", tool_name, e)
                        results.setdefault("errors", []).append(str(e))
                        messages.append(ChatMessage(
                            role="assistant", 
                            content=f"Error calling tool {tool_name}: {e}"
                        ))

                # Check for final content
                elif content:
                    results["summary"] = content
                    break
                else:
                    # No tools and no content - ask LLM to provide final answer
                    messages.append(ChatMessage(
                        role="assistant", 
                        content="Please provide your final answer based on the information gathered."
                    ))

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
            result = await self.run(task)
            
            # Emit events for each tool call
            for call in result.get("calls", []):
                yield {"type": "mcp_call", "server": call["server"], "action": call["action"], "params": call["params"]}
                yield {"type": "mcp_result", "server": call["server"], "action": call["action"], "result": call["result"]}
            
            # Emit final summary if available
            if "summary" in result:
                yield {"type": "final", "summary": result["summary"]}
                
            # Emit errors if any
            for error in result.get("errors", []):
                yield {"type": "error", "message": error}
                
        except Exception as e:
            yield {"type": "error", "message": f"Agent execution failed: {e}"}
            
        yield {"type": "end"}
