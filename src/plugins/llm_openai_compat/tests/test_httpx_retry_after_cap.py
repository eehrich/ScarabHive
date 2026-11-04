"""A server's Retry-After reaches the error whole, for the model's block; inf is no hint."""
import pytest


def test_httpx_client_reads_the_ask_whole():
    from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient

    parse = HTTPXOpenAIClient._parse_retry_after
    assert parse(None, "3600") == 3600.0
    assert parse(None, "inf") is None
    assert parse(None, "2") == 2.0


async def _refused(monkeypatch, retry_after, stream):
    import httpx
    import pytest
    from agent_system.llm.models import ChatMessage, LLMRateLimitError
    from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient

    client = HTTPXOpenAIClient(model="m", api_key="k", base_url="https://llm.example/v1", verify=False,
                               max_retries=1, retry_backoff=0.0)
    client.rate_limit_max_retries = 2
    calls, slept = [], []

    async def refuses(self, request, **kwargs):
        calls.append(request.url)
        return httpx.Response(429, text="slow down", headers={"retry-after": retry_after}, request=request)

    async def sleep(seconds, token=None):
        slept.append(seconds)

    monkeypatch.setattr(httpx.AsyncClient, "send", refuses)
    monkeypatch.setattr(client, "_cancellable_sleep", sleep)
    messages = [ChatMessage(role="user", content="hi")]
    with pytest.raises(LLMRateLimitError) as refused:
        if stream:
            async for _ in client.chat_tools_streaming(messages, tools=[]):
                pass
        else:
            await client.chat_tools(messages, [])
    return refused.value, len(calls), slept


@pytest.mark.parametrize("stream", [False, True])
async def test_a_long_ask_is_not_slept_and_reaches_the_error_whole(monkeypatch, stream):
    error, calls, slept = await _refused(monkeypatch, "3600", stream)
    assert error.retry_after == 3600.0, "the model's block reads this"
    assert calls == 1 and slept == [], "a turn parked over a wait the fallback chain could skip"


@pytest.mark.parametrize("stream", [False, True])
async def test_a_short_ask_is_waited_out(monkeypatch, stream):
    error, calls, slept = await _refused(monkeypatch, "2", stream)
    assert error.retry_after == 2.0
    assert calls == 3 and len(slept) == 2 and all(2.0 <= seconds <= 3.0 for seconds in slept)
