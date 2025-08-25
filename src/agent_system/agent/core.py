from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

from ..config.models import AgentConfig
from ..llm.clients import ChatMessage, make_llm
from ..mcp.base import MCPRegistry
from ..utils.prompt_renderer import render_prompts


class Agent:
    def __init__(self, config: AgentConfig, registry: MCPRegistry) -> None:
        self.config = config
        self.registry = registry
        self.llm = None
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
            logging.getLogger(__name__).warning("LLM initialization failed: %s", e)
            self.llm = None

    async def run(self, task: str) -> dict[str, Any]:
        """Run the agent task with LLM planning and tool execution.
        
        Returns a dictionary with task results, tool calls, and any errors.
        """
        results: dict[str, Any] = {"task": task, "calls": []}

        if self.llm is None:
            results.setdefault("errors", []).append("No LLM available; agent requires an LLM to plan tool usage.")
            return results

        try:
            available_tools = self.registry.list()
            logger = logging.getLogger(__name__)

            # Render prompts with automatic datetime context
            rendered = render_prompts(
                self.config.prompts.system_template, 
                {"tools": available_tools},
                auto_datetime=self.config.context.auto_datetime,
                timezone=self.config.context.timezone,
                location=self.config.context.location
            )
            system_msg = rendered.get("system_prompt") or "You are an assistant agent."
            tools_msg = rendered.get("tools_prompt")

            messages = [ChatMessage(role="system", content=system_msg)]
            if tools_msg:
                messages.append(ChatMessage(role="system", content=tools_msg))
            messages.append(ChatMessage(role="user", content=task))

            # Build tool schemas for LLM
            tools_schema: list[dict] = []
            for tool_name in available_tools:
                server = self.registry.get(tool_name)
                tools_schema.append(server.get_schema())

            max_steps = max(1, int(getattr(self.config, "max_steps", 6)))
            
            for step in range(max_steps):
                # Log LLM input
                try:
                    llm_in = [m.model_dump() for m in messages]
                except Exception:
                    llm_in = [repr(m) for m in messages]
                logger.debug("LLM input (step %d): %s", step + 1, llm_in)

                # Get LLM response with tool calling
                llm_out = await self.llm.chat_tools(messages, tools_schema)
                assistant = llm_out.get("assistant", {})
                logger.debug("LLM assistant message (step %d): %s", step + 1, assistant)
                
                tool_calls = assistant.get("tool_calls") or []
                content = assistant.get("content")

                if tool_calls:
                    # Execute each tool call
                    for idx, tc in enumerate(tool_calls):
                        func = tc.get("function", {})
                        tool_name = func.get("name")
                        raw_args = func.get("arguments")
                        
                        # Parse arguments
                        params: dict[str, Any] = {}
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
                            out = await server.call(action_name, params)
                            logger.info("Tool %s returned: %s", tool_name, str(out)[:500])
                            
                            results["calls"].append({
                                "server": tool_name, 
                                "action": action_name, 
                                "params": params, 
                                "result": out
                            })
                            
                            # Add tool result to conversation
                            tool_call_id = tc.get("id") or tc.get("tool_call_id") or f"{tool_name}-call-{int(time.time()*1000)}-{idx}"
                            tool_msg_content = json.dumps(out, ensure_ascii=False)
                            messages.append(ChatMessage(
                                role="tool", 
                                tool_call_id=tool_call_id, 
                                name=tool_name, 
                                content=tool_msg_content
                            ))
                        except Exception as e:
                            logger.exception("Tool %s invocation failed: %s", tool_name, e)
                            results.setdefault("errors", []).append(str(e))
                            messages.append(ChatMessage(
                                role="assistant", 
                                content=f"Observation: tool {tool_name} error: {e}"
                            ))
                            continue
                    # Continue to next step after tool execution
                    continue

                # No tool calls; check for final content
                if content:
                    results["summary"] = content
                    break

                # Neither tools nor content
                results.setdefault("errors", []).append("LLM returned neither tool calls nor content.")
                break

            else:
                results.setdefault("errors", []).append("LLM planner reached max steps without final answer.")
                
        except Exception as e:
            logger.exception("LLM planning failed: %s", e)
            results.setdefault("errors", []).append(f"LLM planning failed: {e}")

        return results

    async def run_events(self, task: str) -> AsyncIterator[dict[str, Any]]:
        """Run the agent and yield structured events for UI streaming.

        Events (JSON objects):
        - {type: "start", task}
        - {type: "thinking", step, assistant}  # assistant content + tool_calls
        - {type: "mcp_call", step, server, action, params}
        - {type: "mcp_result", step, server, action, result}
        - {type: "final", summary}
        - {type: "error", message}
        - {type: "end"}
        """
        results: dict[str, Any] = {"task": task, "calls": []}
        yield {"type": "start", "task": task}

        if self.llm is None:
            msg = "No LLM available; agent requires an LLM to plan tool usage."
            results.setdefault("errors", []).append(msg)
            yield {"type": "error", "message": msg}
            yield {"type": "end"}
            return

        try:
            available_tools = self.registry.list()
            logger = logging.getLogger(__name__)

            # Render prompts with automatic datetime context
            rendered = render_prompts(
                self.config.prompts.system_template, 
                {"tools": available_tools},
                auto_datetime=self.config.context.auto_datetime,
                timezone=self.config.context.timezone,
                location=self.config.context.location
            )
            system_msg = rendered.get("system_prompt") or "You are an assistant agent."
            tools_msg = rendered.get("tools_prompt")

            messages = [ChatMessage(role="system", content=system_msg)]
            if tools_msg:
                messages.append(ChatMessage(role="system", content=tools_msg))
            messages.append(ChatMessage(role="user", content=task))

            max_steps = max(1, int(getattr(self.config, "max_steps", 6)))

            # Build tool schemas
            tools_schema: list[dict] = []
            for tool_name in available_tools:
                server = self.registry.get(tool_name)
                tools_schema.append(server.get_schema())

            for step in range(max_steps):
                try:
                    llm_in = [m.model_dump() for m in messages]
                except Exception:
                    llm_in = [repr(m) for m in messages]
                logger.debug("LLM input (step %d): %s", step + 1, llm_in)

                llm_out = await self.llm.chat_tools(messages, tools_schema)
                assistant = llm_out.get("assistant", {})
                tool_calls = assistant.get("tool_calls") or []
                content = assistant.get("content")

                # Emit thinking event with the raw assistant output for this step
                yield {"type": "thinking", "step": step + 1, "assistant": assistant}

                if tool_calls:
                    for idx, tc in enumerate(tool_calls):
                        func = tc.get("function", {})
                        tool_name = func.get("name")
                        raw_args = func.get("arguments")
                        
                        # Parse arguments (same as run() method)
                        params: dict[str, Any] = {}
                        if isinstance(raw_args, str) and raw_args:
                            try:
                                params = json.loads(raw_args)
                            except json.JSONDecodeError:
                                logger.warning("Failed to parse tool arguments: %s", raw_args)
                                params = {}
                        elif isinstance(raw_args, dict):
                            params = raw_args
                        
                        if not tool_name or tool_name not in available_tools:
                            warn = f"Unknown tool requested: {tool_name}"
                            logger.warning(warn)
                            yield {"type": "error", "message": warn}
                            continue
                        
                        # Get action name and validate (same as run() method)
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

                        yield {"type": "mcp_call", "step": step + 1, "server": tool_name, "action": action_name, "params": params}
                        
                        try:
                            out = await server.call(action_name, params)
                            results["calls"].append({
                                "server": tool_name, 
                                "action": action_name, 
                                "params": params, 
                                "result": out
                            })
                            
                            tool_call_id = tc.get("id") or tc.get("tool_call_id") or f"{tool_name}-call-{int(time.time()*1000)}-{idx}"
                            tool_msg_content = json.dumps(out, ensure_ascii=False)
                            messages.append(ChatMessage(
                                role="tool", 
                                tool_call_id=tool_call_id, 
                                name=tool_name, 
                                content=tool_msg_content
                            ))
                            yield {"type": "mcp_result", "step": step + 1, "server": tool_name, "action": action_name, "result": out}
                        except Exception as e:
                            logger.exception("Tool %s invocation failed: %s", tool_name, e)
                            results.setdefault("errors", []).append(str(e))
                            messages.append(ChatMessage(
                                role="assistant", 
                                content=f"Observation: tool {tool_name} error: {e}"
                            ))
                            yield {"type": "error", "message": f"Tool {tool_name} failed: {e}"}
                            continue
                    # Continue after tools are executed
                    continue

                if content:
                    results["summary"] = content
                    yield {"type": "final", "summary": content}
                    break

                results.setdefault("errors", []).append("LLM returned neither tool calls nor content.")
                yield {"type": "error", "message": "LLM returned neither tool calls nor content."}
                break

            else:
                results.setdefault("errors", []).append("LLM planner reached max steps without final answer.")
                yield {"type": "error", "message": "LLM planner reached max steps without final answer."}

        except Exception as e:
            logger.exception("LLM planning failed: %s", e)
            results.setdefault("errors", []).append(f"LLM planning failed: {e}")
            yield {"type": "error", "message": f"LLM planning failed: {e}"}

        yield {"type": "end"}
