"""
Text-to-Speech (TTS) client abstraction layer — service definition only.

Provides the provider-agnostic interface for TTS generation. Concrete
clients live in TTS provider plugins under src/plugins_llm/ (their
manifests declare `provides_tts`; dispatch goes through
agent_system.llm.registry, same seam as the LLM providers).

Usage:
    from agent_system.llm.tts import create_tts_from_profile, TTSVoice, TTSSpeaker

    client = create_tts_from_profile(config, "gemini-tts")

    # Single speaker
    result = await client.synthesize("Hello world!", voice=TTSVoice(name="Kore"))
    
    # Multi-speaker
    result = await client.synthesize_multi_speaker(
        text='Joe: How are you?\\nJane: Great!',
        speakers=[
            TTSSpeaker(name="Joe", voice=TTSVoice(name="Kore")),
            TTSSpeaker(name="Jane", voice=TTSVoice(name="Puck")),
        ],
    )
    
    # Save to WAV file
    result.save_wav("output.wav")
"""

from __future__ import annotations

import logging
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class TTSVoice:
    """Voice configuration for TTS.
    
    Attributes:
        name: Prebuilt voice name (e.g. "Kore", "Puck", "Zephyr").
              See Gemini docs for full list of 30 voices.
    """
    name: str


@dataclass
class TTSSpeaker:
    """Speaker configuration for multi-speaker TTS.
    
    Attributes:
        name: Speaker identifier (must match names used in transcript text).
        voice: Voice to use for this speaker.
    """
    name: str
    voice: TTSVoice


@dataclass
class TTSResult:
    """Result from TTS generation.
    
    Contains raw PCM audio data and metadata needed to write a WAV file.
    
    Attributes:
        audio_data: Raw PCM audio bytes.
        sample_rate: Sample rate in Hz (default 24000 for Gemini).
        sample_width: Sample width in bytes (default 2 = 16-bit).
        channels: Number of audio channels (default 1 = mono).
        model: Model used for generation.
        voice_name: Primary voice name used.
        metadata: Additional provider-specific metadata.
    """
    audio_data: bytes
    sample_rate: int = 24000
    sample_width: int = 2
    channels: int = 1
    model: str = ""
    voice_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        """Duration of the audio in seconds."""
        if not self.audio_data:
            return 0.0
        bytes_per_sample = self.sample_width * self.channels
        if bytes_per_sample == 0:
            return 0.0
        return len(self.audio_data) / (self.sample_rate * bytes_per_sample)

    def save_wav(self, path: str | Path) -> Path:
        """Save audio data as a WAV file.
        
        Args:
            path: Output file path.
            
        Returns:
            The resolved Path object.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(self.sample_width)
            wf.setframerate(self.sample_rate)
            wf.writeframes(self.audio_data)
        return path


# ---------------------------------------------------------------------------
# Base client
# ---------------------------------------------------------------------------

class TTSClient:
    """Base class for Text-to-Speech clients.
    
    Subclasses must implement ``synthesize`` and optionally
    ``synthesize_multi_speaker``.
    """

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[TTSVoice] = None,
        language: Optional[str] = None,
        system_instruction: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> TTSResult:
        """Generate speech from text with a single speaker.
        
        Args:
            text: The text (or prompt) to convert to speech.
                  Can include style/direction instructions.
            voice: Voice to use. Provider default if None.
            language: Optional language hint (auto-detected by most models).
            system_instruction: Optional system prompt to control speech style,
                language, pacing, etc. Supported by Gemini TTS.
            seed: Optional seed for reproducible output.
            
        Returns:
            TTSResult with raw PCM audio data.
        """
        raise NotImplementedError

    async def synthesize_multi_speaker(
        self,
        text: str,
        *,
        speakers: list[TTSSpeaker],
        language: Optional[str] = None,
    ) -> TTSResult:
        """Generate multi-speaker speech from a transcript.
        
        The transcript text must reference speakers by the names given
        in ``speakers``.  Gemini supports up to 2 speakers.
        
        Args:
            text: Transcript with speaker labels.
            speakers: Speaker → voice mappings (max 2 for Gemini).
            language: Optional language hint.
            
        Returns:
            TTSResult with raw PCM audio data.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_tts_from_profile(
    config: Any,
    tts_profile: str,
) -> TTSClient:
    """Create a TTS client from a named profile in the system config.
    
    Looks up the profile in ``config.llm.tts_profiles``, resolves the
    model reference from ``config.llm.tts_models``, and returns a
    configured client.
    
    Args:
        config: AgentSystemConfig instance.
        tts_profile: Profile name (e.g. "gemini-tts-flash").
        
    Returns:
        Configured TTSClient.
        
    Raises:
        ValueError: If profile or model not found.
    """
    llm_cfg = config.llm_system
    if not llm_cfg:
        raise ValueError("No llm_system configuration found in config.")

    profiles = llm_cfg.tts_profiles
    if tts_profile not in profiles:
        raise ValueError(
            f"TTS profile {tts_profile!r} not found. "
            f"Available: {list(profiles.keys())}"
        )

    profile = profiles[tts_profile]
    model_ref = profile.model_ref

    models = llm_cfg.tts_models
    if model_ref not in models:
        raise ValueError(
            f"TTS model {model_ref!r} (from profile {tts_profile!r}) not found. "
            f"Available: {list(models.keys())}"
        )

    model_cfg = models[model_ref]

    # Late import for the same reason as factory._build_client: the seam
    # stays patchable, and TTS provider plugins load lazily on first use.
    from agent_system.llm import registry

    return registry.build_tts_client(model_cfg)
