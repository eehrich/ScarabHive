from __future__ import annotations

from typing import AsyncIterator, Optional

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str


class LLMClient:
    async def chat(self, messages: list[ChatMessage]) -> str:
        raise NotImplementedError


class OllamaClient(LLMClient):
    def __init__(self, model: str) -> None:
        self.model = model
        try:
            import ollama  # type: ignore
        except Exception as e:
            raise RuntimeError("ollama package required for OllamaClient") from e
        self._ollama = ollama

    async def chat(self, messages: list[ChatMessage]) -> str:
        # Simple non-streaming call via sync API in a thread would be ideal; here we use blocking call
        # because the scaffold focuses on structure. In production, adapt to asyncio.
        result = self._ollama.chat(model=self.model, messages=[m.model_dump() for m in messages])
        return result.get("message", {}).get("content", "")


class OpenAIClient(LLMClient):
    def __init__(self, api_key: str, model: str) -> None:
        try:
            from openai import OpenAI  # type: ignore
        except Exception as e:
            raise RuntimeError("openai package required for OpenAIClient") from e
        self._OpenAI = OpenAI
        self._client = OpenAI(api_key=api_key)
        self.model = model

    async def chat(self, messages: list[ChatMessage]) -> str:
        resp = self._client.chat.completions.create(model=self.model, messages=[m.model_dump() for m in messages])
        return resp.choices[0].message.content or ""


def make_llm(provider: str, model: str, openai_api_key: Optional[str]) -> LLMClient:
    if provider == "ollama":
        return OllamaClient(model)
    if provider == "openai":
        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when provider=openai")
        return OpenAIClient(openai_api_key, model)
    raise ValueError(f"Unknown LLM provider: {provider}")
