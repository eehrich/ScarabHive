"""Google Gemini TTS client (official google-genai SDK).

Moved out of agent_system/llm/tts.py (2026-08-26): provider wire code
lives with its plugin, and this was the last SDK import in core llm/.
Registered for the TTS registry seam via TTS_PROVIDERS below and
`provides_tts = ["gemini_tts"]` in this plugin's manifest.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

from agent_system.llm.tts import TTSClient, TTSResult, TTSSpeaker, TTSVoice

if TYPE_CHECKING:
    from agent_system.config.models import TTSModelConfig

logger = logging.getLogger(__name__)


class GeminiTTSClient(TTSClient):
    """Google Gemini TTS client using the official google-genai SDK.
    
    Supported models:
        - gemini-2.5-flash-preview-tts  (fast, cost-effective)
        - gemini-2.5-pro-preview-tts    (higher quality)
    
    Output format: 24 kHz, 16-bit, mono PCM.
    Context window limit: 32k tokens.
    """

    # Gemini TTS output is always 24kHz 16-bit mono PCM
    SAMPLE_RATE = 24000
    SAMPLE_WIDTH = 2
    CHANNELS = 1
    DEFAULT_VOICE = "Kore"

    def __init__(
        self,
        model: str = "gemini-2.5-flash-preview-tts",
        api_key: Optional[str] = None,
        request_timeout: int = 300,
        max_retries: int = 3,
    ) -> None:
        """Initialise the Gemini TTS client.
        
        Args:
            model: Gemini TTS model name.
            api_key: Google AI API key. Falls back to GEMINI_API_KEY / GOOGLE_API_KEY env vars.
            request_timeout: Request timeout in seconds.
            max_retries: Max retry attempts on transient errors.
        """
        import os

        self.model = model
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "API key required for Gemini TTS. Set api_key parameter, "
                "or GEMINI_API_KEY / GOOGLE_API_KEY environment variable."
            )
        self.request_timeout = request_timeout
        self.max_retries = max_retries

        from google import genai as _genai
        self._client = _genai.Client(api_key=self.api_key)

        logger.info(
            "Initialized GeminiTTSClient model=%s timeout=%ss retries=%s",
            self.model, self.request_timeout, self.max_retries,
        )

    # ----- public API --------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[TTSVoice] = None,
        language: Optional[str] = None,
        system_instruction: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> TTSResult:
        """Single-speaker TTS via Gemini."""
        from google.genai import types

        voice_name = voice.name if voice else self.DEFAULT_VOICE

        speech_config = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_name,
                )
            )
        )

        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=speech_config,
            seed=seed,
        )

        # Gemini TTS models don't support system_instruction in config
        # (causes 500 INTERNAL). Prepend it to the text as a prompt prefix.
        if system_instruction:
            text = f"{system_instruction}\n\n{text}"

        audio_data = await self._generate(
            text,
            config,
            hook_meta={
                "voice": voice_name,
                "seed": seed,
                "language": language,
                "system_instruction_chars": len(system_instruction) if system_instruction else 0,
                "speakers": "single",
            },
        )

        return TTSResult(
            audio_data=audio_data,
            sample_rate=self.SAMPLE_RATE,
            sample_width=self.SAMPLE_WIDTH,
            channels=self.CHANNELS,
            model=self.model,
            voice_name=voice_name,
        )

    async def synthesize_multi_speaker(
        self,
        text: str,
        *,
        speakers: list[TTSSpeaker],
        language: Optional[str] = None,
    ) -> TTSResult:
        """Multi-speaker TTS via Gemini (max 2 speakers)."""
        from google.genai import types

        if len(speakers) > 2:
            raise ValueError("Gemini TTS supports at most 2 speakers.")
        if not speakers:
            raise ValueError("At least one speaker is required.")

        speaker_voice_configs = [
            types.SpeakerVoiceConfig(
                speaker=sp.name,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=sp.voice.name,
                    )
                ),
            )
            for sp in speakers
        ]

        speech_config = types.SpeechConfig(
            multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                speaker_voice_configs=speaker_voice_configs,
            )
        )

        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=speech_config,
        )

        voice_names = ", ".join(f"{s.name}={s.voice.name}" for s in speakers)
        audio_data = await self._generate(
            text,
            config,
            hook_meta={
                "voices": voice_names,
                "language": language,
                "speakers": f"multi:{len(speakers)}",
            },
        )

        return TTSResult(
            audio_data=audio_data,
            sample_rate=self.SAMPLE_RATE,
            sample_width=self.SAMPLE_WIDTH,
            channels=self.CHANNELS,
            model=self.model,
            voice_name=voice_names,
        )

    # ----- internal ----------------------------------------------------------

    async def _generate(
        self,
        text: str,
        config: Any,
        *,
        hook_meta: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """Call the Gemini generate_content API and extract audio bytes.

        Runs the synchronous SDK call in a thread executor to stay async.
        Retries on transient server errors with exponential backoff.

        Emits PRE_LLM_REQUEST / POST_LLM_RESPONSE hook events so the message
        debugger (and any other hook consumer) sees the TTS call.

        Args:
            text: The (possibly system-prompt-prefixed) input text.
            config: GenerateContentConfig passed to the SDK.
            hook_meta: Extra request metadata for the debugger payload —
                voice, seed, language, speakers, etc. Not used by the SDK.
        """
        import asyncio
        import time as _time
        from google.genai.errors import ServerError, APIError

        url = f"google-genai://{self.model}:generate_content"
        # Send full text (not preview) so the debugger has the complete prompt.
        # 7-10 kB per scene is fine for the SQLite debugger DB; the raw audio
        # response is what would blow it up, and we never put that in payloads.
        request_payload: Dict[str, Any] = {
            "model": self.model,
            "contents": text,
            "contents_chars": len(text),
            "modalities": ["AUDIO"],
        }
        if hook_meta:
            request_payload.update(hook_meta)
        await self._notify_tts_pre_request(url, request_payload)

        last_error: Optional[Exception] = None
        _start = _time.time()

        for attempt in range(self.max_retries):
            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self.model,
                    contents=text,
                    config=config,
                )

                # Extract audio bytes from response
                if (
                    not response.candidates
                    or not response.candidates[0].content
                    or not response.candidates[0].content.parts
                ):
                    raise RuntimeError(
                        f"Gemini TTS returned empty response for model={self.model}"
                    )

                part = response.candidates[0].content.parts[0]
                if not hasattr(part, "inline_data") or part.inline_data is None:
                    raise RuntimeError(
                        f"Gemini TTS response has no inline_data (model={self.model})"
                    )

                audio_bytes = part.inline_data.data
                if not audio_bytes:
                    raise RuntimeError("Gemini TTS returned zero-length audio data")

                duration = len(audio_bytes) / (
                    self.SAMPLE_RATE * self.SAMPLE_WIDTH * self.CHANNELS
                )
                logger.info(
                    "Gemini TTS generated %.1fs audio (%d bytes) model=%s",
                    duration, len(audio_bytes), self.model,
                )
                await self._notify_tts_post_response(
                    url=url,
                    duration_ms=(_time.time() - _start) * 1000,
                    audio_seconds=duration,
                    audio_bytes_len=len(audio_bytes),
                )
                return audio_bytes

            except ServerError as exc:
                # 5xx server errors and rate limits (429) are retryable
                last_error = exc
                wait = min(2 ** attempt * 2, 30)
                logger.warning(
                    "Gemini TTS transient error (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1, self.max_retries, exc, wait,
                )
                await self._notify_tts_post_response(
                    url=url,
                    duration_ms=(_time.time() - _start) * 1000,
                    error=f"[RETRY {attempt + 1}/{self.max_retries}] {type(exc).__name__}: {exc}",
                    finish_reason="retry",
                )
                await asyncio.sleep(wait)

            except APIError as exc:
                # 429 rate limit is also retryable
                if getattr(exc, "code", 0) == 429:
                    last_error = exc
                    wait = min(2 ** attempt * 2, 30)
                    logger.warning(
                        "Gemini TTS rate limited (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1, self.max_retries, exc, wait,
                    )
                    await self._notify_tts_post_response(
                        url=url,
                        duration_ms=(_time.time() - _start) * 1000,
                        error=f"[RETRY {attempt + 1}/{self.max_retries}] 429: {exc}",
                        finish_reason="retry",
                    )
                    await asyncio.sleep(wait)
                else:
                    await self._notify_tts_post_response(
                        url=url,
                        duration_ms=(_time.time() - _start) * 1000,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    raise

            except Exception as exc:
                # Non-retryable error
                logger.error("Gemini TTS error: %s", exc)
                await self._notify_tts_post_response(
                    url=url,
                    duration_ms=(_time.time() - _start) * 1000,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

        await self._notify_tts_post_response(
            url=url,
            duration_ms=(_time.time() - _start) * 1000,
            error=f"Failed after {self.max_retries} attempts: {last_error!r}",
        )
        raise RuntimeError(
            f"Gemini TTS failed after {self.max_retries} attempts: {last_error}"
        )

    async def _notify_tts_pre_request(self, url: str, payload: Dict[str, Any]) -> None:
        """Fire PRE_LLM_REQUEST hook so the message debugger sees TTS calls.

        TTS calls bypass the per-agent ``wire_llm_hooks`` path (they're invoked
        directly, not via Agent), so we dispatch to the global hook registry here.
        """
        try:
            import time as _time
            from ..hooks import get_hook_registry, HookContext, HookType
            try:
                from ..mcp.status import current_request_id
                req_id = current_request_id.get('') or ''
            except Exception:
                req_id = ''
            context = HookContext(
                hook_type=HookType.PRE_LLM_REQUEST,
                request_id=req_id,
                session_id='',
                agent=None,
                agent_name='gemini_tts',
                llm_request_payload=payload,
                llm_provider='gemini_tts',
                llm_model=self.model,
                llm_request_url=url,
                llm_is_streaming=False,
                metadata={"timestamp_ms": _time.time() * 1000},
            )
            await get_hook_registry().execute_hooks(HookType.PRE_LLM_REQUEST, context)
        except Exception as e:
            logger.debug("Gemini TTS pre_llm_request hook error: %s", e)

    async def _notify_tts_post_response(
        self,
        *,
        url: str,
        duration_ms: float,
        audio_seconds: Optional[float] = None,
        audio_bytes_len: Optional[int] = None,
        error: Optional[str] = None,
        finish_reason: Optional[str] = None,
    ) -> None:
        """Fire POST_LLM_RESPONSE hook with TTS result or error."""
        try:
            import time as _time
            from ..hooks import get_hook_registry, HookContext, HookType
            try:
                from ..mcp.status import current_request_id
                req_id = current_request_id.get('') or ''
            except Exception:
                req_id = ''
            response_data: Dict[str, Any] = {}
            if audio_seconds is not None:
                response_data["audio_seconds"] = audio_seconds
            if audio_bytes_len is not None:
                response_data["audio_bytes"] = audio_bytes_len
            context = HookContext(
                hook_type=HookType.POST_LLM_RESPONSE,
                request_id=req_id,
                session_id='',
                agent=None,
                agent_name='gemini_tts',
                llm_response_data=response_data or None,
                llm_provider='gemini_tts',
                llm_model=self.model,
                llm_request_url=url,
                llm_duration_ms=duration_ms,
                llm_error=error,
                llm_finish_reason=finish_reason or ("stop" if not error else None),
                llm_is_streaming=False,
                metadata={"timestamp_ms": _time.time() * 1000},
            )
            await get_hook_registry().execute_hooks(HookType.POST_LLM_RESPONSE, context)
        except Exception as e:
            logger.debug("Gemini TTS post_llm_response hook error: %s", e)


# Available Gemini TTS voices for reference / validation
GEMINI_TTS_VOICES: list[str] = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda",
    "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus",
    "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
    "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]


def build_gemini_tts(cfg: "TTSModelConfig") -> TTSClient:
    """Factory for the registry: body of the former make_tts_client branch."""
    return GeminiTTSClient(
        model=cfg.model or "gemini-2.5-flash-preview-tts",
        api_key=cfg.api_key,
        request_timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
    )
