"""Google Gemini native image generation client (plain httpx REST API).

Serves models such as ``gemini-nano-banana-2.1`` and ``gemini-3.1-flash-image``
via ``POST {base_url}/models/{model}:generateContent`` with
``generationConfig.responseModalities`` and ``generationConfig.imageConfig``.
Supports both text-to-image and multi-reference image editing over the same
endpoint.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any, Optional, Sequence, TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from agent_system.llm.image import (
    GeneratedImage,
    ImageClient,
    ImageInput,
    ImageResult,
    detect_image_mime,
    image_dimensions,
    notify_image_request,
    notify_image_response,
)
from agent_system.llm.tls import httpx_verify
from plugins.llm_common.api_keys import _shown
from plugins.llm_common.cancellation import await_call
from plugins.llm_common.http_status import RETRYABLE_STATUS, retry_after_seconds

from .gemini_utils import extract_usage_from_metadata

if TYPE_CHECKING:
    from agent_system.config.models import ImageModelConfig

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
#: Upper bound on one decoded image returned by an API response (50 MiB).
MAX_RESPONSE_IMAGE_BYTES = 50 * 1024 * 1024
#: Exact base64 character ceiling matching MAX_RESPONSE_IMAGE_BYTES.
MAX_B64_CHARS = ((MAX_RESPONSE_IMAGE_BYTES + 2) // 3) * 4
#: Raw string bound allowing line wraps (~1 newline per 64-76 chars) without whitespace bombs.
MAX_RAW_B64_CHARS = MAX_B64_CHARS + MAX_B64_CHARS // 32 + 64
MAX_REFERENCE_IMAGES = 14
#: Transport errors safe to retry on a paid POST (connection never completed).
_RETRYABLE_TRANSPORT = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)


def _err_text(exc: BaseException) -> str:
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _targets_google(base_url: Optional[str]) -> bool:
    """True only when ``base_url`` is unset or uses HTTPS on generativelanguage.googleapis.com."""
    cleaned = (base_url or "").strip()
    if not cleaned:
        return True
    parsed = urlparse(cleaned)
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "generativelanguage.googleapis.com"
    )


def _resolve_gemini_key(api_key: Optional[str], base_url: Optional[str]) -> tuple[str, str]:
    """Key-follows-endpoint: only fall back to GEMINI/GOOGLE env vars for official HTTPS Google endpoint."""
    cleaned_url = (base_url or "").strip()
    effective_url = (cleaned_url or DEFAULT_BASE_URL).rstrip("/")
    explicit = (api_key or "").strip()
    if explicit:
        return explicit, effective_url
    if not _targets_google(cleaned_url):
        raise ValueError(
            f"provider=gemini_image targets {_shown(effective_url)!r}, which is not the official "
            "HTTPS Google Gemini endpoint — set `api_key:` on the image model entry so "
            "GEMINI_API_KEY is not sent over plain HTTP or to another host")
    for env_var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        val = (os.getenv(env_var) or "").strip()
        if val:
            return val, effective_url
    raise ValueError(
        "GEMINI_API_KEY or GOOGLE_API_KEY is required when provider=gemini_image")


def _encode_gemini_parts(
    validated_refs: list[tuple[ImageInput, str]],
    prompt: str,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for ref, mime in validated_refs:
        parts.append({
            "inlineData": {
                "mimeType": mime,
                "data": base64.b64encode(ref.data).decode("ascii"),
            }
        })
    parts.append({"text": prompt})
    return parts


class GeminiImageClient(ImageClient):
    """httpx client for Gemini native image generation (``:generateContent``)."""

    supports_reference_images = True
    max_reference_images = MAX_REFERENCE_IMAGES
    supports_aspect_ratio = True
    supports_image_size = True
    supports_seed = True
    supports_n = False

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        default_aspect_ratio: Optional[str] = None,
        default_image_size: Optional[str] = None,
        request_timeout: int = 180,
        max_retries: int = 3,
        ssl_verify: Optional[bool] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_aspect_ratio = default_aspect_ratio
        self.default_image_size = default_image_size
        self.request_timeout = request_timeout
        self.max_retries = max(0, int(max_retries))
        self._ssl_verify = ssl_verify

    async def generate(
        self,
        prompt: str,
        *,
        reference_images: Optional[Sequence[ImageInput]] = None,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        seed: Optional[int] = None,
        n: int = 1,
        cancellation_token: Any = None,
    ) -> ImageResult:
        validated_refs = self.check_request(
            prompt,
            reference_images=reference_images,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            seed=seed,
            n=n,
        )

        eff_ratio = aspect_ratio or self.default_aspect_ratio
        eff_size = image_size or self.default_image_size

        parts = (
            await asyncio.to_thread(_encode_gemini_parts, validated_refs, prompt)
            if validated_refs
            else [{"text": prompt}]
        )

        gen_config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
        image_cfg: dict[str, Any] = {}
        if eff_ratio:
            image_cfg["aspectRatio"] = eff_ratio
        if eff_size:
            image_cfg["imageSize"] = eff_size
        if image_cfg:
            gen_config["imageConfig"] = image_cfg
        if seed is not None:
            gen_config["seed"] = seed

        wire_payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_config,
        }

        url = f"{self.base_url}/models/{self.model}:generateContent"
        # Metadata-only summary for message_debugger (never log raw base64 images).
        hook_payload = {
            "model": self.model,
            "prompt": prompt,
            "aspect_ratio": eff_ratio,
            "image_size": eff_size,
            "seed": seed,
            "reference_images": [
                {"mime_type": mime, "bytes": len(ref.data), "label": ref.label}
                for ref, mime in validated_refs
            ],
        }

        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(
            connect=15.0,
            read=float(self.request_timeout),
            write=60.0,
            pool=10.0,
        )

        last_exc: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=timeout, verify=httpx_verify(self._ssl_verify)) as client:
            for attempt in range(self.max_retries + 1):
                if cancellation_token and cancellation_token.is_cancelled:
                    raise asyncio.CancelledError("Request cancelled by user")

                await notify_image_request(
                    provider="gemini_image", model=self.model, url=url, payload=hook_payload)
                started = time.perf_counter()
                try:
                    resp = await await_call(
                        asyncio.create_task(client.post(url, headers=headers, json=wire_payload)),
                        cancellation_token,
                    )
                    duration_ms = (time.perf_counter() - started) * 1000.0
                except asyncio.CancelledError:
                    await notify_image_response(
                        provider="gemini_image", model=self.model, url=url,
                        duration_ms=(time.perf_counter() - started) * 1000.0,
                        error="cancelled",
                    )
                    raise
                except httpx.HTTPError as exc:
                    duration_ms = (time.perf_counter() - started) * 1000.0
                    err = _err_text(exc)
                    last_exc = exc
                    await notify_image_response(
                        provider="gemini_image", model=self.model, url=url,
                        duration_ms=duration_ms, error=err,
                    )
                    # Only retry pre-response connection failures; ReadTimeout on a paid POST
                    # could have already generated and billed the image.
                    if isinstance(exc, _RETRYABLE_TRANSPORT) and attempt < self.max_retries:
                        await _sleep_cancel(min(2.0 ** attempt, 16.0), cancellation_token)
                        continue
                    hint = (
                        " (increase request_timeout rather than retrying a timed-out generation)"
                        if isinstance(exc, httpx.ReadTimeout)
                        else ""
                    )
                    raise RuntimeError(
                        f"gemini_image request failed after {attempt + 1} attempt(s): {err}{hint}"
                    ) from exc

                if resp.status_code in RETRYABLE_STATUS:
                    err = f"HTTP {resp.status_code}: {resp.text[:400]}"
                    last_exc = RuntimeError(err)
                    await notify_image_response(
                        provider="gemini_image", model=self.model, url=url,
                        duration_ms=duration_ms, error=err,
                    )
                    if attempt < self.max_retries:
                        wait_s = (
                            retry_after_seconds(resp.headers.get("retry-after"), cap=30.0)
                            or min(2.0 ** attempt, 16.0)
                        )
                        await _sleep_cancel(wait_s, cancellation_token)
                        continue
                    raise RuntimeError(
                        f"gemini_image failed after {attempt + 1} attempt(s): {err}")

                if resp.status_code < 200 or resp.status_code >= 300:
                    err = f"HTTP {resp.status_code}: {resp.text[:500]}"
                    await notify_image_response(
                        provider="gemini_image", model=self.model, url=url,
                        duration_ms=duration_ms, error=err,
                    )
                    raise RuntimeError(f"gemini_image error ({self.model}): {err}")

                try:
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise ValueError(f"expected JSON object, got {type(data).__name__}")
                except ValueError as exc:
                    err = f"Invalid JSON response: {exc}"
                    await notify_image_response(
                        provider="gemini_image", model=self.model, url=url,
                        duration_ms=duration_ms, error=err,
                    )
                    raise RuntimeError(f"gemini_image invalid JSON: {exc}") from exc

                usage = extract_usage_from_metadata(data.get("usageMetadata")) or None
                try:
                    images, text, finish_reason = await asyncio.to_thread(_parse_gemini_response, data)
                except ValueError as exc:
                    await notify_image_response(
                        provider="gemini_image", model=self.model, url=url,
                        duration_ms=duration_ms, usage=usage, error=str(exc),
                    )
                    raise RuntimeError(f"gemini_image ({self.model}): {exc}") from exc

                await notify_image_response(
                    provider="gemini_image",
                    model=self.model,
                    url=url,
                    duration_ms=duration_ms,
                    image_count=len(images),
                    image_bytes_len=sum(len(im.data) for im in images),
                    dimensions=[(im.width, im.height) for im in images],
                    usage=usage,
                    finish_reason=finish_reason,
                )
                return ImageResult(
                    images=images,
                    model=self.model,
                    text=text,
                    usage=usage,
                    metadata={"finish_reason": finish_reason, "provider": "gemini_image"},
                )

        raise RuntimeError(f"gemini_image failed: {last_exc}")


def _decode_b64_image(b64_text: str) -> GeneratedImage:
    if len(b64_text) > MAX_RAW_B64_CHARS:
        raise ValueError(
            f"generated image payload exceeds {MAX_RESPONSE_IMAGE_BYTES // (1024 * 1024)} MiB limit")
    compact = "".join(b64_text.split())
    if not compact or len(compact) > MAX_B64_CHARS:
        raise ValueError(
            f"generated image payload exceeds {MAX_RESPONSE_IMAGE_BYTES // (1024 * 1024)} MiB limit"
            if compact
            else "empty base64 image payload")
    try:
        raw = base64.b64decode(compact, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid base64 in inlineData: {exc}") from exc
    if len(raw) > MAX_RESPONSE_IMAGE_BYTES:
        raise ValueError(
            f"generated image exceeds {MAX_RESPONSE_IMAGE_BYTES // (1024 * 1024)} MiB limit")
    mime = detect_image_mime(raw)
    w, h = image_dimensions(raw)
    return GeneratedImage(data=raw, mime_type=mime, width=w, height=h)


def _parse_gemini_response(
    data: dict[str, Any],
) -> tuple[list[GeneratedImage], str, Optional[str]]:
    """Extract validated images and non-thought text from a Gemini generateContent body."""
    feedback = data.get("promptFeedback") or {}
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise ValueError(f"prompt blocked: {feedback.get('blockReason')}")

    candidates = data.get("candidates") or []
    if not candidates or not isinstance(candidates, list):
        raise ValueError("response contained no candidates")

    first = candidates[0] if isinstance(candidates[0], dict) else {}
    finish_reason = first.get("finishReason")
    content = first.get("content") or {}
    raw_parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(raw_parts, list):
        raise ValueError(f"response contained no content parts (finishReason={finish_reason})")

    images: list[GeneratedImage] = []
    texts: list[str] = []
    for part in raw_parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought") is True:
            continue
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            images.append(_decode_b64_image(str(inline["data"])))
        elif isinstance(part.get("text"), str) and part["text"].strip():
            texts.append(part["text"].strip())

    if not images:
        detail = f" (finishReason={finish_reason})" if finish_reason else ""
        note = f": {' '.join(texts)[:200]}" if texts else ""
        raise ValueError(f"model returned no images{detail}{note}")

    return images, "\n".join(texts), finish_reason


async def _sleep_cancel(seconds: float, cancellation_token: Any) -> None:
    if not cancellation_token:
        await asyncio.sleep(seconds)
        return
    await await_call(asyncio.create_task(asyncio.sleep(seconds)), cancellation_token)


def build_gemini_image(cfg: "ImageModelConfig", ssl_verify: Optional[bool] = None) -> GeminiImageClient:
    """Factory registered under ``IMAGE_PROVIDERS['gemini_image']``."""
    api_key, base_url = _resolve_gemini_key(cfg.api_key, cfg.base_url)
    return GeminiImageClient(
        model=cfg.model,
        api_key=api_key,
        base_url=base_url,
        default_aspect_ratio=cfg.aspect_ratio,
        default_image_size=cfg.image_size,
        request_timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
        ssl_verify=ssl_verify,
    )
