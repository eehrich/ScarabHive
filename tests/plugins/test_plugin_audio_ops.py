"""Tests for the audio_ops plugin.

Test coverage:
- Path validation (traversal attacks, missing files)
- Format validation (supported vs unsupported)
- Cut operation (time ranges, edge cases)
- Info retrieval
- List operation (patterns, empty dirs)
"""
from __future__ import annotations

import warnings
import pytest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, AsyncMock

if TYPE_CHECKING:
    from plugins.audio_ops.server import AudioOpsServer

# Suppress audioop deprecation warning for pydub (Python 3.12+)
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydub")

# Check if pydub is available
try:
    from pydub import AudioSegment
    HAS_PYDUB = True
except ImportError:
    HAS_PYDUB = False
    AudioSegment = None

pytestmark = pytest.mark.skipif(not HAS_PYDUB, reason="pydub not installed")


@pytest.fixture
def temp_storage(tmp_path: Path) -> Path:
    """Create temporary storage directory."""
    storage = tmp_path / "audio_ops"
    storage.mkdir()
    return storage


@pytest.fixture
def mock_system_config() -> MagicMock:
    """Mock AgentSystemConfig."""
    config = MagicMock()
    config.debug = False
    return config


@pytest.fixture
def mock_mcp_config(temp_storage: Path) -> MagicMock:
    """Mock MCPConfig with storage_path."""
    config = MagicMock()
    config.storage_path = str(temp_storage)
    return config


@pytest.fixture
def mock_status() -> MagicMock:
    """Mock status scope."""
    status = MagicMock()
    status.progress = AsyncMock()
    status.end = AsyncMock()
    status.error = AsyncMock()
    return status


@pytest.fixture
def server(mock_system_config: MagicMock, mock_mcp_config: MagicMock) -> "AudioOpsServer":
    """Create AudioOpsServer instance."""
    from plugins.audio_ops.server import AudioOpsServer
    return AudioOpsServer("audio_ops", mock_system_config, mock_mcp_config)


@pytest.fixture
def sample_wav(temp_storage: Path) -> Path:
    """Create a sample WAV file for testing."""
    if not HAS_PYDUB:
        pytest.skip("pydub not available")
    
    # Create 5 second silent audio
    audio = AudioSegment.silent(duration=5000, frame_rate=44100)
    filepath = temp_storage / "test.wav"
    audio.export(str(filepath), format="wav")
    return filepath


@pytest.fixture
def sample_mp3(temp_storage: Path) -> Path:
    """Create a sample MP3 file for testing."""
    if not HAS_PYDUB:
        pytest.skip("pydub not available")
    
    # Create 10 second silent audio
    audio = AudioSegment.silent(duration=10000, frame_rate=44100)
    filepath = temp_storage / "test.mp3"
    audio.export(str(filepath), format="mp3")
    return filepath


class TestPathValidation:
    """Test path validation and security."""
    
    @pytest.mark.asyncio
    async def test_path_traversal_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Path traversal attempts should be rejected."""
        result = await server.info({
            "file": "../../../etc/passwd",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "SecurityError"
    
    @pytest.mark.asyncio
    async def test_absolute_path_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Absolute paths should be rejected."""
        result = await server.info({
            "file": "/etc/passwd",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "SecurityError"
    
    @pytest.mark.asyncio
    async def test_missing_file(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Missing files should return file_not_found error."""
        result = await server.info({
            "file": "nonexistent.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "FileNotFoundError"
    
    @pytest.mark.asyncio
    async def test_path_with_storage_prefix_normalized(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path, temp_storage: Path
    ) -> None:
        """Paths containing the storage path prefix should be normalized to just filename.
        
        This handles the case where ComfyUI returns full paths like 'data/audio/temp/file.wav'
        and audio_ops needs to strip the storage path prefix.
        """
        # Create full path string as ComfyUI would return it
        full_path = f"{temp_storage}/{sample_wav.name}"
        
        result = await server.info({
            "file": full_path,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["format"] == "wav"
    
    @pytest.mark.asyncio
    async def test_path_with_windows_separators_normalized(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path, temp_storage: Path
    ) -> None:
        """Windows-style paths should be normalized to forward slashes."""
        # Create Windows-style path string
        full_path = f"{temp_storage}\\{sample_wav.name}".replace("/", "\\")
        
        result = await server.info({
            "file": full_path,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["format"] == "wav"
    
    @pytest.mark.asyncio
    async def test_filename_only_still_works(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Just the filename (without path) should still work."""
        result = await server.info({
            "file": sample_wav.name,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["format"] == "wav"


class TestFormatValidation:
    """Test audio format validation."""
    
    @pytest.mark.asyncio
    async def test_unsupported_format_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Unsupported formats should be rejected."""
        # Create a dummy .txt file
        txt_file = temp_storage / "test.txt"
        txt_file.write_text("not audio")
        
        result = await server.info({
            "file": "test.txt",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "FormatError"
    
    @pytest.mark.asyncio
    async def test_wav_format_accepted(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """WAV format should be accepted."""
        result = await server.info({
            "file": sample_wav.name,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["format"] == "wav"
    
    @pytest.mark.asyncio
    async def test_mp3_format_accepted(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_mp3: Path
    ) -> None:
        """MP3 format should be accepted."""
        result = await server.info({
            "file": sample_mp3.name,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["format"] == "mp3"


class TestInfoTool:
    """Test the info tool."""
    
    @pytest.mark.asyncio
    async def test_info_returns_metadata(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Info should return audio metadata."""
        result = await server.info({
            "file": sample_wav.name,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert "duration_seconds" in result
        assert "channels" in result
        assert "sample_rate" in result
        assert "sample_width_bytes" in result
        assert "size_bytes" in result
        
        # Check values are reasonable
        assert result["duration_seconds"] == pytest.approx(5.0, rel=0.1)
        assert result["channels"] >= 1
        assert result["sample_rate"] > 0


class TestCreateTool:
    """Test the create tool."""
    
    @pytest.mark.asyncio
    async def test_create_basic(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Create should generate a silent audio file."""
        result = await server.create({
            "dest_file": "silence.wav",
            "duration_ms": 1000,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["destination"] == "silence.wav"
        assert result["duration_seconds"] == 1.0
        assert result["duration_ms"] == 1000
        assert result["format"] == "wav"
        assert result["channels"] == 2  # Default
        assert result["sample_rate"] == 44100  # Default
        
        # Verify file exists
        output_path = temp_storage / "silence.wav"
        assert output_path.exists()
        
        # Verify it's actually silent audio
        from pydub import AudioSegment
        audio = AudioSegment.from_file(str(output_path))
        assert len(audio) == pytest.approx(1000, abs=10)  # ~1000ms
    
    @pytest.mark.asyncio
    async def test_create_custom_settings(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Create with custom sample rate and channels should work."""
        result = await server.create({
            "dest_file": "mono_48k.wav",
            "duration_ms": 500,
            "sample_rate": 48000,
            "channels": 1,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["channels"] == 1
        assert result["sample_rate"] == 48000
        assert result["duration_ms"] == 500
        
        # Verify file properties
        output_path = temp_storage / "mono_48k.wav"
        from pydub import AudioSegment
        audio = AudioSegment.from_file(str(output_path))
        assert audio.channels == 1
        assert audio.frame_rate == 48000
    
    @pytest.mark.asyncio
    async def test_create_different_formats(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Create should support different output formats."""
        formats = ["silence.wav", "silence.mp3", "silence.flac"]
        
        for filename in formats:
            result = await server.create({
                "dest_file": filename,
                "duration_ms": 100,
                "_status": mock_status,
            })
            
            assert result["status"] == "success"
            assert (temp_storage / filename).exists()
    
    @pytest.mark.asyncio
    async def test_create_missing_dest_file(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Create should reject missing dest_file."""
        result = await server.create({
            "duration_ms": 1000,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert "dest_file is required" in result["error"]
    
    @pytest.mark.asyncio
    async def test_create_missing_duration(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Create should reject missing duration_ms."""
        result = await server.create({
            "dest_file": "test.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert "duration_ms is required" in result["error"]
    
    @pytest.mark.asyncio
    async def test_create_negative_duration(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Create should reject negative duration."""
        result = await server.create({
            "dest_file": "test.wav",
            "duration_ms": -500,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert "must be positive" in result["error"]
    
    @pytest.mark.asyncio
    async def test_create_zero_duration(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Create should reject zero duration."""
        result = await server.create({
            "dest_file": "test.wav",
            "duration_ms": 0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert "must be positive" in result["error"]
    
    @pytest.mark.asyncio
    async def test_create_invalid_sample_rate(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Create should reject invalid sample rates."""
        result = await server.create({
            "dest_file": "test.wav",
            "duration_ms": 1000,
            "sample_rate": 12345,  # Invalid
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert "Invalid sample_rate" in result["error"]
    
    @pytest.mark.asyncio
    async def test_create_invalid_channels(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Create should reject invalid channel counts."""
        result = await server.create({
            "dest_file": "test.wav",
            "duration_ms": 1000,
            "channels": 5,  # Invalid
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert "Invalid channels" in result["error"]
    
    @pytest.mark.asyncio
    async def test_create_long_duration(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Create should handle longer durations."""
        result = await server.create({
            "dest_file": "long_silence.wav",
            "duration_ms": 30000,  # 30 seconds
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["duration_seconds"] == 30.0
        
        output_path = temp_storage / "long_silence.wav"
        from pydub import AudioSegment
        audio = AudioSegment.from_file(str(output_path))
        assert len(audio) == pytest.approx(30000, abs=50)


class TestCutTool:
    """Test the cut tool."""
    
    @pytest.mark.asyncio
    async def test_cut_basic(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path, temp_storage: Path
    ) -> None:
        """Basic cut operation should work."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "output.wav",
            "start_time": 1.0,
            "end_time": 3.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["mode"] == "extract"
        assert result["result_duration_seconds"] == pytest.approx(2.0, rel=0.1)
        
        # Verify output file exists
        output_path = temp_storage / "output.wav"
        assert output_path.exists()
    
    @pytest.mark.asyncio
    async def test_cut_extract_mode_explicit(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path, temp_storage: Path
    ) -> None:
        """Extract mode should copy the segment."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "extracted.wav",
            "start_time": 1.0,
            "end_time": 4.0,
            "mode": "extract",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["mode"] == "extract"
        # Source is 5s, we extract 3s (1s to 4s)
        assert result["result_duration_seconds"] == pytest.approx(3.0, rel=0.1)
    
    @pytest.mark.asyncio
    async def test_cut_remove_mode(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path, temp_storage: Path
    ) -> None:
        """Remove mode should delete the segment and keep the rest."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "trimmed.wav",
            "start_time": 1.0,
            "end_time": 3.0,
            "mode": "remove",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["mode"] == "remove"
        # Source is 5s, we remove 2s (1s to 3s), result should be 3s
        assert result["source_duration_seconds"] == pytest.approx(5.0, rel=0.1)
        assert result["result_duration_seconds"] == pytest.approx(3.0, rel=0.1)
        
        # Verify output file exists
        output_path = temp_storage / "trimmed.wav"
        assert output_path.exists()
    
    @pytest.mark.asyncio
    async def test_cut_invalid_mode_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Invalid mode should be rejected."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "output.wav",
            "start_time": 1.0,
            "end_time": 3.0,
            "mode": "invalid",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_cut_format_conversion(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path, temp_storage: Path
    ) -> None:
        """Cut should support format conversion."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "output.mp3",
            "start_time": 0.0,
            "end_time": 2.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        
        output_path = temp_storage / "output.mp3"
        assert output_path.exists()
    
    @pytest.mark.asyncio
    async def test_cut_negative_start_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Negative start time should be rejected."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "output.wav",
            "start_time": -1.0,
            "end_time": 2.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_cut_end_before_start_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """End time before start should be rejected."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "output.wav",
            "start_time": 3.0,
            "end_time": 1.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_cut_start_exceeds_duration(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Start time beyond duration should be rejected."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "output.wav",
            "start_time": 100.0,  # Way beyond 5s duration
            "end_time": 101.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "TimeRangeError"
    
    @pytest.mark.asyncio
    async def test_cut_end_exceeds_duration(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """End time beyond duration should be rejected."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "output.wav",
            "start_time": 1.0,
            "end_time": 100.0,  # Way beyond 5s duration
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "TimeRangeError"
    
    @pytest.mark.asyncio
    async def test_cut_missing_source(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Cut with missing source should fail."""
        result = await server.cut({
            "source_file": "nonexistent.wav",
            "dest_file": "output.wav",
            "start_time": 0.0,
            "end_time": 1.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "FileNotFoundError"


class TestListTool:
    """Test the list tool."""
    
    @pytest.mark.asyncio
    async def test_list_empty_directory(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """List on empty directory should return empty list."""
        result = await server.list({
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["files"] == []
    
    @pytest.mark.asyncio
    async def test_list_with_files(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path, sample_mp3: Path
    ) -> None:
        """List should find audio files."""
        result = await server.list({
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert len(result["files"]) == 2
        
        filenames = [f["name"] for f in result["files"]]
        assert sample_wav.name in filenames
        assert sample_mp3.name in filenames
    
    @pytest.mark.asyncio
    async def test_list_with_pattern(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path, sample_mp3: Path
    ) -> None:
        """List with pattern should filter files."""
        result = await server.list({
            "pattern": "*.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        
        filenames = [f["name"] for f in result["files"]]
        assert sample_wav.name in filenames
        assert sample_mp3.name not in filenames
    
    @pytest.mark.asyncio
    async def test_list_includes_metadata(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """List should include file metadata."""
        result = await server.list({
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert len(result["files"]) >= 1
        
        file_info = result["files"][0]
        assert "name" in file_info
        assert "duration_seconds" in file_info
        assert "size_bytes" in file_info


class TestStatusMessages:
    """Test that status scope messages are sent correctly."""
    
    @pytest.mark.asyncio
    async def test_info_sends_progress(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Info should send progress message."""
        await server.info({
            "file": sample_wav.name,
            "_status": mock_status,
        })
        
        mock_status.progress.assert_called()
    
    @pytest.mark.asyncio
    async def test_cut_sends_progress(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Cut should send progress messages."""
        await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "output.wav",
            "start_time": 0.0,
            "end_time": 2.0,
            "_status": mock_status,
        })
        
        # Should have multiple progress calls
        assert mock_status.progress.call_count >= 1
    
    @pytest.mark.asyncio
    async def test_error_sends_error_status(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Errors should send error status."""
        await server.info({
            "file": "nonexistent.wav",
            "_status": mock_status,
        })
        
        mock_status.error.assert_called()


class TestMergeTool:
    """Tests for merge tool."""
    
    @pytest.mark.asyncio
    async def test_merge_two_files(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Merging two files should concatenate them."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        # Create two source files
        audio1 = AudioSegment.silent(duration=2000)  # 2 seconds
        audio2 = AudioSegment.silent(duration=3000)  # 3 seconds
        
        file1 = temp_storage / "part1.wav"
        file2 = temp_storage / "part2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.merge({
            "source_files": ["part1.wav", "part2.wav"],
            "dest_file": "merged.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["source_count"] == 2
        # Combined duration should be ~5 seconds
        assert result["result_duration_seconds"] == pytest.approx(5.0, rel=0.1)
        
        # Verify output file exists
        output = temp_storage / "merged.wav"
        assert output.exists()
    
    @pytest.mark.asyncio
    async def test_merge_multiple_files(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Merging multiple files should work."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        # Create three source files
        files = []
        for i in range(3):
            audio = AudioSegment.silent(duration=1000)  # 1 second each
            filepath = temp_storage / f"segment{i}.wav"
            audio.export(str(filepath), format="wav")
            files.append(f"segment{i}.wav")
        
        result = await server.merge({
            "source_files": files,
            "dest_file": "all_merged.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["source_count"] == 3
        assert result["result_duration_seconds"] == pytest.approx(3.0, rel=0.1)
    
    @pytest.mark.asyncio
    async def test_merge_with_crossfade(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Merging with crossfade should reduce total duration."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        # Create two source files
        audio1 = AudioSegment.silent(duration=2000)  # 2 seconds
        audio2 = AudioSegment.silent(duration=2000)  # 2 seconds
        
        file1 = temp_storage / "fade1.wav"
        file2 = temp_storage / "fade2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.merge({
            "source_files": ["fade1.wav", "fade2.wav"],
            "dest_file": "crossfaded.wav",
            "crossfade_ms": 500,  # 500ms crossfade
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["source_count"] == 2
        # Duration should be less than 4 seconds due to crossfade
        # Expected: 2 + 2 - 0.5 = 3.5 seconds
        assert result["result_duration_seconds"] == pytest.approx(3.5, rel=0.1)
    
    @pytest.mark.asyncio
    async def test_merge_single_file_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Merging single file should be rejected (need at least 2)."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=2000)
        filepath = temp_storage / "single.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.merge({
            "source_files": ["single.wav"],
            "dest_file": "single_merged.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert "at least 2" in result["error"]
    
    @pytest.mark.asyncio
    async def test_merge_empty_list_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Merging empty list should fail."""
        result = await server.merge({
            "source_files": [],
            "dest_file": "empty.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert "source_files" in result["error"].lower() or "empty" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_merge_nonexistent_file_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Merging nonexistent file should fail."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        # Create one valid file
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "exists.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.merge({
            "source_files": ["exists.wav", "does_not_exist.wav"],
            "dest_file": "merged.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert "not found" in result["error"].lower() or "does_not_exist" in result["error"]
    
    @pytest.mark.asyncio
    async def test_merge_different_formats(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Merging different formats should work (pydub handles conversion)."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        # Create wav and mp3 files
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "source.wav"
        file2 = temp_storage / "source.mp3"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="mp3")
        
        result = await server.merge({
            "source_files": ["source.wav", "source.mp3"],
            "dest_file": "mixed_formats.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["source_count"] == 2


class TestLoadTool:
    """Tests for load tool."""
    
    @pytest.mark.asyncio
    async def test_load_full_file(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Loading full file should return multimodal content."""
        result = await server.load({
            "file": sample_wav.name,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["file"] == sample_wav.name
        assert result["duration_seconds"] == pytest.approx(5.0, rel=0.1)
        assert result["format"] == "wav"
        assert "_multimodal_content" in result
        
        multimodal = result["_multimodal_content"]
        assert len(multimodal) == 1
        assert multimodal[0]["type"] == "audio"
        assert multimodal[0]["mime_type"] == "audio/wav"
    
    @pytest.mark.asyncio
    async def test_load_segment(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path, temp_storage: Path
    ) -> None:
        """Loading a segment should extract and return it."""
        result = await server.load({
            "file": sample_wav.name,
            "start_time": 1.0,
            "end_time": 3.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["file"] == sample_wav.name
        assert "segment" in result
        assert result["segment"]["start_time"] == 1.0
        assert result["segment"]["end_time"] == 3.0
        assert result["segment"]["segment_duration_seconds"] == pytest.approx(2.0, rel=0.1)
        
        # Multimodal should point to segment file
        multimodal = result["_multimodal_content"]
        assert "_temp_segment_" in multimodal[0]["path"]
    
    @pytest.mark.asyncio
    async def test_load_with_only_start_time(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Loading with only start time should load from start to end."""
        result = await server.load({
            "file": sample_wav.name,
            "start_time": 2.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["segment"]["start_time"] == 2.0
        assert result["segment"]["end_time"] == pytest.approx(5.0, rel=0.1)  # Full duration
        assert result["segment"]["segment_duration_seconds"] == pytest.approx(3.0, rel=0.1)
    
    @pytest.mark.asyncio
    async def test_load_with_only_end_time(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Loading with only end time should load from 0 to end."""
        result = await server.load({
            "file": sample_wav.name,
            "end_time": 2.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["segment"]["start_time"] == 0.0
        assert result["segment"]["end_time"] == 2.0
        assert result["segment"]["segment_duration_seconds"] == pytest.approx(2.0, rel=0.1)
    
    @pytest.mark.asyncio
    async def test_load_nonexistent_file(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Loading nonexistent file should fail."""
        result = await server.load({
            "file": "does_not_exist.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "FileNotFoundError"
    
    @pytest.mark.asyncio
    async def test_load_invalid_time_range(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Loading with end before start should fail."""
        result = await server.load({
            "file": sample_wav.name,
            "start_time": 3.0,
            "end_time": 1.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_load_start_beyond_duration(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Loading with start beyond duration should fail."""
        result = await server.load({
            "file": sample_wav.name,
            "start_time": 100.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "TimeRangeError"
    
    @pytest.mark.asyncio
    async def test_load_mp3(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_mp3: Path
    ) -> None:
        """Loading MP3 should work and return correct mime type."""
        result = await server.load({
            "file": sample_mp3.name,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["format"] == "mp3"
        assert result["mime_type"] == "audio/mpeg"


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    @pytest.mark.asyncio
    async def test_cut_exact_duration(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Cut with exact duration bounds should work."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "full_copy.wav",
            "start_time": 0.0,
            "end_time": 5.0,  # Exact duration
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["result_duration_seconds"] == pytest.approx(5.0, rel=0.1)
    
    @pytest.mark.asyncio
    async def test_cut_very_small_segment(
        self, server: "AudioOpsServer", mock_status: MagicMock, sample_wav: Path
    ) -> None:
        """Cutting very small segment should work."""
        result = await server.cut({
            "source_file": sample_wav.name,
            "dest_file": "tiny.wav",
            "start_time": 1.0,
            "end_time": 1.1,  # 0.1 second
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["result_duration_seconds"] == pytest.approx(0.1, rel=0.2)
    
    @pytest.mark.asyncio
    async def test_special_characters_in_filename(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Files with spaces should work."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        # Create file with space in name
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "test file.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.info({
            "file": "test file.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"


class TestMixTool:
    """Tests for mix tool."""
    
    @pytest.mark.asyncio
    async def test_mix_equal_balance(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mixing two files with default factor (0.5) should produce equal mix."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        # Create two source files of same duration
        audio1 = AudioSegment.silent(duration=3000)  # 3 seconds
        audio2 = AudioSegment.silent(duration=3000)  # 3 seconds
        
        file1 = temp_storage / "track1.wav"
        file2 = temp_storage / "track2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "track1.wav",
            "file2": "track2.wav",
            "dest_file": "mixed.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["mix_factor"] == 0.5
        assert result["result_duration_seconds"] == pytest.approx(3.0, rel=0.1)
        
        # Verify output file exists
        output = temp_storage / "mixed.wav"
        assert output.exists()
    
    @pytest.mark.asyncio
    async def test_mix_custom_factor(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mixing with custom factor should work."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=2000)
        audio2 = AudioSegment.silent(duration=2000)
        
        file1 = temp_storage / "base.wav"
        file2 = temp_storage / "overlay.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "base.wav",
            "file2": "overlay.wav",
            "dest_file": "custom_mix.wav",
            "mix_factor": 0.3,  # 70% file1, 30% file2
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["mix_factor"] == 0.3
    
    @pytest.mark.asyncio
    async def test_mix_factor_zero(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mix factor 0.0 should output only file1."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=2000)
        audio2 = AudioSegment.silent(duration=2000)
        
        file1 = temp_storage / "only1.wav"
        file2 = temp_storage / "muted.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "only1.wav",
            "file2": "muted.wav",
            "dest_file": "factor_zero.wav",
            "mix_factor": 0.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["mix_factor"] == 0.0
    
    @pytest.mark.asyncio
    async def test_mix_factor_one(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mix factor 1.0 should output only file2."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=2000)
        audio2 = AudioSegment.silent(duration=2000)
        
        file1 = temp_storage / "muted1.wav"
        file2 = temp_storage / "only2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "muted1.wav",
            "file2": "only2.wav",
            "dest_file": "factor_one.wav",
            "mix_factor": 1.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["mix_factor"] == 1.0
    
    @pytest.mark.asyncio
    async def test_mix_different_durations(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mixing files with different durations should pad shorter one."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=5000)  # 5 seconds
        audio2 = AudioSegment.silent(duration=3000)  # 3 seconds
        
        file1 = temp_storage / "longer.wav"
        file2 = temp_storage / "shorter.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "longer.wav",
            "file2": "shorter.wav",
            "dest_file": "diff_duration.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        # Result should match longer file's duration
        assert result["result_duration_seconds"] == pytest.approx(5.0, rel=0.1)
        assert result["file1_duration_seconds"] == pytest.approx(5.0, rel=0.1)
        assert result["file2_duration_seconds"] == pytest.approx(3.0, rel=0.1)
    
    @pytest.mark.asyncio
    async def test_mix_different_formats(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mixing WAV and MP3 should work."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=2000)
        audio2 = AudioSegment.silent(duration=2000)
        
        file1 = temp_storage / "source.wav"
        file2 = temp_storage / "source.mp3"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="mp3")
        
        result = await server.mix({
            "file1": "source.wav",
            "file2": "source.mp3",
            "dest_file": "mixed_formats.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
    
    @pytest.mark.asyncio
    async def test_mix_format_conversion(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mix output can be different format than inputs."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "in1.wav"
        file2 = temp_storage / "in2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "in1.wav",
            "file2": "in2.wav",
            "dest_file": "output.mp3",
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["format"] == "mp3"
        
        output = temp_storage / "output.mp3"
        assert output.exists()
    
    @pytest.mark.asyncio
    async def test_mix_missing_file1(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mix with missing file1 should fail."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        # Create only file2
        audio2 = AudioSegment.silent(duration=1000)
        file2 = temp_storage / "exists.wav"
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "nonexistent.wav",
            "file2": "exists.wav",
            "dest_file": "mixed.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "FileNotFoundError"
    
    @pytest.mark.asyncio
    async def test_mix_missing_file2(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mix with missing file2 should fail."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        # Create only file1
        audio1 = AudioSegment.silent(duration=1000)
        file1 = temp_storage / "exists.wav"
        audio1.export(str(file1), format="wav")
        
        result = await server.mix({
            "file1": "exists.wav",
            "file2": "nonexistent.wav",
            "dest_file": "mixed.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "FileNotFoundError"
    
    @pytest.mark.asyncio
    async def test_mix_invalid_factor_negative(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mix factor below 0 should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "f1.wav"
        file2 = temp_storage / "f2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "f1.wav",
            "file2": "f2.wav",
            "dest_file": "mixed.wav",
            "mix_factor": -0.5,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_mix_invalid_factor_above_one(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mix factor above 1.0 should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "a1.wav"
        file2 = temp_storage / "a2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "a1.wav",
            "file2": "a2.wav",
            "dest_file": "mixed.wav",
            "mix_factor": 1.5,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_mix_invalid_factor_string(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mix factor as non-numeric string should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "x1.wav"
        file2 = temp_storage / "x2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "x1.wav",
            "file2": "x2.wav",
            "dest_file": "mixed.wav",
            "mix_factor": "invalid",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_mix_missing_required_params(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Mix without required parameters should fail."""
        # Missing file1
        result = await server.mix({
            "file2": "file.wav",
            "dest_file": "out.wav",
            "_status": mock_status,
        })
        assert result["status"] == "error"
        assert "file1" in result["error"].lower()
        
        # Missing file2
        result = await server.mix({
            "file1": "file.wav",
            "dest_file": "out.wav",
            "_status": mock_status,
        })
        assert result["status"] == "error"
        assert "file2" in result["error"].lower()
        
        # Missing dest_file
        result = await server.mix({
            "file1": "file1.wav",
            "file2": "file2.wav",
            "_status": mock_status,
        })
        assert result["status"] == "error"
        assert "dest_file" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_mix_sends_status_updates(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Mix should send status progress and end messages."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "s1.wav"
        file2 = temp_storage / "s2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        await server.mix({
            "file1": "s1.wav",
            "file2": "s2.wav",
            "dest_file": "status_test.wav",
            "_status": mock_status,
        })
        
        # Should have progress and end calls
        assert mock_status.progress.call_count >= 1
        mock_status.end.assert_called_once()


class TestMixEnvelope:
    """Tests for mix tool with envelope (dynamic mixing curve)."""
    
    @pytest.mark.asyncio
    async def test_mix_envelope_basic(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Basic envelope mixing should work."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=3000)  # 3 seconds
        audio2 = AudioSegment.silent(duration=3000)
        
        file1 = temp_storage / "env1.wav"
        file2 = temp_storage / "env2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        # Simple crossfade envelope
        envelope = [
            {"time": 0.0, "factor": 0.0},   # Start with file1
            {"time": 3.0, "factor": 1.0}    # End with file2
        ]
        
        result = await server.mix({
            "file1": "env1.wav",
            "file2": "env2.wav",
            "dest_file": "envelope_mix.wav",
            "envelope": envelope,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert "envelope" in result
        assert result["envelope_points"] == 2
        assert result["result_duration_seconds"] == pytest.approx(3.0, rel=0.1)
    
    @pytest.mark.asyncio
    async def test_mix_envelope_multi_point(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Multi-point envelope for complex transitions."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=5000)  # 5 seconds
        audio2 = AudioSegment.silent(duration=5000)
        
        file1 = temp_storage / "multi1.wav"
        file2 = temp_storage / "multi2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        # Complex envelope: file1 -> file2 -> file1
        envelope = [
            {"time": 0.0, "factor": 0.0},   # file1 only
            {"time": 2.0, "factor": 1.0},   # file2 only
            {"time": 3.0, "factor": 1.0},   # stay on file2
            {"time": 5.0, "factor": 0.0}    # back to file1
        ]
        
        result = await server.mix({
            "file1": "multi1.wav",
            "file2": "multi2.wav",
            "dest_file": "multi_point.wav",
            "envelope": envelope,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["envelope_points"] == 4
    
    @pytest.mark.asyncio
    async def test_mix_envelope_unsorted_points(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope points should be sorted automatically."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=2000)
        audio2 = AudioSegment.silent(duration=2000)
        
        file1 = temp_storage / "unsort1.wav"
        file2 = temp_storage / "unsort2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        # Points not in time order
        envelope = [
            {"time": 2.0, "factor": 1.0},
            {"time": 0.0, "factor": 0.0},
            {"time": 1.0, "factor": 0.5}
        ]
        
        result = await server.mix({
            "file1": "unsort1.wav",
            "file2": "unsort2.wav",
            "dest_file": "sorted.wav",
            "envelope": envelope,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        # Verify envelope was sorted
        assert result["envelope"][0]["time"] == 0.0
        assert result["envelope"][1]["time"] == 1.0
        assert result["envelope"][2]["time"] == 2.0
    
    @pytest.mark.asyncio
    async def test_mix_envelope_single_point_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope with less than 2 points should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "single1.wav"
        file2 = temp_storage / "single2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "single1.wav",
            "file2": "single2.wav",
            "dest_file": "single.wav",
            "envelope": [{"time": 0.0, "factor": 0.5}],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
        assert "at least 2" in result["error"]
    
    @pytest.mark.asyncio
    async def test_mix_envelope_empty_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Empty envelope should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "empty1.wav"
        file2 = temp_storage / "empty2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "empty1.wav",
            "file2": "empty2.wav",
            "dest_file": "empty.wav",
            "envelope": [],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_mix_envelope_invalid_factor(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope with invalid factor should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "badfac1.wav"
        file2 = temp_storage / "badfac2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        # Factor out of range
        result = await server.mix({
            "file1": "badfac1.wav",
            "file2": "badfac2.wav",
            "dest_file": "badfac.wav",
            "envelope": [
                {"time": 0.0, "factor": 0.0},
                {"time": 1.0, "factor": 1.5}  # Invalid: > 1.0
            ],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_mix_envelope_negative_time(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope with negative time should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "negtime1.wav"
        file2 = temp_storage / "negtime2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "negtime1.wav",
            "file2": "negtime2.wav",
            "dest_file": "negtime.wav",
            "envelope": [
                {"time": -1.0, "factor": 0.0},  # Invalid: negative
                {"time": 1.0, "factor": 1.0}
            ],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_mix_envelope_missing_time(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope point missing 'time' should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "notime1.wav"
        file2 = temp_storage / "notime2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "notime1.wav",
            "file2": "notime2.wav",
            "dest_file": "notime.wav",
            "envelope": [
                {"factor": 0.0},  # Missing time
                {"time": 1.0, "factor": 1.0}
            ],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
        assert "time" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_mix_envelope_missing_factor(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope point missing 'factor' should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "nofac1.wav"
        file2 = temp_storage / "nofac2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "nofac1.wav",
            "file2": "nofac2.wav",
            "dest_file": "nofac.wav",
            "envelope": [
                {"time": 0.0},  # Missing factor
                {"time": 1.0, "factor": 1.0}
            ],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
        assert "factor" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_mix_envelope_and_factor_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Specifying both envelope and mix_factor should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "both1.wav"
        file2 = temp_storage / "both2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "both1.wav",
            "file2": "both2.wav",
            "dest_file": "both.wav",
            "mix_factor": 0.5,
            "envelope": [
                {"time": 0.0, "factor": 0.0},
                {"time": 1.0, "factor": 1.0}
            ],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
        assert "both" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_mix_envelope_not_list_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope that is not a list should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=1000)
        audio2 = AudioSegment.silent(duration=1000)
        
        file1 = temp_storage / "notlist1.wav"
        file2 = temp_storage / "notlist2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        result = await server.mix({
            "file1": "notlist1.wav",
            "file2": "notlist2.wav",
            "dest_file": "notlist.wav",
            "envelope": {"time": 0.0, "factor": 0.5},  # Dict instead of list
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_mix_envelope_different_durations(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope with different duration files should work."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio1 = AudioSegment.silent(duration=4000)  # 4 seconds
        audio2 = AudioSegment.silent(duration=2000)  # 2 seconds
        
        file1 = temp_storage / "diff1.wav"
        file2 = temp_storage / "diff2.wav"
        audio1.export(str(file1), format="wav")
        audio2.export(str(file2), format="wav")
        
        envelope = [
            {"time": 0.0, "factor": 0.0},
            {"time": 2.0, "factor": 0.5},
            {"time": 4.0, "factor": 1.0}
        ]
        
        result = await server.mix({
            "file1": "diff1.wav",
            "file2": "diff2.wav",
            "dest_file": "diff_dur.wav",
            "envelope": envelope,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        # Result duration should match longer file
        assert result["result_duration_seconds"] == pytest.approx(4.0, rel=0.1)


class TestVolumeTool:
    """Tests for volume adjustment tool."""
    
    @pytest.mark.asyncio
    async def test_volume_gain_positive(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Positive gain should increase volume."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=2000)
        filepath = temp_storage / "source.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "source.wav",
            "dest_file": "louder.wav",
            "gain_db": 6.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["gain_db"] == 6.0
        assert (temp_storage / "louder.wav").exists()
    
    @pytest.mark.asyncio
    async def test_volume_gain_negative(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Negative gain should decrease volume."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=2000)
        filepath = temp_storage / "source.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "source.wav",
            "dest_file": "quieter.wav",
            "gain_db": -12.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["gain_db"] == -12.0
    
    @pytest.mark.asyncio
    async def test_volume_gain_zero(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Zero gain should make no change."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "unchanged.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "unchanged.wav",
            "dest_file": "same.wav",
            "gain_db": 0.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["gain_db"] == 0.0
    
    @pytest.mark.asyncio
    async def test_volume_with_normalize(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Normalize option should work."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=2000)
        filepath = temp_storage / "norm_source.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "norm_source.wav",
            "dest_file": "normalized.wav",
            "gain_db": -6.0,
            "normalize": True,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["normalized"] is True
    
    @pytest.mark.asyncio
    async def test_volume_format_conversion(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Volume tool should support format conversion."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "convert.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "convert.wav",
            "dest_file": "converted.mp3",
            "gain_db": 3.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["format"] == "mp3"
        assert (temp_storage / "converted.mp3").exists()
    
    @pytest.mark.asyncio
    async def test_volume_missing_source(
        self, server: "AudioOpsServer", mock_status: MagicMock
    ) -> None:
        """Missing source file should fail."""
        result = await server.volume({
            "source_file": "nonexistent.wav",
            "dest_file": "output.wav",
            "gain_db": 0.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "FileNotFoundError"
    
    @pytest.mark.asyncio
    async def test_volume_missing_gain_and_envelope(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Must provide either gain_db or envelope."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "nogain.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "nogain.wav",
            "dest_file": "output.wav",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_volume_gain_out_of_range_high(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Gain above +24 dB should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "highgain.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "highgain.wav",
            "dest_file": "output.wav",
            "gain_db": 30.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_volume_gain_out_of_range_low(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Gain below -60 dB should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "lowgain.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "lowgain.wav",
            "dest_file": "output.wav",
            "gain_db": -70.0,
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_volume_gain_invalid_type(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Non-numeric gain should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "badgain.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "badgain.wav",
            "dest_file": "output.wav",
            "gain_db": "loud",
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"


class TestVolumeEnvelope:
    """Tests for volume tool with envelope."""
    
    @pytest.mark.asyncio
    async def test_volume_envelope_basic(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Basic volume envelope should work."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=3000)
        filepath = temp_storage / "env_source.wav"
        audio.export(str(filepath), format="wav")
        
        # Fade out envelope
        envelope = [
            {"time": 0.0, "gain_db": 0.0},
            {"time": 3.0, "gain_db": -40.0}
        ]
        
        result = await server.volume({
            "source_file": "env_source.wav",
            "dest_file": "faded.wav",
            "envelope": envelope,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert "envelope" in result
        assert result["envelope_points"] == 2
    
    @pytest.mark.asyncio
    async def test_volume_envelope_multi_point(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Multi-point volume envelope for complex automation."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=5000)
        filepath = temp_storage / "multi_env.wav"
        audio.export(str(filepath), format="wav")
        
        # Duck and recover
        envelope = [
            {"time": 0.0, "gain_db": 0.0},
            {"time": 1.0, "gain_db": -20.0},  # Duck
            {"time": 3.0, "gain_db": -20.0},  # Hold
            {"time": 4.0, "gain_db": 0.0}     # Recover
        ]
        
        result = await server.volume({
            "source_file": "multi_env.wav",
            "dest_file": "ducked.wav",
            "envelope": envelope,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["envelope_points"] == 4
    
    @pytest.mark.asyncio
    async def test_volume_envelope_with_normalize(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope with normalization should work."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=2000)
        filepath = temp_storage / "env_norm.wav"
        audio.export(str(filepath), format="wav")
        
        envelope = [
            {"time": 0.0, "gain_db": -6.0},
            {"time": 2.0, "gain_db": 6.0}
        ]
        
        result = await server.volume({
            "source_file": "env_norm.wav",
            "dest_file": "env_normalized.wav",
            "envelope": envelope,
            "normalize": True,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        assert result["normalized"] is True
    
    @pytest.mark.asyncio
    async def test_volume_envelope_single_point_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope with less than 2 points should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "single_env.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "single_env.wav",
            "dest_file": "output.wav",
            "envelope": [{"time": 0.0, "gain_db": 0.0}],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
        assert "at least 2" in result["error"]
    
    @pytest.mark.asyncio
    async def test_volume_envelope_invalid_gain(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope with gain out of range should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "bad_env.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "bad_env.wav",
            "dest_file": "output.wav",
            "envelope": [
                {"time": 0.0, "gain_db": 0.0},
                {"time": 1.0, "gain_db": 50.0}  # Too high
            ],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
    
    @pytest.mark.asyncio
    async def test_volume_envelope_missing_gain_db(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope point missing gain_db should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "missing_gain.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "missing_gain.wav",
            "dest_file": "output.wav",
            "envelope": [
                {"time": 0.0},  # Missing gain_db
                {"time": 1.0, "gain_db": 0.0}
            ],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
        assert "gain_db" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_volume_envelope_and_gain_rejected(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Specifying both envelope and gain_db should be rejected."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=1000)
        filepath = temp_storage / "both_vol.wav"
        audio.export(str(filepath), format="wav")
        
        result = await server.volume({
            "source_file": "both_vol.wav",
            "dest_file": "output.wav",
            "gain_db": 6.0,
            "envelope": [
                {"time": 0.0, "gain_db": 0.0},
                {"time": 1.0, "gain_db": 6.0}
            ],
            "_status": mock_status,
        })
        
        assert result["status"] == "error"
        assert result["error_type"] == "ValidationError"
        assert "both" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_volume_envelope_unsorted_points(
        self, server: "AudioOpsServer", mock_status: MagicMock, temp_storage: Path
    ) -> None:
        """Envelope points should be sorted automatically."""
        if not HAS_PYDUB:
            pytest.skip("pydub not available")
        
        audio = AudioSegment.silent(duration=2000)
        filepath = temp_storage / "unsorted_vol.wav"
        audio.export(str(filepath), format="wav")
        
        # Points not in time order
        envelope = [
            {"time": 2.0, "gain_db": 0.0},
            {"time": 0.0, "gain_db": -12.0},
            {"time": 1.0, "gain_db": -6.0}
        ]
        
        result = await server.volume({
            "source_file": "unsorted_vol.wav",
            "dest_file": "sorted_vol.wav",
            "envelope": envelope,
            "_status": mock_status,
        })
        
        assert result["status"] == "success"
        # Verify envelope was sorted
        assert result["envelope"][0]["time"] == 0.0
        assert result["envelope"][1]["time"] == 1.0
        assert result["envelope"][2]["time"] == 2.0
