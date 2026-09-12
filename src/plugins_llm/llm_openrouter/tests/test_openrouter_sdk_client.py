"""The OpenRouter SDK route, driven through the real SDK.

Nothing here fakes the SDK: every test runs ``chat_tools()`` down through
``openrouter.OpenRouter.responses.send_async`` and stops at an httpx
``MockTransport``. That is the only way these tests can catch what this
plugin exists to detect — a typed request model that stops carrying one of
our fields, or a typed result model that swallows ``usage``.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.cache_key import MARKER_STYLE_ANTHROPIC
from agent_system.llm.models import ChatMessage, LLMServerError
from plugins_llm.llm_openrouter.openrouter_sdk_client import (
    OpenRouterSDKClient,
    build_openrouter_sdk_client,
)

MESSAGES = [ChatMessage(role="user", content="hi")]
#: The shape the agent server actually builds (nested "function").
TOOLS = [{"type": "function",
          "function": {"name": "search", "description": "d",
                       "parameters": {"type": "object", "properties": {}}}}]
#: The tolerated flat shape — valid only once _convert_tools tags it.
FLAT_TOOLS = [{"name": "search", "description": "d",
               "parameters": {"type": "object", "properties": {}}}]

#: A complete /responses body, `usage` in the shape measured live on
#: 2026-09-01 (deepseek-v4-flash via deepinfra, gemini-3.5-flash-lite).
OK_BODY = {
    "id": "resp_1", "object": "response", "created_at": 1, "completed_at": 2,
    "error": None, "frequency_penalty": 0, "incomplete_details": None,
    "instructions": None, "metadata": {}, "model": "openai/gpt-5.6",
    "parallel_tool_calls": True, "presence_penalty": 0, "status": "completed",
    "temperature": 1, "tool_choice": "auto", "tools": [], "top_p": 1,
    "output": [{"type": "message", "id": "msg_1", "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "done",
                             "annotations": []}]}],
    "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
              "input_tokens_details": {"cached_tokens": 64},
              "output_tokens_details": {"reasoning_tokens": 12},
              "cost": 0.00123, "is_byok": False,
              "cost_details": {"upstream_inference_cost": 0.001,
                               "upstream_inference_input_cost": 0.0004,
                               "upstream_inference_output_cost": 0.0006}},
}

#: The same block MINUS the two fields the SDK's UsageCostDetails requires.
#: Not a shape anyone has seen live — see the degeneration test below.
THIN_COST_DETAILS = {"upstream_inference_cost": 0.001}


def _client(**kw) -> OpenRouterSDKClient:
    defaults = dict(model="openai/gpt-5.6", api_key="sk-test",
                    base_url="https://openrouter.ai/api/v1",
                    max_retries=2, retry_backoff=0.0)
    defaults.update(kw)
    return OpenRouterSDKClient(**defaults)


class Route:
    """Scripted MockTransport that records what the SDK really sent."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.script.pop(0) if self.script else httpx.Response(200, json=OK_BODY)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content.decode()) for r in self.requests]


@pytest.fixture
def route(monkeypatch):
    """Replace AsyncClient with a REAL one over a MockTransport.

    Real, not a stub: the SDK calls ``client.send()`` with its own built
    request and reads the response itself, so a hand-rolled fake with a
    ``.post()`` would never exercise the code under test.
    """
    r = Route()
    real = httpx.AsyncClient

    def factory(*_a, **kw):
        kw.pop("verify", None)
        client = real(transport=httpx.MockTransport(r.handler), **kw)
        r.clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(OpenRouterSDKClient, "_cancellable_sleep",
                        lambda self, s, t=None: _noop())
    return r


async def _noop():
    return None


class TestTheRequestTravelsThroughTheSdk:
    @pytest.mark.asyncio
    async def test_it_reaches_the_responses_endpoint(self, route):
        await _client().chat_tools(MESSAGES, TOOLS)
        assert str(route.requests[0].url) == "https://openrouter.ai/api/v1/responses"

    @pytest.mark.asyncio
    async def test_the_key_is_on_the_request(self, route):
        """The SDK owns the auth scheme now, not our ``_headers()``. A key
        that stops travelling fails only against the live gateway."""
        await _client().chat_tools(MESSAGES, TOOLS)
        assert route.requests[0].headers["authorization"] == "Bearer sk-test"

    @pytest.mark.asyncio
    async def test_the_payload_fields_arrive(self, route):
        client = _client(service_tier="flex", thinking_level="medium",
                         provider_routing={"order": ["deepinfra"],
                                           "allow_fallbacks": False},
                         max_tokens=256)
        await client.chat_tools(MESSAGES, TOOLS)
        body = route.bodies[0]
        assert body["model"] == "openai/gpt-5.6"
        assert body["service_tier"] == "flex"
        assert body["reasoning"] == {"effort": "medium"}
        assert body["provider"] == {"order": ["deepinfra"], "allow_fallbacks": False}
        assert body["max_output_tokens"] == 256
        assert body["tools"][0]["name"] == "search"
        assert body["parallel_tool_calls"] is True

    @pytest.mark.asyncio
    async def test_the_sticky_session_and_the_metadata_header_travel(self, route):
        """Both are gateway features the SDK models as a body field and a
        header — the typed signature drops anything it does not know, so
        both need pinning on this route too."""
        client = _client(prompt_cache_key="auto")
        await client.chat_tools(MESSAGES, TOOLS)
        body = route.bodies[0]
        assert body["session_id"] == body["prompt_cache_key"]
        assert route.requests[0].headers["x-openrouter-metadata"] == "enabled"

    @pytest.mark.asyncio
    async def test_the_passthrough_fields_travel(self, route):
        client = _client(plugins=[{"id": "response-healing"}],
                         prompt_cache_options={"mode": "explicit"},
                         safety_identifier="book-42")
        await client.chat_tools(MESSAGES, TOOLS)
        body = route.bodies[0]
        assert body["plugins"] == [{"id": "response-healing"}]
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        assert body["safety_identifier"] == "book-42"

    @pytest.mark.asyncio
    async def test_a_flat_tool_is_accepted_too(self, route):
        """The typed request validator is what found the missing ``type`` on
        the flat tool shape (union_tag_invalid). It must stay found."""
        await _client().chat_tools(MESSAGES, FLAT_TOOLS)
        assert route.bodies[0]["tools"][0]["type"] == "function"

    @pytest.mark.asyncio
    async def test_the_openai_cache_breakpoint_survives_the_typed_request(self, route):
        """The load-bearing claim of this plugin's docstring.

        Our payload builder writes ``prompt_cache_breakpoint`` onto content
        parts. A typed model that does not know the field drops it WITHOUT
        an error, and the only visible symptom would be a cache-hit rate
        quietly falling to zero.
        """
        from agent_system.llm.cache_key import CACHE_BP_SENTINEL

        client = _client(prompt_cache_key="auto")
        messages = [ChatMessage(role="system",
                                content=f"prefix{CACHE_BP_SENTINEL}suffix"),
                    ChatMessage(role="user", content="hi")]
        await client.chat_tools(messages, TOOLS)
        sent = json.dumps(route.bodies[0])
        assert "prompt_cache_breakpoint" in sent, (
            "the cache breakpoint never left the process")
        assert "prompt_cache_key" in route.bodies[0]

    @pytest.mark.asyncio
    async def test_store_is_dropped_from_our_payload_and_set_by_the_sdk(self, route):
        """``store`` has no SDK parameter; the SDK sends it itself. Pinned so
        a future SDK that stops sending it cannot make our requests stateful
        without a red test."""
        await _client().chat_tools(MESSAGES, TOOLS)
        assert route.bodies[0]["store"] is False

    @pytest.mark.asyncio
    async def test_an_ordinary_call_reports_no_losses(self, route, caplog):
        """Counter-check to the warning below — and the reason
        _SDK_HANDLES_ITSELF exists. ``store`` has no SDK parameter either,
        but the SDK sends it; reporting it as "not being sent" would be a
        false alarm on every fresh process."""
        OpenRouterSDKClient._unmapped_reported.clear()
        with caplog.at_level(logging.WARNING):
            await _client(service_tier="flex", thinking_level="medium",
                          prompt_cache_key="auto").chat_tools(MESSAGES, TOOLS)
        assert "no SDK parameter" not in caplog.text

    @pytest.mark.asyncio
    async def test_an_unmapped_payload_field_is_reported(self, route, caplog):
        """Silence is the failure mode: a payload key with no SDK parameter
        simply stops travelling. It must cost a warning."""
        OpenRouterSDKClient._unmapped_reported.clear()
        client = _client()
        original = client._build_payload

        def with_extra(messages, tools):
            payload = original(messages, tools)
            payload["safety_settings"] = [{"category": "x", "threshold": "y"}]
            return payload

        client._build_payload = with_extra
        with caplog.at_level(logging.WARNING):
            await client.chat_tools(MESSAGES, TOOLS)
        assert "safety_settings" in caplog.text
        assert "no SDK parameter" in caplog.text
        assert "safety_settings" not in route.bodies[0]


class TestTheAnswerIsReadFromTheRawBody:
    @pytest.mark.asyncio
    async def test_usage_survives(self, route):
        """The live shape, end to end. Cost accounting prefers the billed
        `cost` over its own estimate, so every field here has a consumer."""
        result = await _client().chat_tools(MESSAGES, TOOLS)
        usage = result["usage"]
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 20
        assert usage["cost"] == 0.00123
        assert usage["prompt_tokens_details"]["cached_tokens"] == 64
        assert usage["completion_tokens_details"]["reasoning_tokens"] == 12

    @pytest.mark.asyncio
    async def test_usage_survives_a_cost_details_the_typed_model_refuses(self, route):
        """Why the raw body is read instead of the typed result.

        `UsageCostDetails` requires upstream_inference_input_cost/_output_cost.
        Without them the sub-model fails, and because `usage` is
        OptionalNullable the WHOLE block degrades to Unset() rather than
        raising — measured against openrouter 1.1.108, though no live
        response has shown that shape. Reading the body sidesteps it.
        """
        body = json.loads(json.dumps(OK_BODY))
        body["usage"]["cost_details"] = THIN_COST_DETAILS
        route.script = [httpx.Response(200, json=body)]
        result = await _client().chat_tools(MESSAGES, TOOLS)
        assert result["usage"]["cost"] == 0.00123
        assert result["usage"]["prompt_tokens"] == 100

    def test_the_typed_model_really_does_lose_it(self):
        """The PREMISE of the decision above, pinned against the SDK itself.

        If a future openrouter release makes `usage` survive a thin
        `cost_details`, this goes red — and the docstring's reasoning has
        expired. A justification nobody re-checks is how stale designs
        survive."""
        from openrouter.components.openresponsesresult import OpenResponsesResult

        body = json.loads(json.dumps(OK_BODY))
        body["usage"]["cost_details"] = THIN_COST_DETAILS
        assert type(OpenResponsesResult.model_validate(body).usage).__name__ == "Unset"
        # Counter-check: with the live shape the very same model keeps it.
        assert OpenResponsesResult.model_validate(OK_BODY).usage.cost == 0.00123

    @pytest.mark.asyncio
    async def test_a_body_the_typed_model_rejects_still_yields_the_answer(self, route):
        """A 200 whose body misses fields the SDK marks required raises
        ResponseValidationError. The answer is in that exception's
        raw_response, and the inherited parser is happy with it."""
        lean = {"output": OK_BODY["output"], "usage": OK_BODY["usage"]}
        route.script = [httpx.Response(200, json=lean)]
        result = await _client().chat_tools(MESSAGES, TOOLS)
        assert result["assistant"]["content"] == "done"
        assert result["usage"]["cost"] == 0.00123

    @pytest.mark.asyncio
    async def test_verbatim_output_items_reach_the_assistant(self, route):
        """The reason the Responses route exists: reasoning items round-trip
        verbatim. They must survive the SDK hop untouched."""
        body = dict(OK_BODY)
        body["output"] = [
            {"type": "reasoning", "id": "rs_1", "summary": [],
             "encrypted_content": "OPAQUE-BLOB", "status": "completed"},
            *OK_BODY["output"],
        ]
        route.script = [httpx.Response(200, json=body)]
        result = await _client().chat_tools(MESSAGES, TOOLS)
        assert "OPAQUE-BLOB" in json.dumps(result["assistant"]["reasoning_details"])


class TestTheInheritedLoopStillOwnsTheRetries:
    @pytest.mark.asyncio
    async def test_a_429_on_flex_drops_the_tier_and_retries(self, route):
        route.script = [httpx.Response(429, json={"error": {"message": "busy"}}),
                        httpx.Response(200, json=OK_BODY)]
        await _client(service_tier="flex").chat_tools(MESSAGES, TOOLS)
        assert route.bodies[0]["service_tier"] == "flex"
        # Not "absent": the SDK parameter defaults to "auto", so dropping the
        # key from our payload sends the standard tier explicitly instead of
        # omitting it. The httpx route omits it. Same effect — off the
        # saturated flex queue — measured difference, pinned here.
        assert route.bodies[1]["service_tier"] == "auto", (
            "the tier drop must reach the SDK request, not just our dict")

    @pytest.mark.asyncio
    async def test_the_capture_hook_is_removed_again(self, route):
        """The httpx client belongs to the inherited loop and is REUSED
        across attempts. A capture hook that is appended and not taken back
        stacks up once per retry on somebody else's object."""
        route.script = [httpx.Response(500, text="x"),
                        httpx.Response(200, json=OK_BODY)]
        await _client().chat_tools(MESSAGES, TOOLS)
        assert len(route.requests) == 2
        assert route.clients[0].event_hooks.get("response") == []

    @pytest.mark.asyncio
    async def test_a_transport_error_is_retried(self, route):
        route.script = [httpx.ConnectError("boom"), httpx.Response(200, json=OK_BODY)]
        result = await _client().chat_tools(MESSAGES, TOOLS)
        assert result["assistant"]["content"] == "done"
        assert len(route.requests) == 2

    @pytest.mark.asyncio
    async def test_a_500_is_retried_then_raised_typed(self, route):
        route.script = [httpx.Response(500, text="upstream down")] * 3
        with pytest.raises(LLMServerError) as exc:
            await _client().chat_tools(MESSAGES, TOOLS)
        assert exc.value.provider == "openrouter_sdk"
        assert len(route.requests) == 3

    @pytest.mark.asyncio
    async def test_the_sdk_does_not_retry_underneath_us(self, route):
        """One attempt per loop iteration. The SDK's own retry layer would
        multiply requests and hide them from the per-attempt hooks."""
        route.script = [httpx.Response(500, text="x")] * 3
        with pytest.raises(LLMServerError):
            await _client(max_retries=0).chat_tools(MESSAGES, TOOLS)
        assert len(route.requests) == 1


class TestItIsDistinguishableFromTheHttpxRoute:
    def test_the_provider_name_differs(self):
        from plugins_llm.llm_openai_compat.openai_responses_client import (
            OpenAIResponsesClient,
        )
        assert OpenRouterSDKClient._PROVIDER == "openrouter_sdk"
        assert OpenAIResponsesClient._PROVIDER == "openai_responses"

    @pytest.mark.asyncio
    async def test_the_hooks_see_the_sdk_route(self, route):
        seen: list[dict] = []
        client = _client()
        client.set_llm_hooks(on_pre_request=lambda i: _record(seen, i))
        await client.chat_tools(MESSAGES, TOOLS)
        assert seen and seen[0]["provider"] == "openrouter_sdk"


async def _record(sink: list, info: dict) -> None:
    sink.append(info)


class TestConstructionHandlesWhatTheSdkCannotSend:
    def test_safety_settings_warn_but_build(self, caplog):
        """Not refused: OpenRouter drops the field on /responses anyway
        (measured 2026-09-01 — an invalid value earns HTTP 200 there and
        HTTP 400 on /chat/completions). Blocking the build would invent a
        difference between the routes that does not exist."""
        with caplog.at_level(logging.WARNING):
            client = build_openrouter_sdk_client(
                model="m", api_key="k", base_url="https://openrouter.ai/api/v1",
                safety_settings={"HARM": "BLOCK_NONE"},
                prompt_cache_marker_style=None)
        assert isinstance(client, OpenRouterSDKClient)
        assert "safety_settings" in caplog.text
        # And the field must not reach the payload builder either, or every
        # request would additionally log an unmapped-field warning.
        assert client.safety_settings is None

    def test_anthropic_cache_markers_are_refused(self):
        with pytest.raises(ValueError, match="cache_control"):
            build_openrouter_sdk_client(
                model="m", api_key="k", base_url="https://openrouter.ai/api/v1",
                safety_settings=None,
                prompt_cache_marker_style=MARKER_STYLE_ANTHROPIC)

    def test_the_ordinary_case_builds(self):
        client = build_openrouter_sdk_client(
            model="m", api_key="k", base_url="https://openrouter.ai/api/v1",
            safety_settings=None, prompt_cache_marker_style=None)
        assert isinstance(client, OpenRouterSDKClient)


class TestTheRegistryFindsIt:
    def test_the_manifest_declares_the_provider(self):
        from agent_system.llm import registry
        registry.reset_for_tests()
        try:
            assert "openrouter_sdk" in registry.known_providers()
            assert registry.default_base_url("openrouter_sdk") == \
                "https://openrouter.ai/api/v1"
        finally:
            registry.reset_for_tests()

    def test_the_factory_is_exported_under_that_name(self):
        from agent_system.llm import registry
        registry.reset_for_tests()
        try:
            assert registry.get_provider("openrouter_sdk") is not None
        finally:
            registry.reset_for_tests()


class TestThisRouteDoesNotStream:
    """The sibling reads the gateway's event stream with httpx directly.

    This class exists to send the request through the SDK instead, so it must
    keep the non-streaming path no matter what the model's capabilities say —
    otherwise the request would travel past the very transport under test and
    the A/B comparison would silently stop comparing.
    """

    def test_streaming_stays_off_even_when_capabilities_allow_it(self):
        client = OpenRouterSDKClient(
            model="deepseek/deepseek-v4-flash", api_key="k",
            base_url="https://openrouter.ai/api/v1",
            capabilities={"streaming": True})

        assert client.supports_streaming() is False

    def test_the_sibling_with_the_same_capabilities_does_stream(self):
        """Guards the comparison itself: if the sibling stopped streaming, the
        assertion above would pass for the wrong reason."""
        from plugins_llm.llm_openai_compat.openai_responses_client import (
            OpenAIResponsesClient,
        )

        sibling = OpenAIResponsesClient(
            model="deepseek/deepseek-v4-flash", api_key="k",
            base_url="https://openrouter.ai/api/v1",
            capabilities={"streaming": True})

        assert sibling.supports_streaming() is True
