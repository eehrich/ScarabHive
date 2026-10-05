"""The decisions client against a scripted HTTP server on 127.0.0.1.

No patch of httpx: the real client posts to a real socket, and each test
scripts what comes back -- status, headers, body. What is pinned here is the
part of the shared client standard the patched-transport tests in
test_system_one.py cannot reach: a Retry-After the host sends, and an answer
body whose shape is wrong in a way that used to escape as an AttributeError
and lose what the call cost.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from plugins.llm_decisions.system_one import SYSTEM_ONE, DecisionsClient, DecisionsError

QUESTIONS = {"safe": {"type": "noul", "instructions": "Is it safe?"}}
BILLED = {"input_tokens": 300, "output_tokens": 2, "cost": 1.2e-05}
OK = {"model": "m", "answers": {"safe": {"type": "noul", "noul": 0.9}}, "usage": BILLED}


class _Host:
    """Answers each request with the next scripted (status, headers, body)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = 0

    async def _serve(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                       if line.lower().startswith(b"content-length")), 0)
        await reader.readexactly(length)
        self.requests += 1
        status, headers, body = self.replies.pop(0)
        raw = json.dumps(body).encode()
        extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n{extra}"
                     f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/v1/systemone"

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


class _Registry:
    def __init__(self):
        self.ends = []

    async def execute_hooks(self, hook_type, context, **kwargs):
        if hook_type.value == "post_llm_response":
            self.ends.append(context)
        return context


def _client(url, retries=0):
    return DecisionsClient(model="m", url=url, api_key="k", max_retries=retries, host=SYSTEM_ONE)


@pytest.fixture
def waits(monkeypatch):
    """The backoff waits, recorded and skipped."""
    seen, real_sleep = [], asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda delay: (seen.append(delay), real_sleep(0))[1])
    return seen


@pytest.mark.parametrize("answers", [["safe", 0.9], {"safe": 0.9}, {"safe": None}])
async def test_an_answers_body_of_the_wrong_shape_is_refused_with_what_it_cost(answers):
    """A list where the answers object belongs, or a bare number where one answer
    belongs, was an AttributeError: untyped, and the usage of a billed call was
    lost for the hook's error row and for the decision tool's total."""
    registry = _Registry()
    host = _Host((200, {}, {"model": "m", "answers": answers, "usage": BILLED}))
    with patch("agent_system.hooks.get_hook_registry", lambda: registry):
        async with host as url:
            with pytest.raises(DecisionsError) as refused:
                await _client(url).decide("rm -rf /tmp/x", QUESTIONS)
    assert refused.value.usage == BILLED
    assert len(registry.ends) == 1 and registry.ends[0].llm_usage == BILLED


async def test_usage_figures_that_are_not_numbers_are_a_decisions_error():
    host = _Host((200, {}, {**OK, "usage": {"input_tokens": "lots", "cost": "n/a"}}))
    async with host as url:
        with pytest.raises(DecisionsError, match="usage"):
            await _client(url).decide("x", QUESTIONS)


async def test_a_refusal_message_stays_short_whatever_the_answer_holds():
    huge = {"type": "noul", "noul": None, "junk": "x" * 100_000}
    host = _Host((200, {}, {"model": "m", "answers": {"safe": huge}, "usage": BILLED}))
    async with host as url:
        with pytest.raises(DecisionsError) as refused:
            await _client(url).decide("x", QUESTIONS)
    assert len(str(refused.value)) < 1000


async def test_a_busy_host_is_waited_for_as_long_as_it_asks(waits):
    host = _Host((429, {"Retry-After": "7"}, {"error": "slow down"}), (200, {}, OK))
    async with host as url:
        result = await _client(url, retries=1).decide("x", QUESTIONS)
    assert result["safe"].value == 0.9 and waits == [7.0] and host.requests == 2


@pytest.mark.parametrize("header", ["soon", "-1", "inf", "nan"])
async def test_a_retry_after_that_is_no_duration_falls_back_to_the_backoff(waits, header):
    host = _Host((503, {"Retry-After": header}, {}), (200, {}, OK))
    async with host as url:
        await _client(url, retries=1).decide("x", QUESTIONS)
    assert waits == [2.0]


async def test_a_retry_after_beyond_the_bound_ends_the_call_at_once(waits):
    """Sleeping an hour inside a tool call helps nobody: the call fails now, says why."""
    registry = _Registry()
    host = _Host((429, {"Retry-After": "3600"}, {}), (200, {}, OK))
    with patch("agent_system.hooks.get_hook_registry", lambda: registry):
        async with host as url:
            with pytest.raises(DecisionsError, match="3600"):
                await _client(url, retries=2).decide("x", QUESTIONS)
    assert waits == [] and host.requests == 1
    assert len(registry.ends) == 1 and registry.ends[0].llm_finish_reason == "error"
