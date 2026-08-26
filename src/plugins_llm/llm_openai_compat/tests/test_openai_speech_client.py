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


async def _instant_sleep(_delay):
    return None


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
