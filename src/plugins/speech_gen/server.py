"""speech_gen tool server — sandboxed text-to-speech synthesis for agents."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Awaitable, Dict, Optional, TypeVar, TYPE_CHECKING

from agent_system.llm.retry_utils import execute_with_cancellation
from agent_system.llm.tts import (
    TTSClient,
    TTSResult,
    TTSSpeaker,
    TTSVoice,
    create_tts_from_profile,
)
from agent_system.paths import data_path
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.utils.multimodal_tool_content import attach_problem
from agent_system.utils.path_sandbox import PathSandbox, PathSandboxDenied

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

MAX_STATUS_LINE = 140
DEFAULT_MAX_TEXT_CHARS = 5000
DEFAULT_MAX_REFERENCE_MB = 15.0
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


def _dir_list(raw: Any, default: list[str]) -> list[str]:
    if raw is None:
        return default
    if isinstance(raw, str):
        cleaned = raw.strip()
        return [cleaned] if cleaned else []
    if isinstance(raw, (list, tuple, set)):
        return [s for item in raw if (s := str(item).strip())]
    return []


def _is_windows_reserved_name(path: Path) -> bool:
    if os.name != "nt":
        return False
    return any(
        part.rstrip(" .").split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_RESERVED_STEMS
        for part in path.parts
    )


def _has_windows_ads(path: Path) -> bool:
    if os.name != "nt":
        return False
    return any(":" in part for part in path.parts[1:])


def _read_bounded_bytes(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as fh:
        return fh.read(max_bytes + 1)


def _positive_int(raw: Any, default: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        val = int(raw)
        if isinstance(raw, float) and not math.isfinite(raw):
            return default
        return val if val > 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _positive_float(raw: Any, default: float) -> float:
    if isinstance(raw, bool):
        return default
    try:
        val = float(raw)
        return val if math.isfinite(val) and val > 0 else default
    except (TypeError, ValueError):
        return default


async def _await_with_cancellation(coro: Awaitable[_T], cancellation_token: Any) -> _T:
    """Await ``coro``, translating a cancelled token into ``asyncio.CancelledError``."""
    if not cancellation_token:
        return await coro
    if cancellation_token.is_cancelled:
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[union-attr]
        raise asyncio.CancelledError("Request cancelled by user")
    task: asyncio.Task[_T] = asyncio.create_task(coro)  # type: ignore[arg-type]
    try:
        return await execute_with_cancellation(task, cancellation_token)
    except Exception as exc:
        if cancellation_token.is_cancelled:
            raise asyncio.CancelledError("Request cancelled by user") from exc
        raise
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def _check_cancelled(cancellation_token: Any, status: Any, filename: str) -> None:
    if cancellation_token and cancellation_token.is_cancelled:
        if status:
            await status.error(_status_line(f"Cancelled synthesis of {filename}"))
        raise asyncio.CancelledError("Request cancelled by user")


def _save_wav_atomic(result: TTSResult, target: Path) -> tuple[Path, int, float]:
    """Validate PCM fields and write ``target`` atomically as WAV.

    Returns ``(target, file_bytes, duration_seconds)``.
    """
    if not result.audio_data:
        raise ValueError("TTS provider returned empty audio data")
    if (
        isinstance(result.sample_rate, bool)
        or not isinstance(result.sample_rate, int)
        or result.sample_rate <= 0
    ):
        raise ValueError(f"invalid sample_rate: {result.sample_rate}")
    if (
        isinstance(result.channels, bool)
        or not isinstance(result.channels, int)
        or result.channels <= 0
    ):
        raise ValueError(f"invalid channels: {result.channels}")
    if (
        isinstance(result.sample_width, bool)
        or not isinstance(result.sample_width, int)
        or result.sample_width not in (1, 2, 3, 4)
    ):
        raise ValueError(f"invalid sample_width: {result.sample_width}")
    frame_bytes = result.sample_width * result.channels
    if len(result.audio_data) % frame_bytes != 0:
        raise ValueError(
            f"PCM payload length ({len(result.audio_data)}) is not a multiple of frame size ({frame_bytes})")

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    byte_count = 0
    try:
        try:
            fh = os.fdopen(fd, "wb")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        with fh:
            with wave.open(fh, "wb") as wf:
                wf.setnchannels(result.channels)
                wf.setsampwidth(result.sample_width)
                wf.setframerate(result.sample_rate)
                wf.writeframes(result.audio_data)
            fh.flush()
            byte_count = os.fstat(fh.fileno()).st_size
        for attempt in range(5):
            try:
                os.replace(tmp_name, target)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

    duration = len(result.audio_data) / (result.sample_rate * frame_bytes)
    return target, byte_count, duration


class SpeechGenServer(SchemaBasedToolServer):
    """Tool server exposing configured ``llm_system.tts_profiles`` inside a PathSandbox."""

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        server_config: "ToolServerConfig",
    ) -> None:
        super().__init__(name, system_config, server_config)

        base = Path.cwd()
        out_dirs = _dir_list(
            _cfg_get(server_config, "output_directories"),
            [str(data_path("speech_gen"))],
        )
        in_dirs = _dir_list(
            _cfg_get(server_config, "input_directories"),
            list(out_dirs),
        )

        self.output_sandbox = PathSandbox.from_config(out_dirs, base=base, read_only=False)
        self.input_sandbox = PathSandbox.from_config(in_dirs, base=base, read_only=True)

        # None / empty list -> allow all configured tts_profiles; a non-empty list
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

        raw_cloning = _cfg_get(server_config, "allow_voice_cloning", False)
        self.allow_voice_cloning: bool = raw_cloning is True

        self.max_text_chars: int = _positive_int(
            _cfg_get(server_config, "max_text_chars", DEFAULT_MAX_TEXT_CHARS),
            DEFAULT_MAX_TEXT_CHARS,
        )
        self.max_reference_size_mb: float = _positive_float(
            _cfg_get(server_config, "max_reference_size_mb", DEFAULT_MAX_REFERENCE_MB),
            DEFAULT_MAX_REFERENCE_MB,
        )

    def _visible_profiles(self) -> dict[str, Any]:
        llm_cfg = getattr(self.system_config, "llm_system", None)
        configured = getattr(llm_cfg, "tts_profiles", None) or {}
        if self.allowed_profiles is None:
            return dict(configured)
        return {name: configured[name] for name in self.allowed_profiles if name in configured}

    def get_template_vars(self) -> Dict[str, Any]:
        tv = super().get_template_vars()
        visible = sorted(self._visible_profiles())
        tv["profiles"] = visible
        tv["has_default_profile"] = bool(self.default_profile and self.default_profile in visible)
        tv["allow_voice_cloning"] = self.allow_voice_cloning
        return tv

    async def list_profiles(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        llm_cfg = getattr(self.system_config, "llm_system", None)
        models = getattr(llm_cfg, "tts_models", None) or {}
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
                "default_voice": model_cfg.voice if model_cfg else None,
                "is_default": pname == self.default_profile,
            })
        if status:
            await status.end(_status_line(f"Listed {len(entries)} TTS profile(s)"))
        return {
            "status": "success",
            "default_profile": self.default_profile if self.default_profile in visible else None,
            "allow_voice_cloning": self.allow_voice_cloning,
            "max_text_chars": self.max_text_chars,
            "profiles": entries,
        }

    async def synthesize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        cancellation_token = params.get("_cancellation_token")

        text = params.get("text")
        output_path = params.get("output_path")
        raw_profile = params.get("profile")
        voice_param = params.get("voice")
        speakers_param = params.get("speakers")
        system_instruction = params.get("system_instruction")
        seed = params.get("seed")
        reference_audio_param = params.get("reference_audio")
        reference_text = params.get("reference_text")
        attach_preview = params.get("attach_preview", False)

        # Basic argument validation
        if not isinstance(text, str) or not text.strip():
            return await self._fail(status, "text is required and must be a non-empty string")
        if len(text) > self.max_text_chars:
            return await self._fail(
                status,
                f"text is {len(text)} characters, over the {self.max_text_chars} character limit",
            )
        if not isinstance(output_path, str) or not output_path.strip():
            return await self._fail(status, "output_path is required and must be a non-empty string")
        if raw_profile is not None and (not isinstance(raw_profile, str) or not raw_profile.strip()):
            return await self._fail(status, "profile must be a non-empty string when provided")
        if not isinstance(attach_preview, bool):
            return await self._fail(status, "attach_preview must be true or false")
        if voice_param is not None and (not isinstance(voice_param, str) or not voice_param.strip()):
            return await self._fail(status, "voice must be a non-empty string when provided")
        if system_instruction is not None and not isinstance(system_instruction, str):
            return await self._fail(status, "system_instruction must be a string")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            return await self._fail(status, "seed must be an integer")
        if reference_audio_param is not None and (
            not isinstance(reference_audio_param, str) or not reference_audio_param.strip()
        ):
            return await self._fail(status, "reference_audio must be a non-empty file path")
        if reference_text is not None and not isinstance(reference_text, str):
            return await self._fail(status, "reference_text must be a string")
        if reference_text is not None and reference_audio_param is None:
            return await self._fail(status, "reference_text requires reference_audio")

        # Voice cloning gate enforced at runtime (not only in schema)
        if reference_audio_param is not None and not self.allow_voice_cloning:
            return await self._fail(
                status,
                f"voice cloning (reference_audio) is disabled on {self.name} "
                "(set allow_voice_cloning: true on this instance to enable it)",
            )

        # Multi-speaker validation
        parsed_speakers: list[TTSSpeaker] = []
        if speakers_param is not None:
            if voice_param is not None or reference_audio_param is not None:
                return await self._fail(
                    status, "speakers cannot be combined with voice or reference_audio")
            if system_instruction is not None or seed is not None:
                return await self._fail(
                    status,
                    "speakers (multi-speaker synthesis) does not support system_instruction or seed")
            if not isinstance(speakers_param, list) or not (1 <= len(speakers_param) <= 2):
                return await self._fail(status, "speakers must be a list of 1 to 2 {name, voice} objects")
            seen_names: set[str] = set()
            for idx, entry in enumerate(speakers_param):
                if not isinstance(entry, dict):
                    return await self._fail(status, f"speakers[{idx}] must be an object with name and voice")
                sname = entry.get("name")
                svoice = entry.get("voice")
                if not isinstance(sname, str) or not sname.strip():
                    return await self._fail(status, f"speakers[{idx}].name must be a non-empty string")
                if not isinstance(svoice, str) or not svoice.strip():
                    return await self._fail(status, f"speakers[{idx}].voice must be a non-empty string")
                clean_name = sname.strip()
                if clean_name in seen_names:
                    return await self._fail(status, f"duplicate speaker name: {clean_name!r}")
                seen_names.add(clean_name)
                parsed_speakers.append(TTSSpeaker(name=clean_name, voice=TTSVoice(name=svoice.strip())))

        # Profile allowlist validation
        visible = self._visible_profiles()
        if not visible:
            return await self._fail(
                status,
                "No TTS profiles are configured or allowed for this instance "
                "(configure llm_system.tts_profiles in config/llm.yaml)",
            )
        effective_default = self.default_profile if self.default_profile in visible else None
        profile = raw_profile.strip() if isinstance(raw_profile, str) else effective_default
        if not profile:
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

        # Output path sandbox & format checks (before building client or calling API)
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
        if _has_windows_ads(resolved_out):
            return await self._fail(
                status, f"output_path cannot contain ':' (alternate data stream): {output_path}")
        if resolved_out.suffix.lower() != ".wav":
            return await self._fail(
                status,
                f"output_path must end in '.wav' (got {resolved_out.name!r}); "
                "TTS outputs 16-bit PCM WAV",
            )

        await _check_cancelled(cancellation_token, status, resolved_out.name)

        # Build client before reading reference audio so capability checks run first
        try:
            client = create_tts_from_profile(self.system_config, profile)
        except Exception as exc:  # noqa: BLE001
            return await self._fail(status, f"Failed to initialize TTS profile {profile!r}: {exc}")

        if parsed_speakers and type(client).synthesize_multi_speaker is TTSClient.synthesize_multi_speaker:
            return await self._fail(
                status,
                f"profile {profile!r} ({type(client).__name__}) does not support multi-speaker synthesis",
            )

        voice_obj: Optional[TTSVoice] = None
        if not parsed_speakers:
            if reference_audio_param is not None:
                if not client.supports_voice_cloning:
                    return await self._fail(
                        status,
                        f"profile {profile!r} ({type(client).__name__}) does not support voice cloning",
                    )
                try:
                    resolved_in = self.input_sandbox.resolve(reference_audio_param, write=False)
                except PathSandboxDenied as exc:
                    return await self._fail(status, str(exc))
                if not resolved_in.is_file():
                    return await self._fail(status, f"reference_audio not found: {reference_audio_param}")
                max_bytes = max(1, int(self.max_reference_size_mb * 1024 * 1024))
                try:
                    ref_bytes = await asyncio.to_thread(_read_bounded_bytes, resolved_in, max_bytes)
                except OSError as exc:
                    return await self._fail(status, f"cannot read reference_audio {resolved_in.name}: {exc}")
                if not ref_bytes:
                    return await self._fail(
                        status, f"reference_audio {resolved_in.name} is empty (0 bytes)")
                if len(ref_bytes) > max_bytes:
                    return await self._fail(
                        status,
                        f"reference_audio {resolved_in.name} exceeds the {self.max_reference_size_mb:g} MiB limit",
                    )
                clone_label = voice_param.strip() if voice_param else (resolved_in.stem or "cloned-voice")
                voice_obj = TTSVoice(
                    name=clone_label,
                    reference_audio=ref_bytes,
                    reference_text=reference_text,
                )
            elif voice_param is not None:
                voice_obj = TTSVoice(name=voice_param.strip())

            try:
                client.check_voice(voice_obj)
            except ValueError as exc:
                return await self._fail(status, str(exc))
        else:
            try:
                for sp in parsed_speakers:
                    client.check_voice(sp.voice)
            except ValueError as exc:
                return await self._fail(status, str(exc))

        warnings: list[str] = []
        if not parsed_speakers and not client.supports_style_prompt:
            ignored = [
                k for k, v in (("system_instruction", system_instruction), ("seed", seed))
                if v is not None
            ]
            if ignored:
                warnings.append(
                    f"profile {profile!r} ({type(client).__name__}) does not support "
                    f"{', '.join(ignored)}; those parameter(s) were ignored"
                )

        if status:
            await status.progress(_status_line(f"Synthesizing {resolved_out.name} via {profile}"))

        try:
            if parsed_speakers:
                coro = client.synthesize_multi_speaker(text, speakers=parsed_speakers)
            else:
                coro = client.synthesize(
                    text,
                    voice=voice_obj,
                    system_instruction=system_instruction,
                    seed=seed,
                )
            result = await _await_with_cancellation(coro, cancellation_token)
        except asyncio.CancelledError:
            if status:
                await status.error(_status_line(f"Cancelled synthesis of {resolved_out.name}"))
            raise
        except Exception as exc:  # noqa: BLE001
            return await self._fail(status, f"Speech synthesis failed ({profile}): {exc}")

        await _check_cancelled(cancellation_token, status, resolved_out.name)

        try:
            saved_path, byte_count, duration_s = await asyncio.to_thread(
                _save_wav_atomic, result, resolved_out)
        except Exception as exc:  # noqa: BLE001
            return await self._fail(status, f"Failed to save synthesized WAV: {exc}")

        answer: Dict[str, Any] = {
            "status": "success",
            "path": str(saved_path),
            "duration_seconds": round(duration_s, 2),
            "sample_rate": result.sample_rate,
            "channels": result.channels,
            "bytes": byte_count,
            "mime_type": "audio/wav",
            "profile": profile,
            "model": result.model,
        }
        if result.voice_name:
            answer["voice"] = result.voice_name
        if warnings:
            answer["warnings"] = warnings

        if attach_preview:
            problem = attach_problem(saved_path, "audio")
            if problem:
                answer["not_attached"] = problem
            else:
                answer["_multimodal_content"] = [{
                    "type": "audio",
                    "path": str(saved_path),
                    "mime_type": "audio/wav",
                    "description": f"Synthesized speech ({duration_s:.1f}s, {profile})",
                }]

        if status:
            await status.end(
                _status_line(
                    f"Synthesized {saved_path.name}: {duration_s:.1f}s ({byte_count} bytes) via {profile}"
                ),
                meta={
                    "output_path": str(saved_path),
                    "bytes": byte_count,
                    "duration_seconds": round(duration_s, 2),
                    "profile": profile,
                    "model": result.model,
                },
            )
        return answer

    async def _fail(self, status: Any, message: str) -> Dict[str, Any]:
        if status:
            await status.error(_status_line(message))
        return {"status": "error", "error": message}
