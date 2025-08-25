from __future__ import annotations

import re
from typing import Any
import json
import time

from ..config.models import AgentConfig
from ..utils.prompt_renderer import render_system_prompt
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
                    reply = await self.llm.chat(messages)
                    parsed = extract_json(reply if isinstance(reply, str) else str(reply))
                    if not parsed:
                        results.setdefault("errors", []).append("LLM did not return valid tool JSON.")
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
                            server = self.registry.get(tool_name)
                            out = await server.call(params.get("tool", params.get("action", "search")) if isinstance(params, dict) else params)
                        except Exception as e:
                            messages.append(ChatMessage(role="assistant", content=f"Observation: tool {tool_name} raised {e}"))
                            results.setdefault("errors", []).append(str(e))
                            continue
                        # record call and attach observation for next LLM step
                        call_record = {"server": tool_name, "tool": params.get("tool", params.get("action", "call")), "params": params, "result": out}
                        results["calls"].append(call_record)
                        obs_text = json.dumps(out, ensure_ascii=False)
                        messages.append(ChatMessage(role="assistant", content=f"Observation from {tool_name}: {obs_text}"))
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
