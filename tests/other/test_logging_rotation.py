"""Tests for log rotation functionality"""

import logging
import tempfile
from pathlib import Path
import pytest
from agent_system.config.models import LoggingConfig
from agent_system.utils.logging import setup_logging


class TestLoggingConfigSizeParsing:
    """Test size format parsing in LoggingConfig"""
    
    def test_size_string_mb(self):
        """Test parsing MB format"""
        config = LoggingConfig(max_bytes="10MB")
        assert config.max_bytes == 10 * 1024 * 1024
    
    def test_size_string_kb(self):
        """Test parsing KB format"""
        config = LoggingConfig(max_bytes="100KB")
        assert config.max_bytes == 100 * 1024
    
    def test_size_string_gb(self):
        """Test parsing GB format"""
        config = LoggingConfig(max_bytes="1GB")
        assert config.max_bytes == 1 * 1024 * 1024 * 1024
    
    def test_size_string_short_format(self):
        """Test short format (M, K, G)"""
        config1 = LoggingConfig(max_bytes="5M")
        assert config1.max_bytes == 5 * 1024 * 1024
        
        config2 = LoggingConfig(max_bytes="500K")
        assert config2.max_bytes == 500 * 1024
        
        config3 = LoggingConfig(max_bytes="2G")
        assert config3.max_bytes == 2 * 1024 * 1024 * 1024
    
    def test_size_integer(self):
        """Test plain integer in bytes"""
        config = LoggingConfig(max_bytes=10485760)
        assert config.max_bytes == 10485760
    
    def test_size_string_as_integer(self):
        """Test string containing integer"""
        config = LoggingConfig(max_bytes="10485760")
        assert config.max_bytes == 10485760
    
    def test_size_case_insensitive(self):
        """Test case insensitivity"""
        config1 = LoggingConfig(max_bytes="10mb")
        assert config1.max_bytes == 10 * 1024 * 1024
        
        config2 = LoggingConfig(max_bytes="10Mb")
        assert config2.max_bytes == 10 * 1024 * 1024
    
    def test_size_with_whitespace(self):
        """Test format with whitespace"""
        config = LoggingConfig(max_bytes="10 MB")
        assert config.max_bytes == 10 * 1024 * 1024
    
    def test_size_decimal(self):
        """Test decimal values"""
        config = LoggingConfig(max_bytes="2.5MB")
        assert config.max_bytes == int(2.5 * 1024 * 1024)
    
    def test_invalid_format(self):
        """Test invalid formats raise ValidationError"""
        with pytest.raises(Exception):  # Pydantic ValidationError
            LoggingConfig(max_bytes="10XB")
        
        with pytest.raises(Exception):
            LoggingConfig(max_bytes="MB10")
        
        with pytest.raises(Exception):
            LoggingConfig(max_bytes="ten MB")
        
        with pytest.raises(Exception):
            LoggingConfig(max_bytes="")


class TestLogRotation:
    """Test actual log rotation behavior"""
    
    def test_rotation_disabled(self, tmp_path):
        """Test that rotation_enabled=False truncates log file on restart"""
        log_file = tmp_path / "test.log"
        
        # First run - write some data
        setup_logging(
            enabled=True,
            level="INFO",
            file_path=str(log_file),
            rotation_enabled=False,
            max_bytes=1024,
            backup_count=3
        )
        
        logger = logging.getLogger("test_rotation")
        logger.info("First run message")
        
        # Close handlers
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
        
        first_content = log_file.read_text()
        assert "First run message" in first_content
        
        # Second run - file should be truncated
        setup_logging(
            enabled=True,
            level="INFO",
            file_path=str(log_file),
            rotation_enabled=False,
            max_bytes=1024,
            backup_count=3
        )
        
        logger = logging.getLogger("test_rotation")
        logger.info("Second run message")
        
        # Clean up
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
        
        second_content = log_file.read_text()
        # Old message should be gone (file was truncated)
        assert "First run message" not in second_content
        assert "Second run message" in second_content
    
    def test_rotation_enabled_creates_backups(self, tmp_path):
        """Test that rotation_enabled=True creates backup files"""
        log_file = tmp_path / "test.log"
        
        # Small max_bytes to trigger rotation quickly
        setup_logging(
            enabled=True,
            level="INFO",
            file_path=str(log_file),
            rotation_enabled=True,
            max_bytes=500,  # 500 bytes
            backup_count=3
        )
        
        logger = logging.getLogger("test_rotation")
        
        # Write enough data to trigger rotation
        for i in range(50):
            logger.info(f"Test message number {i} with some padding to increase size")
        
        # Flush handlers
        for handler in logging.root.handlers:
            handler.flush()
        
        # Clean up handlers
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
        
        # Check that rotation files were created
        assert log_file.exists(), "Main log file should exist"
        
        # At least one rotation file should exist
        rotation1 = Path(f"{log_file}.1")
        assert rotation1.exists(), "Rotation file .1 should exist after exceeding max_bytes"
        
        # Check content is preserved
        main_content = log_file.read_text()
        rotation_content = rotation1.read_text()
        
        # Should have some messages in both files
        assert "Test message" in main_content
        assert "Test message" in rotation_content
    
    def test_rotation_respects_backup_count(self, tmp_path):
        """Test that backup_count limits number of rotation files"""
        log_file = tmp_path / "test.log"
        
        setup_logging(
            enabled=True,
            level="INFO",
            file_path=str(log_file),
            rotation_enabled=True,
            max_bytes=200,  # Very small to trigger multiple rotations
            backup_count=2  # Only keep 2 backups
        )
        
        logger = logging.getLogger("test_rotation")
        
        # Write lots of data to trigger multiple rotations
        for i in range(100):
            logger.info(f"Message {i} with padding text to increase file size quickly")
            # Force flush periodically
            if i % 10 == 0:
                for handler in logging.root.handlers:
                    handler.flush()
        
        # Final flush
        for handler in logging.root.handlers:
            handler.flush()
        
        # Clean up handlers
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
        
        # Check that we don't exceed backup_count
        assert log_file.exists()
        assert Path(f"{log_file}.1").exists()
        assert Path(f"{log_file}.2").exists()
        
        # .3 should not exist (backup_count=2)
        assert not Path(f"{log_file}.3").exists()
    
    def test_rotation_preserves_log_order(self, tmp_path):
        """Test that rotation maintains chronological order"""
        log_file = tmp_path / "test.log"
        
        setup_logging(
            enabled=True,
            level="INFO",
            file_path=str(log_file),
            rotation_enabled=True,
            max_bytes=300,
            backup_count=5
        )
        
        logger = logging.getLogger("test_rotation")
        
        # Write messages with identifiable order
        for i in range(30):
            logger.info(f"Sequential message {i:03d} with some text to fill space")
            if i % 5 == 0:
                for handler in logging.root.handlers:
                    handler.flush()
        
        # Final flush and cleanup
        for handler in logging.root.handlers:
            handler.flush()
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
        
        # Collect all messages from all files (oldest to newest)
        all_messages = []
        
        # Find all rotation files
        rotation_files = [log_file]
        for i in range(1, 10):
            rotation = Path(f"{log_file}.{i}")
            if rotation.exists():
                rotation_files.append(rotation)
            else:
                break
        
        # Read in reverse order (highest number first = oldest)
        for rotation in sorted(rotation_files, reverse=True):
            if rotation.exists():
                content = rotation.read_text()
                # Extract message numbers
                import re
                messages = re.findall(r'Sequential message (\d+)', content)
                all_messages.extend([int(m) for m in messages])
        
        # Messages should be in increasing order
        assert all_messages == sorted(all_messages), "Messages should be in chronological order"


class TestLoggingConfigDefaults:
    """Test default values in LoggingConfig"""
    
    def test_defaults(self):
        """Test default configuration values"""
        config = LoggingConfig()
        
        assert config.enabled is True
        assert config.level == "DEBUG"
        assert config.file == "logs/agent.log"
        assert config.rotation_enabled is True
        # Default is int (10MB in bytes)
        assert config.max_bytes == 10 * 1024 * 1024
        assert config.backup_count == 5
