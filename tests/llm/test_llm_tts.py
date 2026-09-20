"""Tests for the TTS (Text-to-Speech) client abstraction layer."""
import wave
from unittest.mock import MagicMock, patch

import pytest

from agent_system.llm.tts import (
    TTSResult,
    TTSSpeaker,
    TTSVoice,
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
# Factory tests
# ---------------------------------------------------------------------------

class TestRegistryDispatch:
    """create_tts_from_profile hands the resolved TTSModelConfig to
    registry.build_tts_client — same seam pattern as the LLM providers."""

    def test_unknown_provider_names_the_known_ones(self):
        from agent_system.llm import registry
        with pytest.raises(registry.ProviderNotFoundError, match="gemini_tts"):
            registry.get_tts_provider("unknown_provider")

    def test_profile_builds_the_real_gemini_client(self):
        from plugins_llm.llm_gemini.gemini_tts_client import GeminiTTSClient
        config = TestCreateTTSFromProfile._make_config(None)
        client = create_tts_from_profile(config, "gemini-tts")
        assert isinstance(client, GeminiTTSClient)
        assert client.model == "gemini-2.5-flash-preview-tts"
        assert client.api_key == "test-key"

    def test_openai_speech_provider_builds_the_httpx_client(self):
        from agent_system.config.models import TTSModelConfig
        from agent_system.llm import registry
        from plugins_llm.llm_openai_compat.openai_speech_client import (
            OpenAISpeechTTSClient,
        )
        client = registry.build_tts_client(TTSModelConfig(
            provider="openai_speech", model="qwen/qwen-audio-3.0-tts-flash",
            api_key="sk-or-test", voice="loongjohn"))
        assert isinstance(client, OpenAISpeechTTSClient)
        assert client.default_voice == "loongjohn"
        assert "openrouter.ai" in client.base_url


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
            ),
        }
        return config

    def test_resolve_profile_passes_the_model_config_to_the_registry(self):
        config = self._make_config()
        from agent_system.llm import registry
        with patch.object(registry, "build_tts_client") as mock_build:
            create_tts_from_profile(config, "gemini-tts")
            (cfg,), _ = mock_build.call_args
            assert cfg is config.llm_system.tts_models["gemini-tts-flash"]

    def test_missing_profile(self):
        config = self._make_config()
        with pytest.raises(ValueError, match="not found"):
            create_tts_from_profile(config, "nonexistent")

    def test_missing_model_ref(self):
        config = self._make_config()
        # Profile references a model that doesn't exist
        from agent_system.config.models import TTSProfile
        config.llm_system.tts_profiles = {
            "gemini-tts": TTSProfile(model_ref="gemini-tts-flash", description="Flash TTS"),
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
        assert p.description is None

    def test_llm_system_config_tts_fields(self):
        from agent_system.config.models import LLMSystemConfig, TTSModelConfig, TTSProfile
        cfg = LLMSystemConfig(
            tts_models={
                "test": TTSModelConfig(model="gemini-2.5-flash-preview-tts"),
            },
            tts_profiles={
                "test-profile": TTSProfile(model_ref="test"),
            },
        )
        assert "test" in cfg.tts_models
        assert "test-profile" in cfg.tts_profiles

    def test_a_tts_profile_pointing_at_no_model_is_refused_at_load(self):
        """Config load is the cheap moment; the first synthesis is not.

        This section used to carry a `default_tts_profile` too, set in llm.yaml and
        read by nothing, with a test that asserted pydantic returned the value it had
        just been handed. That is what made it look like configuration for a year.
        """
        from agent_system.config.models import LLMSystemConfig, TTSProfile
        with pytest.raises(ValueError, match="tts_profiles"):
            LLMSystemConfig(tts_profiles={"a": TTSProfile(model_ref="gone")})


