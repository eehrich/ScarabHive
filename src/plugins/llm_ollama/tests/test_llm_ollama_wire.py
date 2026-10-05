"""What goes over the wire to /api/chat and what comes back from it.

Real httpx behind a MockTransport, so status handling and body reading are
httpx's own -- the MagicMock tests next door cannot see a lost error body.
Every shape here was first measured against a live Ollama 0.34 (qwen3-vl:8b,
qwen3:4b): `message.thinking` beside the answer, `{"error": ...}` on a 404,
`think` accepted as bool or level string.
"""
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from agent_system.config.models import LLMModelConfig
from agent_system.llm.models import ChatMessage, LLMServerError, MultimodalToolContent
from plugins.llm_ollama.ollama_client import OllamaNativeAsyncClient
from plugins.llm_ollama.provider import build_ollama


def _serve(client, handler, capabilities=("completion", "thinking"), show_status=200):
    """Route the client's /api/chat requests to *handler*; returns their bodies.

    /api/show answers with *capabilities* -- the client asks it once before
    the first request that would send `think`.
    """
    sent = []

    def record(request):
        if request.url.path == "/api/show":
            client.show_calls = getattr(client, "show_calls", 0) + 1
            return httpx.Response(show_status, json={"capabilities": list(capabilities)})
        sent.append(json.loads(request.content))
        return handler(request)

    transport = httpx.MockTransport(record)
    real = httpx.AsyncClient
    client._httpx = SimpleNamespace(
        AsyncClient=lambda **kw: real(transport=transport, **kw),
        HTTPStatusError=httpx.HTTPStatusError,
        RemoteProtocolError=httpx.RemoteProtocolError,
        NetworkError=httpx.NetworkError,
        ConnectError=httpx.ConnectError,
    )
    return sent


def _ndjson(*chunks):
    return httpx.Response(200, content="\n".join(json.dumps(c) for c in chunks).encode())


def _hooks(client):
    seen = []

    async def pre(info):
        seen.append(("pre", info))

    async def post(info):
        seen.append(("post", info))

    client.set_llm_hooks(on_pre_request=pre, on_post_response=post)
    return seen


def _build(**cfg):
    return build_ollama(LLMModelConfig(provider="ollama", model="m", ollama_mode="native", **cfg))


ASK = [ChatMessage(role="user", content="Is 91 prime?")]
NOT_FOUND = httpx.Response(404, json={"error": "model 'm' not found"})


class TestThinkingComesBack:
    @pytest.mark.asyncio
    async def test_a_stream_emits_thinking_and_keeps_it(self):
        client = OllamaNativeAsyncClient(model="m")
        _serve(client, lambda r: _ndjson(
            {"message": {"thinking": "7 times 13"}, "done": False},
            {"message": {"thinking": " is 91"}, "done": False},
            {"message": {"content": "no"}, "done": False},
            {"done": True, "done_reason": "stop", "eval_count": 5}))

        events = [e async for e in client.chat_tools_streaming(ASK, [])]

        assert [e["delta"] for e in events if e["type"] == "thinking_delta"] == ["7 times 13", " is 91"]
        final = events[-1]["assistant"]
        assert final["reasoning_content"] == "7 times 13 is 91"
        assert final["content"] == "no"

    @pytest.mark.asyncio
    async def test_a_blocking_answer_keeps_it(self):
        client = OllamaNativeAsyncClient(model="m")
        _serve(client, lambda r: httpx.Response(200, json={
            "message": {"content": "no", "thinking": "7 times 13"}, "done_reason": "stop"}))

        result = await client.chat_tools(ASK, [])

        assert result["assistant"]["reasoning_content"] == "7 times 13"

    @pytest.mark.parametrize("mode, replayed", [
        ("keep_all", [True, True]), ("keep_last", [False, True]), ("strip", [False, False])])
    def test_the_replay_mode_decides_which_goes_back(self, mode, replayed):
        client = _build(reasoning_details_mode=mode)
        wire = client._map_messages([
            *ASK, ChatMessage(role="assistant", content="a", reasoning_content="first"),
            ChatMessage(role="user", content="and?"),
            ChatMessage(role="assistant", content="b", reasoning_content="second")])

        assert ["thinking" in wire[i] for i in (1, 3)] == replayed

    def test_it_travels_back_as_thinking(self):
        client = OllamaNativeAsyncClient(model="m")
        wire = client._map_messages([
            *ASK, ChatMessage(role="assistant", content="", reasoning_content="7 times 13",
                              tool_calls=[{"id": "c1", "type": "function",
                                           "function": {"name": "f", "arguments": "{}"}}]),
            ChatMessage(role="tool", tool_call_id="c1", name="f", content="ok")])

        assert wire[1]["thinking"] == "7 times 13"
        assert wire[2]["tool_name"] == "f"


class TestTheConfigReachesTheRequest:
    """Through the provider factory: the path a model entry in llm.yaml takes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level, think", [("none", False), ("minimal", "low"), ("high", "high"), ("max", "high")])
    async def test_thinking_level_becomes_think(self, level, think):
        client = _build(thinking_level=level)
        sent = _serve(client, lambda r: httpx.Response(200, json={"message": {"content": "x"}}))

        await client.chat_tools(ASK, [])

        assert sent[0]["think"] == think

    @pytest.mark.asyncio
    async def test_include_thoughts_becomes_think(self):
        client = _build(include_thoughts=True)
        sent = _serve(client, lambda r: httpx.Response(200, json={"message": {"content": "x"}}))

        await client.chat_tools(ASK, [])

        assert sent[0]["think"] is True

    @pytest.mark.asyncio
    async def test_a_model_that_cannot_think_is_not_asked_to(self):
        """Ollama 400s `think: true` for such a model ("does not support thinking")."""
        client = _build(thinking_level="high")
        sent = _serve(client, lambda r: httpx.Response(200, json={"message": {"content": "x"}}),
                      capabilities=("completion",))

        await client.chat_tools(ASK, [])
        await client.chat_tools(ASK, [])

        assert all("think" not in body for body in sent) and len(sent) == 2
        assert client.show_calls == 1

    @pytest.mark.asyncio
    async def test_an_unanswered_question_sends_it_as_configured_once(self):
        client = _build(thinking_level="high")
        sent = _serve(client, lambda r: httpx.Response(200, json={"message": {"content": "x"}}),
                      show_status=500)

        await client.chat_tools(ASK, [])
        await client.chat_tools(ASK, [])

        assert [body["think"] for body in sent] == ["high", "high"]
        assert client.show_calls == 1

    @pytest.mark.asyncio
    async def test_switching_thinking_off_needs_no_question(self):
        client = _build(thinking_level="none")
        sent = _serve(client, lambda r: httpx.Response(200, json={"message": {"content": "x"}}),
                      capabilities=("completion",))

        await client.chat_tools(ASK, [])

        assert sent[0]["think"] is False
        assert not getattr(client, "show_calls", 0)

    @pytest.mark.asyncio
    async def test_without_a_level_the_model_decides(self):
        client = _build()
        sent = _serve(client, lambda r: _ndjson({"done": True}))

        [e async for e in client.chat_tools_streaming(ASK, [])]

        assert "think" not in sent[0]

    @pytest.mark.asyncio
    async def test_max_tokens_becomes_num_predict(self):
        client = _build(max_tokens=15)
        sent = _serve(client, lambda r: _ndjson({"done": True}))

        [e async for e in client.chat_tools_streaming(ASK, [])]

        assert sent[0]["options"]["num_predict"] == 15


class TestOllamasReasonSurvives:
    @pytest.mark.asyncio
    async def test_a_blocking_call_raises_with_it(self):
        client = OllamaNativeAsyncClient(model="m")
        _serve(client, lambda r: NOT_FOUND)

        with pytest.raises(httpx.HTTPStatusError, match="model 'm' not found"):
            await client.chat_tools(ASK, [])

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_the_reason(self):
        client = OllamaNativeAsyncClient(model="m")
        _serve(client, lambda r: httpx.Response(502, text="upstream proxy gave up"))

        with pytest.raises(LLMServerError, match="upstream proxy gave up"):
            await client.chat_tools(ASK, [])

    @pytest.mark.asyncio
    async def test_a_failure_after_the_200_is_an_error_not_an_empty_answer(self):
        client = OllamaNativeAsyncClient(model="m")
        _serve(client, lambda r: _ndjson(
            {"message": {"content": "par"}, "done": False},
            {"error": "llama runner process has terminated"}))

        seen = _hooks(client)

        events = [e async for e in client.chat_tools_streaming(ASK, [])]

        assert "runner process has terminated" in events[-1]["assistant"]["error"]["message"]
        assert "runner process has terminated" in seen[-1][1]["error"]

    @pytest.mark.asyncio
    async def test_a_stream_raises_with_it(self):
        client = OllamaNativeAsyncClient(model="m")
        _serve(client, lambda r: NOT_FOUND)

        with pytest.raises(httpx.HTTPStatusError, match="model 'm' not found"):
            [e async for e in client.chat_tools_streaming(ASK, [])]


class TestTheHooksSeeEveryCall:
    @pytest.mark.asyncio
    async def test_a_blocking_call_shows_its_payload_and_its_end(self):
        client = OllamaNativeAsyncClient(model="m")
        seen = _hooks(client)
        _serve(client, lambda r: httpx.Response(200, json={"message": {"content": "x"}, "done_reason": "stop"}))

        await client.chat(ASK)

        assert seen[0][0] == "pre" and seen[0][1]["payload"]["messages"]
        assert seen[1][0] == "post" and seen[1][1]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_a_stream_shows_its_payload(self):
        client = OllamaNativeAsyncClient(model="m")
        seen = _hooks(client)
        _serve(client, lambda r: _ndjson({"done": True, "done_reason": "stop"}))

        [e async for e in client.chat_tools_streaming(ASK, [])]

        assert seen[0][1]["payload"]["messages"]
        assert seen[-1][1]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("streaming", [False, True])
    async def test_a_failed_call_is_reported_as_one(self, streaming):
        client = OllamaNativeAsyncClient(model="m")
        seen = _hooks(client)
        _serve(client, lambda r: NOT_FOUND)

        with pytest.raises(httpx.HTTPStatusError):
            if streaming:
                [e async for e in client.chat_tools_streaming(ASK, [])]
            else:
                await client.chat_tools(ASK, [])

        assert seen[-1][0] == "post" and "not found" in seen[-1][1]["error"]


def test_a_tools_image_reaches_the_model(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    client = OllamaNativeAsyncClient(
        model="m", capabilities=SimpleNamespace(image_input=True, audio_input=False,
                                                default_api_type=None, developer_role=None))

    wire = client._map_messages([
        *ASK, ChatMessage(role="tool", tool_call_id="c1", name="shot", content="taken",
                          multimodal_content=[MultimodalToolContent(
                              type="image", path=str(png), mime_type="image/png")])])

    assert [m["role"] for m in wire] == ["user", "tool", "user"]
    assert wire[2]["images"] == [base64.b64encode(png.read_bytes()).decode()]


def test_openai_compat_mode_warns_about_an_unwired_key_once(caplog):
    """The delegated openai factory warned a second time, under provider=openai."""
    caplog.set_level("WARNING")
    client = build_ollama(LLMModelConfig(provider="ollama", model="m", reasoning_details_mode="strip"))

    warnings = [r.getMessage() for r in caplog.records if "reasoning_details_mode" in r.getMessage()]
    assert warnings == ["reasoning_details_mode is not wired for provider=ollama and will be ignored (model=m)."]
    assert type(client).__name__ == "OpenAIAsyncClient"
