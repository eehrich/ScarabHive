"""Audio Operations MCP Server implementation."""

from __future__ import annotations

import asyncio
import builtins
import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)

# Supported audio formats
SUPPORTED_FORMATS = {".flac", ".mp3", ".wav"}


class AudioOpsError(Exception):
    """Base exception for audio operations."""
    
    def __init__(self, message: str, error_type: str = "AudioOpsError", details: dict | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


def _int32_to_int24_bytes(samples: "np.ndarray") -> bytes:
    """Pack an int32 sample array into 3-byte little-endian PCM.

    pydub supports sample_width=3 (24-bit PCM), but numpy has no native
    int24 dtype. We drop the high byte of each little-endian int32 sample
    to produce the packed 24-bit representation pydub round-trips.
    """
    import numpy as np
    if samples.dtype != np.int32:
        samples = samples.astype(np.int32)
    raw = samples.tobytes()
    # Build a uint8 view and strip every 4th byte (the high byte).
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 4)
    return arr[:, :3].tobytes()


class AudioOpsServer(SchemaBasedMCPServer):
    """MCP server for audio file manipulation.
    
    Provides tools for:
    - Cutting audio segments
    - Getting audio file metadata
    - Listing audio files
    """
    
    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPConfig"
    ) -> None:
        """Initialize audio operations server.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Storage path configuration - supports {session_id} template for session isolation
        self._storage_path_template = getattr(mcp_config, 'storage_path', "data/audio_ops")
        # Base storage path (without session_id substitution) for cleanup and fallback
        self._storage_path_base = Path(self._storage_path_template.replace("{session_id}", "").rstrip("/\\"))
        # Don't create directory on init - only when needed for write operations
        # Legacy: self.storage_path for backward compatibility (uses base path)
        self.storage_path = self._storage_path_base
        
        logger.info(f"AudioOpsServer initialized: storage_path={self.storage_path}")
    
    def _resolve_storage_path(self, session_id: str | None = None) -> Path:
        """Resolve storage path, substituting {session_id} if present in template.
        
        This enables session-based isolation of audio files. When multiple agents
        run concurrently, each gets its own subdirectory to avoid file conflicts.
        
        Args:
            session_id: Session ID to substitute into the path template.
                       If None and template contains {session_id}, returns base path.
        
        Returns:
            Resolved Path object with {session_id} substituted if applicable.
            
        Example:
            Template: "data/writer/audio/temp/{session_id}"
            Session ID: "abc123"
            Result: Path("data/writer/audio/temp/abc123")
        """
        if "{session_id}" not in self._storage_path_template:
            # No template - return base path
            return self._storage_path_base
        
        if not session_id:
            # Template exists but no session_id provided - use base path
            logger.debug("storage_path template contains {session_id} but no session_id provided, using base path")
            return self._storage_path_base
        
        # Substitute session_id into template
        resolved_path = Path(self._storage_path_template.replace("{session_id}", session_id))
        # Don't create directory here - will be created when needed for write operations
        return resolved_path

    def _validate_path(self, filename: str, session_id: str | None = None, ensure_parent: bool = False) -> Path:
        """Validate and resolve file path within storage directory.
        
        Args:
            filename: Filename to validate
            session_id: Optional session ID for session-isolated storage paths
            ensure_parent: If True, create parent directories (for write operations)
            
        Returns:
            Resolved Path object
            
        Raises:
            AudioOpsError: If path is invalid or outside storage
        """
        if not filename:
            raise AudioOpsError(
                "Filename cannot be empty",
                error_type="ValidationError",
                details={"reason": "empty_filename"}
            )
        
        # Resolve effective storage path (with session isolation if configured)
        effective_storage = self._resolve_storage_path(session_id)
        
        # Normalize path separators
        filename = filename.replace("\\", "/")
        
        # If filename contains the storage path, strip it to get just the filename
        # This handles cases where ComfyUI returns full paths like "data/writer/audio/temp/file.flac"
        # Check both base storage path and effective storage path
        for storage in [effective_storage, self._storage_path_base]:
            storage_str = str(storage).replace("\\", "/")
            if filename.startswith(storage_str + "/"):
                filename = filename[len(storage_str) + 1:]
                break
            elif filename.startswith(storage_str):
                filename = filename[len(storage_str):]
                if filename.startswith("/"):
                    filename = filename[1:]
                break
        
        # IMPORTANT: If filename starts with the session_id folder, strip it
        # This handles cases where the agent passes "session_id/file.flac" but we already
        # resolved the storage path to include the session_id
        if session_id and filename.startswith(session_id + "/"):
            filename = filename[len(session_id) + 1:]
        elif session_id and filename.startswith(session_id):
            filename = filename[len(session_id):]
            if filename.startswith("/"):
                filename = filename[1:]
        
        # If it's still a path (contains /), extract just the filename for safety
        # This handles edge cases where paths don't match exactly
        if "/" in filename:
            # Check if the path starts with our storage path components
            filename_path = Path(filename)
            storage_parts = effective_storage.parts
            filename_parts = filename_path.parts
            
            # Find where the actual filename starts (after storage path overlap)
            overlap_len = 0
            for i, part in enumerate(filename_parts):
                if i < len(storage_parts) and part == storage_parts[i]:
                    overlap_len = i + 1
                elif i < len(storage_parts):
                    break
            
            if overlap_len > 0:
                # Strip the overlapping storage path parts
                filename = str(Path(*filename_parts[overlap_len:]))
        
        # Prevent path traversal
        if ".." in filename:
            raise AudioOpsError(
                f"Invalid filename: {filename}. Path traversal not allowed.",
                error_type="SecurityError",
                details={"file": filename, "reason": "path_traversal_attempt"}
            )
        
        resolved = (effective_storage / filename).resolve()
        
        # Ensure still within storage path
        try:
            resolved.relative_to(effective_storage.resolve())
        except ValueError:
            raise AudioOpsError(
                f"File must be within storage directory: {filename}",
                error_type="SecurityError",
                details={"file": filename, "storage_path": str(effective_storage)}
            )
        
        # Create parent directories if requested (for write operations)
        if ensure_parent:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        
        return resolved
    
    def _validate_format(self, filepath: Path) -> str:
        """Validate audio format by extension.
        
        Args:
            filepath: Path to check
            
        Returns:
            Format string (e.g., 'wav', 'mp3', 'flac')
            
        Raises:
            AudioOpsError: If format not supported
        """
        ext = filepath.suffix.lower()
        if ext not in SUPPORTED_FORMATS:
            raise AudioOpsError(
                f"Unsupported audio format: {ext}. Supported: {', '.join(SUPPORTED_FORMATS)}",
                error_type="FormatError",
                details={"extension": ext, "supported": list(SUPPORTED_FORMATS)}
            )
        return ext[1:]  # Remove leading dot
    
    async def _load_audio(self, filepath: Path):
        """Load audio file using pydub.

        Async: the whole-file decode (AudioSegment.from_file) is offloaded to a
        worker thread so it never blocks the shared event loop. Callers must
        ``await`` this.

        Args:
            filepath: Path to audio file

        Returns:
            AudioSegment object

        Raises:
            AudioOpsError: If file cannot be loaded
        """
        try:
            from pydub import AudioSegment
        except ImportError:
            raise AudioOpsError(
                "pydub library not installed. Run: pip install pydub",
                error_type="DependencyError",
                details={"missing": "pydub"}
            )

        fmt = self._validate_format(filepath)

        try:
            return await asyncio.to_thread(AudioSegment.from_file, str(filepath), format=fmt)
        except FileNotFoundError:
            raise AudioOpsError(
                f"Audio file not found: {filepath.name}",
                error_type="FileNotFoundError",
                details={"file": filepath.name, "storage_path": str(self.storage_path)}
            )
        except Exception as e:
            raise AudioOpsError(
                f"Failed to load audio file: {filepath.name}. {str(e)}",
                error_type="AudioLoadError",
                details={"file": filepath.name, "error": str(e)}
            )
    
    async def cut(self, params: dict[str, Any]) -> dict[str, Any]:
        """Cut audio: extract or remove a segment.
        
        Args:
            params: Tool parameters:
                - source_file: Source audio filename
                - dest_file: Destination filename
                - start_time: Start position in seconds
                - end_time: End position in seconds
                - mode: 'extract' (copy segment) or 'remove' (delete segment, keep rest)
            
        Returns:
            Dict with operation result
        """
        status = params.get("_status")
        
        try:
            source_file = params.get("source_file")
            dest_file = params.get("dest_file")
            start_time = params.get("start_time")
            end_time = params.get("end_time")
            mode = params.get("mode", "extract")
            
            # Validate required parameters
            if not source_file:
                raise AudioOpsError("source_file is required", "ValidationError")
            if not dest_file:
                raise AudioOpsError("dest_file is required", "ValidationError")
            if start_time is None:
                raise AudioOpsError("start_time is required", "ValidationError")
            if end_time is None:
                raise AudioOpsError("end_time is required", "ValidationError")
            
            # Validate mode
            if mode not in ("extract", "remove"):
                raise AudioOpsError(
                    f"Invalid mode: {mode}. Must be 'extract' or 'remove'",
                    error_type="ValidationError",
                    details={"mode": mode, "valid_modes": ["extract", "remove"]}
                )
            
            # Validate time values
            try:
                start_time = float(start_time)
                end_time = float(end_time)
            except (ValueError, TypeError):
                raise AudioOpsError(
                    "start_time and end_time must be numbers",
                    error_type="ValidationError",
                    details={"start_time": start_time, "end_time": end_time}
                )
            
            if start_time < 0:
                raise AudioOpsError(
                    f"start_time cannot be negative: {start_time}",
                    error_type="ValidationError",
                    details={"start_time": start_time}
                )
            
            if end_time <= start_time:
                raise AudioOpsError(
                    f"end_time ({end_time}s) must be greater than start_time ({start_time}s)",
                    error_type="ValidationError",
                    details={"start_time": start_time, "end_time": end_time}
                )
            
            # Validate paths (with session isolation if configured)
            session_id = params.get("_session_id")
            source_path = self._validate_path(source_file, session_id)
            dest_path = self._validate_path(dest_file, session_id, ensure_parent=True)
            
            # Validate destination format
            dest_format = self._validate_format(dest_path)
            
            if status:
                await status.progress(f"Loading: {source_file}")
            
            # Load audio
            audio = await self._load_audio(source_path)
            duration_sec = len(audio) / 1000.0
            
            # Validate time range against duration
            if start_time >= duration_sec:
                raise AudioOpsError(
                    f"start_time ({start_time}s) exceeds file duration ({duration_sec:.2f}s)",
                    error_type="TimeRangeError",
                    details={"start_time": start_time, "duration": duration_sec}
                )
            
            if end_time > duration_sec:
                raise AudioOpsError(
                    f"end_time ({end_time}s) exceeds file duration ({duration_sec:.2f}s)",
                    error_type="TimeRangeError",
                    details={"end_time": end_time, "duration": duration_sec}
                )
            
            # Cut segment (pydub uses milliseconds)
            start_ms = int(start_time * 1000)
            end_ms = int(end_time * 1000)
            
            if mode == "extract":
                # Extract: copy the segment between start and end
                if status:
                    await status.progress(f"Extracting: {start_time}s to {end_time}s")
                result_audio = audio[start_ms:end_ms]
                operation_desc = f"Extracted {start_time}s-{end_time}s"
            else:
                # Remove: keep everything except the segment
                if status:
                    await status.progress(f"Removing: {start_time}s to {end_time}s")
                before = audio[:start_ms]
                after = audio[end_ms:]
                result_audio = before + after
                operation_desc = f"Removed {start_time}s-{end_time}s"
            
            # Export
            await asyncio.to_thread(result_audio.export, str(dest_path), format=dest_format)
            
            result_duration = len(result_audio) / 1000.0
            
            if status:
                await status.end(f"Created {dest_file} ({result_duration:.2f}s) - {operation_desc}", meta={
                    "source": source_file,
                    "destination": dest_file,
                    "duration": result_duration,
                    "mode": mode
                })
            
            return {
                "status": "success",
                "mode": mode,
                "source": source_file,
                "destination": dest_file,
                "start_time": start_time,
                "end_time": end_time,
                "source_duration_seconds": round(duration_sec, 2),
                "result_duration_seconds": round(result_duration, 2),
                "format": dest_format,
                "operation": operation_desc
            }
            
        except AudioOpsError as e:
            if status:
                await status.error(str(e), meta={"error_type": e.error_type, **e.details})
            return {
                "status": "error",
                "error": str(e),
                "error_type": e.error_type,
                "details": e.details
            }
        except Exception as e:
            logger.error(f"Unexpected error in cut: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def merge(self, params: dict[str, Any]) -> dict[str, Any]:
        """Merge multiple audio files into one.
        
        Args:
            params: Tool parameters:
                - source_files: List of audio filenames to merge (in order)
                - dest_file: Output filename
                - crossfade_ms: Crossfade duration in milliseconds (default: 0)
            
        Returns:
            Dict with operation result
        """
        status = params.get("_status")
        
        try:
            source_files = params.get("source_files")
            dest_file = params.get("dest_file")
            crossfade_ms = params.get("crossfade_ms", 0)
            
            # Validate required parameters
            if not source_files:
                raise AudioOpsError("source_files is required", "ValidationError")
            if not isinstance(source_files, list):
                raise AudioOpsError("source_files must be a list", "ValidationError")
            if len(source_files) < 2:
                raise AudioOpsError("source_files must contain at least 2 files", "ValidationError")
            if not dest_file:
                raise AudioOpsError("dest_file is required", "ValidationError")
            
            # Validate crossfade
            try:
                crossfade_ms = int(crossfade_ms)
                if crossfade_ms < 0:
                    raise ValueError("negative")
            except (ValueError, TypeError):
                raise AudioOpsError(
                    "crossfade_ms must be a non-negative integer",
                    error_type="ValidationError",
                    details={"crossfade_ms": crossfade_ms}
                )
            
            # Validate destination path and format (with session isolation if configured)
            session_id = params.get("_session_id")
            dest_path = self._validate_path(dest_file, session_id, ensure_parent=True)
            dest_format = self._validate_format(dest_path)
            
            if status:
                await status.progress(f"Loading {len(source_files)} audio files...")
            
            # Load all audio files
            segments = []
            total_source_duration = 0.0
            for i, filename in enumerate(source_files):
                source_path = self._validate_path(filename, session_id)
                if not source_path.exists():
                    raise AudioOpsError(
                        f"Source file not found: {filename}",
                        error_type="FileNotFoundError",
                        details={"file": filename, "index": i}
                    )
                audio = await self._load_audio(source_path)
                segments.append(audio)
                total_source_duration += len(audio) / 1000.0
                
                if status:
                    await status.progress(f"Loaded {i+1}/{len(source_files)}: {filename}")
            
            if status:
                await status.progress("Merging audio segments...")
            
            # Merge segments
            if crossfade_ms > 0:
                # Merge with crossfade
                result = segments[0]
                for i, segment in enumerate(segments[1:], 1):
                    # Ensure crossfade doesn't exceed segment length
                    actual_crossfade = min(crossfade_ms, len(result), len(segment))
                    result = result.append(segment, crossfade=actual_crossfade)
                    if status:
                        await status.progress(f"Merged {i+1}/{len(segments)} segments")
            else:
                # Simple concatenation (faster)
                from pydub import AudioSegment
                result = AudioSegment.empty()
                for segment in segments:
                    result += segment
            
            # Export
            await asyncio.to_thread(result.export, str(dest_path), format=dest_format)
            
            result_duration = len(result) / 1000.0
            
            if status:
                await status.end(
                    f"Created {dest_file} ({result_duration:.2f}s) from {len(source_files)} files",
                    meta={
                        "source_files": source_files,
                        "destination": dest_file,
                        "duration": result_duration
                    }
                )
            
            return {
                "status": "success",
                "source_files": source_files,
                "source_count": len(source_files),
                "destination": dest_file,
                "total_source_duration_seconds": round(total_source_duration, 2),
                "result_duration_seconds": round(result_duration, 2),
                "crossfade_ms": crossfade_ms,
                "format": dest_format
            }
            
        except AudioOpsError as e:
            if status:
                await status.error(str(e), meta={"error_type": e.error_type, **e.details})
            return {
                "status": "error",
                "error": str(e),
                "error_type": e.error_type,
                "details": e.details
            }
        except Exception as e:
            logger.error(f"Unexpected error in merge: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get audio file metadata.
        
        Args:
            params: Tool parameters with file
            
        Returns:
            Dict with audio metadata
        """
        status = params.get("_status")
        
        try:
            filename = params.get("file")
            
            if not filename:
                raise AudioOpsError("file parameter is required", "ValidationError")
            
            # Validate path (with session isolation if configured)
            session_id = params.get("_session_id")
            filepath = self._validate_path(filename, session_id)
            
            if not filepath.exists():
                raise AudioOpsError(
                    f"File not found: {filename}",
                    error_type="FileNotFoundError",
                    details={"file": filename, "storage_path": str(self.storage_path)}
                )
            
            if status:
                await status.progress(f"Reading: {filename}")
            
            audio = await self._load_audio(filepath)
            
            result = {
                "status": "success",
                "file": filename,
                "duration_seconds": round(len(audio) / 1000.0, 2),
                "format": self._validate_format(filepath),
                "channels": audio.channels,
                "sample_rate": audio.frame_rate,
                "sample_width_bytes": audio.sample_width,
                "size_bytes": filepath.stat().st_size
            }
            
            if status:
                await status.end(f"{filename}: {result['duration_seconds']}s, {result['channels']}ch, {result['sample_rate']}Hz")
            
            return result
            
        except AudioOpsError as e:
            if status:
                await status.error(str(e), meta={"error_type": e.error_type, **e.details})
            return {
                "status": "error",
                "error": str(e),
                "error_type": e.error_type,
                "details": e.details
            }
        except Exception as e:
            logger.error(f"Unexpected error in info: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def create(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a silent audio file with specified duration.
        
        Args:
            params: Tool parameters:
                - dest_file: Destination filename with extension
                - duration_ms: Duration in milliseconds
                - sample_rate: Sample rate in Hz (default: 44100)
                - channels: Number of channels (1=mono, 2=stereo, default: 2)
            
        Returns:
            Dict with operation result
        """
        status = params.get("_status")
        
        try:
            from pydub import AudioSegment
            
            dest_file = params.get("dest_file")
            duration_ms = params.get("duration_ms")
            sample_rate = params.get("sample_rate", 44100)
            channels = params.get("channels", 2)
            
            # Validate required parameters
            if not dest_file:
                raise AudioOpsError("dest_file is required", "ValidationError")
            if duration_ms is None:
                raise AudioOpsError("duration_ms is required", "ValidationError")
            
            # Validate duration
            try:
                duration_ms = int(duration_ms)
            except (ValueError, TypeError):
                raise AudioOpsError(
                    f"duration_ms must be an integer, got: {type(duration_ms).__name__}",
                    error_type="ValidationError",
                    details={"duration_ms": duration_ms}
                )
            
            if duration_ms <= 0:
                raise AudioOpsError(
                    f"duration_ms must be positive, got: {duration_ms}",
                    error_type="ValidationError",
                    details={"duration_ms": duration_ms}
                )
            
            # Validate sample_rate
            try:
                sample_rate = int(sample_rate)
            except (ValueError, TypeError):
                raise AudioOpsError(
                    f"sample_rate must be an integer, got: {type(sample_rate).__name__}",
                    error_type="ValidationError",
                    details={"sample_rate": sample_rate}
                )
            
            if sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000, 96000):
                raise AudioOpsError(
                    f"Invalid sample_rate: {sample_rate}. Common values: 8000, 16000, 22050, 24000, 44100, 48000, 96000",
                    error_type="ValidationError",
                    details={"sample_rate": sample_rate}
                )
            
            # Validate channels
            try:
                channels = int(channels)
            except (ValueError, TypeError):
                raise AudioOpsError(
                    f"channels must be an integer, got: {type(channels).__name__}",
                    error_type="ValidationError",
                    details={"channels": channels}
                )
            
            if channels not in (1, 2):
                raise AudioOpsError(
                    f"Invalid channels: {channels}. Must be 1 (mono) or 2 (stereo)",
                    error_type="ValidationError",
                    details={"channels": channels}
                )
            
            # Validate destination path and format (with session isolation if configured)
            session_id = params.get("_session_id")
            dest_path = self._validate_path(dest_file, session_id, ensure_parent=True)
            dest_format = self._validate_format(dest_path)
            
            if status:
                await status.progress(f"Creating {duration_ms}ms silent audio ({channels}ch @ {sample_rate}Hz)")
            
            # Create silent audio
            silent_audio = AudioSegment.silent(
                duration=duration_ms,
                frame_rate=sample_rate
            )
            
            # Set channel count
            if channels == 1:
                silent_audio = silent_audio.set_channels(1)
            else:
                silent_audio = silent_audio.set_channels(2)
            
            # Export
            await asyncio.to_thread(silent_audio.export, str(dest_path), format=dest_format)
            
            duration_sec = duration_ms / 1000.0
            
            if status:
                await status.end(
                    f"Created {dest_file} ({duration_sec:.2f}s) - {channels}ch @ {sample_rate}Hz",
                    meta={
                        "file": dest_file,
                        "duration_seconds": duration_sec,
                        "format": dest_format,
                        "channels": channels,
                        "sample_rate": sample_rate
                    }
                )
            
            return {
                "status": "success",
                "destination": dest_file,
                "duration_seconds": round(duration_sec, 2),
                "duration_ms": duration_ms,
                "format": dest_format,
                "channels": channels,
                "sample_rate": sample_rate
            }
            
        except AudioOpsError as e:
            if status:
                await status.error(str(e), meta={"error_type": e.error_type, **e.details})
            return {
                "status": "error",
                "error": str(e),
                "error_type": e.error_type,
                "details": e.details
            }
        except Exception as e:
            logger.error(f"Unexpected error in create: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def list(self, params: dict[str, Any]) -> dict[str, Any]:
        """List audio files in storage directory.
        
        Args:
            params: Tool parameters with optional pattern
            
        Returns:
            Dict with file listing
        """
        status = params.get("_status")
        
        try:
            pattern = params.get("pattern")
            
            # Resolve storage path (with session isolation if configured)
            session_id = params.get("_session_id")
            effective_storage = self._resolve_storage_path(session_id)
            
            if status:
                await status.progress(f"Scanning: {effective_storage}")
            
            files = []
            
            if pattern:
                # Reject path-traversal in the user-supplied glob (Path.glob
                # honours '..' segments and can escape the storage root).
                if ".." in pattern:
                    raise AudioOpsError(
                        f"Invalid pattern: {pattern}. Path traversal not allowed.",
                        error_type="SecurityError",
                        details={"pattern": pattern, "reason": "path_traversal_attempt"}
                    )
                # Use glob pattern
                matching_files = list(effective_storage.glob(pattern))
            else:
                # List all supported audio files
                matching_files = []
                for ext in SUPPORTED_FORMATS:
                    matching_files.extend(effective_storage.glob(f"*{ext}"))
            
            for filepath in sorted(matching_files):
                if filepath.is_file() and filepath.suffix.lower() in SUPPORTED_FORMATS:
                    try:
                        audio = await self._load_audio(filepath)
                        files.append({
                            "name": filepath.name,
                            "size_bytes": filepath.stat().st_size,
                            "duration_seconds": round(len(audio) / 1000.0, 2),
                            "format": filepath.suffix[1:].lower()
                        })
                    except Exception as e:
                        # Include file but note it couldn't be read
                        files.append({
                            "name": filepath.name,
                            "size_bytes": filepath.stat().st_size,
                            "error": f"Cannot read: {str(e)}"
                        })
            
            if status:
                await status.end(f"Found {len(files)} audio files")
            
            return {
                "status": "success",
                "storage_path": str(effective_storage),
                "files": files,
                "total_count": len(files)
            }
            
        except Exception as e:
            logger.error(f"Unexpected error in list: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def load(self, params: dict[str, Any]) -> dict[str, Any]:
        """Load audio file for LLM analysis.
        
        Optionally loads only a segment (start_time to end_time).
        Returns multimodal content that the LLM can analyze.
        
        Args:
            params: Tool parameters:
                - file: Audio filename to load
                - start_time: Start position in seconds (optional)
                - end_time: End position in seconds (optional)
            
        Returns:
            Dict with file info and _multimodal_content for LLM
        """
        status = params.get("_status")
        
        try:
            filename = params.get("file")
            start_time = params.get("start_time")
            end_time = params.get("end_time")
            
            if not filename:
                raise AudioOpsError("file is required", "ValidationError")
            
            # Validate path and load audio (with session isolation if configured)
            session_id = params.get("_session_id")
            filepath = self._validate_path(filename, session_id)
            if not filepath.exists():
                raise AudioOpsError(
                    f"File not found: {filename}",
                    error_type="FileNotFoundError",
                    details={"file": filename}
                )
            
            if status:
                await status.progress(f"Loading: {filename}")
            
            audio = await self._load_audio(filepath)
            duration_sec = len(audio) / 1000.0
            
            # Handle segment extraction if start/end provided
            segment_info = None
            output_path = filepath  # Default: use original file
            
            if start_time is not None or end_time is not None:
                # Validate time range
                actual_start = float(start_time) if start_time is not None else 0.0
                actual_end = float(end_time) if end_time is not None else duration_sec
                
                if actual_start < 0:
                    raise AudioOpsError(
                        "start_time cannot be negative",
                        error_type="ValidationError",
                        details={"start_time": actual_start}
                    )
                if actual_start > duration_sec:
                    raise AudioOpsError(
                        f"start_time ({actual_start}s) exceeds duration ({duration_sec:.2f}s)",
                        error_type="TimeRangeError",
                        details={"start_time": actual_start, "duration": duration_sec}
                    )
                if actual_end <= actual_start:
                    raise AudioOpsError(
                        "end_time must be greater than start_time",
                        error_type="ValidationError",
                        details={"start_time": actual_start, "end_time": actual_end}
                    )
                
                # Clamp end to duration
                actual_end = min(actual_end, duration_sec)
                
                if status:
                    await status.progress(f"Extracting segment: {actual_start}s to {actual_end}s")
                
                # Extract segment
                start_ms = int(actual_start * 1000)
                end_ms = int(actual_end * 1000)
                segment = audio[start_ms:end_ms]
                segment_duration = len(segment) / 1000.0
                
                # Save segment to temp file for multimodal content (in session-isolated dir).
                # Use NamedTemporaryFile(delete=False) so concurrent load() calls
                # for the same (file, start, end) tuple don't race on the same path
                # (one could be unlinked downstream while another is mid-use).
                # The file must outlive this call - the caller consumes
                # _multimodal_content asynchronously - so we keep delete=False
                # and do not unlink here.
                import tempfile
                effective_storage = self._resolve_storage_path(session_id)
                effective_storage.mkdir(parents=True, exist_ok=True)
                tmp = tempfile.NamedTemporaryFile(
                    prefix=f"_temp_segment_{filepath.stem}_{start_ms}_{end_ms}_",
                    suffix=".wav",
                    dir=str(effective_storage),
                    delete=False,
                )
                tmp.close()
                segment_path = Path(tmp.name)
                await asyncio.to_thread(segment.export, str(segment_path), format="wav")
                output_path = segment_path
                
                segment_info = {
                    "start_time": actual_start,
                    "end_time": actual_end,
                    "segment_duration_seconds": round(segment_duration, 2)
                }
            
            # Build multimodal content for LLM
            mime_type = self._get_mime_type(output_path)
            
            multimodal_content = [{
                "type": "audio",
                "path": str(output_path),
                "mime_type": mime_type,
                "description": f"Audio file: {filename}" + (
                    f" (segment {segment_info['start_time']}s-{segment_info['end_time']}s)"
                    if segment_info else ""
                )
            }]
            
            if status:
                await status.end(f"Loaded {filename} for analysis")
            
            result: dict[str, Any] = {
                "status": "success",
                "file": filename,
                "duration_seconds": round(duration_sec, 2),
                "format": filepath.suffix[1:].lower(),
                "mime_type": mime_type,
                "_multimodal_content": multimodal_content
            }
            
            if segment_info:
                result["segment"] = segment_info
            
            return result
            
        except AudioOpsError as e:
            if status:
                await status.error(str(e), meta={"error_type": e.error_type, **e.details})
            return {
                "status": "error",
                "error": str(e),
                "error_type": e.error_type,
                "details": e.details
            }
        except Exception as e:
            logger.error(f"Unexpected error in load: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    def _get_mime_type(self, filepath: Path) -> str:
        """Get MIME type for audio file."""
        ext = filepath.suffix.lower()
        mime_map = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".flac": "audio/flac",
            ".ogg": "audio/ogg",
            ".m4a": "audio/mp4",
            ".aac": "audio/aac",
        }
        return mime_map.get(ext, "audio/unknown")

    async def mix(self, params: dict[str, Any]) -> dict[str, Any]:
        """Mix two audio files together with a configurable factor or envelope.
        
        The mix_factor controls the volume balance (static):
        - 0.0 = 100% file1, 0% file2
        - 0.5 = 50% file1, 50% file2 (equal mix)
        - 1.0 = 0% file1, 100% file2
        
        Alternatively, use envelope for dynamic mixing over time:
        - List of {"time": <seconds>, "factor": <0.0-1.0>} points
        - Linear interpolation between points
        - First point should be at time=0, values before are clamped
        - Values after last point use the last point's factor
        
        If files have different durations, the shorter file is padded with silence.
        
        Args:
            params: Tool parameters:
                - file1: First audio filename
                - file2: Second audio filename
                - dest_file: Output filename
                - mix_factor: Static balance between files (0.0-1.0, default: 0.5)
                - envelope: List of {time, factor} points for dynamic mixing
            
        Returns:
            Dict with operation result
        """
        status = params.get("_status")
        
        try:
            file1 = params.get("file1")
            file2 = params.get("file2")
            dest_file = params.get("dest_file")
            mix_factor = params.get("mix_factor")
            envelope = params.get("envelope")
            
            # Validate required parameters
            if not file1:
                raise AudioOpsError("file1 is required", "ValidationError")
            if not file2:
                raise AudioOpsError("file2 is required", "ValidationError")
            if not dest_file:
                raise AudioOpsError("dest_file is required", "ValidationError")
            
            # Validate mix_factor OR envelope (not both)
            use_envelope = envelope is not None
            
            if use_envelope and mix_factor is not None:
                raise AudioOpsError(
                    "Cannot specify both mix_factor and envelope. Use one or the other.",
                    error_type="ValidationError",
                    details={"mix_factor": mix_factor, "envelope": "provided"}
                )
            
            # Type-checked variables for later use
            validated_envelope: builtins.list[dict[str, float]] | None = None
            validated_factor: float = 0.5
            
            if use_envelope:
                # Validate envelope
                validated_envelope = self._validate_envelope(envelope)
            else:
                # Use static mix_factor (default 0.5)
                if mix_factor is None:
                    validated_factor = 0.5
                else:
                    try:
                        validated_factor = float(mix_factor)
                    except (ValueError, TypeError):
                        raise AudioOpsError(
                            "mix_factor must be a number",
                            error_type="ValidationError",
                            details={"mix_factor": mix_factor}
                        )
                
                if not 0.0 <= validated_factor <= 1.0:
                    raise AudioOpsError(
                        f"mix_factor must be between 0.0 and 1.0, got {validated_factor}",
                        error_type="ValidationError",
                        details={"mix_factor": validated_factor}
                    )
            
            # Validate paths (with session isolation if configured)
            session_id = params.get("_session_id")
            file1_path = self._validate_path(file1, session_id)
            file2_path = self._validate_path(file2, session_id)
            dest_path = self._validate_path(dest_file, session_id, ensure_parent=True)
            
            if not file1_path.exists():
                raise AudioOpsError(
                    f"File not found: {file1}",
                    error_type="FileNotFoundError",
                    details={"file": file1}
                )
            if not file2_path.exists():
                raise AudioOpsError(
                    f"File not found: {file2}",
                    error_type="FileNotFoundError",
                    details={"file": file2}
                )
            
            # Validate destination format
            dest_format = self._validate_format(dest_path)
            
            if status:
                await status.progress(f"Loading audio files: {file1}, {file2}")
            
            # Load both audio files
            audio1 = await self._load_audio(file1_path)
            audio2 = await self._load_audio(file2_path)
            
            duration1_sec = len(audio1) / 1000.0
            duration2_sec = len(audio2) / 1000.0
            
            # Normalize lengths by padding the shorter one with silence
            from pydub import AudioSegment
            if len(audio1) < len(audio2):
                padding = AudioSegment.silent(
                    duration=len(audio2) - len(audio1),
                    frame_rate=audio1.frame_rate
                )
                audio1 = audio1 + padding
            elif len(audio2) < len(audio1):
                padding = AudioSegment.silent(
                    duration=len(audio1) - len(audio2),
                    frame_rate=audio2.frame_rate
                )
                audio2 = audio2 + padding
            
            total_duration_ms = len(audio1)
            
            if use_envelope and validated_envelope is not None:
                if status:
                    await status.progress(f"Mixing with {len(validated_envelope)}-point envelope")
                mixed_result = self._mix_with_envelope(audio1, audio2, validated_envelope, total_duration_ms)
            else:
                if status:
                    await status.progress(f"Mixing with factor {validated_factor}")
                mixed_result = self._mix_with_factor(audio1, audio2, validated_factor)
            
            # Export
            await asyncio.to_thread(mixed_result.export, str(dest_path), format=dest_format)
            
            result_duration = len(mixed_result) / 1000.0
            
            # Build response
            response: dict[str, Any] = {
                "status": "success",
                "file1": file1,
                "file2": file2,
                "destination": dest_file,
                "file1_duration_seconds": round(duration1_sec, 2),
                "file2_duration_seconds": round(duration2_sec, 2),
                "result_duration_seconds": round(result_duration, 2),
                "format": dest_format
            }
            
            if use_envelope and validated_envelope is not None:
                response["envelope"] = validated_envelope
                response["envelope_points"] = len(validated_envelope)
            else:
                response["mix_factor"] = validated_factor
            
            if status:
                mode_desc = f"{len(validated_envelope)}-point envelope" if (use_envelope and validated_envelope) else f"factor {validated_factor}"
                await status.end(
                    f"Created {dest_file} ({result_duration:.2f}s) - mixed with {mode_desc}",
                    meta={
                        "file1": file1,
                        "file2": file2,
                        "destination": dest_file,
                        "duration": result_duration
                    }
                )
            
            return response
            
        except AudioOpsError as e:
            if status:
                await status.error(str(e), meta={"error_type": e.error_type, **e.details})
            return {
                "status": "error",
                "error": str(e),
                "error_type": e.error_type,
                "details": e.details
            }
        except Exception as e:
            logger.error(f"Unexpected error in mix: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    def _validate_envelope(self, envelope: Any) -> builtins.list[dict[str, float]]:
        """Validate and normalize envelope parameter.
        
        Args:
            envelope: List of {time, factor} points
            
        Returns:
            Validated and sorted envelope list
            
        Raises:
            AudioOpsError: If envelope is invalid
        """
        if not isinstance(envelope, builtins.list):
            raise AudioOpsError(
                "envelope must be a list of {time, factor} points",
                error_type="ValidationError",
                details={"envelope_type": type(envelope).__name__}
            )
        
        if len(envelope) < 2:
            raise AudioOpsError(
                "envelope must have at least 2 points",
                error_type="ValidationError",
                details={"point_count": len(envelope)}
            )
        
        validated = []
        for i, point in enumerate(envelope):
            if not isinstance(point, dict):
                raise AudioOpsError(
                    f"envelope point {i} must be an object with 'time' and 'factor'",
                    error_type="ValidationError",
                    details={"point_index": i, "point_type": type(point).__name__}
                )
            
            if "time" not in point:
                raise AudioOpsError(
                    f"envelope point {i} missing 'time' property",
                    error_type="ValidationError",
                    details={"point_index": i, "point": point}
                )
            
            if "factor" not in point:
                raise AudioOpsError(
                    f"envelope point {i} missing 'factor' property",
                    error_type="ValidationError",
                    details={"point_index": i, "point": point}
                )
            
            try:
                time_val = float(point["time"])
                factor_val = float(point["factor"])
            except (ValueError, TypeError) as e:
                raise AudioOpsError(
                    f"envelope point {i}: time and factor must be numbers",
                    error_type="ValidationError",
                    details={"point_index": i, "point": point, "error": str(e)}
                )
            
            if time_val < 0:
                raise AudioOpsError(
                    f"envelope point {i}: time cannot be negative ({time_val})",
                    error_type="ValidationError",
                    details={"point_index": i, "time": time_val}
                )
            
            if not 0.0 <= factor_val <= 1.0:
                raise AudioOpsError(
                    f"envelope point {i}: factor must be 0.0-1.0, got {factor_val}",
                    error_type="ValidationError",
                    details={"point_index": i, "factor": factor_val}
                )
            
            validated.append({"time": time_val, "factor": factor_val})
        
        # Sort by time
        validated.sort(key=lambda p: p["time"])
        
        return validated
    
    def _interpolate_factor(self, envelope: builtins.list[dict[str, float]], time_sec: float) -> float:
        """Get interpolated mix factor at a given time.
        
        Args:
            envelope: Sorted list of {time, factor} points
            time_sec: Time position in seconds
            
        Returns:
            Interpolated factor value (0.0-1.0)
        """
        # Before first point: use first point's factor
        if time_sec <= envelope[0]["time"]:
            return envelope[0]["factor"]
        
        # After last point: use last point's factor
        if time_sec >= envelope[-1]["time"]:
            return envelope[-1]["factor"]
        
        # Find surrounding points and interpolate
        for i in range(len(envelope) - 1):
            p1 = envelope[i]
            p2 = envelope[i + 1]
            
            if p1["time"] <= time_sec <= p2["time"]:
                # Linear interpolation
                if p2["time"] == p1["time"]:
                    return p1["factor"]
                
                t = (time_sec - p1["time"]) / (p2["time"] - p1["time"])
                return p1["factor"] + t * (p2["factor"] - p1["factor"])
        
        # Fallback (shouldn't reach here)
        return envelope[-1]["factor"]
    
    def _mix_with_factor(self, audio1, audio2, mix_factor: float):
        """Mix two audio segments with a static factor.
        
        Args:
            audio1: First AudioSegment
            audio2: Second AudioSegment
            mix_factor: Balance (0.0=file1 only, 1.0=file2 only)
            
        Returns:
            Mixed AudioSegment
        """
        import math
        
        vol1 = 1.0 - mix_factor
        vol2 = mix_factor
        
        # Apply volume adjustments (dB)
        # For vol=0, we effectively mute by applying -120dB
        if vol1 > 0:
            db1 = 20 * math.log10(vol1)
            audio1 = audio1 + db1
        else:
            audio1 = audio1 - 120  # Effectively mute
        
        if vol2 > 0:
            db2 = 20 * math.log10(vol2)
            audio2 = audio2 + db2
        else:
            audio2 = audio2 - 120  # Effectively mute
        
        # Overlay (mix) the two audio tracks
        return audio1.overlay(audio2)
    
    def _mix_with_envelope(self, audio1, audio2, envelope: builtins.list[dict[str, float]], total_ms: int):
        """Mix two audio segments with a dynamic envelope.

        Vectorised via numpy: builds a per-sample factor envelope and applies
        `out = a1 * (1-f) + a2 * f` over the whole buffer at once. Avoids the
        O(N) Python loop over 10ms chunks (millions of iterations for
        book-length audio).

        Args:
            audio1: First AudioSegment
            audio2: Second AudioSegment
            envelope: List of {time, factor} points
            total_ms: Total duration in milliseconds

        Returns:
            Mixed AudioSegment
        """
        import numpy as np

        # Both segments are pre-padded to the same length by the caller.
        # Take format metadata from audio1 (audio2 is overlaid onto it).
        sample_rate = audio1.frame_rate
        channels = audio1.channels
        sample_width = audio1.sample_width

        # Pull interleaved PCM into numpy arrays. AudioSegment.get_array_of_samples()
        # already matches sample_width (int8/int16/int32).
        a1 = np.array(audio1.get_array_of_samples(), dtype=np.int32)
        a2 = np.array(audio2.get_array_of_samples(), dtype=np.int32)

        # Defensive length align (overlay tolerates mismatch; we just truncate).
        n = min(a1.shape[0], a2.shape[0])
        a1 = a1[:n]
        a2 = a2[:n]

        # Number of frames (samples per channel).
        frame_count = n // channels

        # Build per-frame factor envelope via vectorised linear interpolation
        # over the envelope control points.
        frame_times = np.arange(frame_count, dtype=np.float64) / float(sample_rate)
        env_times = np.array([p["time"] for p in envelope], dtype=np.float64)
        env_factors = np.array([p["factor"] for p in envelope], dtype=np.float64)
        # np.interp clamps to first/last value outside the range - matches
        # _interpolate_factor semantics.
        factors = np.interp(frame_times, env_times, env_factors)

        # Expand to per-sample (repeat each frame factor across channels).
        if channels > 1:
            factors = np.repeat(factors, channels)

        vol1 = (1.0 - factors).astype(np.float32)
        vol2 = factors.astype(np.float32)

        # Mix and clip to the sample range for the given width.
        max_val = (1 << (8 * sample_width - 1)) - 1
        min_val = -(1 << (8 * sample_width - 1))

        mixed = a1.astype(np.float32) * vol1 + a2.astype(np.float32) * vol2
        np.clip(mixed, min_val, max_val, out=mixed)

        # Cast back to the segment's PCM byte format. pydub supports 1/2/4
        # natively (np.int8/int16/int32). 24-bit (sample_width=3) has no
        # numpy dtype — we pack int32 samples into 3 bytes manually so
        # 24-bit masters survive the round-trip instead of silently being
        # written as int16-mangled bytes.
        if sample_width == 1:
            out_bytes = mixed.astype(np.int8).tobytes()
        elif sample_width == 2:
            out_bytes = mixed.astype(np.int16).tobytes()
        elif sample_width == 3:
            out_bytes = _int32_to_int24_bytes(mixed.astype(np.int32))
        elif sample_width == 4:
            out_bytes = mixed.astype(np.int32).tobytes()
        else:
            # Unknown width — keep prior behaviour but log so we hear about it.
            logger.warning("Unexpected sample_width=%s in _mix_with_envelope", sample_width)
            out_bytes = mixed.astype(np.int16).tobytes()

        return audio1._spawn(out_bytes)

    async def volume(self, params: dict[str, Any]) -> dict[str, Any]:
        """Adjust volume of an audio file with static gain or dynamic envelope.
        
        Option 1 - Static gain_db:
        - Positive values increase volume (e.g., +6 dB doubles perceived loudness)
        - Negative values decrease volume (e.g., -6 dB halves perceived loudness)
        - 0 = no change
        
        Option 2 - Dynamic envelope:
        - List of {"time": <seconds>, "gain_db": <decibels>} points
        - Linear interpolation between points
        - First point should be at time=0
        
        Args:
            params: Tool parameters:
                - source_file: Input audio filename
                - dest_file: Output filename
                - gain_db: Static volume adjustment in decibels
                - envelope: List of {time, gain_db} points for dynamic volume
                - normalize: If true, normalize to 0 dB peak after processing
            
        Returns:
            Dict with operation result
        """
        status = params.get("_status")
        
        try:
            source_file = params.get("source_file")
            dest_file = params.get("dest_file")
            gain_db = params.get("gain_db")
            envelope = params.get("envelope")
            normalize = params.get("normalize", False)
            
            # Validate required parameters
            if not source_file:
                raise AudioOpsError("source_file is required", "ValidationError")
            if not dest_file:
                raise AudioOpsError("dest_file is required", "ValidationError")
            
            # Validate gain_db OR envelope (not both)
            use_envelope = envelope is not None
            use_gain = gain_db is not None
            
            if use_envelope and use_gain:
                raise AudioOpsError(
                    "Cannot specify both gain_db and envelope. Use one or the other.",
                    error_type="ValidationError",
                    details={"gain_db": gain_db, "envelope": "provided"}
                )
            
            # Allow normalize without gain_db or envelope
            if not use_envelope and not use_gain and not normalize:
                raise AudioOpsError(
                    "Either gain_db, envelope, or normalize must be provided",
                    error_type="ValidationError"
                )
            
            # Type-checked variables
            validated_envelope: builtins.list[dict[str, float]] | None = None
            validated_gain: float = 0.0
            
            if use_envelope:
                validated_envelope = self._validate_volume_envelope(envelope)
            elif use_gain:
                # Use static gain_db
                try:
                    validated_gain = float(gain_db)
                except (ValueError, TypeError):
                    raise AudioOpsError(
                        "gain_db must be a number",
                        error_type="ValidationError",
                        details={"gain_db": gain_db}
                    )
                
                # Reasonable range check (-60 dB to +24 dB)
                if not -60.0 <= validated_gain <= 24.0:
                    raise AudioOpsError(
                        f"gain_db should be between -60 and +24 dB, got {validated_gain}",
                        error_type="ValidationError",
                        details={"gain_db": validated_gain}
                    )
            # else: Only normalize, no gain adjustment (validated_gain = 0.0)
            
            # Validate paths (with session isolation if configured)
            session_id = params.get("_session_id")
            source_path = self._validate_path(source_file, session_id)
            dest_path = self._validate_path(dest_file, session_id, ensure_parent=True)
            
            if not source_path.exists():
                raise AudioOpsError(
                    f"File not found: {source_file}",
                    error_type="FileNotFoundError",
                    details={"file": source_file}
                )
            
            # Validate destination format
            dest_format = self._validate_format(dest_path)
            
            if status:
                await status.progress(f"Loading: {source_file}")
            
            # Load audio
            audio = await self._load_audio(source_path)
            
            if use_envelope and validated_envelope is not None:
                if status:
                    await status.progress(f"Applying {len(validated_envelope)}-point volume envelope")
                result = self._apply_volume_envelope(audio, validated_envelope)
            elif use_gain:
                if status:
                    await status.progress(f"Applying {validated_gain:+.1f} dB gain")
                result = audio + validated_gain
            else:
                # Only normalize, no gain adjustment
                if status:
                    await status.progress("No gain adjustment, only normalization")
                result = audio
            
            # Optional normalization
            if normalize:
                if status:
                    await status.progress("Normalizing to 0 dB peak")
                # Calculate peak and adjust
                peak_amplitude = result.max_dBFS
                if peak_amplitude < 0:
                    result = result - peak_amplitude  # Boost to 0 dB peak
            
            # Export
            await asyncio.to_thread(result.export, str(dest_path), format=dest_format)
            
            result_duration = len(result) / 1000.0
            
            # Build response
            response: dict[str, Any] = {
                "status": "success",
                "source": source_file,
                "destination": dest_file,
                "duration_seconds": round(result_duration, 2),
                "format": dest_format,
                "normalized": normalize
            }
            
            if use_envelope and validated_envelope is not None:
                response["envelope"] = validated_envelope
                response["envelope_points"] = len(validated_envelope)
            elif use_gain:
                response["gain_db"] = validated_gain
            # else: only normalize, no gain details to include
            
            if status:
                if use_envelope and validated_envelope:
                    mode_desc = f"{len(validated_envelope)}-point envelope"
                elif use_gain:
                    mode_desc = f"{validated_gain:+.1f} dB"
                else:
                    mode_desc = "normalization only"
                await status.end(
                    f"Created {dest_file} - volume adjusted by {mode_desc}",
                    meta={
                        "source": source_file,
                        "destination": dest_file,
                        "duration": result_duration
                    }
                )
            
            return response
            
        except AudioOpsError as e:
            if status:
                await status.error(str(e), meta={"error_type": e.error_type, **e.details})
            return {
                "status": "error",
                "error": str(e),
                "error_type": e.error_type,
                "details": e.details
            }
        except Exception as e:
            logger.error(f"Unexpected error in volume: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    def _validate_volume_envelope(self, envelope: Any) -> builtins.list[dict[str, float]]:
        """Validate and normalize volume envelope parameter.
        
        Args:
            envelope: List of {time, gain_db} points
            
        Returns:
            Validated and sorted envelope list
            
        Raises:
            AudioOpsError: If envelope is invalid
        """
        if not isinstance(envelope, builtins.list):
            raise AudioOpsError(
                "envelope must be a list of {time, gain_db} points",
                error_type="ValidationError",
                details={"envelope_type": type(envelope).__name__}
            )
        
        if len(envelope) < 2:
            raise AudioOpsError(
                "envelope must have at least 2 points",
                error_type="ValidationError",
                details={"point_count": len(envelope)}
            )
        
        validated = []
        for i, point in enumerate(envelope):
            if not isinstance(point, dict):
                raise AudioOpsError(
                    f"envelope point {i} must be an object with 'time' and 'gain_db'",
                    error_type="ValidationError",
                    details={"point_index": i, "point_type": type(point).__name__}
                )
            
            if "time" not in point:
                raise AudioOpsError(
                    f"envelope point {i} missing 'time' property",
                    error_type="ValidationError",
                    details={"point_index": i, "point": point}
                )
            
            if "gain_db" not in point:
                raise AudioOpsError(
                    f"envelope point {i} missing 'gain_db' property",
                    error_type="ValidationError",
                    details={"point_index": i, "point": point}
                )
            
            try:
                time_val = float(point["time"])
                gain_val = float(point["gain_db"])
            except (ValueError, TypeError) as e:
                raise AudioOpsError(
                    f"envelope point {i}: time and gain_db must be numbers",
                    error_type="ValidationError",
                    details={"point_index": i, "point": point, "error": str(e)}
                )
            
            if time_val < 0:
                raise AudioOpsError(
                    f"envelope point {i}: time cannot be negative ({time_val})",
                    error_type="ValidationError",
                    details={"point_index": i, "time": time_val}
                )
            
            # Reasonable gain range
            if not -60.0 <= gain_val <= 24.0:
                raise AudioOpsError(
                    f"envelope point {i}: gain_db should be -60 to +24, got {gain_val}",
                    error_type="ValidationError",
                    details={"point_index": i, "gain_db": gain_val}
                )
            
            validated.append({"time": time_val, "gain_db": gain_val})
        
        # Sort by time
        validated.sort(key=lambda p: p["time"])
        
        return validated
    
    def _interpolate_gain(self, envelope: builtins.list[dict[str, float]], time_sec: float) -> float:
        """Get interpolated gain at a given time.
        
        Args:
            envelope: Sorted list of {time, gain_db} points
            time_sec: Time position in seconds
            
        Returns:
            Interpolated gain value in dB
        """
        # Before first point: use first point's gain
        if time_sec <= envelope[0]["time"]:
            return envelope[0]["gain_db"]
        
        # After last point: use last point's gain
        if time_sec >= envelope[-1]["time"]:
            return envelope[-1]["gain_db"]
        
        # Find surrounding points and interpolate
        for i in range(len(envelope) - 1):
            p1 = envelope[i]
            p2 = envelope[i + 1]
            
            if p1["time"] <= time_sec <= p2["time"]:
                # Linear interpolation
                if p2["time"] == p1["time"]:
                    return p1["gain_db"]
                
                t = (time_sec - p1["time"]) / (p2["time"] - p1["time"])
                return p1["gain_db"] + t * (p2["gain_db"] - p1["gain_db"])
        
        # Fallback
        return envelope[-1]["gain_db"]
    
    def _apply_volume_envelope(self, audio, envelope: builtins.list[dict[str, float]]):
        """Apply dynamic volume envelope to audio.

        Vectorised via numpy: builds a per-sample gain envelope (converted from
        dB to linear factor) and applies it in one multiplication. Avoids the
        O(N) Python loop over 10ms chunks (millions of iterations for
        book-length audio).

        Args:
            audio: AudioSegment
            envelope: List of {time, gain_db} points

        Returns:
            Processed AudioSegment
        """
        import numpy as np

        sample_rate = audio.frame_rate
        channels = audio.channels
        sample_width = audio.sample_width

        samples = np.array(audio.get_array_of_samples(), dtype=np.int32)
        n = samples.shape[0]
        frame_count = n // channels

        # Build per-frame gain (dB) via vectorised interpolation, then convert
        # to linear amplitude factor. np.interp clamps outside the range - same
        # behaviour as _interpolate_gain.
        frame_times = np.arange(frame_count, dtype=np.float64) / float(sample_rate)
        env_times = np.array([p["time"] for p in envelope], dtype=np.float64)
        env_gains_db = np.array([p["gain_db"] for p in envelope], dtype=np.float64)
        gains_db = np.interp(frame_times, env_times, env_gains_db)
        gains = np.power(10.0, gains_db / 20.0).astype(np.float32)

        if channels > 1:
            gains = np.repeat(gains, channels)

        max_val = (1 << (8 * sample_width - 1)) - 1
        min_val = -(1 << (8 * sample_width - 1))

        processed = samples.astype(np.float32) * gains
        np.clip(processed, min_val, max_val, out=processed)

        # Cast back to the segment's PCM byte format — see _mix_with_envelope
        # for the same 24-bit handling.
        if sample_width == 1:
            out_bytes = processed.astype(np.int8).tobytes()
        elif sample_width == 2:
            out_bytes = processed.astype(np.int16).tobytes()
        elif sample_width == 3:
            out_bytes = _int32_to_int24_bytes(processed.astype(np.int32))
        elif sample_width == 4:
            out_bytes = processed.astype(np.int32).tobytes()
        else:
            logger.warning("Unexpected sample_width=%s in _apply_volume_envelope", sample_width)
            out_bytes = processed.astype(np.int16).tobytes()

        return audio._spawn(out_bytes)

    async def detect_silence(self, params: dict[str, Any]) -> dict[str, Any]:
        """Detect silent segments in audio file.
        
        Uses ffmpeg silencedetect filter to find segments below threshold.
        
        Args:
            params: Tool parameters from LLM
            
        Returns:
            Dict with status and list of silence segments
        """
        import subprocess
        import re
        import json
        
        status = params.get("_status")
        source_file = params.get("source_file")

        # Session ID for path resolution
        session_id = params.get("_session_id")

        try:
            # SECURITY: these values are interpolated into the ffmpeg '-af'
            # filter string. ffmpeg filtergraph syntax uses commas/semicolons
            # to chain filters (e.g. amovie=/etc/passwd reads arbitrary files),
            # so a non-numeric value would inject filters. Coerce to numbers -
            # the MCP arg schema is not enforced at this boundary. Raised inside
            # the try so the handler's except AudioOpsError returns a clean
            # error response.
            try:
                threshold_db = float(params.get("threshold_db", -40))
                min_duration = float(params.get("min_duration", 0.3))
            except (TypeError, ValueError):
                raise AudioOpsError(
                    "threshold_db and min_duration must be numeric",
                    error_type="ValueError"
                )

            if status:
                await status.progress("Detecting silence...")

            # Validate and resolve path
            source_path = self._validate_path(source_file, session_id)

            if not source_path.exists():
                raise AudioOpsError(
                    f"Source file not found: {source_file}",
                    error_type="FileNotFoundError"
                )

            # Run ffmpeg silencedetect
            detect_cmd = [
                "ffmpeg", "-i", str(source_path),
                "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}",
                "-f", "null", "-"
            ]
            
            result = await asyncio.to_thread(subprocess.run, detect_cmd, capture_output=True, text=True)

            # Check ffmpeg succeeded - otherwise an empty match list would
            # incorrectly report "no silences" for a broken/unreadable file.
            if result.returncode != 0:
                raise AudioOpsError(
                    f"ffmpeg silencedetect failed: {result.stderr[:500]}",
                    error_type="ProcessingError",
                    details={"returncode": result.returncode, "source_file": source_file}
                )

            # Parse silence_start and silence_end from stderr
            silence_starts = re.findall(r'silence_start: ([\d.]+)', result.stderr)
            silence_ends = re.findall(r'silence_end: ([\d.]+)', result.stderr)
            silence_durations = re.findall(r'silence_duration: ([\d.]+)', result.stderr)
            
            # Build silence list
            silences = []
            for i, start in enumerate(silence_starts):
                start_sec = float(start)
                if i < len(silence_ends):
                    end_sec = float(silence_ends[i])
                    duration = float(silence_durations[i]) if i < len(silence_durations) else (end_sec - start_sec)
                else:
                    # Silence extends to end of file
                    probe_cmd = [
                        "ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", str(source_path)
                    ]
                    probe_result = await asyncio.to_thread(subprocess.run, probe_cmd, capture_output=True, text=True)
                    if probe_result.returncode == 0:
                        probe_data = json.loads(probe_result.stdout)
                        end_sec = float(probe_data.get("format", {}).get("duration", start_sec))
                        duration = end_sec - start_sec
                    else:
                        continue
                
                silences.append({
                    "start": start_sec,
                    "end": end_sec,
                    "duration": duration
                })
            
            if status:
                await status.end(f"Found {len(silences)} silence segments")

            return {
                "status": "success",
                "silence_count": len(silences),
                "silences": silences,
                "threshold_db": threshold_db,
                "min_duration": min_duration,
                "source_file": source_file
            }

        except AudioOpsError as e:
            if status:
                await status.error(str(e), meta={"error_type": e.error_type, **e.details})
            return {
                "status": "error",
                "error": str(e),
                "error_type": e.error_type,
                "details": e.details
            }
        except Exception as e:
            logger.exception(f"Unexpected error detecting silence in {source_file}")
            if status:
                await status.error(str(e), meta={"error_type": "UnexpectedError"})
            return {
                "status": "error",
                "error": str(e),
                "error_type": "UnexpectedError"
            }

    async def compress_silence(self, params: dict[str, Any]) -> dict[str, Any]:
        """Compress long silences to maximum duration.
        
        Finds silence segments exceeding max_duration and trims them,
        keeping half at start and half at end of each segment.
        
        Args:
            params: Tool parameters from LLM
            
        Returns:
            Dict with status and compression statistics
        """
        import subprocess
        import re
        import json
        
        status = params.get("_status")
        source_file = params.get("source_file")
        dest_file = params.get("dest_file")

        # Session ID for path resolution
        session_id = params.get("_session_id")

        try:
            # SECURITY: threshold_db and mp3_bitrate are interpolated into
            # ffmpeg filter/codec arguments; coerce to numbers to prevent
            # filtergraph injection (see detect_silence). max_silence is only
            # used in numeric comparisons but coerce it too. Raised inside the
            # try so the handler's except AudioOpsError returns a clean error.
            try:
                max_silence = float(params.get("max_silence", 1.0))
                threshold_db = float(params.get("threshold_db", -40))
                mp3_bitrate = int(params.get("mp3_bitrate", 192))
            except (TypeError, ValueError):
                raise AudioOpsError(
                    "max_silence, threshold_db and mp3_bitrate must be numeric",
                    error_type="ValueError"
                )

            if status:
                await status.progress("Analyzing silence...")

            # Validate and resolve paths
            source_path = self._validate_path(source_file, session_id)
            dest_path = self._validate_path(dest_file, session_id, ensure_parent=True)
            
            if not source_path.exists():
                raise AudioOpsError(
                    f"Source file not found: {source_file}",
                    error_type="FileNotFoundError"
                )
            
            # Step 1: Detect silences with low threshold to find ALL silences
            min_detect_duration = 0.3
            detect_cmd = [
                "ffmpeg", "-i", str(source_path),
                "-af", f"silencedetect=noise={threshold_db}dB:d={min_detect_duration}",
                "-f", "null", "-"
            ]
            
            result = await asyncio.to_thread(subprocess.run, detect_cmd, capture_output=True, text=True)

            # Check ffmpeg succeeded - otherwise an empty match list would
            # incorrectly report "no silences" for a broken/unreadable file.
            if result.returncode != 0:
                raise AudioOpsError(
                    f"ffmpeg silencedetect failed: {result.stderr[:500]}",
                    error_type="ProcessingError",
                    details={"returncode": result.returncode, "source_file": source_file}
                )

            # Parse silence segments
            silence_starts = re.findall(r'silence_start: ([\d.]+)', result.stderr)
            silence_ends = re.findall(r'silence_end: ([\d.]+)', result.stderr)
            
            if not silence_starts:
                # No silences found, just copy file
                if status:
                    await status.progress("No silences detected, copying file...")
                await asyncio.to_thread(subprocess.run, [
                    "ffmpeg", "-y", "-i", str(source_path),
                    "-c", "copy", str(dest_path)
                ], capture_output=True, check=True)

                if status:
                    await status.end("No silences to compress")
                return {
                    "status": "success",
                    "compressed_count": 0,
                    "time_saved": 0.0,
                    "source_file": source_file,
                    "dest_file": dest_file
                }
            
            # Build list of silence segments exceeding max_silence
            silences_to_compress = []
            for i, start in enumerate(silence_starts):
                start_sec = float(start)
                if i < len(silence_ends):
                    end_sec = float(silence_ends[i])
                else:
                    # Silence extends to end - get file duration
                    probe_cmd = [
                        "ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", str(source_path)
                    ]
                    probe_result = await asyncio.to_thread(subprocess.run, probe_cmd, capture_output=True, text=True)
                    if probe_result.returncode == 0:
                        probe_data = json.loads(probe_result.stdout)
                        end_sec = float(probe_data.get("format", {}).get("duration", start_sec))
                    else:
                        continue
                
                duration = end_sec - start_sec
                if duration > max_silence:
                    silences_to_compress.append((start_sec, end_sec, duration))
            
            if not silences_to_compress:
                # No silences exceed threshold, copy file
                if status:
                    await status.progress("No silences exceed max duration, copying file...")
                await asyncio.to_thread(subprocess.run, [
                    "ffmpeg", "-y", "-i", str(source_path),
                    "-c", "copy", str(dest_path)
                ], capture_output=True, check=True)

                if status:
                    await status.end("No silences exceed max duration")
                return {
                    "status": "success",
                    "compressed_count": 0,
                    "time_saved": 0.0,
                    "source_file": source_file,
                    "dest_file": dest_file
                }
            
            if status:
                await status.progress(f"Compressing {len(silences_to_compress)} silence segments...")
            
            # Step 2: Build segments to keep
            keep_duration = max_silence / 2.0
            
            # Get total duration
            probe_cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", str(source_path)
            ]
            probe_result = await asyncio.to_thread(subprocess.run, probe_cmd, capture_output=True, text=True)
            total_duration = 0.0
            if probe_result.returncode == 0:
                probe_data = json.loads(probe_result.stdout)
                total_duration = float(probe_data.get("format", {}).get("duration", 0))
            
            # Build segments to extract
            segments = []
            current_pos = 0.0
            
            for start, end, _ in silences_to_compress:
                seg_end = start + keep_duration
                if seg_end > current_pos:
                    segments.append((current_pos, seg_end))
                current_pos = end - keep_duration
            
            # Add final segment
            if current_pos < total_duration:
                segments.append((current_pos, total_duration))
            
            # Step 3: Build ffmpeg filter
            filter_parts = []
            for i, (seg_start, seg_end) in enumerate(segments):
                if seg_end <= seg_start:
                    continue
                filter_parts.append(
                    f"[0:a]atrim=start={seg_start:.3f}:end={seg_end:.3f},asetpts=PTS-STARTPTS[s{i}]"
                )
            
            if not filter_parts:
                raise AudioOpsError(
                    "No valid segments to extract",
                    error_type="ProcessingError"
                )
            
            # Concat all segments
            segment_labels = "".join(f"[s{i}]" for i in range(len(filter_parts)))
            filter_complex = ";".join(filter_parts) + f";{segment_labels}concat=n={len(filter_parts)}:v=0:a=1[out]"
            
            # Run ffmpeg
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", str(source_path),
                "-filter_complex", filter_complex,
                "-map", "[out]",
            ]
            
            # Add codec settings
            if dest_path.suffix.lower() == '.flac':
                ffmpeg_cmd.extend(["-c:a", "flac"])
            else:
                ffmpeg_cmd.extend(["-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate}k"])
            
            ffmpeg_cmd.append(str(dest_path))
            
            result = await asyncio.to_thread(subprocess.run, ffmpeg_cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                raise AudioOpsError(
                    f"ffmpeg failed: {result.stderr[:500]}",
                    error_type="ProcessingError"
                )
            
            # Get new duration
            probe_result = await asyncio.to_thread(subprocess.run, [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", str(dest_path)
            ], capture_output=True, text=True)
            
            new_duration = 0.0
            if probe_result.returncode == 0:
                probe_data = json.loads(probe_result.stdout)
                new_duration = float(probe_data.get("format", {}).get("duration", 0))
            
            time_saved = total_duration - new_duration
            
            if status:
                await status.end(
                    f"Compressed {len(silences_to_compress)} silences, saved {time_saved:.1f}s "
                    f"({total_duration:.1f}s -> {new_duration:.1f}s)"
                )

            return {
                "status": "success",
                "compressed_count": len(silences_to_compress),
                "time_saved": time_saved,
                "original_duration": total_duration,
                "new_duration": new_duration,
                "source_file": source_file,
                "dest_file": dest_file
            }

        except AudioOpsError as e:
            if status:
                await status.error(str(e), meta={"error_type": e.error_type, **e.details})
            return {
                "status": "error",
                "error": str(e),
                "error_type": e.error_type,
                "details": e.details
            }
        except Exception as e:
            logger.exception(f"Unexpected error compressing silence in {source_file}")
            if status:
                await status.error(str(e), meta={"error_type": "UnexpectedError"})
            return {
                "status": "error",
                "error": str(e),
                "error_type": "UnexpectedError"
            }


# Plugin factory for dynamic loading
PLUGIN_FACTORY = AudioOpsServer
