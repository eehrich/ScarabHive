"""Tests for the TTS (Text-to-Speech) client abstraction layer."""
import wave
from unittest.mock import MagicMock, patch

import pytest

from agent_system.llm.tts import (
    GeminiTTSClient,
    TTSResult,
    TTSSpeaker,
    TTSVoice,
    GEMINI_TTS_VOICES,
    make_tts_client,
    create_tts_from_profile,
)


# ---------------------------------------------------------------------------
# TTSResult tests
# ---------------------------------------------------------------------------

class TestTTSResult:
    """Tests for TTSResult data class."""

    def test_duration_seconds(self):
        # 24000 Hz, 2 bytes/sample, 1 channel → 1 second = 48000 bytes
        data = b"\x00" * 48000
        result = TTSResult(audio_data=data, sample_rate=24000, sample_width=2, channels=1)
        assert result.duration_seconds == pytest.approx(1.0)

    def test_duration_empty(self):
        result = TTSResult(audio_data=b"")
        assert result.duration_seconds == 0.0

    def test_save_wav(self, tmp_path):
        # Minimal valid PCM: 100 samples of silence (24kHz mono 16-bit)
        pcm = b"\x00\x00" * 100
        result = TTSResult(audio_data=pcm, sample_rate=24000, sample_width=2, channels=1)

        out = tmp_path / "out.wav"
        returned = result.save_wav(out)

        assert returned == out
        assert out.exists()

        # Verify WAV metadata
        with wave.open(str(out), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 24000
            assert wf.getnframes() == 100

    def test_save_wav_creates_dirs(self, tmp_path):
        pcm = b"\x00\x00" * 10
        result = TTSResult(audio_data=pcm)
        nested = tmp_path / "a" / "b" / "c" / "out.wav"
        result.save_wav(nested)
        assert nested.exists()


# ---------------------------------------------------------------------------
# TTSVoice / TTSSpeaker tests
# ---------------------------------------------------------------------------

class TestVoiceModels:
    def test_voice_creation(self):
        v = TTSVoice(name="Kore")
        assert v.name == "Kore"

    def test_speaker_creation(self):
        sp = TTSSpeaker(name="Jane", voice=TTSVoice(name="Puck"))
        assert sp.name == "Jane"
        assert sp.voice.name == "Puck"


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
        with patch("agent_system.llm.tts.GeminiTTSClient.__init__", return_value=None):
            client = GeminiTTSClient.__new__(GeminiTTSClient)
            # Manually set attributes that __init__ would set
            client.model = "gemini-2.5-flash-preview-tts"
            client.api_key = "test-key"
            client.request_timeout = 300
            client.max_retries = 3
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
# Factory tests
# ---------------------------------------------------------------------------

class TestMakeTTSClient:
    def test_gemini_provider(self):
        with patch("agent_system.llm.tts.GeminiTTSClient") as mock_cls:
            make_tts_client("gemini_tts", model="gemini-2.5-flash-preview-tts", api_key="key")
            mock_cls.assert_called_once_with(
                model="gemini-2.5-flash-preview-tts",
                api_key="key",
                request_timeout=300,
                max_retries=3,
            )

    def test_unknown_provider(self):
        with pytest.raises(ValueError, match="Unsupported TTS provider"):
            make_tts_client("unknown_provider", api_key="key")


class TestCreateTTSFromProfile:
    def _make_config(self):
        """Create a minimal mock config."""
        from agent_system.config.models import TTSModelConfig, TTSProfile

        config = MagicMock()
        config.llm_system.tts_models = {
            "gemini-tts-flash": TTSModelConfig(
                provider="gemini_tts",
                model="gemini-2.5-flash-preview-tts",
                api_key="test-key",
                request_timeout=300,
                max_retries=3,
            ),
        }
        config.llm_system.tts_profiles = {
            "gemini-tts": TTSProfile(
                model_ref="gemini-tts-flash",
                description="Flash TTS",
                default_voice="Kore",
            ),
        }
        return config

    def test_resolve_profile(self):
        config = self._make_config()
        with patch("agent_system.llm.tts.make_tts_client") as mock_factory:
            create_tts_from_profile(config, "gemini-tts")
            mock_factory.assert_called_once_with(
                provider="gemini_tts",
                model="gemini-2.5-flash-preview-tts",
                api_key="test-key",
                request_timeout=300,
                max_retries=3,
            )

    def test_missing_profile(self):
        config = self._make_config()
        with pytest.raises(ValueError, match="not found"):
            create_tts_from_profile(config, "nonexistent")

    def test_missing_model_ref(self):
        config = self._make_config()
        # Profile references a model that doesn't exist
        from agent_system.config.models import TTSProfile
        config.llm_system.tts_profiles = {
            "gemini-tts": TTSProfile(model_ref="gemini-tts-flash", description="Flash TTS", default_voice="Kore"),
            "broken": TTSProfile(model_ref="does-not-exist"),
        }
        with pytest.raises(ValueError, match="does-not-exist.*not found"):
            create_tts_from_profile(config, "broken")


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------

class TestTTSConfigModels:
    def test_tts_model_config_defaults(self):
        from agent_system.config.models import TTSModelConfig
        cfg = TTSModelConfig(model="gemini-2.5-flash-preview-tts")
        assert cfg.provider == "gemini_tts"
        assert cfg.request_timeout == 300
        assert cfg.max_retries == 3
        assert cfg.api_key is None

    def test_tts_profile_defaults(self):
        from agent_system.config.models import TTSProfile
        p = TTSProfile(model_ref="my-model")
        assert p.model_ref == "my-model"
        assert p.default_voice is None
        assert p.description is None

    def test_llm_system_config_tts_fields(self):
        from agent_system.config.models import LLMSystemConfig, TTSModelConfig, TTSProfile
        cfg = LLMSystemConfig(
            tts_models={
                "test": TTSModelConfig(model="gemini-2.5-flash-preview-tts"),
            },
            tts_profiles={
                "test-profile": TTSProfile(model_ref="test", default_voice="Kore"),
            },
            default_tts_profile="test-profile",
        )
        assert "test" in cfg.tts_models
        assert "test-profile" in cfg.tts_profiles
        assert cfg.default_tts_profile == "test-profile"


# ---------------------------------------------------------------------------
# Voice list test
# ---------------------------------------------------------------------------

class TestGeminiVoices:
    def test_voice_list_has_30_entries(self):
        assert len(GEMINI_TTS_VOICES) == 30

    def test_common_voices_present(self):
        for name in ["Kore", "Puck", "Zephyr", "Charon", "Fenrir"]:
            assert name in GEMINI_TTS_VOICES
