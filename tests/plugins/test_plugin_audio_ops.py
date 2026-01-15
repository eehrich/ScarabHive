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
from unittest.mock import MagicMock, AsyncMock, patch

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
