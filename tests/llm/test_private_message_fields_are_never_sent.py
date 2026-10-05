"""Our own message fields (``PRIVATE_MESSAGE_FIELDS``) never reach a provider.

The batch wrapper dumps the whole message and popped a list of names kept by
hand; a new private field (``prefixed_by``) would have gone out with it.
Ollama dumps it too and relies on ``ollama_utils.normalize_message`` copying
only the fields its API reads.
"""
from types import SimpleNamespace

import pytest

from agent_system.llm.models import PRIVATE_MESSAGE_FIELDS, ChatMessage, LLMClient


def test_prefixed_by_is_one_of_them():
    # The tests below check against the set itself: without this line a field
    # missing from it would pass them all.
    assert {"injected_by", "prefixed_by"} <= PRIVATE_MESSAGE_FIELDS


def _task() -> ChatMessage:
    return ChatMessage(role="user", content="Hinweis\n\n---\n\nAufgabe",
                       prefixed_by={"hint": "Hinweis\n\n---\n\n"}, request_id="r1", step=1)


def test_ollama_sends_none_of_them():
    from plugins.llm_ollama.ollama_client import OllamaNativeAsyncClient

    (msg,) = OllamaNativeAsyncClient(model="m", base_url="http://localhost:11434")._map_messages([_task()])
    assert not PRIVATE_MESSAGE_FIELDS & msg.keys(), msg
    assert msg["content"] == "Hinweis\n\n---\n\nAufgabe"


class _Sync(LLMClient):
    model = "m"

    async def chat(self, messages, cancellation_token=None):
        return "sync"

    async def chat_tools(self, messages, tools, cancellation_token=None):
        return {"assistant": {"role": "assistant", "content": "sync"}}


@pytest.mark.asyncio
async def test_the_batch_queue_gets_none_of_them():
    from agent_system.llm.batch.batch_client import BatchLLMClient

    seen = {}

    class Queue:
        async def submit_request(self, **kwargs):
            seen["messages"] = kwargs["messages"]
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    client = BatchLLMClient(underlying_client=_Sync(), queue_manager=Queue(),
                            batch_provider_config=SimpleNamespace(fallback_to_sync=False),
                            model_name="m", batch_provider="openai")
    await client.chat([_task()])
    (msg,) = seen["messages"]
    assert not PRIVATE_MESSAGE_FIELDS & msg.keys(), msg
