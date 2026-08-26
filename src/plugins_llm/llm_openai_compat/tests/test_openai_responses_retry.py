"""The retry paths of the Responses client.

These branches decide what happens when a provider misbehaves, and they were
entirely uncovered: a coverage run over the client never executed the
transport-retry, tier-drop, 5xx-retry or exhaustion arms. They are also where
a live UnboundLocalError came from (`_tier_dropped` read before assignment on
a retry that followed a 429), so "never executed" was not the same as "fine".

Everything here drives the real `chat_tools()` against a fake transport;
nothing reimplements the loop.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.models import (
    ChatMessage,
    LLMQuotaExhaustedError,
    LLMRateLimitError,
    LLMServerError,
)
from plugins_llm.llm_openai_compat.openai_responses_client import OpenAIResponsesClient

OK_BODY = {"output": [{"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": "done"}]}]}
MESSAGES = [ChatMessage(role="user", content="hi")]


def _client(**kw) -> OpenAIResponsesClient:
    defaults = dict(model="openai/gpt-5.6-terra", api_key="sk-test",
                    base_url="https://openrouter.ai/api/v1",
                    service_tier="flex", max_retries=2, retry_backoff=0.0)
    defaults.update(kw)
    return OpenAIResponsesClient(**defaults)


class _Transport:
    """Replays a scripted list of responses/exceptions and records payloads."""

    def __init__(self, script):
        self.script = list(script)
        self.payloads: list[dict] = []

    async def post(self, url, json=None, headers=None):
        # A COPY: the client pops service_tier out of the very dict it sent, so
        # recording the reference would rewrite history and the tier-drop
        # assertion would compare a payload against its own later state.
        self.payloads.append(copy.deepcopy(json))
        item = self.script.pop(0) if self.script else httpx.Response(200, json=OK_BODY)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def calls(self) -> int:
        return len(self.payloads)


@pytest.fixture
def transport(monkeypatch):
    """Replace the AsyncClient so no socket is opened and sleeps are free."""
    holder = {}

    def install(script):
        t = _Transport(script)
        holder["t"] = t

        class _FakeAsyncClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return t
            async def __aexit__(self, *a): return False

        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
        return t

    return install


class TestTransportErrors:
    async def test_a_timeout_is_retried_and_then_succeeds(self, transport):
        t = transport([httpx.TimeoutException("boom"), httpx.Response(200, json=OK_BODY)])
        result = await _client().chat_tools(MESSAGES, [])
        assert t.calls == 2
        assert result["assistant"]["content"] == "done"

    async def test_transport_errors_are_raised_once_exhausted(self, transport):
        t = transport([httpx.TransportError("x")] * 5)
        with pytest.raises(httpx.TransportError):
            await _client(max_retries=2).chat_tools(MESSAGES, [])
        assert t.calls == 3, "max_retries=2 means three attempts in total"


class TestServerErrors:
    async def test_a_500_is_retried(self, transport):
        t = transport([httpx.Response(500, text="upstream"), httpx.Response(200, json=OK_BODY)])
        await _client().chat_tools(MESSAGES, [])
        assert t.calls == 2

    async def test_a_500_becomes_a_typed_error_once_exhausted(self, transport):
        transport([httpx.Response(503, text="down")] * 5)
        with pytest.raises(LLMServerError) as exc:
            await _client(max_retries=1).chat_tools(MESSAGES, [])
        assert exc.value.status_code == 503


class TestRateLimitAndTierDrop:
    async def test_a_429_drops_the_flex_tier_and_retries(self, transport):
        t = transport([httpx.Response(429, text="slow down"), httpx.Response(200, json=OK_BODY)])
        await _client(service_tier="flex").chat_tools(MESSAGES, [])

        assert t.calls == 2
        assert t.payloads[0].get("service_tier") == "flex"
        assert "service_tier" not in t.payloads[1], \
            "the retry went out on the flex tier again"

    async def test_the_tier_drop_does_not_consume_a_retry_slot(self, transport):
        """The request changed substantially, so it is not a repeat."""
        t = transport([httpx.Response(429, text="a"),   # drops the tier, free
                       httpx.Response(500, text="b"),   # slot 1
                       httpx.Response(500, text="c"),   # slot 2
                       httpx.Response(200, json=OK_BODY)])
        await _client(max_retries=2, service_tier="flex").chat_tools(MESSAGES, [])
        assert t.calls == 4

    async def test_a_429_without_a_tier_is_a_typed_rate_limit(self, transport):
        transport([httpx.Response(429, text="too many")])
        with pytest.raises(LLMRateLimitError) as exc:
            await _client(service_tier=None).chat_tools(MESSAGES, [])
        # EXACT type: LLMQuotaExhaustedError is a subclass, so pytest.raises
        # alone would also pass if every 429 were classified as an exhausted
        # quota - and the whole point of the branch is telling them apart.
        assert type(exc.value) is LLMRateLimitError

    async def test_an_exhausted_quota_is_told_apart_from_a_rate_limit(self, transport):
        """Different remedy: waiting helps for one, never for the other."""
        transport([httpx.Response(429, text="quota exceeded for this project")])
        with pytest.raises(LLMQuotaExhaustedError):
            await _client(service_tier=None).chat_tools(MESSAGES, [])

    async def test_retry_after_is_carried_into_the_error(self, transport):
        transport([httpx.Response(429, text="slow", headers={"retry-after": "42"})])
        with pytest.raises(LLMRateLimitError) as exc:
            await _client(service_tier=None).chat_tools(MESSAGES, [])
        assert type(exc.value) is LLMRateLimitError
        assert exc.value.retry_after == 42.0


class TestBackoffGrows:
    """Exponential backoff against an overloaded provider is the property you
    do not want to lose by accident. Every other test here runs with
    retry_backoff=0.0 for speed, which leaves the formula unexercised - a
    mutation to `backoff = 0` stayed green.
    """

    @pytest.fixture
    def slept(self, monkeypatch):
        values: list[float] = []

        async def fake_sleep(self, seconds, token=None):
            values.append(seconds)

        monkeypatch.setattr(OpenAIResponsesClient, "_cancellable_sleep", fake_sleep)
        return values

    async def test_each_retry_waits_longer_than_the_last(self, transport, slept):
        transport([httpx.Response(500, text="a"), httpx.Response(500, text="b"),
                   httpx.Response(200, json=OK_BODY)])
        await _client(max_retries=2, retry_backoff=1.5).chat_tools(MESSAGES, [])

        assert slept == [1.5, 3.0], "expected retry_backoff * 2**attempt"

    async def test_a_transport_retry_waits_too(self, transport, slept):
        """TWO retries on purpose: at attempt 0 the exponent is 1, so a single
        retry cannot tell `backoff * 2**attempt` from a flat `backoff` - a
        mutation removing the exponent stayed green against one retry."""
        transport([httpx.TimeoutException("x"), httpx.TimeoutException("y"),
                   httpx.Response(200, json=OK_BODY)])
        await _client(max_retries=2, retry_backoff=2.0).chat_tools(MESSAGES, [])

        assert slept == [2.0, 4.0]

    async def test_the_tier_drop_does_not_wait(self, transport, slept):
        """It is not a repeat - the request changed, so there is nothing to
        back off from."""
        transport([httpx.Response(429, text="a"), httpx.Response(200, json=OK_BODY)])
        await _client(service_tier="flex", retry_backoff=1.0).chat_tools(MESSAGES, [])

        assert slept == []


ENC_400 = "invalid_encrypted_content: reasoning item rs_abc is not usable"


class TestTheHealDoesNotUndoTheTierDrop:
    """The branch the live crash came from.

    `_tier_dropped` is WRITTEN in the 429 arm and READ only here, in the
    encrypted-reasoning heal. The heal rebuilds the payload from scratch via
    `_build_payload()`, which puts `service_tier` back - so without the flag
    the healed request would go out on the very flex tier a 429 just told us
    to leave.

    The original defect was the flag living inside the retry loop: a heal
    after a 429 read it before assignment and raised UnboundLocalError. The
    nine tests written first covered every arm EXCEPT this one, so the
    mutation "move the declaration back into the loop" stayed green.
    """

    async def test_a_heal_after_a_tier_drop_stays_on_the_standard_tier(self, transport):
        t = transport([
            httpx.Response(429, text="flex saturated"),   # drops the tier
            httpx.Response(400, text=ENC_400),            # heals, rebuilds payload
            httpx.Response(200, json=OK_BODY),
        ])
        await _client(service_tier="flex").chat_tools(MESSAGES, [])

        assert t.calls == 3
        assert t.payloads[0].get("service_tier") == "flex"
        assert "service_tier" not in t.payloads[1], "the drop was undone by the retry"
        assert "service_tier" not in t.payloads[2],             "the heal rebuilt the payload and put the flex tier back"

    async def test_a_heal_without_a_previous_429_works(self, transport):
        """The literal live crash: the heal reads the flag on the first
        attempt, where no 429 has ever set it."""
        t = transport([httpx.Response(400, text=ENC_400),
                       httpx.Response(200, json=OK_BODY)])
        result = await _client(service_tier="flex").chat_tools(MESSAGES, [])

        assert t.calls == 2
        assert result["assistant"]["content"] == "done"
        assert t.payloads[1].get("service_tier") == "flex",             "no 429 happened, so the tier must survive the heal"

    async def test_the_heal_does_not_consume_a_retry_slot(self, transport):
        t = transport([httpx.Response(400, text=ENC_400),   # heal, free
                       httpx.Response(500, text="a"),       # slot 1
                       httpx.Response(500, text="b"),       # slot 2
                       httpx.Response(200, json=OK_BODY)])
        await _client(max_retries=2, service_tier=None).chat_tools(MESSAGES, [])
        assert t.calls == 4

    async def test_the_heal_happens_only_once(self, transport):
        """A second identical 400 must become a real error, not an endless
        strip-and-retry."""
        transport([httpx.Response(400, text=ENC_400)] * 4)
        with pytest.raises(Exception) as exc:
            await _client(max_retries=1, service_tier=None).chat_tools(MESSAGES, [])
        assert not isinstance(exc.value, RecursionError)


BODY_429 = {"error": {"code": "rate_limit_exceeded", "message": "too many requests"}}


class TestRateLimitInsideAnHttp200:
    """OpenRouter proxies upstream errors as a body error inside a 200.

    Surfacing that as an assistant error would trigger a persistent model
    fallback for a condition that clears in seconds, so this arm backs off and
    retries like the transport-level 429 - including its own one-shot flex
    tier drop.
    """

    async def test_a_body_rate_limit_drops_the_tier_first(self, transport):
        t = transport([httpx.Response(200, json=BODY_429),
                       httpx.Response(200, json=OK_BODY)])
        await _client(service_tier="flex").chat_tools(MESSAGES, [])

        assert t.calls == 2
        assert t.payloads[0].get("service_tier") == "flex"
        assert "service_tier" not in t.payloads[1]

    async def test_without_a_tier_it_backs_off(self, transport, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(self, seconds, token=None):
            slept.append(seconds)

        monkeypatch.setattr(OpenAIResponsesClient, "_cancellable_sleep", fake_sleep)
        transport([httpx.Response(200, json=BODY_429),
                   httpx.Response(200, json=BODY_429),
                   httpx.Response(200, json=OK_BODY)])

        await _client(max_retries=2, retry_backoff=3.0,
                      service_tier=None).chat_tools(MESSAGES, [])

        assert slept == [3.0, 6.0], "body-429 must back off exponentially too"


#: What the gateway answers when a history carries another model's reasoning
#: items. Verbatim from production (2026-08-21) — including the status code,
#: which is 404, not 400.
CROSS_MODEL_BODY = (
    '{"error":{"message":"Your request contains encrypted reasoning or '
    'compaction content that was produced under a different model. Encrypted '
    'payloads can only be replayed to the endpoint that created them."}}')

SAMPLE_OUTPUT = [
    {"type": "reasoning", "id": "rs_abc", "status": "completed",
     "encrypted_content": "BLOB", "format": "openai-responses-api",
     "summary": [{"type": "summary_text", "text": "thinking"}]},
    {"type": "function_call", "id": "fc_1", "call_id": "call_1",
     "name": "get_value", "arguments": "{}"},
]


def _poisoned_messages():
    """A history carrying a foreign model's reasoning item — the shape the
    gateway rejects. Built through the client's own formatter so the block is
    the real thing, not a hand-drawn imitation."""
    assistant = ChatMessage(**_client()._format_response({"output": SAMPLE_OUTPUT})["assistant"])
    return [ChatMessage(role="user", content="hi"), assistant,
            ChatMessage(role="tool", content="r", tool_call_id="call_1")]


def _rejection(status: int, body: str) -> httpx.Response:
    """A 4xx needs its request attached — the client builds an HTTPStatusError
    from `response.request`, and a bare Response has none."""
    return httpx.Response(status, text=body,
                          request=httpx.Request("POST", "https://openrouter.ai/api/v1/responses"))


class TestCrossModelReasoningRejection:
    """The rejection that cost the most: a healable content problem wearing
    the status code of a dead endpoint.

    Measured over five days of production logs: ten of these, every one turned
    into a persistent fallback onto a 4x-priced model, because the heal below
    was gated on status 400 and on wording this body does not use.
    """

    async def test_it_heals_on_the_same_model_instead_of_raising(self, transport):
        msgs = _poisoned_messages()
        assert any(getattr(m, "reasoning_details", None) for m in msgs), \
            "fixture is not poisoned — the test would prove nothing"
        t = transport([_rejection(404, CROSS_MODEL_BODY), httpx.Response(200, json=OK_BODY)])

        result = await _client().chat_tools(msgs, [])

        assert result["assistant"]["content"] == "done"
        assert t.calls == 2, "the healed request was never sent"
        assert not any(i["type"] == "reasoning" for i in t.payloads[1]["input"]), \
            "the retry replayed the very artifact that was rejected"
        assert not any(getattr(m, "reasoning_details", None) for m in msgs), \
            "session still poisoned — the next turn would be rejected again"

    async def test_a_real_missing_model_404_still_reaches_the_fallback_chain(self, transport):
        """The heal is gated on the BODY, not the status: 404 is also how the
        gateway says 'no such model', and that one must keep raising so the
        chain moves on."""
        t = transport([_rejection(404, '{"error":{"message":"No endpoints found for model x"}}')])
        with pytest.raises(httpx.HTTPStatusError):
            await _client().chat_tools(MESSAGES, [])
        assert t.calls == 1

    async def test_the_heal_fires_once_per_request(self, transport):
        """A second rejection means stripping did not help. Raise instead of
        circling on the same body."""
        t = transport([_rejection(404, CROSS_MODEL_BODY), _rejection(404, CROSS_MODEL_BODY)])
        with pytest.raises(httpx.HTTPStatusError):
            await _client().chat_tools(_poisoned_messages(), [])
        assert t.calls == 2


class TestBodyLevelServerError:
    """The gateway proxies its own 5xx as HTTP 200 with an error body — same
    class as a status-code 5xx, delivered in a different envelope. It gets the
    same treatment: retry here, instead of handing the caller an error whose
    only lever is switching the model.

    Production, five days: every body-level error carried code=server_error
    ("stream closed with reason: error", "Upstream idle timeout exceeded",
    "The operation was aborted") — all transient, none of them about the model.
    """

    BODY_SERVER_ERROR = {"error": {"code": "server_error",
                                   "message": "stream closed with reason: error"}}

    async def test_it_is_retried_on_the_same_model(self, transport):
        t = transport([httpx.Response(200, json=self.BODY_SERVER_ERROR),
                       httpx.Response(200, json=OK_BODY)])
        result = await _client().chat_tools(MESSAGES, [])
        assert t.calls == 2
        assert result["assistant"]["content"] == "done"
        assert "error" not in result["assistant"]

    async def test_a_deterministic_refusal_is_not_retried(self, transport):
        """A content filter answers the same way every time — repeating it
        only delays the fallback chain that exists for exactly this case."""
        t = transport([httpx.Response(200, json={"error": {"code": "content_filter",
                                                           "message": "blocked"}}),
                       httpx.Response(200, json=OK_BODY)])
        result = await _client().chat_tools(MESSAGES, [])
        assert t.calls == 1, "the refusal was repeated instead of handed to the chain"
        assert result["assistant"]["error"]["type"] == "upstream_error_content_filter"

    async def test_it_gives_up_after_the_retry_budget(self, transport):
        """Exhausted retries surface as an upstream error — the model switch
        is then the right answer, just not the first one."""
        t = transport([httpx.Response(200, json=self.BODY_SERVER_ERROR)] * 3)
        result = await _client(max_retries=2).chat_tools(MESSAGES, [])
        assert t.calls == 3
        assert result["assistant"]["error"]["type"] == "upstream_error_server_error"

    async def test_it_backs_off_between_attempts(self, transport, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(self, seconds, token=None):
            slept.append(seconds)

        monkeypatch.setattr(OpenAIResponsesClient, "_cancellable_sleep", fake_sleep)
        transport([httpx.Response(200, json=self.BODY_SERVER_ERROR),
                   httpx.Response(200, json=self.BODY_SERVER_ERROR),
                   httpx.Response(200, json=OK_BODY)])
        await _client(max_retries=2, retry_backoff=3.0).chat_tools(MESSAGES, [])
        assert slept == [3.0, 6.0]


class TestReasoningRejectionDetection:
    """The detector's tolerance. Too narrow and a healable rejection escapes
    as a dead endpoint (what happened); too wide and an unrelated 404 gets
    stripped and repeated instead of moving down the chain."""

    @pytest.mark.parametrize("body", [
        "The encrypted content for item rs_x could not be verified",
        '"code": "invalid_encrypted_content"',
        "Corrupted thought signature.",
        "Your request contains encrypted reasoning or compaction content",
        "content that was produced under a different model",
    ])
    def test_known_rejections_are_recognised(self, body):
        assert OpenAIResponsesClient._is_reasoning_artifact_rejection(body)

    @pytest.mark.parametrize("body", [
        "Invalid request: missing field input",
        "No endpoints found for model foo/bar",
        "Insufficient credits",
    ])
    def test_unrelated_errors_are_not(self, body):
        assert not OpenAIResponsesClient._is_reasoning_artifact_rejection(body)
