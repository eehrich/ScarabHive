"""TTS client for the OpenAI-compatible speech API.

The de-facto TTS wire standard: ``POST {base_url}/audio/speech`` with
``{model, input, voice, response_format}``; the response is a raw audio
byte stream. OpenRouter adopted it verbatim (``/api/v1/audio/speech``),
so this one httpx client serves api.openai.com AND openrouter.ai — and
through OpenRouter every TTS model behind it (GPT-4o-Mini-TTS, Gemini
Flash TTS, Voxtral, Qwen voices, ...).

``response_format`` is fixed to ``pcm``: TTSResult carries raw PCM and
writes WAV itself, and PCM avoids an mp3 decode dependency. The sample
rate is parsed from the Content-Type when the gateway sends one
(``audio/pcm;rate=24000;channels=1``), defaulting to OpenAI's 24 kHz /
16-bit / mono.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional, TYPE_CHECKING

import httpx

from agent_system.llm.tts import (
    TTSClient, TTSResult, TTSVoice,
    notify_tts_request, notify_tts_response,
)
from plugins_llm.llm_common.api_keys import resolve_api_key

if TYPE_CHECKING:
    from agent_system.config.models import TTSModelConfig

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
#: OpenAI PCM output contract; used when the response names no rate.
DEFAULT_SAMPLE_RATE = 24000
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OpenAISpeechTTSClient(TTSClient):
    """httpx client for ``/audio/speech`` (OpenAI + OpenRouter)."""

    SAMPLE_WIDTH = 2
    CHANNELS = 1

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        default_voice: Optional[str] = None,
        request_timeout: int = 300,
        max_retries: int = 3,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_voice = default_voice
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self._unsupported_warned = False

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[TTSVoice] = None,
        language: Optional[str] = None,
        system_instruction: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> TTSResult:
        voice_name = (voice.name if voice else None) or self.default_voice
        if not voice_name:
            raise ValueError(
                f"The speech API requires a voice: pass TTSVoice or set "
                f"`voice:` on the TTS model entry (model={self.model})")
        if system_instruction or language or seed is not None:
            # Visible instead of silently dropped: the speech wire has no
            # fields for style prompts, language hints, or seeds — a caller
            # coming from the Gemini path would lose its narrator style
            # prompt WITHOUT this being loud. Warn once per client, not per
            # segment.
            if not self._unsupported_warned:
                self._unsupported_warned = True
                logger.warning(
                    "system_instruction/language/seed have no request field "
                    "on the speech API and are ignored (model=%s).", self.model)

        payload = {
            "model": self.model,
            "input": text,
            "voice": voice_name,
            "response_format": "pcm",
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/audio/speech"

        # Same reason as the Gemini TTS client: TTS calls never pass through
        # the per-agent hook wiring, so without this dispatch every consumer
        # (message_debugger, cost/latency capture) is blind to them — and
        # switching a profile from gemini_tts to this provider used to make
        # the whole audio pipeline invisible in one line.
        started = time.time()
        await notify_tts_request(
            provider="openai_speech", model=self.model, url=url,
            payload={**payload, "input_chars": len(text)})

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                    response = await client.post(url, json=payload, headers=headers)
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}: {response.text[:300]}",
                        request=response.request, response=response)
                elif response.status_code >= 400:
                    raise await self._failed(
                        url, started,
                        httpx.HTTPStatusError(
                            f"Speech API error {response.status_code}: "
                            f"{response.text[:500]}",
                            request=response.request, response=response))
                elif response.status_code >= 300 or not response.content:
                    # Everything below 400 used to count as success, and the
                    # body was never checked: a redirect or an empty 200 went
                    # into the WAV header as "audio" and only turned up as
                    # noise in the finished chapter. The Gemini client has
                    # refused zero-length audio all along.
                    raise await self._failed(
                        url, started,
                        RuntimeError(
                            f"Speech API returned no audio (HTTP "
                            f"{response.status_code}, "
                            f"{len(response.content)} bytes, "
                            f"content-type="
                            f"{response.headers.get('content-type', '?')}) "
                            f"— model={self.model}, url={url}"))
                else:
                    result = self._to_result(response, voice_name)
                    await notify_tts_response(
                        provider="openai_speech", model=self.model, url=url,
                        duration_ms=(time.time() - started) * 1000,
                        audio_seconds=result.duration_seconds,
                        audio_bytes_len=len(result.audio_data))
                    return result
            except httpx.TransportError as e:  # includes TimeoutException
                last_error = e
            if attempt < self.max_retries:
                delay = 2.0 * (attempt + 1)
                logger.warning(
                    "Speech API attempt %d/%d failed (%s) — retrying in %.0fs",
                    attempt + 1, self.max_retries + 1, last_error, delay)
                await notify_tts_response(
                    provider="openai_speech", model=self.model, url=url,
                    duration_ms=(time.time() - started) * 1000,
                    error=f"[RETRY {attempt + 1}/{self.max_retries + 1}] "
                          f"{type(last_error).__name__}: {last_error}",
                    finish_reason="retry")
                await asyncio.sleep(delay)
        raise await self._failed(url, started, last_error)  # type: ignore[arg-type]

    async def _failed(self, url: str, started: float,
                      error: Exception) -> Exception:
        """Tell the hooks about a terminal failure and hand the error back
        to be raised — so no exit from the retry loop leaves the debugger
        with a request that never got a response."""
        await notify_tts_response(
            provider="openai_speech", model=self.model, url=url,
            duration_ms=(time.time() - started) * 1000,
            error=f"{type(error).__name__}: {error}")
        return error

    def _to_result(self, response: httpx.Response, voice_name: str) -> TTSResult:
        sample_rate = DEFAULT_SAMPLE_RATE
        channels = self.CHANNELS
        content_type = response.headers.get("content-type", "")
        rate_match = re.search(r"(?<![a-z])rate=(\d+)", content_type)
        if rate_match:
            sample_rate = int(rate_match.group(1))
        ch_match = re.search(r"channels=(\d+)", content_type)
        if ch_match:
            channels = int(ch_match.group(1))
        return TTSResult(
            audio_data=response.content,
            sample_rate=sample_rate,
            sample_width=self.SAMPLE_WIDTH,
            channels=channels,
            model=self.model,
            voice_name=voice_name,
            metadata={"content_type": content_type},
        )


def build_openai_speech(cfg: "TTSModelConfig") -> TTSClient:
    """Factory for the registry (manifest key ``provides_tts``).

    Key follows the EFFECTIVE endpoint (llm_common.api_keys): this defaults
    to OpenRouter, so a missing OPENROUTER_API_KEY must not fall through to
    OPENAI_API_KEY.
    """
    api_key, effective_url = resolve_api_key(
        cfg.api_key, cfg.base_url,
        default_base_url=DEFAULT_BASE_URL,
        provider="openai_speech")
    return OpenAISpeechTTSClient(
        model=cfg.model,
        api_key=api_key,
        base_url=effective_url,
        default_voice=cfg.voice,
        request_timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
    )
