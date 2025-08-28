from __future__ import annotations

from typing import Any

from ...llm.clients import ChatMessage, make_llm
from ...mcp.base import MCPServer


class LLMRouterServer(MCPServer):
    def __init__(self, name: str, config: dict | None = None, ssl_verify: bool = True) -> None:
        super().__init__(name, config, ssl_verify=ssl_verify)
        self.config = config or {}
        # Store base configuration but don't create a fixed client
        self.default_provider = self.config.get("default_provider", "openai")
        self.default_model = self.config.get("model", "gpt-5-mini")
        self.openai_api_key = self.config.get("openai_api_key")
        self.ollama_url = self.config.get("ollama_url")
        self.context_window = self.config.get("context_window")
        self.ollama_mode = self.config.get("ollama_mode")
        self.request_timeout = self.config.get("request_timeout")

    def _make_client(self, provider: str | None = None, model: str | None = None):
        """Create an LLM client with specified or default parameters."""
        provider = provider or self.default_provider
        model = model or self.default_model
        return make_llm(
            provider,
            model,
            self.openai_api_key,
            self.ollama_url,
            self.context_window,
            self.ollama_mode,
            self.request_timeout,
            ssl_verify=self.ssl_verify,
        )

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "chat":
            # Extract provider and model from parameters
            provider = params.get("provider")
            model = params.get("model")
            
            # Create appropriate client
            client = self._make_client(provider, model)
            
            # Handle both message formats
            if "messages" in params:
                messages = [ChatMessage(**m) for m in params["messages"]]
            elif "message" in params:
                messages = [ChatMessage(role="user", content=params["message"])]
            else:
                return {"error": "No message or messages provided"}
            
            try:
                content = await client.chat(messages)
                return {
                    "content": content,
                    "provider": provider or self.default_provider,
                    "model": model or self.default_model
                }
            except Exception as e:
                return {
                    "error": str(e),
                    "provider": provider or self.default_provider,
                    "model": model or self.default_model
                }
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
                        "messages": {
                            "type": "array", 
                            "description": "Array of message objects with role and content",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {"type": "string", "description": "Message role (user, assistant, system)"},
                                    "content": {"type": "string", "description": "Message content"}
                                },
                                "required": ["role", "content"]
                            }
                        },
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
