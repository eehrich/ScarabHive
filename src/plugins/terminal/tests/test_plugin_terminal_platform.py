"""Tests for Terminal plugin platform detection."""

import os
import platform
from unittest.mock import patch

import pytest

from plugins.terminal.platform_detect import PlatformDetector


class TestPlatformDetector:
    """Test suite for PlatformDetector."""

    def test_detect_bash_on_linux(self):
        """Test bash detection on Linux."""
        with patch('platform.system', return_value='Linux'):
            with patch('shutil.which', return_value='/bin/bash'):
                with patch('os.path.exists', return_value=True):
                    detector = PlatformDetector()
                    bash_path, shell_name = detector.detect_bash()
                    
                    assert bash_path in ['/bin/bash', '/usr/bin/bash']
                    assert shell_name == 'bash'

    def test_detect_git_bash_on_windows(self):
        """Test Git Bash detection on Windows."""
        with patch('platform.system', return_value='Windows'):
            git_bash_path = r"C:\Program Files\Git\bin\bash.exe"
            
            def mock_exists(path):
                return path == git_bash_path
            
            with patch('os.path.exists', side_effect=mock_exists):
                detector = PlatformDetector()
                bash_path, shell_name = detector.detect_bash()
                
                assert bash_path == git_bash_path
                assert shell_name == 'Git Bash'

    def test_wsl_exe_alone_is_no_bash(self):
        """wsl.exe answers `-c <command>` with "invalid command line argument"
        (measured): taken as the bash, it ran nothing. Refused at load instead."""
        with patch('platform.system', return_value='Windows'):
            with patch('os.path.exists', return_value=False):  # Git Bash not found
                with patch('shutil.which', side_effect=lambda x: 'wsl.exe' if x == 'wsl' else None):
                    with pytest.raises(RuntimeError, match="platform.bash_path"):
                        PlatformDetector().detect_bash()

    def test_detect_bash_in_path_windows(self):
        """Test bash in PATH detection on Windows."""
        with patch('platform.system', return_value='Windows'):
            with patch('os.path.exists', return_value=False):  # Git Bash not found
                with patch('shutil.which', side_effect=lambda x: 'bash.exe' if x == 'bash' else None):
                    detector = PlatformDetector()
                    bash_path, shell_name = detector.detect_bash()
                    
                    assert bash_path == 'bash.exe'
                    assert shell_name == 'bash'

    def test_no_bash_found_windows(self):
        """Test error when no bash found on Windows."""
        with patch('platform.system', return_value='Windows'):
            with patch('os.path.exists', return_value=False):
                # Mock shutil.which at import time to avoid subprocess creation
                with patch('shutil.which', return_value=None):
                    # Also patch subprocess to prevent any subprocess calls
                    with patch('subprocess.run', return_value=None):
                        detector = PlatformDetector()
                        
                        with pytest.raises(RuntimeError) as exc_info:
                            detector.detect_bash()
                        
                        assert "No bash executable found" in str(exc_info.value)
                        assert "Git Bash" in str(exc_info.value)

    def test_no_bash_found_linux(self):
        """Test error when no bash found on Linux."""
        with patch('platform.system', return_value='Linux'):
            with patch('shutil.which', return_value=None):
                with patch('os.path.exists', return_value=False):
                    detector = PlatformDetector()
                    
                    with pytest.raises(RuntimeError) as exc_info:
                        detector.detect_bash()
                    
                    assert "No bash executable found" in str(exc_info.value)

    @pytest.mark.skipif(platform.system() not in ['Windows', 'Linux', 'Darwin'], reason="Unsupported platform")
    def test_real_bash_detection(self):
        """Integration test: detect bash on real system."""
        detector = PlatformDetector()
        
        try:
            bash_path, shell_name = detector.detect_bash()
            
            # Verify bash exists and is executable
            assert os.path.exists(bash_path), f"Bash path {bash_path} does not exist"
            assert shell_name in ['bash', 'Git Bash', 'WSL'], f"Unknown shell name: {shell_name}"
            
            # Try to verify it's actually bash (if not WSL)
            if shell_name != 'WSL':
                assert 'bash' in bash_path.lower(), f"Path {bash_path} doesn't look like bash"
        
        except RuntimeError as e:
            # If no bash found, skip test
            pytest.skip(f"No bash found on system: {e}")
