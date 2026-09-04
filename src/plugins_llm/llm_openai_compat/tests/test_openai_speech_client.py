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


class TestVoiceCloning:
    """OpenRouter's stateless cloning: the sample rides in `input_references`
    as a base64 data URI (plus its transcript) and `voice` stays out."""

    WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20

    @pytest.mark.asyncio
    async def test_reference_goes_out_as_input_references_without_voice(self):
        import base64
        seen = {}

        def handler(url, json, headers):
            seen.update(json=json)
            return _response()

        with _respond(handler):
            result = await _client().synthesize(
                "Hallo", voice=TTSVoice(
                    name="clone:egon", reference_audio=self.WAV,
                    reference_text="Draussen zwitscherte ein Vogel."))

        refs = seen["json"]["input_references"]
        assert refs == [
            {"type": "input_audio", "input_audio": {
                "data": "data:audio/wav;base64,"
                        + base64.b64encode(self.WAV).decode()}},
            {"type": "text", "text": "Draussen zwitscherte ein Vogel."},
        ]
        assert "voice" not in seen["json"]
        assert seen["json"]["input"] == "Hallo"
        assert result.voice_name == "clone:egon"

    @pytest.mark.asyncio
    async def test_reference_without_transcript_sends_only_the_audio_part(self):
        seen = {}

        def handler(url, json, headers):
            seen.update(json=json)
            return _response()

        with _respond(handler):
            await _client(default_voice=None).synthesize(
                "Hallo", voice=TTSVoice(name="c", reference_audio=self.WAV))
        assert [p["type"] for p in seen["json"]["input_references"]] == ["input_audio"]

    @pytest.mark.asyncio
    async def test_oversized_reference_is_refused_before_any_request(self):
        posted = []

        def handler(url, json, headers):
            posted.append(url)
            return _response()

        too_big = b"\x00" * (15 * 1024 * 1024 + 1)
        with _respond(handler), pytest.raises(ValueError, match="15 MiB"):
            await _client().synthesize(
                "x", voice=TTSVoice(name="c", reference_audio=too_big))
        assert posted == []

    def test_mime_follows_the_container_magic(self):
        from plugins_llm.llm_openai_compat.openai_speech_client import _audio_mime
        assert _audio_mime(b"RIFF....WAVE") == "audio/wav"
        assert _audio_mime(b"fLaC....") == "audio/flac"
        assert _audio_mime(b"OggS....") == "audio/ogg"
        assert _audio_mime(b"FORM....AIFF") == "audio/aiff"
        assert _audio_mime(b"....ftypM4A ") == "audio/mp4"
        assert _audio_mime(b"ID3\x04\x00\x00") == "audio/mpeg"
        # MPEG-1 Layer III without / with CRC, MPEG-2.5: the 11 sync bits
        for b1 in (0xFB, 0xFA, 0xE3):
            assert _audio_mime(bytes([0xFF, b1, 0x90, 0])) == "audio/mpeg"

    def test_unknown_container_is_refused_not_labelled_wav(self):
        from plugins_llm.llm_openai_compat.openai_speech_client import _audio_mime
        with pytest.raises(ValueError, match="container"):
            _audio_mime(b"\x00\x01garbage")

    def test_client_declares_the_capability(self):
        assert OpenAISpeechTTSClient.supports_voice_cloning is True

    @pytest.mark.asyncio
    async def test_hook_payload_carries_the_size_not_the_sample(self):
        """3 MB of base64 per segment would otherwise land in the debugger."""
        hooked = {}

        async def fake_request(**kwargs):
            hooked.update(kwargs["payload"])

        with _respond(lambda u, j, h: _response()), \
             patch("plugins_llm.llm_openai_compat.openai_speech_client."
                   "notify_tts_request", fake_request):
            await _client().synthesize(
                "Hallo", voice=TTSVoice(name="c", reference_audio=self.WAV))
        assert "input_references" not in hooked
        assert hooked["reference_bytes"] == len(self.WAV)
        assert hooked["input_chars"] == 5
        assert hooked["voice"] == "c"  # the clone's label, absent on the wire


class TestCheckVoice:
    """check_voice carries every precondition synthesize would trip over, so
    a caller can fail a long run once up front."""

    def test_no_voice_and_no_reference_is_refused(self):
        with pytest.raises(ValueError, match="requires a voice"):
            _client(default_voice=None).check_voice(None)

    def test_a_reference_alone_is_enough(self):
        _client(default_voice=None).check_voice(
            TTSVoice(name="", reference_audio=b"RIFF....WAVE"))

    def test_an_empty_reference_is_refused(self):
        with pytest.raises(ValueError, match="empty"):
            _client().check_voice(TTSVoice(name="c", reference_audio=b""))

    def test_size_and_container_are_checked_here_too(self):
        with pytest.raises(ValueError, match="15 MiB"):
            _client().check_voice(
                TTSVoice(name="c", reference_audio=b"RIFF" + b"\x00" * (15 * 1024 * 1024)))
        with pytest.raises(ValueError, match="container"):
            _client().check_voice(TTSVoice(name="c", reference_audio=b"\x00\x01garbage"))

    @pytest.mark.asyncio
    async def test_an_empty_reference_never_becomes_a_preset_voice(self):
        """b"" is falsy: before, it fell through to the `voice:` branch and
        the clone label went out as a preset voice name."""
        posted = []
        with _respond(lambda u, j, h: posted.append(j) or _response()), \
             pytest.raises(ValueError, match="empty"):
            await _client().synthesize("x", voice=TTSVoice(name="clone:egon", reference_audio=b""))
        assert posted == []

    @pytest.mark.asyncio
    async def test_a_clone_without_label_never_reports_the_preset_default(self):
        hooked = {}

        async def fake_request(**kwargs):
            hooked.update(kwargs["payload"])

        with _respond(lambda u, j, h: _response()), \
             patch("plugins_llm.llm_openai_compat.openai_speech_client."
                   "notify_tts_request", fake_request):
            result = await _client(default_voice="alloy").synthesize(
                "x", voice=TTSVoice(name="", reference_audio=b"RIFF....WAVE"))
        assert result.voice_name == "cloned-voice"
        assert hooked["voice"] == "cloned-voice"


def test_the_speech_api_declares_no_style_prompt():
    """system_instruction/language/seed have no wire field here — a caller
    must be able to ask BEFORE it records them as applied."""
    from agent_system.llm.tts import TTSClient
    assert OpenAISpeechTTSClient.supports_style_prompt is False
    assert TTSClient.supports_style_prompt is True
