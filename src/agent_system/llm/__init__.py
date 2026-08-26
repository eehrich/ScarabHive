"""LLM module exports."""

from .models import ChatMessage, LLMClient
from .tts import (
    TTSClient,
    TTSResult,
    TTSVoice,
    TTSSpeaker,
    GeminiTTSClient,
    GEMINI_TTS_VOICES,
    make_tts_client,
    create_tts_from_profile,
)
from .capabilities import (
    ModelCapability,
    ModelCapabilities,
    ensure_model_supports,
    init_capabilities_registry,
    load_capabilities_from_config,
)

__all__ = [
    "ChatMessage",
    "LLMClient",
    # TTS
    "TTSClient",
    "TTSResult",
    "TTSVoice",
    "TTSSpeaker",
    "GeminiTTSClient",
    "GEMINI_TTS_VOICES",
    "make_tts_client",
    "create_tts_from_profile",
    # Capabilities
    "ModelCapability",
    "ModelCapabilities",
    "ensure_model_supports",
    "init_capabilities_registry",
    "load_capabilities_from_config"
]