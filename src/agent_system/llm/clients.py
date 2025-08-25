from __future__ import annotations

from typing import Optional
import json
import re

from pydantic import BaseModel
import logging


class ChatMessage(BaseModel):
    role: str
    content: str


class LLMClient:
    async def chat(self, messages: list[ChatMessage]) -> str:
        raise NotImplementedError


class OpenAIAsyncClient(LLMClient):
    """Async client using OpenAI SDK.

    Can talk to:
      - OpenAI (default base)
      - OpenAI-compatible servers (e.g., Ollama) via base_url="http://host:port/v1"
    """

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None) -> None:
        try:
            from openai import AsyncOpenAI  # type: ignore
        except Exception as e:
            raise RuntimeError("openai package required for OpenAIAsyncClient") from e
        self._AsyncOpenAI = AsyncOpenAI
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self.model = model

    async def chat(self, messages: list[ChatMessage]) -> str:
        logger = logging.getLogger(__name__)
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[m.model_dump() for m in messages],
            )
            # Log lightly to avoid huge dumps
            try:
                logger.debug("OpenAI resp id=%s choices=%d", getattr(resp, "id", None), len(getattr(resp, "choices", []) or []))
            except Exception:
                pass
            choice = resp.choices[0] if resp.choices else None
            if not choice:
                return ""
            message = choice.message
            # Prefer content text
            content = getattr(message, "content", None)
            if content:
                return content
            # If tool_calls are present, synthesize our tool JSON
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                try:
                    first = tool_calls[0]
                    function = getattr(first, "function", None)
                    name = getattr(function, "name", None) if function is not None else getattr(first, "name", None)
                    arguments = getattr(function, "arguments", None) if function is not None else getattr(first, "arguments", None)
                    params = {}
                    if isinstance(arguments, str):
                        try:
                            params = json.loads(arguments)
                        except Exception:
                            # best-effort: attempt relaxed quotes
                            params = json.loads(arguments.replace("'", '"')) if arguments else {}
                    elif isinstance(arguments, dict):
                        params = arguments
                    if name:
                        return json.dumps({"type": "tool", "tool": name, "params": params}, ensure_ascii=False)
                except Exception:
                    pass
            # Last fallback: dump the structure to string
            try:
                return getattr(message, "content", None) or ""
            except Exception:
                return ""
        except Exception as e:
            logger.exception("OpenAI chat failed: %s", e)
            return ""


def make_llm(provider: str, model: str, openai_api_key: Optional[str], ollama_url: Optional[str] = None) -> LLMClient:
    """Factory creating an async LLM client.

    - provider=openai: use AsyncOpenAI against OpenAI API.
    - provider=ollama: use AsyncOpenAI against Ollama's OpenAI-compatible endpoint at base_url .../v1.
    """
    if provider == "openai":
        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when provider=openai")
        return OpenAIAsyncClient(model=model, api_key=openai_api_key)
    if provider == "ollama":
        base = (ollama_url.rstrip("/") + "/v1") if ollama_url else "http://127.0.0.1:11434/v1"
        # OpenAI SDK requires some api_key value; Ollama ignores it
        api_key = openai_api_key or "ollama"
        return OpenAIAsyncClient(model=model, api_key=api_key, base_url=base)
    raise ValueError(f"Unknown LLM provider: {provider}")
