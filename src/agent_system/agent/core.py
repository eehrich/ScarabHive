from __future__ import annotations

import re
from typing import Any

from ..config.models import AgentConfig
from ..llm.clients import ChatMessage, make_llm
from ..mcp.base import MCPRegistry


class Agent:
    def __init__(self, config: AgentConfig, registry: MCPRegistry) -> None:
        self.config = config
        self.registry = registry
        self.llm = None
        try:
            self.llm = make_llm(config.llm.provider, config.llm.model, config.llm.openai_api_key)
        except Exception:
            # LLM optional; continue without it
            self.llm = None

    async def run(self, task: str) -> dict[str, Any]:
        task_l = task.lower()
        results: dict[str, Any] = {"task": task, "calls": []}

        # Very simple routing heuristics
        try:
            if any(k in task_l for k in ["tweet", "twitter", "x.com"]):
                server = self.registry.get("twitter_search")
                payload = {"tool": "search", "params": {"query": task, "limit": 5}}
                out = await server.call(payload["tool"], payload["params"])
                results["calls"].append({"server": "twitter_search", **payload, "result": out})

            ticker_match = re.search(r"\b([A-Z]{1,5})(?:\s+stock|\s+price|\s+quote|\b)", task)
            if ticker_match:
                ticker = ticker_match.group(1)
                server = self.registry.get("yahoo_finance")
                payload = {"tool": "quote", "params": {"ticker": ticker}}
                out = await server.call(payload["tool"], payload["params"])
                results["calls"].append({"server": "yahoo_finance", **payload, "result": out})

            if any(k in task_l for k in ["search ", "websearch", "google", "find "]):
                server_name = "websearch_google" if "websearch_google" in self.registry.list() else (
                    "websearch_abstract" if "websearch_abstract" in self.registry.list() else None
                )
                if server_name:
                    server = self.registry.get(server_name)
                    payload = {"tool": "search", "params": {"query": task, "max_results": 5}}
                    out = await server.call(payload["tool"], payload["params"])
                    results["calls"].append({"server": server_name, **payload, "result": out})

        except Exception as e:
            results.setdefault("errors", []).append(str(e))

        # LLM summary/plan as last step if available
        if self.llm is not None:
            try:
                servers = ", ".join(self.registry.list())
                prompt = (
                    "You are an AI agent. Available tools: "
                    + servers
                    + ". Summarize the results provided and answer the task succinctly. Task: "
                    + task
                )
                answer = await self.llm.chat([ChatMessage(role="user", content=prompt)])
                results["summary"] = answer
            except Exception as _:
                results.setdefault("notes", []).append("LLM call failed; returned raw tool outputs only.")
        else:
            results.setdefault("notes", []).append("LLM not available; returned raw tool outputs only.")

        return results
