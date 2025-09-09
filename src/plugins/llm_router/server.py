from __future__ import annotations

import os
from typing import Any
from pathlib import Path

from agent_system.llm.clients import ChatMessage, make_llm
from agent_system.mcp.base import MCPServer


class LLMRouterServer(MCPServer):
    def __init__(self, name: str, config: dict | None = None, ssl_verify: bool = True) -> None:
        super().__init__(name, config, ssl_verify=ssl_verify)
        self.config = config or {}
        # Store base configuration but don't create a fixed client
        self.default_provider = self._determine_default_provider()
        self.default_model = self.config.get("model", "gpt-4o-mini")
        self.openai_api_key = self.config.get("openai_api_key")
        self.ollama_url = self.config.get("ollama_url")
        self.context_window = self.config.get("context_window")
        self.ollama_mode = self.config.get("ollama_mode")
        self.request_timeout = self.config.get("request_timeout")

    def _determine_default_provider(self) -> str:
        """Determine the best default provider based on available configuration."""
        # Check if OpenAI is properly configured
        openai_key = self.config.get("openai_api_key") or os.getenv("OPENAI_API_KEY")
        if openai_key:
            return "openai"

        # Check if Ollama is available (try to connect or check if it's configured)
        ollama_url = self.config.get("ollama_url") or "http://127.0.0.1:11434"
        if self._is_ollama_available(ollama_url):
            return "ollama"

        # Fallback to OpenAI even if not configured (will fail gracefully with helpful error)
        return "openai"

    def _is_ollama_available(self, url: str) -> bool:
        """Check if Ollama is available at the given URL."""
        try:
            import httpx
            # Try a quick connection to Ollama
            with httpx.Client(timeout=2.0) as client:
                response = client.get(f"{url}/api/tags")
                return response.status_code == 200
        except Exception:
            return False

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
            # Handle both message formats first
            if "messages" in params:
                messages = [ChatMessage(**m) for m in params["messages"]]
            elif "message" in params:
                messages = [ChatMessage(role="user", content=params["message"])]
            else:
                return {"error": "No message or messages provided"}

            # Extract provider and model from parameters, with fallback to defaults
            provider = params.get("provider") or self.default_provider
            model = params.get("model") or self.default_model

            try:
                # Create appropriate client
                client = self._make_client(provider, model)

                content = await client.chat(messages)
                return {
                    "content": content,
                    "provider": provider,
                    "model": model
                }
            except ValueError as e:
                if "API_KEY" in str(e) or "api_key" in str(e):
                    # Try to provide helpful fallback suggestions
                    suggestions = []
                    if provider == "openai":
                        suggestions.append("configure OPENAI_API_KEY environment variable")
                        if self._is_ollama_available(self.ollama_url or "http://127.0.0.1:11434"):
                            suggestions.append("use provider='ollama' instead")
                    elif provider == "ollama":
                        suggestions.append("ensure Ollama is running locally")
                        suggestions.append("configure ollama_url if using custom Ollama instance")

                    return {
                        "error": f"Provider '{provider}' is not properly configured",
                        "details": str(e),
                        "suggestions": suggestions,
                        "available_providers": self._get_available_providers()
                    }
                raise
            except Exception as e:
                return {
                    "error": f"Chat failed with provider '{provider}': {str(e)}",
                    "provider": provider,
                    "model": model
                }
        raise ValueError(f"Unknown tool: {tool}")

    def _get_available_providers(self) -> list[str]:
        """Get list of potentially available providers."""
        available = []
        if self.openai_api_key or os.getenv("OPENAI_API_KEY"):
            available.append("openai")
        if self._is_ollama_available(self.ollama_url or "http://127.0.0.1:11434"):
            available.append("ollama")
        return available

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for LLM router."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema:
            raise RuntimeError("Missing required schema.yaml for llm_router plugin")
        return schema

    def get_default_action(self) -> str:
        """Return the default action for LLM router."""
        return "chat"
