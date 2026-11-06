"""Thought signatures on the native Gemini routes: kept where they came, sent back the way Google documents.

- Only the first functionCall part of a step carries the signature; it used to
  be copied onto every parallel call and replayed on each.
- A text part's signature was never read, so a chat turn lost its reasoning.
- The batch result dropped every signature, and the sync path behind a failed
  batch then sent the open turn's calls unsigned -- a sure 400.

SDK parts are SimpleNamespace or real ``types.Part``, never MagicMock (a
MagicMock answers every attribute, ``thought`` and ``function_call`` included).
"""
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types

from agent_system.llm.batch.models import BatchJob, BatchRequest
from agent_system.llm.models import ChatMessage
from plugins.llm_gemini.gemini_batch import GeminiBatchClient
from plugins.llm_gemini.gemini_client import GeminiClient
from plugins.llm_gemini.gemini_sdk_client import GeminiSDKClient
from plugins.llm_gemini.gemini_signatures import BYPASS, text_signature_details
from plugins.llm_gemini.gemini_utils import convert_openai_messages_to_gemini


def _call(call_id, signature=None):
    call = {"id": call_id, "type": "function", "function": {"name": "look", "arguments": "{}"}}
    if signature:
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


def _step(*calls, content=None, details=None):
    return [ChatMessage(role="assistant", content=content, tool_calls=list(calls), reasoning_details=details),
            *[ChatMessage(role="tool", tool_call_id=c["id"], name="look", content="ok") for c in calls]]


def _signatures(contents):
    """thoughtSignature per functionCall part, per model turn."""
    return [[p.get("thoughtSignature") for p in c["parts"] if "functionCall" in p]
            for c in contents if c["role"] == "model"]


class TestReplayOnTheDictConverter:
    """convert_openai_messages_to_gemini: the REST route, and the SDK route builds on it."""

    def test_only_the_first_parallel_call_goes_back_signed(self):
        """Older sessions stored the first signature on every parallel call."""
        messages = [ChatMessage(role="user", content="go"), *_step(_call("a", "S"), _call("b", "S"), _call("c"))]
        _, contents = convert_openai_messages_to_gemini(messages)
        assert _signatures(contents) == [["S", None, None]]

    def test_an_unsigned_step_of_the_current_turn_takes_the_dummy_on_its_first_call(self):
        """Another model made it; the sync path behind a failed batch sent it unsigned."""
        messages = [ChatMessage(role="user", content="go"), *_step(_call("a"), _call("b"))]
        _, contents = convert_openai_messages_to_gemini(messages)
        assert _signatures(contents) == [[BYPASS, None]]

    def test_an_unsigned_step_of_an_earlier_turn_stays_unsigned(self):
        messages = [ChatMessage(role="user", content="go"), *_step(_call("a")),
                    ChatMessage(role="user", content="next")]
        _, contents = convert_openai_messages_to_gemini(messages)
        assert _signatures(contents) == [[None]]

    def test_a_text_signature_goes_back_on_the_text(self):
        details = text_signature_details("TXT")
        messages = [ChatMessage(role="user", content="hi"),
                    ChatMessage(role="assistant", content="hello", reasoning_details=details),
                    ChatMessage(role="user", content="more"),
                    *_step(_call("a", "S"), content="let me look", details=details)]
        _, contents = convert_openai_messages_to_gemini(messages)
        model = [c for c in contents if c["role"] == "model"]
        assert model[0]["parts"] == [{"text": "hello", "thoughtSignature": "TXT"}]
        assert model[1]["parts"][0] == {"text": "let me look", "thoughtSignature": "TXT"}

    def test_another_models_signatures_stay_home(self):
        """A switch between two Gemini models: the first one's signatures are a 400 at the second."""
        details = text_signature_details("TXT")
        messages = [ChatMessage(role="user", content="go"),
                    ChatMessage(role="assistant", content="let me look", reasoning_model="gemini-old",
                                reasoning_details=details, tool_calls=[_call("a", "S")]),
                    ChatMessage(role="tool", tool_call_id="a", name="look", content="ok")]
        _, own = convert_openai_messages_to_gemini(messages, model="gemini-old")
        assert _signatures(own) == [["S"]] and own[1]["parts"][0].get("thoughtSignature") == "TXT", \
            "fixture: the model's own signatures did not go back"
        _, other = convert_openai_messages_to_gemini(messages, model="gemini-new")
        assert _signatures(other) == [[BYPASS]]
        assert "thoughtSignature" not in other[1]["parts"][0]

    def test_a_tool_call_signature_block_is_no_text_signature(self):
        """OpenRouter writes its tool-call signatures in the same block, with the call's id."""
        details = [{"type": "reasoning.encrypted", "format": "google-gemini-v1", "data": "CALLSIG", "id": "a"}]
        messages = [ChatMessage(role="user", content="hi"),
                    ChatMessage(role="assistant", content="hello", reasoning_details=details)]
        _, contents = convert_openai_messages_to_gemini(messages)
        assert contents[-1]["parts"] == [{"text": "hello"}]

    def test_list_content_beside_tool_calls_goes_as_its_text(self):
        """A list as the text of a part fails the SDK's validation."""
        messages = [ChatMessage(role="user", content="go"),
                    *_step(_call("a", "S"), content=[{"type": "text", "text": "let me look"}])]
        _, contents = convert_openai_messages_to_gemini(messages)
        assert contents[1]["parts"][0] == {"text": "let me look"}


class TestReplayOnTheSdkConverter:
    @staticmethod
    def _parts(messages):
        client = GeminiSDKClient(model="gemini-x", api_key="k", context_window=1000, request_timeout=10)
        _, contents = client._convert_messages_to_sdk(messages)
        return [p for c in contents if c.role == "model" for p in c.parts]

    def test_signatures_become_bytes_and_the_dummy_its_text(self):
        sig = base64.b64encode(b"\x01\x02signed").decode()
        messages = [ChatMessage(role="user", content="go"), *_step(_call("a", sig), _call("b")),
                    *_step(_call("c"), _call("d"))]
        parts = self._parts(messages)
        assert [p.thought_signature for p in parts] == [b"\x01\x02signed", None, BYPASS.encode(), None]

    def test_a_text_signature_goes_back_on_the_text_part(self):
        sig = base64.b64encode(b"text-signed").decode()
        messages = [ChatMessage(role="user", content="hi"),
                    ChatMessage(role="assistant", content="hello", reasoning_details=text_signature_details(sig)),
                    ChatMessage(role="user", content="more")]
        [part] = self._parts(messages)
        assert (part.text, part.thought_signature) == ("hello", b"text-signed")

    def test_the_client_names_its_model_to_the_converter(self):
        sig = base64.b64encode(b"text-signed").decode()
        messages = [ChatMessage(role="user", content="hi"),
                    ChatMessage(role="assistant", content="hello", reasoning_model="gemini-old",
                                reasoning_details=text_signature_details(sig)),
                    ChatMessage(role="user", content="more")]
        [part] = self._parts(messages)
        assert part.thought_signature is None


class TestCaptureOnTheSdkClient:
    @pytest.mark.asyncio
    async def test_each_part_keeps_its_own_signature_and_the_text_its_own(self):
        client = GeminiSDKClient(model="gemini-x", api_key="k", context_window=1000, request_timeout=10)
        client._client = MagicMock()
        call = lambda name: SimpleNamespace(name=name, args={})  # noqa: E731
        parts = [SimpleNamespace(text="let me look", thought=None, function_call=None, thought_signature=b"T"),
                 SimpleNamespace(text=None, thought=None, function_call=call("a"), thought_signature=b"S"),
                 SimpleNamespace(text=None, thought=None, function_call=call("b"), thought_signature=None)]
        client._client.aio.models.generate_content = AsyncMock(return_value=SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason=None)],
            usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1,
                                           total_token_count=2, cached_content_token_count=0)))

        result = await client.chat_tools([ChatMessage(role="user", content="go")],
                                         [{"type": "function", "function": {"name": "a", "parameters": {}}}])

        calls = result["assistant"]["tool_calls"]
        assert calls[0]["extra_content"] == {"google": {"thought_signature": base64.b64encode(b"S").decode()}}
        assert "extra_content" not in calls[1]
        assert result["assistant"]["reasoning_details"] == text_signature_details(base64.b64encode(b"T").decode())


class TestCaptureOnTheRestStream:
    @pytest.mark.asyncio
    async def test_an_empty_last_text_part_keeps_its_signature(self):
        """Google: when streaming, the text signature can arrive on an empty part."""
        client = GeminiClient(model="gemini-x", api_key="k", base_url="https://generativelanguage.googleapis.com/v1beta",
                              context_window=1000, request_timeout=10)
        chunks = [{"candidates": [{"content": {"parts": [
                      {"functionCall": {"name": "a", "args": {}}, "thoughtSignature": "S"},
                      {"functionCall": {"name": "b", "args": {}}}]}}]},
                  {"candidates": [{"content": {"parts": [{"text": "", "thoughtSignature": "T"}]}}]}]
        response = MagicMock(status_code=200)

        async def lines():
            for chunk in chunks:
                yield "data: " + json.dumps(chunk)
        response.aiter_lines = lines
        with patch("httpx.AsyncClient") as client_class:
            http = MagicMock()
            http.__aenter__ = AsyncMock(return_value=http)
            http.__aexit__ = AsyncMock(return_value=None)
            stream = MagicMock()
            stream.__aenter__ = AsyncMock(return_value=response)
            stream.__aexit__ = AsyncMock(return_value=None)
            http.stream = MagicMock(return_value=stream)
            client_class.return_value = http
            events = [e async for e in client.chat_tools_streaming(
                [ChatMessage(role="user", content="go")],
                [{"type": "function", "function": {"name": "a", "parameters": {}}}])]

        final = [e for e in events if e["type"] == "final"][0]["assistant"]
        assert [c.get("extra_content") for c in final["tool_calls"]] == [
            {"google": {"thought_signature": "S"}}, None]
        assert final["reasoning_details"] == text_signature_details("T")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("streaming", [True, False])
    async def test_both_requests_name_the_model_to_the_converter(self, streaming):
        client = GeminiClient(model="gemini-x", api_key="k", base_url="https://generativelanguage.googleapis.com/v1beta",
                              context_window=1000, request_timeout=10)

        class _Seen(Exception):
            pass

        with patch("plugins.llm_gemini.gemini_client.prepare_messages_for_gemini", side_effect=_Seen) as prepare:
            with pytest.raises(_Seen):
                if streaming:
                    async for _ in client._stream([ChatMessage(role="user", content="go")], [], None, None, None,
                                                  MagicMock()):
                        pass
                else:
                    await client._request([ChatMessage(role="user", content="go")], [], None, None, None,
                                          MagicMock())
        assert prepare.call_args.kwargs["model"] == "gemini-x"


class TestBatch:
    @staticmethod
    def _client():
        with patch("google.genai.Client"):
            return GeminiBatchClient(api_key="test-key")

    async def test_a_batch_answer_keeps_the_signatures(self):
        client = self._client()
        answer = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
            role="model", parts=[
                types.Part(text="looking", thought_signature=b"T"),
                types.Part(function_call=types.FunctionCall(name="a", args={}), thought_signature=b"S"),
                types.Part(function_call=types.FunctionCall(name="b", args={}))]))])
        client._sdk_client.batches.get = MagicMock(return_value=types.BatchJob(
            dest=types.BatchJobDestination(inlined_responses=[types.InlinedResponse(response=answer)])))
        job = BatchJob(provider_job_id="batches/x", requests=[BatchRequest(custom_id="r1", messages=[], model="m")])

        [result] = await client.get_batch_results(job)

        message = result["response"]["choices"][0]["message"]
        assert [c.get("extra_content") for c in message["tool_calls"]] == [
            {"google": {"thought_signature": base64.b64encode(b"S").decode()}}, None]
        assert message["reasoning_details"] == text_signature_details(base64.b64encode(b"T").decode())

    def test_the_next_batch_request_replays_them_as_documented(self):
        sig = base64.b64encode(b"S").decode()
        details = text_signature_details(base64.b64encode(b"T").decode())
        messages = [m.model_dump(exclude_none=True) for m in [
            ChatMessage(role="user", content="go"), *_step(_call("a", sig), _call("b"), content="looking", details=details),
            ChatMessage(role="user", content="next"), *_step(_call("c"), _call("d"))]]
        contents = self._client()._convert_messages_to_contents(messages)
        model = [c for c in contents if c.role == "model"]
        assert [p.thought_signature for p in model[0].parts] == [b"T", b"S", None]
        assert [p.thought_signature for p in model[1].parts] == [BYPASS.encode(), None]

    def test_a_batch_request_leaves_another_models_signatures_home(self):
        """The queue keeps reasoning_model for the converter; it went without it before."""
        sig = base64.b64encode(b"S").decode()
        step = _step(_call("a", sig), content="looking", details=text_signature_details(sig))
        step[0] = step[0].model_copy(update={"reasoning_model": "gemini-pro"})
        messages = [m.model_dump(exclude_none=True) for m in [ChatMessage(role="user", content="go"), *step]]
        own = [c for c in self._client()._convert_messages_to_contents(messages, "gemini-pro") if c.role == "model"]
        assert [p.thought_signature for p in own[0].parts] == [b"S", b"S"], "fixture: its own went out"
        other = [c for c in self._client()._convert_messages_to_contents(messages, "gemini-flash")
                 if c.role == "model"]
        assert [p.thought_signature for p in other[0].parts] == [None, BYPASS.encode()]

    @pytest.mark.asyncio
    async def test_the_submit_names_the_requests_model(self, monkeypatch):
        client = self._client()
        seen = []
        monkeypatch.setattr(client, "_convert_messages_to_contents", lambda messages, model=None: seen.append(model) or [])
        job = BatchJob(requests=[BatchRequest(custom_id="r", model="gemini-flash",
                                              messages=[{"role": "user", "content": "go"}])])
        try:
            await client.submit_batch(job, None)
        except Exception:  # noqa: BLE001 - only the conversion is looked at
            pass
        assert seen == ["gemini-flash"]
