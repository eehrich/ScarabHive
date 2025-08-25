from __future__ import annotations

import re
from typing import Any
import json
import time

from ..config.models import AgentConfig
from ..utils.prompt_renderer import render_system_prompt
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

                system_msg = render_system_prompt(self.config.prompts.system_template, {"tools": available_tools})

                messages = [ChatMessage(role="system", content=system_msg), ChatMessage(role="user", content=task)]

                max_steps = 6
                import re

                def extract_json(s: str) -> dict | None:
                    if not s:
                        return None
                    # Prefer JSON inside ```json ... ``` fences
                    m = re.search(r"```json\s*(\{.*?\})\s*```", s, flags=re.DOTALL | re.IGNORECASE)
                    if m:
                        cand = m.group(1)
                        try:
                            return json.loads(cand)
                        except Exception:
                            return None
                    # Then try any fenced block ``` ... ``` containing JSON
                    m = re.search(r"```\s*(\{.*?\})\s*```", s, flags=re.DOTALL)
                    if m:
                        cand = m.group(1)
                        try:
                            return json.loads(cand)
                        except Exception:
                            return None
                    # Finally, find the first balanced JSON object in the text
                    try:
                        start = s.index("{")
                    except ValueError:
                        return None
                    depth = 0
                    for i in range(start, len(s)):
                        if s[i] == "{":
                            depth += 1
                        elif s[i] == "}":
                            depth -= 1
                            if depth == 0:
                                cand = s[start : i + 1]
                                try:
                                    return json.loads(cand)
                                except Exception:
                                    return None
                    return None

                for step in range(max_steps):
                    # Log LLM input (structured)
                    try:
                        llm_in = [m.model_dump() for m in messages]
                    except Exception:
                        # fallback to simple repr
                        llm_in = [repr(m) for m in messages]
                    logger.debug("LLM input (step %d): %s", step + 1, llm_in)

                    # Small retry loop to coerce a valid JSON action from the LLM
                    parsed = None
                    last_raw = None
                    tries = 0
                    while tries < 2 and parsed is None:
                        reply = await self.llm.chat(messages)
                        last_raw = reply
                        logger.debug("LLM raw reply (step %d try %d): %s", step + 1, tries + 1, reply)
                        raw = reply if isinstance(reply, str) else str(reply)
                        parsed = extract_json(raw)
                        if not parsed:
                            # Try heuristic: extract Function(name='foo', arguments={...}) from repr
                            m = re.search(r"Function\(\s*name=(['\"]) (?P<name>[^'\"]+)\1.*?arguments=(?P<args>\{.*?\})\s*\)", raw, flags=re.DOTALL)
                            if m:
                                try:
                                    name = m.group("name")
                                    args_txt = m.group("args")
                                    args = json.loads(args_txt.replace("'", '"'))
                                    parsed = {"type": "tool", "tool": name, "params": args}
                                    logger.info("Heuristic parsed tool call from LLM repr: %s %s", name, args)
                                except Exception:
                                    parsed = None
                        if parsed is None:
                            tries += 1
                            if tries < 2:
                                # Nudge the model to respond correctly
                                messages.append(ChatMessage(role="system", content="Return exactly one action as JSON in a ```json``` block. No prose."))

                    reply = last_raw
                    if not parsed:
                        results.setdefault("errors", []).append("LLM did not return valid tool JSON.")
                        results.setdefault("raw_llm_reply", raw if 'raw' in locals() else str(last_raw))
                        logger.error("LLM parse failed; raw reply: %s", raw if 'raw' in locals() else str(last_raw))
                        break

                    if parsed.get("type") == "final":
                        results["summary"] = parsed.get("answer", "")
                        break

                    if parsed.get("type") == "tool":
                        tool_name = parsed.get("tool")
                        params = parsed.get("params", {}) or {}
                        if tool_name not in available_tools:
                            messages.append(ChatMessage(role="assistant", content=f"Error: unknown tool {tool_name}"))
                            results.setdefault("errors", []).append(f"Requested unknown tool: {tool_name}")
                            break
                        try:
                            action_name = params.get("tool", params.get("action", "search"))
                            logger.info("Calling tool %s action %s with params %s", tool_name, action_name, params)
                            server = self.registry.get(tool_name)
                            # servers expect (tool_action, params) where tool_action is e.g. 'search'
                            if isinstance(params, dict):
                                out = await server.call(action_name, params)
                            else:
                                # if params is a raw string or other, pass it as the second arg and default action
                                out = await server.call("search", params)
                            logger.info("Tool %s returned result: %s", tool_name, out)
                        except Exception as e:
                            logger.exception("Tool %s raised an exception", tool_name)
                            messages.append(ChatMessage(role="assistant", content=f"Observation: tool {tool_name} raised {e}"))
                            results.setdefault("errors", []).append(str(e))
                            continue
                        # record call and attach observation for next LLM step
                        call_record = {"server": tool_name, "action": action_name if isinstance(params, dict) else "search", "params": params, "result": out}
                        results["calls"].append(call_record)
                        # Build a concise observation to keep the loop stable
                        obs_summary = None
                        try:
                            if isinstance(out, (list, tuple)) and out and isinstance(out[0], dict):
                                items = []
                                for item in out[:3]:
                                    title = item.get("title") or item.get("name") or item.get("symbol") or "item"
                                    link = item.get("link") or item.get("url") or ""
                                    snippet = item.get("snippet") or item.get("summary") or item.get("text") or ""
                                    items.append({"title": title, "link": link, "snippet": snippet[:180]})
                                obs_summary = {"summary": items, "total": len(out)}
                            elif isinstance(out, dict):
                                obs_summary = {k: (v[:180] + "...") if isinstance(v, str) and len(v) > 180 else v for k, v in list(out.items())[:8]}
                            else:
                                obs_summary = str(out)
                        except Exception:
                            obs_summary = str(out)
                        messages.append(ChatMessage(role="assistant", content=f"Observation: {json.dumps(obs_summary, ensure_ascii=False)}"))
                        # small pause to avoid tight loops with sync LLMs
                        time.sleep(0.1)
                        continue

                    # unknown directive
                    results.setdefault("errors", []).append("LLM returned unknown directive.")
                    break

                else:
                    results.setdefault("errors", []).append("LLM planner reached max steps without final answer.")
            except Exception as e:
                results.setdefault("errors", []).append(str(e))
                results.setdefault("errors", []).append("LLM planning failed.")
        else:
            results.setdefault("errors", []).append("No LLM available; agent requires an LLM to plan tool usage.")

        return results
