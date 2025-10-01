from __future__ import annotations

from typing import Optional, Any, List, Dict
from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class LLMClient:
    async def chat(self, messages: list[ChatMessage], cancellation_token=None) -> str:
        raise NotImplementedError

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None) -> dict:
        raise NotImplementedError
