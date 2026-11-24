"""OpenRouter image generation client (plain httpx over ``/chat/completions``).

OpenRouter exposes image generation models (Nano Banana 2.1, Gemini Image,
Flux, GPT Image, ...) through ``POST {base_url}/chat/completions`` with
``modalities: ["image", "text"]`` and ``image_config: {aspect_ratio, image_size}``.
Generated images are returned as base64 data URIs in ``choices[0].message.images``
(or content parts) alongside OpenRouter's billed ``usage.cost``.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any, Optional, Sequence, TYPE_CHECKING

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
from plugins.llm_common.api_keys import resolve_api_key
from plugins.llm_common.cancellation import await_call
from plugins.llm_common.http_status import RETRYABLE_STATUS, retry_after_seconds

if TYPE_CHECKING:
    from agent_system.config.models import ImageModelConfig

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
MAX_RESPONSE_IMAGE_BYTES = 50 * 1024 * 1024
MAX_B64_CHARS = ((MAX_RESPONSE_IMAGE_BYTES + 2) // 3) * 4
MAX_RAW_B64_CHARS = MAX_B64_CHARS + MAX_B64_CHARS // 32 + 64
MAX_REFERENCE_IMAGES = 14
#: Strict data-URI header allowlist (matched on the short prefix before the comma only).
_DATA_URI_HEADER_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp|gif));base64$",
    re.IGNORECASE,
)
_RETRYABLE_TRANSPORT = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)


def _err_text(exc: BaseException) -> str:
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _encode_openrouter_content(
    validated_refs: list[tuple[ImageInput, str]],
    prompt: str,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(ref.data).decode('ascii')}"
            },
        }
        for ref, mime in validated_refs
    ]
    content.append({"type": "text", "text": prompt})
    return content


class OpenRouterImageClient(ImageClient):
    """httpx client for OpenRouter ``/chat/completions`` image generation."""

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

        content: Any = (
            await asyncio.to_thread(_encode_openrouter_content, validated_refs, prompt)
            if validated_refs
            else prompt
        )

        wire_payload: dict[str, Any] = {
            "model": self.model,
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": content}],
        }
        image_cfg: dict[str, Any] = {}
        if eff_ratio:
            image_cfg["aspect_ratio"] = eff_ratio
        if eff_size:
            image_cfg["image_size"] = eff_size
        if image_cfg:
            wire_payload["image_config"] = image_cfg
        if seed is not None:
            wire_payload["seed"] = seed

        url = f"{self.base_url}/chat/completions"
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
            "Authorization": f"Bearer {self.api_key}",
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
                    provider="openrouter_image", model=self.model, url=url, payload=hook_payload)
                started = time.perf_counter()
                try:
                    resp = await await_call(
                        asyncio.create_task(client.post(url, headers=headers, json=wire_payload)),
                        cancellation_token,
                    )
                    duration_ms = (time.perf_counter() - started) * 1000.0
                except asyncio.CancelledError:
                    await notify_image_response(
                        provider="openrouter_image", model=self.model, url=url,
                        duration_ms=(time.perf_counter() - started) * 1000.0,
                        error="cancelled",
                    )
                    raise
                except httpx.HTTPError as exc:
                    duration_ms = (time.perf_counter() - started) * 1000.0
                    err = _err_text(exc)
                    last_exc = exc
                    await notify_image_response(
                        provider="openrouter_image", model=self.model, url=url,
                        duration_ms=duration_ms, error=err,
                    )
                    if isinstance(exc, _RETRYABLE_TRANSPORT) and attempt < self.max_retries:
                        await _sleep_cancel(min(2.0 ** attempt, 16.0), cancellation_token)
                        continue
                    hint = (
                        " (increase request_timeout rather than retrying a timed-out generation)"
                        if isinstance(exc, httpx.ReadTimeout)
                        else ""
                    )
                    raise RuntimeError(
                        f"openrouter_image request failed after {attempt + 1} attempt(s): {err}{hint}"
                    ) from exc

                if resp.status_code in RETRYABLE_STATUS:
                    err = f"HTTP {resp.status_code}: {resp.text[:400]}"
                    last_exc = RuntimeError(err)
                    await notify_image_response(
                        provider="openrouter_image", model=self.model, url=url,
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
                        f"openrouter_image failed after {attempt + 1} attempt(s): {err}")

                if resp.status_code < 200 or resp.status_code >= 300:
                    err = f"HTTP {resp.status_code}: {resp.text[:500]}"
                    await notify_image_response(
                        provider="openrouter_image", model=self.model, url=url,
                        duration_ms=duration_ms, error=err,
                    )
                    raise RuntimeError(f"openrouter_image error ({self.model}): {err}")

                try:
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise ValueError(f"expected JSON object, got {type(data).__name__}")
                except ValueError as exc:
                    err = f"Invalid JSON response: {exc}"
                    await notify_image_response(
                        provider="openrouter_image", model=self.model, url=url,
                        duration_ms=duration_ms, error=err,
                    )
                    raise RuntimeError(f"openrouter_image invalid JSON: {exc}") from exc

                usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
                try:
                    images, text, finish_reason = await asyncio.to_thread(_parse_openrouter_response, data)
                except ValueError as exc:
                    await notify_image_response(
                        provider="openrouter_image", model=self.model, url=url,
                        duration_ms=duration_ms, usage=usage, error=str(exc),
                    )
                    raise RuntimeError(f"openrouter_image ({self.model}): {exc}") from exc

                await notify_image_response(
                    provider="openrouter_image",
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
                    metadata={"finish_reason": finish_reason, "provider": "openrouter_image"},
                )

        raise RuntimeError(f"openrouter_image failed: {last_exc}")


def _decode_data_uri(url: str) -> GeneratedImage:
    stripped = url.lstrip()
    if "," not in stripped:
        raise ValueError(
            f"unsupported image URL (expected base64 data:image/(png|jpeg|webp|gif), got {stripped[:40]!r})")
    header, b64_part = stripped.split(",", 1)
    if not _DATA_URI_HEADER_RE.match(header.strip()):
        raise ValueError(
            f"unsupported image URL (expected base64 data:image/(png|jpeg|webp|gif), got {header[:40]!r})")
    if len(b64_part) > MAX_RAW_B64_CHARS:
        raise ValueError(
            f"generated image payload exceeds {MAX_RESPONSE_IMAGE_BYTES // (1024 * 1024)} MiB limit")
    compact = "".join(b64_part.split())
    if not compact or len(compact) > MAX_B64_CHARS:
        raise ValueError(
            f"generated image payload exceeds {MAX_RESPONSE_IMAGE_BYTES // (1024 * 1024)} MiB limit"
            if compact
            else "empty base64 image payload")
    try:
        raw = base64.b64decode(compact, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid base64 image payload: {exc}") from exc
    if len(raw) > MAX_RESPONSE_IMAGE_BYTES:
        raise ValueError(
            f"generated image exceeds {MAX_RESPONSE_IMAGE_BYTES // (1024 * 1024)} MiB limit")
    mime = detect_image_mime(raw)
    w, h = image_dimensions(raw)
    return GeneratedImage(data=raw, mime_type=mime, width=w, height=h)


def _extract_image_url(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    image_url = item.get("image_url")
    if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
        return image_url["url"]
    if isinstance(image_url, str):
        return image_url
    if item.get("type") == "image_url" and isinstance(item.get("url"), str):
        return item["url"]
    return None


def _parse_openrouter_response(
    data: dict[str, Any],
) -> tuple[list[GeneratedImage], str, Optional[str]]:
    if isinstance(data.get("error"), dict):
        msg = data["error"].get("message") or str(data["error"])
        raise ValueError(f"API error: {msg}")

    choices = data.get("choices") or []
    if not choices or not isinstance(choices, list):
        raise ValueError("response contained no choices")

    first = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = first.get("finish_reason")
    message = first.get("message") or {}
    if not isinstance(message, dict):
        raise ValueError("response choice contained no message")

    images: list[GeneratedImage] = []
    texts: list[str] = []

    raw_images = message.get("images")
    if isinstance(raw_images, list):
        for item in raw_images:
            uri = _extract_image_url(item)
            if uri:
                images.append(_decode_data_uri(uri))

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        texts.append(content.strip())
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip():
                texts.append(part["text"].strip())
            else:
                uri = _extract_image_url(part)
                if uri:
                    images.append(_decode_data_uri(uri))

    if not images:
        detail = f" (finish_reason={finish_reason})" if finish_reason else ""
        note = f": {' '.join(texts)[:200]}" if texts else ""
        raise ValueError(f"model returned no images{detail}{note}")

    return images, "\n".join(texts), finish_reason


async def _sleep_cancel(seconds: float, cancellation_token: Any) -> None:
    if not cancellation_token:
        await asyncio.sleep(seconds)
        return
    await await_call(asyncio.create_task(asyncio.sleep(seconds)), cancellation_token)


def build_openrouter_image(
    cfg: "ImageModelConfig",
    ssl_verify: Optional[bool] = None,
) -> OpenRouterImageClient:
    """Factory registered under ``IMAGE_PROVIDERS['openrouter_image']``."""
    api_key, base_url = resolve_api_key(
        cfg.api_key,
        cfg.base_url,
        default_base_url=DEFAULT_BASE_URL,
        provider="openrouter_image",
    )
    return OpenRouterImageClient(
        model=cfg.model,
        api_key=api_key,
        base_url=base_url,
        default_aspect_ratio=cfg.aspect_ratio,
        default_image_size=cfg.image_size,
        request_timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
        ssl_verify=ssl_verify,
    )
