"""
Test the unified execute() method with background parameter.
"""
import pytest
import asyncio
import sys
from plugins.terminal.server import TerminalServer

# Use sys.executable to get the correct Python interpreter path
PYTHON = sys.executable


class TestTerminalExecuteUnified:
    """Test the unified execute method."""

    @pytest.fixture
    def mock_system_config(self):
        """Mock system config."""
        return {}

    @pytest.fixture
    def mock_mcp_config(self):
        """Mock MCP config."""
        return {}

    @pytest.fixture
    def mock_status(self):
        """Mock status reporter."""
        class MockStatus:
            async def update(self, msg): pass
            async def progress(self, msg): pass
            async def end(self, msg, meta=None): pass
            async def error(self, msg): pass
        return MockStatus()

    @pytest.mark.asyncio
    async def test_execute_foreground(self, mock_system_config, mock_mcp_config, mock_status):
        """Test execute with background=false (default)."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            # Execute foreground (background=false is default)
            result = await server.execute({
                "command": "echo 'test'",
                "_status": mock_status
            })
            
            assert result["status"] == "success"
            assert "test" in result["stdout"]
            assert "exit_code" in result
            assert "execution_time" in result
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_execute_foreground_explicit(self, mock_system_config, mock_mcp_config, mock_status):
        """Test execute with background=false explicitly set."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            result = await server.execute({
                "command": "echo 'explicit foreground'",
                "background": False,
                "_status": mock_status
            })
            
            assert result["status"] == "success"
            assert "explicit foreground" in result["stdout"]
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_execute_background(self, mock_system_config, mock_mcp_config, mock_status):
        """Test execute with background=true."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            # Start background process
            result = await server.execute({
                "command": f"{PYTHON} -u -c 'import time; print(\"bg\"); time.sleep(1)'",
                "background": True,
                "_status": mock_status
            })
            
            assert result["status"] == "success"
            assert "process_id" in result
            assert "pid" in result
            
            # Wait for output
            await asyncio.sleep(0.5)
            
            # Get output
            output = await server.get_output({
                "process_id": result["process_id"],
                "_status": mock_status
            })
            
            assert output["status"] == "success"
            # Output should contain "bg"
            assert "bg" in output["stdout"]
            
            # Kill process
            await server.kill_process({
                "process_id": result["process_id"],
                "_status": mock_status
            })
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_execute_background_with_custom_id(self, mock_system_config, mock_mcp_config, mock_status):
        """Test execute background with custom process_id."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            result = await server.execute({
                "command": "echo 'custom'",
                "background": True,
                "process_id": "my_custom_id",
                "_status": mock_status
            })
            
            assert result["status"] == "success"
            assert result["process_id"] == "my_custom_id"
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_execute_with_timeout(self, mock_system_config, mock_mcp_config, mock_status):
        """Test execute foreground with timeout."""
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            result = await server.execute({
                "command": "sleep 5",
                "timeout": 1,
                "_status": mock_status
            })
            
            assert result["status"] == "error"
            assert "timeout" in result["error"].lower()
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_execute_with_cwd_and_env(self, mock_system_config, mock_mcp_config, mock_status):
        """Test execute with working directory and environment variables."""
        import tempfile
        server = TerminalServer("test", mock_system_config, mock_mcp_config)
        
        try:
            temp_dir = tempfile.gettempdir()
            result = await server.execute({
                "command": "echo $MY_VAR",
                "cwd": temp_dir,
                "env_vars": {"MY_VAR": "test123"},
                "_status": mock_status
            })
            
            assert result["status"] == "success"
            assert "test123" in result["stdout"]
        finally:
            await server.cleanup()
