"""Audio Operations MCP Server implementation."""

from __future__ import annotations

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
        
        # Storage path configuration
        self.storage_path = Path(getattr(mcp_config, 'storage_path', "data/audio_ops"))
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"AudioOpsServer initialized: storage_path={self.storage_path}")
    
    def _validate_path(self, filename: str) -> Path:
        """Validate and resolve file path within storage directory.
        
        Args:
            filename: Filename to validate
            
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
        
        # Prevent path traversal
        if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
            raise AudioOpsError(
                f"Invalid filename: {filename}. Path traversal not allowed.",
                error_type="SecurityError",
                details={"file": filename, "reason": "path_traversal_attempt"}
            )
        
        resolved = (self.storage_path / filename).resolve()
        
        # Ensure still within storage path
        try:
            resolved.relative_to(self.storage_path.resolve())
        except ValueError:
            raise AudioOpsError(
                f"File must be within storage directory: {filename}",
                error_type="SecurityError",
                details={"file": filename, "storage_path": str(self.storage_path)}
            )
        
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
    
    def _load_audio(self, filepath: Path):
        """Load audio file using pydub.
        
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
            return AudioSegment.from_file(str(filepath), format=fmt)
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
            
            # Validate paths
            source_path = self._validate_path(source_file)
            dest_path = self._validate_path(dest_file)
            
            # Validate destination format
            dest_format = self._validate_format(dest_path)
            
            if status:
                await status.progress(f"Loading: {source_file}")
            
            # Load audio
            audio = self._load_audio(source_path)
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
            result_audio.export(str(dest_path), format=dest_format)
            
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
            
            # Validate destination path and format
            dest_path = self._validate_path(dest_file)
            dest_format = self._validate_format(dest_path)
            
            if status:
                await status.progress(f"Loading {len(source_files)} audio files...")
            
            # Load all audio files
            segments = []
            total_source_duration = 0.0
            for i, filename in enumerate(source_files):
                source_path = self._validate_path(filename)
                if not source_path.exists():
                    raise AudioOpsError(
                        f"Source file not found: {filename}",
                        error_type="FileNotFoundError",
                        details={"file": filename, "index": i}
                    )
                audio = self._load_audio(source_path)
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
            result.export(str(dest_path), format=dest_format)
            
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
            
            filepath = self._validate_path(filename)
            
            if not filepath.exists():
                raise AudioOpsError(
                    f"File not found: {filename}",
                    error_type="FileNotFoundError",
                    details={"file": filename, "storage_path": str(self.storage_path)}
                )
            
            if status:
                await status.progress(f"Reading: {filename}")
            
            audio = self._load_audio(filepath)
            
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
            
            if status:
                await status.progress(f"Scanning: {self.storage_path}")
            
            files = []
            
            if pattern:
                # Use glob pattern
                matching_files = list(self.storage_path.glob(pattern))
            else:
                # List all supported audio files
                matching_files = []
                for ext in SUPPORTED_FORMATS:
                    matching_files.extend(self.storage_path.glob(f"*{ext}"))
            
            for filepath in sorted(matching_files):
                if filepath.is_file() and filepath.suffix.lower() in SUPPORTED_FORMATS:
                    try:
                        audio = self._load_audio(filepath)
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
                "storage_path": str(self.storage_path),
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


# Plugin factory for dynamic loading
PLUGIN_FACTORY = AudioOpsServer
