"""A 200 whose body does not parse reaches the fallback chain, which knows no plain Exception."""
import httpx
import pytest

from agent_system.llm.models import ChatMessage, LLMServerError
from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient


@pytest.mark.parametrize("body", ['{"choices": [{"mess', ""])
async def test_a_body_that_does_not_parse_is_typed(monkeypatch, body):
    client = HTTPXOpenAIClient(model="m", api_key="k", base_url="https://llm.example/v1", verify=False,
                               max_retries=1, retry_backoff=0.0)
    client.capabilities = {"streaming": False}

    async def truncated(self, request, **kwargs):
        return httpx.Response(200, text=body, headers={"content-type": "application/json"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", truncated)

    with pytest.raises(LLMServerError):
        await client.chat_tools([ChatMessage(role="user", content="hi")], [])


async def test_a_bug_in_our_code_is_no_server_error(monkeypatch):
    """The chain would switch models over it, and report a server outage."""
    client = HTTPXOpenAIClient(model="m", api_key="k", base_url="https://llm.example/v1", verify=False,
                               max_retries=1, retry_backoff=0.0)
    client.capabilities = {"streaming": False}

    async def answers(self, request, **kwargs):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "x"}}]},
                              request=request)

    def broken(response_data):
        raise TypeError("a bug")

    monkeypatch.setattr(httpx.AsyncClient, "send", answers)
    monkeypatch.setattr(client, "_format_response", broken)

    with pytest.raises(Exception, match="a bug") as raised:
        await client.chat_tools([ChatMessage(role="user", content="hi")], [])
    assert not isinstance(raised.value, LLMServerError)


@pytest.mark.parametrize("body", [{}, {"choices": []}, []])
async def test_a_200_without_choices_is_no_answer(monkeypatch, body):
    """It used to become content "", which the loop finally took as the final answer."""
    client = HTTPXOpenAIClient(model="m", api_key="k", base_url="https://llm.example/v1", verify=False,
                               max_retries=1, retry_backoff=0.0)
    client.capabilities = {"streaming": False}
    sent = []

    async def empty(self, request, **kwargs):
        sent.append(request)
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", empty)

    with pytest.raises(LLMServerError, match="without choices"):
        await client.chat_tools([ChatMessage(role="user", content="hi")], [])
    assert len(sent) == 2, "retried once before it gives up"


async def test_a_200_without_choices_then_an_answer_is_the_answer(monkeypatch):
    client = HTTPXOpenAIClient(model="m", api_key="k", base_url="https://llm.example/v1", verify=False,
                               max_retries=1, retry_backoff=0.0)
    client.capabilities = {"streaming": False}
    bodies = [{"choices": []}, {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}]

    async def answers(self, request, **kwargs):
        return httpx.Response(200, json=bodies.pop(0), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", answers)
    result = await client.chat_tools([ChatMessage(role="user", content="hi")], [])
    assert result["assistant"]["content"] == "hi"


async def test_a_body_error_still_reaches_the_answer(monkeypatch):
    """A 200 with an error and no choices is the body-error path, not "no answer"."""
    client = HTTPXOpenAIClient(model="m", api_key="k", base_url="https://llm.example/v1", verify=False,
                               max_retries=0, retry_backoff=0.0)
    client.capabilities = {"streaming": False}

    async def refused(self, request, **kwargs):
        return httpx.Response(200, json={"error": {"code": 400, "message": "content filter"}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", refused)
    result = await client.chat_tools([ChatMessage(role="user", content="hi")], [])
    assert result["assistant"]["error"]["message"] == "content filter"
