"""Integration tests for Terminal plugin persistent sessions and background processes."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.config import AgentSystemConfig, MCPConfig
from plugins.terminal.server import TerminalServer

# Use sys.executable to get the correct Python interpreter path
# Convert Windows paths to Git Bash compatible format
_raw_python = sys.executable
if os.name == 'nt':
    # Convert C:\path\to\python.exe to /c/path/to/python.exe for Git Bash
    _raw_python = _raw_python.replace('\\', '/')
    if len(_raw_python) > 1 and _raw_python[1] == ':':
        _raw_python = '/' + _raw_python[0].lower() + _raw_python[2:]
PYTHON = _raw_python


@pytest.fixture
def mock_status():
    """Create a mock status object."""
    status = AsyncMock()
    status.progress = AsyncMock()
    status.complete = AsyncMock()
    status.error = AsyncMock()
    return status


@pytest.fixture
def mock_system_config():
    """Create a mock system configuration."""
    config = MagicMock(spec=AgentSystemConfig)
    return config


@pytest.fixture
def mock_mcp_config():
    """Create a mock MCP configuration."""
    config = MagicMock(spec=MCPConfig)
    config.security = {
        'whitelist': None,
        'blacklist': [],
        'allow_command_chains': True
    }
    config.limits = {
        'max_output_size_kb': 60,
        'default_timeout_seconds': 300,
        'max_timeout_seconds': 3600,
        'max_concurrent_background': 10
    }
    config.platform = {
        'bash_path': 'auto',
        'initial_cwd': None
    }
    return config


class TestTerminalServerIntegration:
    """Integration tests for TerminalServer."""

    @pytest.mark.asyncio
    async def test_background_process_lifecycle(self, mock_system_config, mock_mcp_config, mock_status):
        """Test complete background process lifecycle."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            # Start background process - use double quotes for Windows and longer sleep
            start_result = await server.execute_background({
                "command": f'{PYTHON} -u -c "import time; import sys; [print(i, flush=True) for i in range(3)]; sys.stdout.flush(); time.sleep(2)"',
                "_status": mock_status
            })
            
            assert start_result["status"] == "success"
            assert "process_id" in start_result
            assert "pid" in start_result
            process_id = start_result["process_id"]
            
            # Wait for output - retry multiple times as background process output may be delayed
            output_result = None
            for _ in range(10):
                await asyncio.sleep(0.2)
                output_result = await server.get_output({
                    "process_id": process_id,
                    "_status": mock_status
                })
                if output_result.get("stdout") and any(str(i) in output_result["stdout"] for i in range(3)):
                    break
            
            assert output_result["status"] == "success"
            assert "is_running" in output_result
            # Output should contain numbers (check that we got at least one digit)
            stdout = output_result.get("stdout", "")
            assert any(str(i) in stdout for i in range(3)), f"Expected digits 0-2 in stdout, got: {stdout}"
            
            # Wait for process to finish (process sleeps for 2 seconds)
            await asyncio.sleep(2.5)
            
            # Get final output
            final_result = await server.get_output({
                "process_id": process_id,
                "_status": mock_status
            })
            
            assert final_result["status"] == "success"
            assert not final_result["is_running"]
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_background_process_kill(self, mock_system_config, mock_mcp_config, mock_status):
        """Test killing a background process."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            # Start long-running process
            start_result = await server.execute_background({
                "command": "sleep 100",
                "_status": mock_status
            })
            
            assert start_result["status"] == "success"
            process_id = start_result["process_id"]
            
            # Wait a bit
            await asyncio.sleep(0.2)
            
            # Kill process
            kill_result = await server.kill_process({
                "process_id": process_id,
                "force": False,
                "_status": mock_status
            })
            
            assert kill_result["status"] == "success"
            assert kill_result["killed"] is True
            assert "signal" in kill_result
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_background_process_force_kill(self, mock_system_config, mock_mcp_config, mock_status):
        """Test force killing a background process."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            # Start process
            start_result = await server.execute_background({
                "command": "sleep 100",
                "_status": mock_status
            })
            
            assert start_result["status"] == "success"
            process_id = start_result["process_id"]
            
            await asyncio.sleep(0.2)
            
            # Force kill
            kill_result = await server.kill_process({
                "process_id": process_id,
                "force": True,
                "_status": mock_status
            })
            
            assert kill_result["status"] == "success"
            assert kill_result["killed"] is True
            assert "SIGKILL" in kill_result["signal"]
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_kill_nonexistent_process(self, mock_system_config, mock_mcp_config, mock_status):
        """Test killing a nonexistent process."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            result = await server.kill_process({
                "process_id": "nonexistent_123",
                "_status": mock_status
            })
            
            assert result["status"] == "error"
            assert result["error_type"] == "ProcessNotFound"
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_get_output_nonexistent_process(self, mock_system_config, mock_mcp_config, mock_status):
        """Test getting output from nonexistent process."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            result = await server.get_output({
                "process_id": "nonexistent_123",
                "_status": mock_status
            })
            
            assert result["status"] == "error"
            assert result["error_type"] == "ProcessNotFound"
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_background_process_custom_id(self, mock_system_config, mock_mcp_config, mock_status):
        """Test background process with custom process ID."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            custom_id = "my_custom_process_123"
            
            start_result = await server.execute_background({
                "command": "echo 'test'",
                "process_id": custom_id,
                "_status": mock_status
            })
            
            assert start_result["status"] == "success"
            assert start_result["process_id"] == custom_id
            
            # Should be able to get output using custom ID
            await asyncio.sleep(0.2)
            
            output_result = await server.get_output({
                "process_id": custom_id,
                "_status": mock_status
            })
            
            assert output_result["status"] == "success"
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_get_output_streams(self, mock_system_config, mock_mcp_config, mock_status):
        """Test getting specific output streams."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            # Start process that outputs to both stdout and stderr
            start_result = await server.execute_background({
                "command": "echo 'stdout'; echo 'stderr' >&2",
                "_status": mock_status
            })
            
            assert start_result["status"] == "success"
            process_id = start_result["process_id"]
            
            await asyncio.sleep(0.3)
            
            # Get stdout only
            stdout_result = await server.get_output({
                "process_id": process_id,
                "stream": "stdout",
                "_status": mock_status
            })
            
            assert stdout_result["status"] == "success"
            assert "stdout" in stdout_result
            
            # Get stderr only
            stderr_result = await server.get_output({
                "process_id": process_id,
                "stream": "stderr",
                "_status": mock_status
            })
            
            assert stderr_result["status"] == "success"
            assert "stderr" in stderr_result
            
            # Get both
            both_result = await server.get_output({
                "process_id": process_id,
                "stream": "both",
                "_status": mock_status
            })
            
            assert both_result["status"] == "success"
            assert "stdout" in both_result
            assert "stderr" in both_result
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_get_output_clear_buffer(self, mock_system_config, mock_mcp_config, mock_status):
        """Test clearing buffer after reading output."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            start_result = await server.execute_background({
                "command": "echo 'test output'",
                "_status": mock_status
            })
            
            assert start_result["status"] == "success"
            process_id = start_result["process_id"]
            
            await asyncio.sleep(0.3)
            
            # Read with clear_buffer=True
            first_result = await server.get_output({
                "process_id": process_id,
                "clear_buffer": True,
                "_status": mock_status
            })
            
            assert first_result["status"] == "success"
            first_output = first_result["stdout"]
            assert len(first_output) > 0
            
            # Read again - buffer should be empty
            second_result = await server.get_output({
                "process_id": process_id,
                "_status": mock_status
            })
            
            assert second_result["status"] == "success"
            # Buffer was cleared, so no output
            assert len(second_result["stdout"]) == 0
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_persistent_session_environment(self, mock_system_config, mock_mcp_config, mock_status):
        """Test that environment does NOT persist (each command runs in separate subprocess)."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            # Set environment variable
            result1 = await server.execute_command({
                "command": "export TEST_PERSIST=hello123",
                "_status": mock_status
            })
            assert result1["status"] == "success"
            
            # Verify it does NOT persist (separate subprocess)
            result2 = await server.execute_command({
                "command": "echo $TEST_PERSIST",
                "_status": mock_status
            })
            assert result2["status"] == "success"
            # Variable not set, output is empty
            assert "hello123" not in result2["stdout"]
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_persistent_session_directory(self, mock_system_config, mock_mcp_config, mock_status):
        """Test that directory changes do NOT persist (each command runs in separate subprocess)."""
        import tempfile
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            temp_dir = tempfile.gettempdir()
            # Change to temp directory
            result1 = await server.execute_command({
                "command": f"cd '{temp_dir}'",
                "_status": mock_status
            })
            assert result1["status"] == "success"
            
            # Check we're NOT in temp dir (separate subprocess, cwd not preserved)
            result2 = await server.execute_command({
                "command": "echo $PWD",
                "_status": mock_status
            })
            assert result2["status"] == "success"
            # We're back in the initial cwd, not temp dir
            assert temp_dir.lower() not in result2["stdout"].lower()
        
        finally:
            await server.cleanup()
