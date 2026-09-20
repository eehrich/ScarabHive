"""LLM module exports."""

from .models import ChatMessage, LLMClient
from .tts import (
    TTSClient,
    TTSResult,
    TTSVoice,
    TTSSpeaker,
    create_tts_from_profile,
)
from .decisions import create_decisions_from_profile
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
    # TTS (service definition; clients live in plugins_llm via provides_tts)
    "TTSClient",
    "TTSResult",
    "TTSVoice",
    "TTSSpeaker",
    "create_tts_from_profile",
    # Decisions (profile lookup; clients live in plugins_llm via provides_decisions)
    "create_decisions_from_profile",
    # Capabilities
    "ModelCapability",
    "ModelCapabilities",
    "ensure_model_supports",
    "init_capabilities_registry",
    "load_capabilities_from_config"
]