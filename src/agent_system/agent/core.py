from __future__ import annotations

import re
from typing import Any, AsyncIterator
import json
import time

from ..config.models import AgentConfig
from ..utils.prompt_renderer import render_system_prompt, render_prompts
import logging
from ..llm.clients import ChatMessage, make_llm
from ..mcp.base import MCPRegistry


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
        except Exception:
            # LLM optional; continue without it
            self.llm = None

    async def run(self, task: str) -> dict[str, Any]:
        task_l = task.lower()
        results: dict[str, Any] = {"task": task, "calls": []}

        # Prefer an LLM-driven planning loop: ask the LLM what tools to call, execute them,
        # provide observations, and repeat until the LLM returns a final answer.
        if self.llm is not None:
            try:
                available_tools = self.registry.list()
                logger = logging.getLogger(__name__)

                # Render prompts (system + tools)
                rendered = render_prompts(self.config.prompts.system_template, {"tools": available_tools})
                system_msg = rendered.get("system_prompt") or "You are an assistant agent."
                tools_msg = rendered.get("tools_prompt")

                messages = [ChatMessage(role="system", content=system_msg)]
                if tools_msg:
                    messages.append(ChatMessage(role="system", content=tools_msg))
                messages.append(ChatMessage(role="user", content=task))

                max_steps = max(1, int(getattr(self.config, "max_steps", 6)))
                import re

                # Native tool calling: build tool schema list the LLM can choose from
                # Use a simple tool schema: a 'search' action with free-form params; servers parse their own params
                tools_schema: list[dict] = []
                for t in available_tools:
                    if t == "web_scraper":
                        tools_schema.append({
                            "type": "function",
                            "function": {
                                "name": t,
                                "description": "Fetch and read a web page by URL.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "action": {"type": "string", "enum": ["fetch"], "description": "Use 'fetch' to download the page"},
                                        "url": {"type": "string", "description": "The absolute URL to fetch"},
                                        "timeout": {"type": "number", "default": 20},
                                        "include_html": {"type": "boolean", "default": False},
                                        "max_chars": {"type": "integer", "default": 0, "description": "If >0, truncate extracted text to this length"},
                                    },
                                    "required": ["url"],
                                    "additionalProperties": True,
                                },
                            },
                        })
                    else:
                        tools_schema.append({
                            "type": "function",
                            "function": {
                                "name": t,
                                "description": f"Call the MCP server '{t}'. Pass a JSON object with fields appropriate for the action.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "action": {"type": "string", "description": "Action to perform, e.g., 'search'"},
                                        "query": {"type": "string", "description": "Query or main input"},
                                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                                    },
                                    "additionalProperties": True,
                                },
                            },
                        })

                for step in range(max_steps):
                    # Log LLM input (structured)
                    try:
                        llm_in = [m.model_dump() for m in messages]
                    except Exception:
                        # fallback to simple repr
                        llm_in = [repr(m) for m in messages]
                    logger.debug("LLM input (step %d): %s", step + 1, llm_in)

                    # Ask the LLM with native tool calling
                    llm_out = await self.llm.chat_tools(messages, tools_schema)
                    assistant = llm_out.get("assistant", {})
                    logger.debug("LLM assistant message (step %d): %s", step + 1, assistant)
                    tool_calls = assistant.get("tool_calls") or []
                    content = assistant.get("content")

                    if tool_calls:
                        # Execute each tool call in order, append tool results as tool messages
                        for idx, tc in enumerate(tool_calls):
                            func = tc.get("function", {})
                            tool_name = func.get("name")
                            raw_args = func.get("arguments")
                            params: dict[str, Any] = {}
                            if isinstance(raw_args, str) and raw_args:
                                try:
                                    params = json.loads(raw_args)
                                except Exception:
                                    # fall back to permissive parsing
                                    try:
                                        params = json.loads(raw_args.replace("'", '"'))
                                    except Exception:
                                        params = {}
                            elif isinstance(raw_args, dict):
                                params = raw_args
                            if not tool_name or tool_name not in available_tools:
                                logger.warning("Unknown tool requested: %s", tool_name)
                                continue
                            action_name = params.get("action") or params.get("tool") or "search"
                            try:
                                logger.info("Invoking tool %s action %s with params %s", tool_name, action_name, params)
                                server = self.registry.get(tool_name)
                                out = await server.call(action_name, params)
                                logger.info("Tool %s returned: %s", tool_name, str(out)[:500])
                                results["calls"].append({"server": tool_name, "action": action_name, "params": params, "result": out})
                                # Append tool result message
                                tool_call_id = tc.get("id") or tc.get("tool_call_id") or f"{tool_name}-call-{int(time.time()*1000)}-{idx}"
                                tool_msg_content = json.dumps(out, ensure_ascii=False)
                                messages.append(ChatMessage(role="tool", tool_call_id=tool_call_id, name=tool_name, content=tool_msg_content))
                            except Exception as e:
                                logger.exception("Tool %s invocation failed: %s", tool_name, e)
                                results.setdefault("errors", []).append(str(e))
                                messages.append(ChatMessage(role="assistant", content=f"Observation: tool {tool_name} error: {e}"))
                                continue
                        # After executing tools, continue to next step to let LLM synthesize
                        continue

                    # No tool calls; if there's final content, capture and finish
                    if content:
                        results["summary"] = content
                        break

                    # If neither tools nor content, consider it as no-op and break
                    results.setdefault("errors", []).append("LLM returned neither tool calls nor content.")
                    break

                else:
                    results.setdefault("errors", []).append("LLM planner reached max steps without final answer.")
            except Exception as e:
                results.setdefault("errors", []).append(str(e))
                results.setdefault("errors", []).append("LLM planning failed.")
        else:
            results.setdefault("errors", []).append("No LLM available; agent requires an LLM to plan tool usage.")

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

            # Render prompts (system + tools)
            rendered = render_prompts(self.config.prompts.system_template, {"tools": available_tools})
            system_msg = rendered.get("system_prompt") or "You are an assistant agent."
            tools_msg = rendered.get("tools_prompt")

            messages = [ChatMessage(role="system", content=system_msg)]
            if tools_msg:
                messages.append(ChatMessage(role="system", content=tools_msg))
            messages.append(ChatMessage(role="user", content=task))

            max_steps = max(1, int(getattr(self.config, "max_steps", 6)))

            tools_schema: list[dict] = []
            for t in available_tools:
                if t == "web_scraper":
                    tools_schema.append({
                        "type": "function",
                        "function": {
                            "name": t,
                            "description": "Fetch and read a web page by URL.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string", "enum": ["fetch"], "description": "Use 'fetch' to download the page"},
                                    "url": {"type": "string", "description": "The absolute URL to fetch"},
                                    "timeout": {"type": "number", "default": 20},
                                    "include_html": {"type": "boolean", "default": False},
                                    "max_chars": {"type": "integer", "default": 0, "description": "If >0, truncate extracted text to this length"},
                                },
                                "required": ["url"],
                                "additionalProperties": True,
                            },
                        },
                    })
                else:
                    tools_schema.append({
                        "type": "function",
                        "function": {
                            "name": t,
                            "description": f"Call the MCP server '{t}'. Pass a JSON object with fields appropriate for the action.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string", "description": "Action to perform, e.g., 'search'"},
                                    "query": {"type": "string", "description": "Query or main input"},
                                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                                },
                                "additionalProperties": True,
                            },
                        },
                    })

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
                        params: dict[str, Any] = {}
                        if isinstance(raw_args, str) and raw_args:
                            try:
                                params = json.loads(raw_args)
                            except Exception:
                                try:
                                    params = json.loads(raw_args.replace("'", '"'))
                                except Exception:
                                    params = {}
                        elif isinstance(raw_args, dict):
                            params = raw_args
                        if not tool_name or tool_name not in available_tools:
                            warn = f"Unknown tool requested: {tool_name}"
                            logger.warning(warn)
                            yield {"type": "error", "message": warn}
                            continue
                        action_name = params.get("action") or params.get("tool") or "search"

                        yield {"type": "mcp_call", "step": step + 1, "server": tool_name, "action": action_name, "params": params}
                        try:
                            server = self.registry.get(tool_name)
                            out = await server.call(action_name, params)
                            results["calls"].append({"server": tool_name, "action": action_name, "params": params, "result": out})
                            tool_call_id = tc.get("id") or tc.get("tool_call_id") or f"{tool_name}-call-{int(time.time()*1000)}-{idx}"
                            tool_msg_content = json.dumps(out, ensure_ascii=False)
                            messages.append(ChatMessage(role="tool", tool_call_id=tool_call_id, name=tool_name, content=tool_msg_content))
                            yield {"type": "mcp_result", "step": step + 1, "server": tool_name, "action": action_name, "result": out}
                        except Exception as e:
                            logger.exception("Tool %s invocation failed: %s", tool_name, e)
                            results.setdefault("errors", []).append(str(e))
                            messages.append(ChatMessage(role="assistant", content=f"Observation: tool {tool_name} error: {e}"))
                            yield {"type": "error", "message": f"Tool {tool_name} failed: {e}"}
                            continue
                    # Let the loop continue after tools are executed
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
            results.setdefault("errors", []).append(str(e))
            results.setdefault("errors", []).append("LLM planning failed.")
            yield {"type": "error", "message": f"LLM planning failed: {e}"}

        yield {"type": "end"}
