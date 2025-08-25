from __future__ import annotations

from typing import Any

from ...llm.clients import ChatMessage, make_llm
from ...mcp.base import MCPServer


class LLMRouterServer(MCPServer):
    def __init__(self, name: str, config: dict | None = None, ssl_verify: bool = True) -> None:
        super().__init__(name, config, ssl_verify=ssl_verify)
        cfg = config or {}
        provider = cfg.get("default_provider", "ollama")
        model = cfg.get("model", "gpt-oss:20b")
        api_key = cfg.get("openai_api_key")
        ollama_url = cfg.get("ollama_url")
        # remaining config values and client creation must be inside __init__
        context_window = cfg.get("context_window")
        ollama_mode = cfg.get("ollama_mode")
        request_timeout = cfg.get("request_timeout")
        self._client = make_llm(
            provider,
            model,
            api_key,
            ollama_url,
            context_window,
            ollama_mode,
            request_timeout,
        )

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "chat":
            messages = [ChatMessage(**m) for m in params.get("messages", [])]
            content = await self._client.chat(messages)
            return {"content": content}
        raise ValueError(f"Unknown tool: {tool}")

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for LLM router."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Route requests to different LLM providers for specialized tasks or alternative AI models.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["chat"], "description": "Use 'chat' to send message to LLM"},
                        "messages": {"type": "array", "description": "Array of message objects with role and content"},
                        "message": {"type": "string", "description": "Single message or prompt to send to the LLM"},
                        "provider": {"type": "string", "description": "Specific LLM provider to use (optional)"},
                        "model": {"type": "string", "description": "Specific model to use (optional)"},
                    },
                    "required": [],
                    "additionalProperties": True,
                },
            },
        }

    def get_default_action(self) -> str:
        """Return the default action for LLM router."""
        return "chat"
