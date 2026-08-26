"""Tests for GeminiTTSClient (mocked google.genai SDK).

Moved from tests/llm/test_llm_tts.py when the client moved into this
plugin (2026-08-26).
"""
from unittest.mock import MagicMock, patch

import pytest

from agent_system.llm.tts import TTSResult, TTSSpeaker, TTSVoice
from plugins_llm.llm_gemini.gemini_tts_client import (
    GEMINI_TTS_VOICES,
    GeminiTTSClient,
)


# ---------------------------------------------------------------------------
# GeminiTTSClient tests (mocked SDK)
# ---------------------------------------------------------------------------

def _make_mock_response(audio_bytes: bytes):
    """Create a mock Gemini generate_content response with inline audio data."""
    inline_data = MagicMock()
    inline_data.data = audio_bytes

    part = MagicMock()
    part.inline_data = inline_data

    content = MagicMock()
    content.parts = [part]

    candidate = MagicMock()
    candidate.content = content

    response = MagicMock()
    response.candidates = [candidate]
    return response


class TestGeminiTTSClient:
    """Unit tests for GeminiTTSClient with mocked google.genai SDK."""

    @pytest.fixture
    def mock_genai(self):
        """Patch google.genai.Client and return mocks."""
        with patch("plugins_llm.llm_gemini.gemini_tts_client.GeminiTTSClient.__init__", return_value=None):
            client = GeminiTTSClient.__new__(GeminiTTSClient)
            # Manually set attributes that __init__ would set
            client.model = "gemini-2.5-flash-preview-tts"
            client.api_key = "test-key"
            client.request_timeout = 300
            client.max_retries = 3
            client.default_voice = "Kore"
            client._client = MagicMock()
            yield client

    @pytest.mark.asyncio
    async def test_synthesize_single_speaker(self, mock_genai):
        pcm = b"\x00\x00" * 24000  # 1 second of silence
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(pcm)
        )

        result = await mock_genai.synthesize("Hello world!", voice=TTSVoice(name="Puck"))

        assert isinstance(result, TTSResult)
        assert result.audio_data == pcm
        assert result.sample_rate == 24000
        assert result.sample_width == 2
        assert result.channels == 1
        assert result.voice_name == "Puck"
        assert result.model == "gemini-2.5-flash-preview-tts"
        assert result.duration_seconds == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_synthesize_default_voice(self, mock_genai):
        pcm = b"\x00\x00" * 100
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(pcm)
        )

        result = await mock_genai.synthesize("Test default voice")
        assert result.voice_name == "Kore"  # Default voice

    @pytest.mark.asyncio
    async def test_synthesize_multi_speaker(self, mock_genai):
        pcm = b"\x00\x00" * 24000
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(pcm)
        )

        speakers = [
            TTSSpeaker(name="Joe", voice=TTSVoice(name="Kore")),
            TTSSpeaker(name="Jane", voice=TTSVoice(name="Puck")),
        ]

        result = await mock_genai.synthesize_multi_speaker(
            "Joe: Hello!\nJane: Hi there!",
            speakers=speakers,
        )

        assert isinstance(result, TTSResult)
        assert result.audio_data == pcm
        assert "Joe=Kore" in result.voice_name
        assert "Jane=Puck" in result.voice_name

    @pytest.mark.asyncio
    async def test_multi_speaker_max_two(self, mock_genai):
        speakers = [
            TTSSpeaker(name="A", voice=TTSVoice(name="Kore")),
            TTSSpeaker(name="B", voice=TTSVoice(name="Puck")),
            TTSSpeaker(name="C", voice=TTSVoice(name="Zephyr")),
        ]
        with pytest.raises(ValueError, match="at most 2 speakers"):
            await mock_genai.synthesize_multi_speaker("text", speakers=speakers)

    @pytest.mark.asyncio
    async def test_multi_speaker_empty(self, mock_genai):
        with pytest.raises(ValueError, match="At least one speaker"):
            await mock_genai.synthesize_multi_speaker("text", speakers=[])

    @pytest.mark.asyncio
    async def test_empty_response_raises(self, mock_genai):
        """Empty candidates should raise RuntimeError."""
        response = MagicMock()
        response.candidates = []
        mock_genai._client.models.generate_content = MagicMock(return_value=response)

        with pytest.raises(RuntimeError, match="empty response"):
            await mock_genai.synthesize("Hello")

    @pytest.mark.asyncio
    async def test_no_inline_data_raises(self, mock_genai):
        """Missing inline_data should raise RuntimeError."""
        part = MagicMock()
        part.inline_data = None
        content = MagicMock()
        content.parts = [part]
        candidate = MagicMock()
        candidate.content = content
        response = MagicMock()
        response.candidates = [candidate]

        mock_genai._client.models.generate_content = MagicMock(return_value=response)

        with pytest.raises(RuntimeError, match="no inline_data"):
            await mock_genai.synthesize("Hello")

    @pytest.mark.asyncio
    async def test_zero_length_audio_raises(self, mock_genai):
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(b"")
        )
        with pytest.raises(RuntimeError, match="zero-length"):
            await mock_genai.synthesize("Hello")

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, mock_genai):
        """Should retry on ServerError and succeed on second attempt."""
        from google.genai.errors import ServerError

        pcm = b"\x00\x00" * 100
        mock_genai._client.models.generate_content = MagicMock(
            side_effect=[
                ServerError(500, {"error": {"message": "internal"}}, None),
                _make_mock_response(pcm),
            ]
        )
        mock_genai.max_retries = 2

        result = await mock_genai.synthesize("Test retry")
        assert result.audio_data == pcm

    @pytest.mark.asyncio
    async def test_retry_exhausted(self, mock_genai):
        """Should raise after exhausting all retries."""
        from google.genai.errors import ServerError

        mock_genai._client.models.generate_content = MagicMock(
            side_effect=ServerError(503, {"error": {"message": "unavailable"}}, None)
        )
        mock_genai.max_retries = 2

        with pytest.raises(RuntimeError, match="failed after 2 attempts"):
            await mock_genai.synthesize("Test fail")

    @pytest.mark.asyncio
    async def test_non_retryable_error_immediate(self, mock_genai):
        """Non-retryable exceptions should propagate immediately."""
        mock_genai._client.models.generate_content = MagicMock(
            side_effect=ValueError("bad input")
        )
        with pytest.raises(ValueError, match="bad input"):
            await mock_genai.synthesize("Test")



# ---------------------------------------------------------------------------
# Voice list test
# ---------------------------------------------------------------------------

class TestGeminiVoices:
    def test_voice_list_has_30_entries(self):
        assert len(GEMINI_TTS_VOICES) == 30

    def test_common_voices_present(self):
        for name in ["Kore", "Puck", "Zephyr", "Charon", "Fenrir"]:
            assert name in GEMINI_TTS_VOICES


class TestBuildFactory:
    """build_gemini_tts must forward the model entry's default voice —
    review finding: TTSModelConfig.voice used to be silently ignored here."""

    def test_model_entry_voice_reaches_the_client(self):
        from unittest.mock import patch as _patch
        from agent_system.config.models import TTSModelConfig
        from plugins_llm.llm_gemini.gemini_tts_client import build_gemini_tts

        with _patch("google.genai.Client"):
            client = build_gemini_tts(TTSModelConfig(
                provider="gemini_tts", model="gemini-3.1-flash-tts-preview",
                api_key="test-key", voice="Algenib"))
        assert client.default_voice == "Algenib"

    def test_without_entry_voice_the_client_default_applies(self):
        from unittest.mock import patch as _patch
        from agent_system.config.models import TTSModelConfig
        from plugins_llm.llm_gemini.gemini_tts_client import build_gemini_tts

        with _patch("google.genai.Client"):
            client = build_gemini_tts(TTSModelConfig(
                provider="gemini_tts", model="gemini-3.1-flash-tts-preview",
                api_key="test-key"))
        assert client.default_voice == "Kore"
