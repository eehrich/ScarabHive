"""Tests for log viewer rotation file handling"""

import pytest
from plugins.log_viewer.endpoints import LogViewerWebEndpoints
from agent_system.config.models import AgentSystemConfig, MCPConfig


@pytest.fixture
def temp_log_dir(tmp_path):
    """Create temporary log directory with rotation files"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir


@pytest.fixture
def mock_system_config():
    """Create mock system config"""
    return AgentSystemConfig(
        name="test",
        version="1.0.0",
        includes=[],
        default_agent="test",
        logging={
            "enabled": True,
            "level": "INFO",
            "file": "logs/test.log"
        }
    )


@pytest.fixture
def mock_mcp_config():
    """Create mock MCP config"""
    from agent_system.config.models import AgentConfig
    return MCPConfig(
        type="log_viewer",
        enabled=True,
        agent_config=AgentConfig()
    )


@pytest.fixture
def mock_server(mock_mcp_config):
    """Create mock MCP server; its allowlist is the config's, as in the real server"""
    class MockServer:
        def __init__(self):
            self.name = "log_viewer"

        @property
        def log_files(self):
            return mock_mcp_config.log_files

        def get_schema_data(self):
            return {}
    
    return MockServer()


class TestRotationFileDetection:
    """Test rotation file detection logic"""
    
    def test_find_rotation_files_single(self, temp_log_dir, mock_system_config, mock_mcp_config, mock_server):
        """Test detection with only base log file"""
        # Create base log file
        log_file = temp_log_dir / "agent.log"
        log_file.write_text("test content\n")
        
        # Update config to point to our temp log
        mock_mcp_config.log_files = [str(log_file)]
        
        endpoints = LogViewerWebEndpoints(
            name="log_viewer",
            system_config=mock_system_config,
            mcp_config=mock_mcp_config,
            server=mock_server
        )
        
        rotation_files = endpoints._find_rotation_files(log_file)
        
        assert len(rotation_files) == 1
        assert rotation_files[0] == log_file
    
    def test_find_rotation_files_with_backups(self, temp_log_dir, mock_system_config, mock_mcp_config, mock_server):
        """Test detection with rotation files"""
        # Create base and rotation files
        log_file = temp_log_dir / "agent.log"
        log_file.write_text("newest content\n")
        
        (temp_log_dir / "agent.log.1").write_text("older content\n")
        (temp_log_dir / "agent.log.2").write_text("oldest content\n")
        
        mock_mcp_config.log_files = [str(log_file)]
        
        endpoints = LogViewerWebEndpoints(
            name="log_viewer",
            system_config=mock_system_config,
            mcp_config=mock_mcp_config,
            server=mock_server
        )
        
        rotation_files = endpoints._find_rotation_files(log_file)
        
        assert len(rotation_files) == 3
        # Should be in oldest-to-newest order
        assert str(rotation_files[0]).endswith("agent.log.2")
        assert str(rotation_files[1]).endswith("agent.log.1")
        assert str(rotation_files[2]).endswith("agent.log")
    
    def test_find_rotation_files_missing_base(self, temp_log_dir, mock_system_config, mock_mcp_config, mock_server):
        """Test detection when base file doesn't exist but rotations do"""
        log_file = temp_log_dir / "agent.log"
        
        # Create only rotation files (base was rotated away)
        (temp_log_dir / "agent.log.1").write_text("content 1\n")
        (temp_log_dir / "agent.log.2").write_text("content 2\n")
        
        mock_mcp_config.log_files = [str(log_file)]
        
        endpoints = LogViewerWebEndpoints(
            name="log_viewer",
            system_config=mock_system_config,
            mcp_config=mock_mcp_config,
            server=mock_server
        )
        
        rotation_files = endpoints._find_rotation_files(log_file)
        
        assert len(rotation_files) == 2
        assert str(rotation_files[0]).endswith("agent.log.2")
        assert str(rotation_files[1]).endswith("agent.log.1")
    
    def test_find_rotation_files_gaps(self, temp_log_dir, mock_system_config, mock_mcp_config, mock_server):
        """Test detection stops at first gap"""
        log_file = temp_log_dir / "agent.log"
        log_file.write_text("newest\n")
        
        (temp_log_dir / "agent.log.1").write_text("older\n")
        # Skip .2
        (temp_log_dir / "agent.log.3").write_text("shouldn't be found\n")
        
        mock_mcp_config.log_files = [str(log_file)]
        
        endpoints = LogViewerWebEndpoints(
            name="log_viewer",
            system_config=mock_system_config,
            mcp_config=mock_mcp_config,
            server=mock_server
        )
        
        rotation_files = endpoints._find_rotation_files(log_file)
        
        # Should stop at .2 (doesn't exist)
        assert len(rotation_files) == 2
        assert rotation_files[0] == temp_log_dir / "agent.log.1"
        assert rotation_files[1] == log_file
    
    def test_find_rotation_files_nonexistent(self, temp_log_dir, mock_system_config, mock_mcp_config, mock_server):
        """Test detection with completely nonexistent log"""
        log_file = temp_log_dir / "nonexistent.log"
        
        mock_mcp_config.log_files = [str(log_file)]
        
        endpoints = LogViewerWebEndpoints(
            name="log_viewer",
            system_config=mock_system_config,
            mcp_config=mock_mcp_config,
            server=mock_server
        )
        
        rotation_files = endpoints._find_rotation_files(log_file)
        
        assert len(rotation_files) == 0


class TestRotationOrderPreservation:
    """Test that chronological order is preserved when reading rotation files"""
    
    def test_order_in_combined_output(self, temp_log_dir, mock_system_config, mock_mcp_config, mock_server):
        """Test that messages from rotation files appear in correct order"""
        log_file = temp_log_dir / "agent.log"
        
        # Create rotation files with identifiable content
        # Oldest first (highest number)
        (temp_log_dir / "agent.log.2").write_text(
            "2026-01-01 10:00:00,000 INFO test Message 001\n"
            "2026-01-01 10:00:01,000 INFO test Message 002\n"
        )
        (temp_log_dir / "agent.log.1").write_text(
            "2026-01-01 10:00:02,000 INFO test Message 003\n"
            "2026-01-01 10:00:03,000 INFO test Message 004\n"
        )
        log_file.write_text(
            "2026-01-01 10:00:04,000 INFO test Message 005\n"
            "2026-01-01 10:00:05,000 INFO test Message 006\n"
        )
        
        mock_mcp_config.log_files = [str(log_file)]
        
        endpoints = LogViewerWebEndpoints(
            name="log_viewer",
            system_config=mock_system_config,
            mcp_config=mock_mcp_config,
            server=mock_server
        )
        
        # Get rotation files
        rotation_files = endpoints._find_rotation_files(log_file)
        
        # Verify order
        assert len(rotation_files) == 3
        
        # Read in the order they're returned (should be oldest to newest)
        all_lines = []
        for rf in rotation_files:
            all_lines.extend(rf.read_text().strip().split('\n'))
        
        # Extract message numbers
        import re
        message_numbers = []
        for line in all_lines:
            match = re.search(r'Message (\d+)', line)
            if match:
                message_numbers.append(int(match.group(1)))
        
        # Should be in sequential order
        assert message_numbers == [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
class TestRotationAPIEndpoints:
    """Test API endpoints with rotation files"""
    
    async def test_list_log_files_with_rotations(self, temp_log_dir, mock_system_config, mock_mcp_config, mock_server):
        """Test list_log_files includes rotation info"""
        from fastapi import Request
        from unittest.mock import MagicMock
        
        log_file = temp_log_dir / "agent.log"
        log_file.write_text("a" * 1000)
        (temp_log_dir / "agent.log.1").write_text("b" * 2000)
        (temp_log_dir / "agent.log.2").write_text("c" * 3000)
        
        mock_mcp_config.log_files = [str(log_file)]
        
        endpoints = LogViewerWebEndpoints(
            name="log_viewer",
            system_config=mock_system_config,
            mcp_config=mock_mcp_config,
            server=mock_server
        )
        
        mock_request = MagicMock(spec=Request)
        response = await endpoints.list_log_files(mock_request)
        
        import json
        data = json.loads(response.body)
        
        assert "logs" in data
        assert len(data["logs"]) == 1
        
        log_info = data["logs"][0]
        assert log_info["exists"] is True
        assert log_info["rotation_count"] == 3
        assert log_info["size"] == 6000  # Sum of all files
        assert len(log_info["rotation_files"]) == 3
    
    async def test_list_log_files_no_rotations(self, temp_log_dir, mock_system_config, mock_mcp_config, mock_server):
        """Test list_log_files with single file (no rotations)"""
        from fastapi import Request
        from unittest.mock import MagicMock
        
        log_file = temp_log_dir / "agent.log"
        test_content = "single file content\n"
        # Use write_bytes to ensure consistent line endings across platforms
        log_file.write_bytes(test_content.encode("utf-8"))
        
        mock_mcp_config.log_files = [str(log_file)]
        
        endpoints = LogViewerWebEndpoints(
            name="log_viewer",
            system_config=mock_system_config,
            mcp_config=mock_mcp_config,
            server=mock_server
        )
        
        mock_request = MagicMock(spec=Request)
        response = await endpoints.list_log_files(mock_request)
        
        import json
        data = json.loads(response.body)
        
        log_info = data["logs"][0]
        assert log_info["rotation_count"] == 1
        assert log_info["size"] == len("single file content\n")
