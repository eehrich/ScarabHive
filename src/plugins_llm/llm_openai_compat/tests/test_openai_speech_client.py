"""Tests for the OpenAI-compatible speech TTS client.

The wire contract under test: POST {base_url}/audio/speech with
{model, input, voice, response_format=pcm}; raw audio bytes come back,
sample rate/channels ride in the Content-Type when the gateway sends one.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.config.models import TTSModelConfig
from agent_system.llm.tts import TTSVoice
from plugins_llm.llm_openai_compat.openai_speech_client import (
    OpenAISpeechTTSClient,
    build_openai_speech,
)


def _client(**kwargs) -> OpenAISpeechTTSClient:
    defaults = dict(model="qwen/qwen-audio-3.0-tts-flash", api_key="sk-or-test",
                    default_voice="loongjohn", max_retries=0)
    defaults.update(kwargs)
    return OpenAISpeechTTSClient(**defaults)


def _respond(handler):
    """Patch httpx.AsyncClient.post with a handler(url, json, headers)."""
    async def fake_post(self, url, json=None, headers=None):
        return handler(url, json, headers)
    return patch.object(httpx.AsyncClient, "post", fake_post)


def _response(status=200, content=b"\x00\x00" * 100,
              content_type="audio/pcm;rate=24000;channels=1"):
    return httpx.Response(
        status_code=status, content=content,
        headers={"content-type": content_type},
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/audio/speech"))


class TestPayload:
    @pytest.mark.asyncio
    async def test_wire_shape_model_input_voice_pcm(self):
        seen = {}

        def handler(url, json, headers):
            seen.update(url=url, json=json, headers=headers)
            return _response()

        with _respond(handler):
            await _client().synthesize("Hallo Welt", voice=TTSVoice(name="Vivian"))

        assert seen["url"] == "https://openrouter.ai/api/v1/audio/speech"
        assert seen["json"] == {
            "model": "qwen/qwen-audio-3.0-tts-flash",
            "input": "Hallo Welt",
            "voice": "Vivian",
            "response_format": "pcm",
        }
        assert seen["headers"]["Authorization"] == "Bearer sk-or-test"

    @pytest.mark.asyncio
    async def test_default_voice_fills_in(self):
        seen = {}

        def handler(url, json, headers):
            seen.update(json=json)
            return _response()

        with _respond(handler):
            result = await _client().synthesize("Text ohne Stimme")
        assert seen["json"]["voice"] == "loongjohn"
        assert result.voice_name == "loongjohn"

    @pytest.mark.asyncio
    async def test_no_voice_anywhere_is_a_loud_error(self):
        with pytest.raises(ValueError, match="requires a voice"):
            await _client(default_voice=None).synthesize("Text")


class TestResponseParsing:
    @pytest.mark.asyncio
    async def test_rate_and_channels_come_from_content_type(self):
        with _respond(lambda *a: _response(
                content_type="audio/pcm;rate=44100;channels=2")):
            result = await _client().synthesize("x")
        assert result.sample_rate == 44100
        assert result.channels == 2

    @pytest.mark.asyncio
    async def test_bitrate_is_not_a_sample_rate(self):
        """Review finding: a plain rate= regex also matched bitrate= and
        would write e.g. 128000 Hz into the WAV header (5x speed)."""
        with _respond(lambda *a: _response(
                content_type="audio/mpeg; bitrate=128000")):
            result = await _client().synthesize("x")
        assert result.sample_rate == 24000

    @pytest.mark.asyncio
    async def test_missing_rate_falls_back_to_openai_default(self):
        with _respond(lambda *a: _response(content_type="audio/pcm")):
            result = await _client().synthesize("x")
        assert result.sample_rate == 24000
        assert result.channels == 1
        assert result.sample_width == 2

    @pytest.mark.asyncio
    async def test_audio_bytes_and_duration(self):
        pcm = b"\x00\x00" * 24000  # 1s at 24kHz mono 16-bit
        with _respond(lambda *a: _response(content=pcm)):
            result = await _client().synthesize("x")
        assert result.audio_data == pcm
        assert result.duration_seconds == pytest.approx(1.0)


class TestErrors:
    @pytest.mark.asyncio
    async def test_client_error_raises_without_retry(self):
        calls = []

        def handler(url, json, headers):
            calls.append(1)
            return _response(status=400, content=b'{"error":"bad voice"}',
                             content_type="application/json")

        with _respond(handler):
            with pytest.raises(httpx.HTTPStatusError, match="400"):
                await _client(max_retries=3).synthesize("x")
        assert len(calls) == 1, "4xx must not be retried"

    @pytest.mark.asyncio
    async def test_server_error_retries_then_succeeds(self, monkeypatch):
        import asyncio
        monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
        responses = [_response(status=503), _response()]

        with _respond(lambda *a: responses.pop(0)):
            result = await _client(max_retries=1).synthesize("x")
        assert result.audio_data

    @pytest.mark.asyncio
    async def test_transport_errors_are_retried(self, monkeypatch):
        """A connect reset is exactly the kind of blip retries exist for —
        and the only path that reaches `raise last_error` at the end."""
        import asyncio
        monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
        calls = []

        def handler(url, json, headers):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("connection reset")
            return _response()

        with _respond(handler):
            result = await _client(max_retries=2).synthesize("x")
        assert result.audio_data
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_exhausted_retries_raise_the_last_error(self, monkeypatch):
        """The end of the loop re-raises what actually went wrong — an
        exception swapped for a generic one here loses the cause."""
        import asyncio
        monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

        def handler(url, json, headers):
            raise httpx.ConnectTimeout("no route")

        with _respond(handler):
            with pytest.raises(httpx.ConnectTimeout, match="no route"):
                await _client(max_retries=1).synthesize("x")

    @pytest.mark.asyncio
    async def test_an_empty_body_is_an_error_not_silent_garbage(self):
        """200 with no bytes used to become a zero-length "audio" result;
        the Gemini client has always refused that."""
        with _respond(lambda *a: _response(content=b"")):
            with pytest.raises(RuntimeError, match="no audio"):
                await _client().synthesize("x")

    @pytest.mark.asyncio
    async def test_a_redirect_is_not_audio(self):
        """Anything below 400 counted as success, so a misconfigured
        base_url could put an HTML body into the WAV header."""
        with _respond(lambda *a: _response(
                status=301, content=b"<html>moved</html>",
                content_type="text/html")):
            with pytest.raises(RuntimeError, match="no audio"):
                await _client().synthesize("x")

    @pytest.mark.asyncio
    async def test_the_request_timeout_reaches_httpx(self, monkeypatch):
        """The client takes request_timeout from the model entry; if it
        never reaches httpx, a hung gateway stalls the scene forever."""
        seen = {}
        real_init = httpx.AsyncClient.__init__

        def spy_init(self, *args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            return real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", spy_init)
        with _respond(lambda *a: _response()):
            await _client(request_timeout=42).synthesize("x")
        assert seen["timeout"] == 42


class TestHookVisibility:
    """The debugger must see this provider too.

    Only the Gemini client dispatched hooks, so the advertised one-line
    profile switch to openai_speech made every TTS call invisible to
    message_debugger and any cost/latency consumer.
    """

    @staticmethod
    def _recorder(seen):
        class _Registry:
            async def execute_hooks(self, hook_type, context):
                seen.append((hook_type, context.llm_provider, context.llm_error))
        return _Registry()

    @pytest.mark.asyncio
    async def test_request_and_response_are_dispatched(self):
        from agent_system.hooks import HookType
        seen = []
        with _respond(lambda *a: _response()):
            with patch("agent_system.hooks.get_hook_registry",
                       return_value=self._recorder(seen)):
                await _client().synthesize("Hallo")
        kinds = [k for k, _, _ in seen]
        assert HookType.PRE_LLM_REQUEST in kinds
        assert HookType.POST_LLM_RESPONSE in kinds
        assert all(p == "openai_speech" for _, p, _ in seen)

    @pytest.mark.asyncio
    async def test_a_failure_is_dispatched_too(self):
        """A request without a response leaves the debugger with a call that
        never ended — the error path has to report as well."""
        from agent_system.hooks import HookType
        seen = []
        with _respond(lambda *a: _response(status=400, content=b"nope",
                                           content_type="application/json")):
            with patch("agent_system.hooks.get_hook_registry",
                       return_value=self._recorder(seen)):
                with pytest.raises(httpx.HTTPStatusError):
                    await _client(max_retries=0).synthesize("Hallo")
        errors = [e for k, _, e in seen
                  if k is HookType.POST_LLM_RESPONSE and e]
        assert errors, "the failed TTS call was never reported to the hooks"


async def _instant_sleep(_delay):
    return None


class TestUnsupportedParams:
    @pytest.mark.asyncio
    async def test_system_instruction_warns_once_not_per_segment(self, caplog):
        """Review finding: the Gemini path sends a 40-line narrator style
        prompt per segment - dropping it must be LOUD (warning), but once
        per client, not once per segment."""
        import logging
        client = _client()
        with _respond(lambda *a: _response()):
            with caplog.at_level(logging.WARNING):
                await client.synthesize("a", system_instruction="Stil: ruhig")
                await client.synthesize("b", system_instruction="Stil: ruhig")
        hits = [r for r in caplog.records
                if "no request field" in r.getMessage()]
        assert len(hits) == 1, f"expected exactly one warning, got {len(hits)}"


class TestKeyFollowsEndpoint:
    """Same rule as openai_responses: the effective URL decides the env var."""

    def test_openrouter_default_wants_the_openrouter_key(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
            build_openai_speech(TTSModelConfig(
                provider="openai_speech", model="m"))

    def test_openai_endpoint_uses_the_openai_key(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
        client = build_openai_speech(TTSModelConfig(
            provider="openai_speech", model="tts-1",
            base_url="https://api.openai.com/v1"))
        assert client.api_key == "sk-openai-secret"

    def test_explicit_key_wins(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = build_openai_speech(TTSModelConfig(
            provider="openai_speech", model="m", api_key="sk-explicit",
            voice="alloy"))
        assert client.api_key == "sk-explicit"
        assert client.default_voice == "alloy"
