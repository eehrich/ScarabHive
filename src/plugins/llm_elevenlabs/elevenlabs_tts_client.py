"""TTS client for ElevenLabs.

``POST {base_url}/v1/text-to-speech/{voice_id}?output_format=pcm_24000`` with
``{text, model_id}``; the body comes back as raw 16-bit mono PCM, which is what
TTSResult carries -- no mp3 decode dependency.

A voice is an id of the account (``TTSVoice.name`` or the model entry's
``voice``). A clone (``TTSVoice.reference_audio``) is an Instant Voice Clone:
the sample is uploaded once (``POST /v1/voices/add``) and the voice id it gets
is kept in a cache file keyed by account and sample, so a narration of a hundred
segments creates one voice, not a hundred -- each one would take a slot of the
account. A cached id the account no longer has (deleted by hand) is dropped
and the clone made again, once.

``eleven_v4`` reads IPA in slashes inside a German line (``/kvɔʁk/``) -- the
same sound every time, where respelled letters came out worse (by ear,
2026-10-03). ``supports_inline_ipa`` tells a caller it may
send such entries of a pronunciation dictionary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import httpx

from agent_system.llm.tls import httpx_verify
from agent_system.llm.tts import (
    TTSClient, TTSResult, TTSVoice,
    notify_tts_request, notify_tts_response,
)
from plugins.llm_common.http_status import RETRYABLE_STATUS

if TYPE_CHECKING:
    from agent_system.config.models import TTSModelConfig

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_v4"
# ponytail: fixed format, every plan has it (pcm_44100 needs Pro); a field on
# TTSModelConfig when a book should get 44.1 kHz.
OUTPUT_FORMAT = "pcm_24000"
SAMPLE_RATE = 24000


def _upload_name(data: bytes) -> tuple[str, str]:
    """(file name, content type) of a sample -- the upload names its container."""
    if data.startswith(b"RIFF"):
        return "reference.wav", "audio/wav"
    if data.startswith(b"fLaC"):
        return "reference.flac", "audio/flac"
    if data.startswith(b"OggS"):
        return "reference.ogg", "audio/ogg"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "reference.mp3", "audio/mpeg"
    raise ValueError(f"Reference audio has an unrecognised container (starts with {data[:4]!r}); "
                     f"use WAV, FLAC, OGG or MP3")


def _clone_cache_file() -> Path:
    from agent_system.paths import data_path
    return data_path("cache", "elevenlabs_clones.json")


class ElevenLabsTTSClient(TTSClient):
    """httpx client for ElevenLabs text to speech, with Instant Voice Clones."""

    supports_voice_cloning = True
    #: No field for a director's note: v4 takes its direction inline, as audio
    #: tags in the text. language/seed are not sent either -- a caller's
    #: narrator prompt would otherwise reach a model that cannot read it.
    supports_style_prompt = False

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        default_voice: Optional[str] = None,
        request_timeout: int = 300,
        max_retries: int = 3,
        clone_cache: Optional[Path] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_voice = default_voice
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.clone_cache = clone_cache
        self.supports_inline_ipa = model.startswith("eleven_v4")

    def check_voice(self, voice: Optional[TTSVoice]) -> None:
        super().check_voice(voice)
        if voice is not None and voice.reference_audio is not None:
            if not voice.reference_audio:
                raise ValueError(f"Reference audio is empty (0 bytes) for voice {voice.name!r}")
            _upload_name(voice.reference_audio)
            return
        if not ((voice.name if voice else None) or self.default_voice):
            raise ValueError(
                f"ElevenLabs needs a voice id of the account: pass TTSVoice(name=<id>), "
                f"a clone, or set `voice:` on the TTS model entry (model={self.model})")

    # -- clones ------------------------------------------------------------

    def _cache_path(self) -> Path:
        return self.clone_cache or _clone_cache_file()

    def _cached(self) -> dict[str, str]:
        try:
            return json.loads(self._cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _store(self, key: str, voice_id: Optional[str]) -> None:
        """Write through a temp file: a run reading it mid-write must not see
        `{}` and clone again (one account slot per clone)."""
        cache = self._cached()
        if voice_id:
            cache[key] = voice_id
        else:
            cache.pop(key, None)
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: a reader holds the file for a moment
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def _cache_key(self, voice: TTSVoice) -> str:
        """Sample AND account: a clone id is only valid in the account that made it."""
        account = hashlib.sha256(self.api_key.encode()).hexdigest()[:12]
        return f"{account}:{hashlib.sha256(voice.reference_audio).hexdigest()}"

    async def _clone_id(self, voice: TTSVoice) -> str:
        """The account's voice id for this sample; uploads it the first time."""
        key = self._cache_key(voice)
        known = self._cached().get(key)
        if known:
            return known
        url = f"{self.base_url}/v1/voices/add"
        file_name, content_type = _upload_name(voice.reference_audio)
        data = {"name": f"{voice.name} ({key.split(':')[1][:8]})",
                "description": "clone made by the elevenlabs TTS client",
                "remove_background_noise": "false"}
        async with httpx.AsyncClient(timeout=self.request_timeout, verify=httpx_verify()) as client:
            response = await client.post(
                url, data=data, headers={"xi-api-key": self.api_key},
                files={"files": (file_name, voice.reference_audio, content_type)})
        if response.status_code >= 400:
            raise RuntimeError(f"ElevenLabs voice clone failed {response.status_code}: {response.text[:400]}")
        voice_id = response.json().get("voice_id")
        if not voice_id:
            raise RuntimeError(f"ElevenLabs voice clone answered without voice_id: {response.text[:300]}")
        self._store(key, voice_id)
        logger.info("ElevenLabs: cloned %r as voice %s", voice.name, voice_id)
        return voice_id

    # -- synthesis ---------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[TTSVoice] = None,
        language: Optional[str] = None,
        system_instruction: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> TTSResult:
        self.check_voice(voice)
        cloned = voice is not None and voice.reference_audio is not None
        for fresh_clone in (False, True):
            voice_id = await self._clone_id(voice) if cloned else (
                (voice.name if voice else None) or self.default_voice)
            try:
                return await self._speak(text, voice_id, voice.name if cloned else voice_id)
            except httpx.HTTPStatusError as exc:
                # Only the account saying the voice is gone (measured: 404 with
                # "voice_not_found") -- any other 404 would clone again per scene
                # and leave a voice slot behind each time.
                gone = exc.response.status_code == 404 and "voice_not_found" in exc.response.text
                if not (cloned and gone and not fresh_clone):
                    raise
                logger.warning("ElevenLabs: cached clone %s is gone -- cloning again", voice_id)
                self._store(self._cache_key(voice), None)
        raise AssertionError("unreachable")

    async def _speak(self, text: str, voice_id: str, label: str) -> TTSResult:
        url = f"{self.base_url}/v1/text-to-speech/{voice_id}?output_format={OUTPUT_FORMAT}"
        payload = {"text": text, "model_id": self.model}
        started = time.time()
        await notify_tts_request(provider="elevenlabs", model=self.model, url=url,
                                 payload={**payload, "voice": label, "input_chars": len(text)})
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.request_timeout, verify=httpx_verify()) as client:
                    response = await client.post(url, json=payload, headers={"xi-api-key": self.api_key})
                if response.status_code in RETRYABLE_STATUS:
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}: {response.text[:300]}",
                        request=response.request, response=response)
                elif response.status_code >= 400:
                    raise await self._failed(url, started, httpx.HTTPStatusError(
                        f"ElevenLabs error {response.status_code}: {response.text[:500]}",
                        request=response.request, response=response))
                elif not response.content:
                    raise await self._failed(url, started, RuntimeError(
                        f"ElevenLabs returned no audio (HTTP {response.status_code}) -- model={self.model}"))
                else:
                    result = TTSResult(audio_data=response.content, sample_rate=SAMPLE_RATE,
                                       sample_width=2, channels=1, model=self.model,
                                       voice_name=label, metadata={"voice_id": voice_id})
                    # No usage: ElevenLabs bills characters and says nothing about it here.
                    await notify_tts_response(
                        provider="elevenlabs", model=self.model, url=url,
                        duration_ms=(time.time() - started) * 1000,
                        audio_seconds=result.duration_seconds,
                        audio_bytes_len=len(result.audio_data))
                    return result
            except httpx.TransportError as exc:
                last_error = exc
            if attempt < self.max_retries:
                delay = 2.0 * (attempt + 1)
                logger.warning("ElevenLabs attempt %d/%d failed (%s) -- retrying in %.0fs",
                               attempt + 1, self.max_retries + 1, last_error, delay)
                await notify_tts_response(
                    provider="elevenlabs", model=self.model, url=url,
                    duration_ms=(time.time() - started) * 1000,
                    error=f"[RETRY {attempt + 1}/{self.max_retries + 1}] "
                          f"{type(last_error).__name__}: {last_error}",
                    finish_reason="retry")
                await asyncio.sleep(delay)
        raise await self._failed(url, started, last_error)  # type: ignore[arg-type]

    async def _failed(self, url: str, started: float, error: Exception) -> Exception:
        await notify_tts_response(
            provider="elevenlabs", model=self.model, url=url,
            duration_ms=(time.time() - started) * 1000,
            error=f"{type(error).__name__}: {error}")
        return error


def build_elevenlabs(cfg: "TTSModelConfig") -> TTSClient:
    """Factory for the registry (manifest key ``provides_tts``)."""
    import os
    api_key = (cfg.api_key or os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("ElevenLabs needs an API key: `api_key:` on the TTS model entry "
                         "or ELEVENLABS_API_KEY in the environment")
    return ElevenLabsTTSClient(
        model=cfg.model or DEFAULT_MODEL,
        api_key=api_key,
        base_url=cfg.base_url or DEFAULT_BASE_URL,
        default_voice=cfg.voice,
        request_timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
    )
