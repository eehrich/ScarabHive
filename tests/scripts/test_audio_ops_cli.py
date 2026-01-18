"""Tests for the audio_ops CLI.

Tests argument parsing and basic command execution.
"""
from __future__ import annotations

import warnings
import subprocess
import sys
import pytest
from pathlib import Path

# Suppress audioop deprecation warning for pydub (Python 3.12+)
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydub")

# Check if pydub is available
try:
    from pydub import AudioSegment
    HAS_PYDUB = True
except ImportError:
    HAS_PYDUB = False
    AudioSegment = None


class TestCLIHelp:
    """Test CLI help output."""
    
    def test_help_shows_all_commands(self):
        """Test that --help shows all available commands."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "--help"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode == 0
        
        # All commands should be listed
        commands = ["info", "cut", "merge", "mix", "volume", "create", "list", "load"]
        for cmd in commands:
            assert cmd in result.stdout, f"Command '{cmd}' not in help output"
    
    def test_help_shows_examples(self):
        """Test that help shows usage examples."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "--help"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode == 0
        assert "Examples:" in result.stdout
        assert "audio-ops info" in result.stdout
        assert "audio-ops cut" in result.stdout
        assert "audio-ops mix" in result.stdout
        assert "audio-ops volume" in result.stdout
        assert "audio-ops create" in result.stdout

    def test_subcommand_help_info(self):
        """Test info command help."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "info", "--help"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode == 0
        assert "file" in result.stdout.lower()
    
    def test_subcommand_help_cut(self):
        """Test cut command help."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "cut", "--help"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode == 0
        assert "--start" in result.stdout
        assert "--end" in result.stdout
        assert "--mode" in result.stdout
        assert "extract" in result.stdout
        assert "remove" in result.stdout
    
    def test_subcommand_help_merge(self):
        """Test merge command help."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "merge", "--help"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode == 0
        assert "--crossfade" in result.stdout
        assert "sources" in result.stdout.lower()
    
    def test_subcommand_help_mix(self):
        """Test mix command help."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "mix", "--help"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode == 0
        assert "--factor" in result.stdout or "-f" in result.stdout
        assert "file1" in result.stdout.lower()
        assert "file2" in result.stdout.lower()
    
    def test_subcommand_help_volume(self):
        """Test volume command help."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "volume", "--help"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode == 0
        assert "--gain" in result.stdout
        assert "--normalize" in result.stdout
    
    def test_subcommand_help_create(self):
        """Test create command help."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "create", "--help"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode == 0
        assert "--duration" in result.stdout
        assert "--sample-rate" in result.stdout
        assert "--channels" in result.stdout


class TestCLIArgumentParsing:
    """Test CLI argument parsing without actual audio processing."""
    
    def test_no_command_shows_help(self):
        """Test that no command shows help."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode == 0
        assert "Audio Operations CLI" in result.stdout or "usage:" in result.stdout.lower()
    
    def test_cut_requires_start_end(self):
        """Test that cut command requires -s and -e."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "cut", "in.wav", "out.wav"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode != 0
        assert "--start" in result.stderr or "-s" in result.stderr
    
    def test_create_requires_duration(self):
        """Test that create command requires --duration."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "create", "silence.wav"],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        assert result.returncode != 0
        assert "--duration" in result.stderr or "-d" in result.stderr


@pytest.mark.skipif(not HAS_PYDUB, reason="pydub not installed")
class TestCLIExecution:
    """Test actual CLI execution with audio files."""
    
    @pytest.fixture
    def temp_storage(self, tmp_path: Path, monkeypatch) -> Path:
        """Create temp storage and patch config."""
        storage = tmp_path / "audio_ops"
        storage.mkdir()
        
        # Create minimal plugins.yaml
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        plugins_yaml = config_dir / "plugins.yaml"
        plugins_yaml.write_text(f"""
plugins:
  servers:
    audio_ops:
      type: audio_ops
      storage_path: "{str(storage).replace(chr(92), '/')}"
""")
        
        # Change CWD so config is found
        monkeypatch.chdir(tmp_path)
        return storage
    
    @pytest.fixture
    def sample_wav(self, temp_storage: Path) -> Path:
        """Create a sample WAV file."""
        audio = AudioSegment.silent(duration=5000, frame_rate=44100)
        filepath = temp_storage / "test.wav"
        audio.export(str(filepath), format="wav")
        return filepath
    
    def test_info_command(self, temp_storage: Path, sample_wav: Path):
        """Test info command on real file."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "info", sample_wav.name],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(temp_storage.parent)
        )
        assert result.returncode == 0
        assert "Duration" in result.stdout
        assert "Sample rate" in result.stdout
        assert "44100" in result.stdout
    
    def test_list_command(self, temp_storage: Path, sample_wav: Path):
        """Test list command."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "list"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(temp_storage.parent)
        )
        assert result.returncode == 0
        assert "test.wav" in result.stdout
    
    def test_create_command(self, temp_storage: Path):
        """Test create command."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "create", "silence.wav", "-d", "1000"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(temp_storage.parent)
        )
        assert result.returncode == 0
        assert "Created" in result.stdout
        assert (temp_storage / "silence.wav").exists()
    
    def test_cut_extract(self, temp_storage: Path, sample_wav: Path):
        """Test cut command in extract mode."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "cut", "test.wav", "segment.wav", 
             "-s", "1", "-e", "3"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(temp_storage.parent)
        )
        assert result.returncode == 0
        assert "Created" in result.stdout
        assert (temp_storage / "segment.wav").exists()
        
        # Verify duration is ~2 seconds
        segment = AudioSegment.from_file(str(temp_storage / "segment.wav"))
        assert 1900 < len(segment) < 2100  # Allow small variance
    
    def test_volume_gain(self, temp_storage: Path, sample_wav: Path):
        """Test volume command with gain."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "volume", "test.wav", "louder.wav",
             "--gain", "6"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(temp_storage.parent)
        )
        assert result.returncode == 0
        assert "Created" in result.stdout
        assert (temp_storage / "louder.wav").exists()
    
    def test_merge_command(self, temp_storage: Path, sample_wav: Path):
        """Test merge command."""
        # Create second file
        audio2 = AudioSegment.silent(duration=3000, frame_rate=44100)
        wav2 = temp_storage / "test2.wav"
        audio2.export(str(wav2), format="wav")
        
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "merge", "merged.wav", 
             "test.wav", "test2.wav"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(temp_storage.parent)
        )
        assert result.returncode == 0
        assert "Created" in result.stdout
        assert (temp_storage / "merged.wav").exists()
        
        # Verify merged duration is ~8 seconds (5 + 3)
        merged = AudioSegment.from_file(str(temp_storage / "merged.wav"))
        assert 7900 < len(merged) < 8100
    
    def test_mix_command(self, temp_storage: Path, sample_wav: Path):
        """Test mix command."""
        # Create second file
        audio2 = AudioSegment.silent(duration=5000, frame_rate=44100)
        wav2 = temp_storage / "test2.wav"
        audio2.export(str(wav2), format="wav")
        
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "mix", "test.wav", "test2.wav", 
             "mixed.wav", "-f", "0.5"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(temp_storage.parent)
        )
        assert result.returncode == 0
        assert "Created" in result.stdout
        assert (temp_storage / "mixed.wav").exists()


class TestCLIErrorHandling:
    """Test CLI error handling."""
    
    def test_info_file_not_found(self, tmp_path: Path, monkeypatch):
        """Test info command with non-existent file."""
        # Create config
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        storage = tmp_path / "audio_ops"
        storage.mkdir()
        plugins_yaml = config_dir / "plugins.yaml"
        plugins_yaml.write_text(f"""
plugins:
  servers:
    audio_ops:
      storage_path: "{str(storage).replace(chr(92), '/')}"
""")
        monkeypatch.chdir(tmp_path)
        
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "info", "nonexistent.wav"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(tmp_path)
        )
        assert result.returncode != 0
        assert "not found" in result.stderr.lower() or "error" in result.stderr.lower()
    
    @pytest.mark.skipif(not HAS_PYDUB, reason="pydub not installed")
    def test_volume_requires_gain_or_normalize(self, tmp_path: Path, monkeypatch):
        """Test volume command requires --gain or --normalize."""
        # Create config and sample file
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        storage = tmp_path / "audio_ops"
        storage.mkdir()
        plugins_yaml = config_dir / "plugins.yaml"
        plugins_yaml.write_text(f"""
plugins:
  servers:
    audio_ops:
      storage_path: "{str(storage).replace(chr(92), '/')}"
""")
        
        # Create sample file
        audio = AudioSegment.silent(duration=1000, frame_rate=44100)
        sample = storage / "test.wav"
        audio.export(str(sample), format="wav")
        
        monkeypatch.chdir(tmp_path)
        
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audio_ops", "volume", "test.wav", "out.wav"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(tmp_path)
        )
        assert result.returncode != 0
        assert "gain" in result.stderr.lower() or "normalize" in result.stderr.lower()
