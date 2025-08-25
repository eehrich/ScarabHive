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
                # Prefer our DuckDuckGo server; fall back to any legacy names if present
                available = set(self.registry.list())
                if "google_search" in available:
                    server_name = "google_search"
                elif "duckduckgo_search" in available:
                    server_name = "duckduckgo_search"
                else:
                    server_name = None
                if server_name:
                    server = self.registry.get(server_name)
                    # Build a cleaner search query from the task
                    def build_search_query(t: str) -> str:
                        # Prefer quoted phrases
                        in_quotes = re.findall(r"['\"]([^'\"]+)['\"]", t)
                        if in_quotes:
                            base = " ".join(in_quotes)
                        else:
                            words = re.findall(r"[A-Za-z0-9+\-]+", t.lower())
                            stop = {
                                "can", "you", "search", "for", "and", "what", "it", "does", "in", "a",
                                "how", "high", "is", "the", "that", "this", "occurs", "occur", "give",
                                "me", "ppm", "please", "google", "find"
                            }
                            keywords = [w for w in words if w not in stop]
                            base = " ".join(keywords)
                        if "car" in t.lower() and "automotive" not in base:
                            base = (base + " automotive car").strip()
                        return base.strip() or t

                    search_query = build_search_query(task)
                    payload = {"tool": "search", "params": {"query": search_query, "max_results": 8}}
                    out = await server.call(payload["tool"], payload["params"])
                    results["calls"].append({"server": server_name, **payload, "result": out})

        except Exception as e:
            results.setdefault("errors", []).append(str(e))

        # LLM summary/plan as last step if available
        if self.llm is not None:
            try:
                servers = ", ".join(self.registry.list())
                # Incorporate search results (if any) to ground the answer
                snippets: list[str] = []
                for c in results.get("calls", [])[:1]:  # use first call for now
                    if isinstance(c.get("result"), dict):
                        r = c["result"]
                        if isinstance(r.get("results"), list):
                            for item in r["results"][:5]:
                                title = (item.get("title") or "").strip()
                                body = (item.get("body") or "").strip()
                                href = (item.get("href") or "").strip()
                                if title or body:
                                    line = f"- {title} :: {body}"
                                    if href:
                                        line += f" [{href}]"
                                    snippets.append(line)
                context_block = "\n".join(snippets)
                prompt = (
                    "You are an AI assistant. Available tools: "
                    + servers
                    + ". Task: "
                    + task
                    + ("\n\nWeb results:\n" + context_block if context_block else "")
                    + "\n\nProvide a concise answer. If probability in ppm is requested, state assumptions."
                )
                answer = await self.llm.chat([ChatMessage(role="user", content=prompt)])
                if answer and isinstance(answer, str) and answer.strip():
                    results["summary"] = answer
                else:
                    results.setdefault("notes", []).append("LLM returned empty response.")
            except Exception as _:
                results.setdefault("notes", []).append("LLM call failed; returned raw tool outputs only.")
        else:
            results.setdefault("notes", []).append("LLM not available; returned raw tool outputs only.")

        return results
