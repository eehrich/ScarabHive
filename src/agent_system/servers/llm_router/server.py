from __future__ import annotations

from typing import Any, Optional

from ...llm.clients import ChatMessage, make_llm
from ...mcp.base import MCPServer


class LLMRouterServer(MCPServer):
    def __init__(self, name: str, config: dict | None = None, ssl_verify: bool = True) -> None:
        super().__init__(name, config, ssl_verify=ssl_verify)
        provider = (config or {}).get("default_provider", "ollama")
        model = (config or {}).get("model", "gpt-oss:20b")
        api_key = (config or {}).get("openai_api_key")
        self._client = make_llm(provider, model, api_key)

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "chat":
            messages = [ChatMessage(**m) for m in params.get("messages", [])]
            content = await self._client.chat(messages)
            return {"content": content}
        raise ValueError(f"Unknown tool: {tool}")
