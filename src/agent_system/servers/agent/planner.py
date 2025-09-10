from __future__ import annotations

from typing import List, Dict
from ...llm.clients import ChatMessage, LLMClient
from ...utils.text_sanitizer import sanitize_for_llm


class Planner:
    """Encapsulates LLM planning logic.

    Planner is responsible for creating the initial conversation messages
    and invoking the LLM `chat_tools` API when requested.
    """

    def __init__(self, llm: LLMClient | None, system_prompt: str, tools_prompt: str | None = None):
        self.llm = llm
        self.system_prompt = system_prompt
        self.tools_prompt = tools_prompt

    def initial_messages(self, task: str) -> List[ChatMessage]:
        msgs = [ChatMessage(role="system", content=self.system_prompt)]
        if self.tools_prompt:
            msgs.append(ChatMessage(role="system", content=self.tools_prompt))
        msgs.append(ChatMessage(role="user", content=sanitize_for_llm(task)))
        return msgs

    async def chat(self, messages: List[ChatMessage], tools_schema: List[Dict]):
        if not self.llm:
            return {"assistant": {"role": "assistant", "content": ""}}
        return await self.llm.chat_tools(messages, tools_schema)
