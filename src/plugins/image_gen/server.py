"""image_gen tool server — sandboxed image generation and editing for agents."""
from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

from agent_system.llm.image import (
    ImageInput,
    create_image_from_profile,
)
from agent_system.paths import data_path
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.utils.multimodal_tool_content import attach_problem
from agent_system.utils.path_sandbox import PathSandbox, PathSandboxDenied

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

MAX_STATUS_LINE = 140
DEFAULT_MAX_REFERENCE_MB = 15.0
#: Total uncompressed reference-image byte cap per request (avoids OOM / 100 MiB JSON limits).
MAX_TOTAL_REFERENCE_BYTES = 60 * 1024 * 1024
_WINDOWS_RESERVED_STEMS = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def _status_line(text: str) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= MAX_STATUS_LINE:
        return clean
    return clean[: MAX_STATUS_LINE - 1].rstrip() + "…"


def _cfg_get(server_config: Any, key: str, default: Any = None) -> Any:
    """Read a config key from flat attributes first, then nested ``config:`` dict."""
    val = getattr(server_config, key, None)
    if val is not None:
        return val
    nested = getattr(server_config, "config", None)
    if isinstance(nested, dict) and key in nested and nested[key] is not None:
        return nested[key]
    return default


def _is_windows_reserved_name(path: Path) -> bool:
    if os.name != "nt":
        return False
    return any(part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_STEMS for part in path.parts)


def _read_and_validate_ref(path: Path) -> ImageInput:
    data = path.read_bytes()
    item = ImageInput(data=data, label=path.name)
    item.resolved_mime()
    return item


class ImageGenServer(SchemaBasedToolServer):
    """Tool server exposing configured ``llm_system.image_profiles`` inside a PathSandbox."""

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        server_config: "ToolServerConfig",
    ) -> None:
        super().__init__(name, system_config, server_config)

        base = Path.cwd()
        raw_out = _cfg_get(server_config, "output_directories")
        if raw_out is None:
            out_dirs = [str(data_path("image_gen"))]
        elif isinstance(raw_out, str):
            out_dirs = [raw_out]
        else:
            out_dirs = [str(d) for d in raw_out]

        raw_in = _cfg_get(server_config, "input_directories")
        if raw_in is None:
            in_dirs = list(out_dirs)
        elif isinstance(raw_in, str):
            in_dirs = [raw_in]
        else:
            in_dirs = [str(d) for d in raw_in]

        self.output_sandbox = PathSandbox.from_config(out_dirs, base=base, read_only=False)
        self.input_sandbox = PathSandbox.from_config(in_dirs, base=base, read_only=True)

        # None / empty list -> allow all configured image_profiles; a non-empty list
        # restricts strictly (even if all entries were blank -> fail closed).
        raw_profiles = _cfg_get(server_config, "profiles")
        if raw_profiles is None or raw_profiles == []:
            self.allowed_profiles: Optional[list[str]] = None
        elif isinstance(raw_profiles, str):
            cleaned = raw_profiles.strip()
            self.allowed_profiles = [cleaned] if cleaned else []
        else:
            self.allowed_profiles = [str(p).strip() for p in raw_profiles if str(p).strip()]

        raw_default = _cfg_get(server_config, "default_profile")
        self.default_profile: Optional[str] = str(raw_default).strip() if raw_default else None

        raw_limit = _cfg_get(server_config, "max_reference_size_mb", DEFAULT_MAX_REFERENCE_MB)
        try:
            limit_mb = float(raw_limit)
            self.max_reference_size_mb = (
                limit_mb if math.isfinite(limit_mb) and limit_mb > 0 else DEFAULT_MAX_REFERENCE_MB
            )
        except (TypeError, ValueError):
            self.max_reference_size_mb = DEFAULT_MAX_REFERENCE_MB

    def _visible_profiles(self) -> dict[str, Any]:
        llm_cfg = getattr(self.system_config, "llm_system", None)
        configured = getattr(llm_cfg, "image_profiles", None) or {}
        if self.allowed_profiles is None:
            return dict(configured)
        return {name: configured[name] for name in self.allowed_profiles if name in configured}

    def get_template_vars(self) -> Dict[str, Any]:
        tv = super().get_template_vars()
        visible = sorted(self._visible_profiles())
        tv["profiles"] = visible
        tv["has_default_profile"] = bool(self.default_profile and self.default_profile in visible)
        return tv

    async def list_profiles(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        llm_cfg = getattr(self.system_config, "llm_system", None)
        models = getattr(llm_cfg, "image_models", None) or {}
        visible = self._visible_profiles()
        entries = []
        for pname in sorted(visible):
            prof = visible[pname]
            model_cfg = models.get(prof.model_ref)
            entries.append({
                "profile": pname,
                "description": prof.description or "",
                "model_ref": prof.model_ref,
                "provider": model_cfg.provider if model_cfg else None,
                "model": model_cfg.model if model_cfg else None,
                "default_aspect_ratio": model_cfg.aspect_ratio if model_cfg else None,
                "default_image_size": model_cfg.image_size if model_cfg else None,
                "is_default": pname == self.default_profile,
            })
        if status:
            await status.end(_status_line(f"Listed {len(entries)} image profile(s)"))
        return {
            "status": "success",
            "default_profile": self.default_profile if self.default_profile in visible else None,
            "profiles": entries,
        }

    async def generate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        cancellation_token = params.get("_cancellation_token")

        prompt = params.get("prompt")
        output_path = params.get("output_path")
        profile = params.get("profile") or self.default_profile
        reference_paths = params.get("reference_images")
        aspect_ratio = params.get("aspect_ratio")
        image_size = params.get("image_size")
        seed = params.get("seed")
        attach_preview = params.get("attach_preview", True)

        if not isinstance(prompt, str) or not prompt.strip():
            return await self._fail(status, "prompt is required and must be a non-empty string")
        if not isinstance(output_path, str) or not output_path.strip():
            return await self._fail(status, "output_path is required and must be a non-empty string")
        if not isinstance(attach_preview, bool):
            return await self._fail(status, "attach_preview must be true or false")
        if aspect_ratio is not None and not isinstance(aspect_ratio, str):
            return await self._fail(status, "aspect_ratio must be a string")
        if image_size is not None and not isinstance(image_size, str):
            return await self._fail(status, "image_size must be a string")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            return await self._fail(status, "seed must be an integer")

        visible = self._visible_profiles()
        if not visible:
            return await self._fail(
                status,
                "No image profiles are configured or allowed for this instance "
                "(configure llm_system.image_profiles in config/llm.yaml)",
            )
        if not profile or not isinstance(profile, str):
            return await self._fail(
                status,
                f"profile is required (available: {', '.join(sorted(visible))})",
            )
        if profile not in visible:
            return await self._fail(
                status,
                f"profile {profile!r} is not allowed on {self.name} "
                f"(available: {', '.join(sorted(visible))})",
            )

        try:
            resolved_out = self.output_sandbox.resolve(output_path, write=True)
        except PathSandboxDenied as exc:
            return await self._fail(status, str(exc))
        if resolved_out.exists() and resolved_out.is_dir():
            return await self._fail(
                status, f"output_path must be a file path, not an existing directory: {output_path}")
        if _is_windows_reserved_name(resolved_out):
            return await self._fail(
                status, f"output_path uses a reserved device name: {output_path}")

        if reference_paths is not None:
            if not isinstance(reference_paths, list) or not all(isinstance(p, str) for p in reference_paths):
                return await self._fail(status, "reference_images must be a list of file paths")

        # Build the client before reading potentially large reference images from disk so
        # capability limits and missing credentials fail immediately without wasting RAM.
        try:
            client = create_image_from_profile(self.system_config, profile)
        except Exception as exc:  # noqa: BLE001
            return await self._fail(status, f"Failed to initialize image profile {profile!r}: {exc}")

        refs: list[ImageInput] = []
        if reference_paths:
            if not client.supports_reference_images:
                return await self._fail(
                    status, f"profile {profile!r} ({type(client).__name__}) does not support reference_images")
            if client.max_reference_images and len(reference_paths) > client.max_reference_images:
                return await self._fail(
                    status,
                    f"profile {profile!r} accepts at most {client.max_reference_images} "
                    f"reference image(s), got {len(reference_paths)}",
                )
            max_bytes = int(self.max_reference_size_mb * 1024 * 1024)
            resolved_refs: list[Path] = []
            total_bytes = 0
            for raw_path in reference_paths:
                try:
                    resolved_in = self.input_sandbox.resolve(raw_path, write=False)
                except PathSandboxDenied as exc:
                    return await self._fail(status, str(exc))
                if not resolved_in.is_file():
                    return await self._fail(status, f"reference image not found: {raw_path}")
                try:
                    size = resolved_in.stat().st_size
                except OSError as exc:
                    return await self._fail(status, f"cannot stat reference image {raw_path}: {exc}")
                if size > max_bytes:
                    return await self._fail(
                        status,
                        f"reference image {resolved_in.name} is {size / (1024 * 1024):.1f} MiB, "
                        f"over the {self.max_reference_size_mb:g} MiB limit",
                    )
                total_bytes += size
                if total_bytes > MAX_TOTAL_REFERENCE_BYTES:
                    return await self._fail(
                        status,
                        f"total reference_images size exceeds "
                        f"{MAX_TOTAL_REFERENCE_BYTES // (1024 * 1024)} MiB",
                    )
                resolved_refs.append(resolved_in)

            for resolved_in in resolved_refs:
                try:
                    item = await asyncio.to_thread(_read_and_validate_ref, resolved_in)
                except (OSError, ValueError) as exc:
                    return await self._fail(status, f"invalid reference image {resolved_in.name}: {exc}")
                refs.append(item)

        if status:
            await status.progress(_status_line(f"Generating {resolved_out.name} via {profile}"))

        try:
            result = await client.generate(
                prompt,
                reference_images=refs or None,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
                seed=seed,
                cancellation_token=cancellation_token,
            )
        except Exception as exc:  # noqa: BLE001
            return await self._fail(status, f"Image generation failed ({profile}): {exc}")

        if not result.images:
            return await self._fail(status, f"Profile {profile!r} returned no images")

        primary = result.images[0]
        try:
            saved_path, actual_mime, byte_count = await asyncio.to_thread(
                primary.save, resolved_out, convert_to_extension=True)
        except Exception as exc:  # noqa: BLE001
            return await self._fail(status, f"Failed to save generated image: {exc}")

        dims = (
            f"{primary.width}x{primary.height}"
            if primary.width and primary.height
            else "unknown"
        )
        answer: Dict[str, Any] = {
            "status": "success",
            "path": str(saved_path),
            "width": primary.width,
            "height": primary.height,
            "bytes": byte_count,
            "mime_type": actual_mime,
            "profile": profile,
            "model": result.model,
        }
        if result.text:
            answer["text"] = result.text

        if attach_preview:
            problem = attach_problem(saved_path, "image")
            if problem:
                answer["not_attached"] = problem
            else:
                answer["_multimodal_content"] = [{
                    "type": "image",
                    "path": str(saved_path),
                    "mime_type": actual_mime,
                    "description": f"Generated image ({dims}, {profile})",
                }]

        if status:
            await status.end(
                _status_line(f"Generated {saved_path.name}: {dims} ({byte_count} bytes) via {profile}"),
                meta={
                    "output_path": str(saved_path),
                    "bytes": byte_count,
                    "profile": profile,
                    "model": result.model,
                },
            )
        return answer

    async def _fail(self, status: Any, message: str) -> Dict[str, Any]:
        if status:
            await status.error(_status_line(message))
        return {"status": "error", "error": message}
