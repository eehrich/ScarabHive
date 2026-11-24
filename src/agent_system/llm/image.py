"""Image generation client abstraction layer — service definition only.

Provides the provider-agnostic interface for image generation and editing.
Concrete clients live in LLM provider plugins under ``src/plugins/`` (their
manifests declare ``provides_image``; dispatch goes through
``agent_system.llm.registry``, the same seam as LLM, TTS and decision clients).

Usage:
    from agent_system.llm.image import create_image_from_profile, ImageInput

    client = create_image_from_profile(config, "nano-banana")
    result = await client.generate("A pixel-art castle at sunset", aspect_ratio="16:9")
    result.images[0].save("castle.png")
"""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from agent_system.llm import hook_notify
from agent_system.llm.tls import network_ssl_verify

#: Supported raster image formats (MIME -> canonical file extension).
IMAGE_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: File extension -> Pillow save format + MIME type.
_PIL_FORMAT_BY_EXT: dict[str, tuple[str, str]] = {
    ".png": ("PNG", "image/png"),
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".webp": ("WEBP", "image/webp"),
    ".gif": ("GIF", "image/gif"),
}


def detect_image_mime(data: bytes) -> str:
    """Detect raster image MIME type from magic bytes.

    Unknown containers raise ValueError rather than guessing: a mislabelled
    input fails cryptically at the provider, and an untrusted response payload
    must not be written under an image MIME without matching bytes.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    raise ValueError(
        f"Unrecognised image container (starts with {data[:8]!r}); "
        "expected PNG, JPEG, WEBP or GIF")


def image_dimensions(data: bytes) -> tuple[Optional[int], Optional[int]]:
    """Return ``(width, height)`` using Pillow, or ``(None, None)`` if unavailable."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:  # noqa: BLE001 — dimensions are informational
        return None, None


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ImageInput:
    """Reference image passed into an image generation or editing request.

    Attributes:
        data: Raw encoded image bytes (PNG, JPEG, WEBP, GIF).
        mime_type: Optional MIME type; detected from magic bytes when omitted.
        label: Optional human-readable label (e.g. filename) for diagnostics.
    """
    data: bytes
    mime_type: str = ""
    label: Optional[str] = None

    def resolved_mime(self) -> str:
        """Validated MIME type matching the actual bytes."""
        if not self.data:
            raise ValueError(
                f"Reference image {self.label or '<unnamed>'!r} is empty (0 bytes)")
        detected = detect_image_mime(self.data)
        if self.mime_type and self.mime_type.lower() != detected:
            raise ValueError(
                f"Reference image {self.label or '<unnamed>'!r} claims "
                f"{self.mime_type!r} but bytes are {detected!r}")
        return detected


@dataclass
class GeneratedImage:
    """One generated image returned by a provider."""
    data: bytes
    mime_type: str
    width: Optional[int] = None
    height: Optional[int] = None

    def save(self, path: str | Path, *, convert_to_extension: bool = True) -> tuple[Path, str, int]:
        """Atomically write the image to ``path``.

        When ``convert_to_extension`` is True and ``path`` has a known raster
        extension (``.png``, ``.jpg``, ``.jpeg``, ``.webp``, ``.gif``) that
        differs from ``self.mime_type``, Pillow converts the image to match the
        requested extension (converting RGBA/LA/P to RGB for JPEG).

        Returns:
            ``(resolved_path, actual_mime_type, byte_count)``
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        out_bytes = self.data
        out_mime = self.mime_type

        ext = target.suffix.lower()
        desired = _PIL_FORMAT_BY_EXT.get(ext) if convert_to_extension else None
        if desired is not None and desired[1] != self.mime_type:
            pil_format, out_mime = desired
            from PIL import Image

            with Image.open(io.BytesIO(self.data)) as img:
                buf = io.BytesIO()
                if pil_format == "JPEG" and img.mode not in ("RGB", "L", "CMYK"):
                    converted = img.convert("RGB")
                    try:
                        converted.save(buf, format=pil_format)
                    finally:
                        converted.close()
                else:
                    img.save(buf, format=pil_format)
                out_bytes = buf.getvalue()

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(out_bytes)
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return target, out_mime, len(out_bytes)


@dataclass
class ImageResult:
    """Result from an image generation call."""
    images: list[GeneratedImage]
    model: str = ""
    text: str = ""
    usage: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Hook dispatch (shared by every image client)
# ---------------------------------------------------------------------------

async def notify_image_request(
    *,
    provider: str,
    model: str,
    url: str,
    payload: dict[str, Any],
) -> None:
    """Fire PRE_LLM_REQUEST for an image call (metadata-only payload, no raw base64)."""
    await hook_notify.notify_request(
        provider=provider, model=model, url=url, payload=payload)


async def notify_image_response(
    *,
    provider: str,
    model: str,
    url: str,
    duration_ms: float,
    image_count: Optional[int] = None,
    image_bytes_len: Optional[int] = None,
    dimensions: Optional[list[tuple[Optional[int], Optional[int]]]] = None,
    usage: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    finish_reason: Optional[str] = None,
) -> None:
    """Fire POST_LLM_RESPONSE with the image result or error.

    Pass ``usage`` only when the provider actually billed the attempt (never on
    a retry that will be superseded by a later attempt).
    """
    response_data: dict[str, Any] = {}
    if image_count is not None:
        response_data["image_count"] = image_count
    if image_bytes_len is not None:
        response_data["image_bytes"] = image_bytes_len
    if dimensions:
        response_data["dimensions"] = [
            f"{w}x{h}" if w and h else "unknown" for w, h in dimensions
        ]
    await hook_notify.notify_response(
        provider=provider, model=model, url=url, duration_ms=duration_ms,
        response_data=response_data or None, usage=usage, error=error,
        finish_reason=finish_reason)


# ---------------------------------------------------------------------------
# Base client
# ---------------------------------------------------------------------------

class ImageClient:
    """Base class for image generation clients.

    Subclasses set capability flags matching what their wire format actually
    honours and implement ``generate``.
    """

    supports_reference_images: bool = False
    max_reference_images: int = 0
    supports_aspect_ratio: bool = True
    supports_image_size: bool = False
    supports_seed: bool = False
    supports_n: bool = False

    def capabilities_dict(self) -> dict[str, Any]:
        """Structured capability summary for tool discovery / diagnostics."""
        return {
            "reference_images": self.supports_reference_images,
            "max_reference_images": self.max_reference_images,
            "aspect_ratio": self.supports_aspect_ratio,
            "image_size": self.supports_image_size,
            "seed": self.supports_seed,
            "multiple_images": self.supports_n,
        }

    def check_request(
        self,
        prompt: str,
        *,
        reference_images: Optional[Sequence[ImageInput]] = None,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        seed: Optional[int] = None,
        n: int = 1,
    ) -> list[tuple[ImageInput, str]]:
        """Validate request parameters against this client's capabilities.

        Returns validated ``[(ImageInput, mime_type), ...]`` for reference images.
        Raises ``ValueError`` before any network call when a requested feature
        cannot be honoured.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError("n must be a positive integer")
        if n > 1 and not self.supports_n:
            raise ValueError(
                f"{type(self).__name__} generates one image per call (n={n} is not supported)")
        if aspect_ratio is not None and not self.supports_aspect_ratio:
            raise ValueError(
                f"{type(self).__name__} does not support aspect_ratio={aspect_ratio!r}")
        if image_size is not None and not self.supports_image_size:
            raise ValueError(
                f"{type(self).__name__} does not support image_size={image_size!r}")
        if seed is not None:
            if isinstance(seed, bool) or not isinstance(seed, int):
                raise ValueError("seed must be an integer")
            if not self.supports_seed:
                raise ValueError(
                    f"{type(self).__name__} does not support seed={seed}")

        refs = list(reference_images or ())
        if refs:
            if not self.supports_reference_images:
                raise ValueError(
                    f"{type(self).__name__} does not support reference_images")
            if self.max_reference_images and len(refs) > self.max_reference_images:
                raise ValueError(
                    f"{type(self).__name__} accepts at most {self.max_reference_images} "
                    f"reference image(s), got {len(refs)}")
        return [(ref, ref.resolved_mime()) for ref in refs]

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
        """Generate (or edit) one or more images from a prompt."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_image_from_profile(
    config: Any,
    image_profile: str,
) -> ImageClient:
    """Create an ImageClient from a named profile in ``config.llm_system.image_profiles``."""
    llm_cfg = getattr(config, "llm_system", None)
    if not llm_cfg:
        raise ValueError("No llm_system configuration found in config.")

    profiles = llm_cfg.image_profiles
    if image_profile not in profiles:
        raise ValueError(
            f"Image profile {image_profile!r} not found. "
            f"Available: {sorted(profiles.keys())}")

    model_ref = profiles[image_profile].model_ref
    models = llm_cfg.image_models
    if model_ref not in models:
        raise ValueError(
            f"Image model {model_ref!r} (from profile {image_profile!r}) not found. "
            f"Available: {sorted(models.keys())}")

    model_cfg = models[model_ref]

    # Late import keeps the registry seam patchable and loads provider plugins lazily.
    from agent_system.llm import registry

    return registry.build_image_client(model_cfg, ssl_verify=network_ssl_verify(config))
