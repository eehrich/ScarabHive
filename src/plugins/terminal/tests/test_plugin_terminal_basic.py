"""Tests for Terminal plugin basic command execution."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.terminal.server import TerminalServer


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
def mock_server_config():
    """Create a mock tool server configuration."""
    config = MagicMock(spec=ToolServerConfig)
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


class TestTerminalServerBasic:
    """Test suite for basic TerminalServer functionality."""

    @pytest.mark.asyncio
    async def test_server_initialization(self, mock_system_config, mock_server_config):
        """Test that server initializes correctly."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        assert server.name == "test"
        assert server.bash_path is not None
        assert server.security is not None
        assert server.executor is not None
        assert server.process_manager is not None
        assert server.max_output_kb == 60
        assert server.default_timeout == 300
        assert server.max_timeout == 3600
        
        # Cleanup
        await server.cleanup()

    @pytest.mark.asyncio
    async def test_execute_simple_command(self, mock_system_config, mock_server_config, mock_status):
        """Test simple command execution."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        try:
            result = await server.execute_command({
                "command": "echo 'Hello World'",
                "_status": mock_status
            })
            
            assert result["status"] == "success"
            assert "Hello World" in result["stdout"]
            assert result["exit_code"] == 0
            assert "command" in result
            assert result["command"] == "echo 'Hello World'"
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_dangerous_command_blocked(self, mock_system_config, mock_server_config, mock_status):
        """Test that dangerous commands are blocked."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        try:
            result = await server.execute_command({
                "command": "rm -rf /",
                "_status": mock_status
            })
            
            assert result["status"] == "error"
            assert result["error_type"] == "SecurityError"
            assert "blocked" in result["error"].lower() or "dangerous" in result["error"].lower()
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_command_timeout(self, mock_system_config, mock_server_config, mock_status):
        """Test command timeout enforcement."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        try:
            result = await server.execute_command({
                "command": "sleep 10",
                "timeout": 1,
                "_status": mock_status
            })
            
            assert result["status"] == "error"
            assert result["error_type"] == "TimeoutError"
            assert "timeout" in result["error"].lower()
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_timeout_exceeds_maximum(self, mock_system_config, mock_server_config, mock_status):
        """Test that timeouts exceeding maximum are rejected."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        try:
            result = await server.execute_command({
                "command": "echo test",
                "timeout": 4000,  # Exceeds max of 3600
                "_status": mock_status
            })
            
            assert result["status"] == "error"
            assert result["error_type"] == "TimeoutExceeded"
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_command_with_cwd(self, mock_system_config, mock_server_config, mock_status, tmp_path):
        """Test command execution with custom working directory."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        try:
            # A directory of its own, recognised by its name: Git Bash on Windows
            # rewrites the path (/c/Users/...) but not the name. The system temp
            # directory named no marker everywhere (macOS: /var/folders/.../T).
            work_dir = tmp_path / "cwd-marker-4711"
            work_dir.mkdir()
            result = await server.execute_command({
                "command": "echo $PWD",  # Use echo $PWD instead of pwd to get actual path
                "cwd": str(work_dir),
                "_status": mock_status
            })
            
            assert result["status"] == "success"
            assert "cwd-marker-4711" in result["stdout"], result["stdout"]
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_command_with_env_vars(self, mock_system_config, mock_server_config, mock_status):
        """Test command execution with environment variables."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        try:
            result = await server.execute_command({
                "command": "echo $TEST_VAR",
                "env_vars": {"TEST_VAR": "hello_from_env"},
                "_status": mock_status
            })
            
            assert result["status"] == "success"
            assert "hello_from_env" in result["stdout"]
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_command_cancellation(self, mock_system_config, mock_server_config, mock_status):
        """Test command cancellation support."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        try:
            # Create mock cancellation token
            cancel_token = MagicMock()
            cancel_token.is_cancelled = True
            
            result = await server.execute_command({
                "command": "sleep 100",
                "_status": mock_status,
                "_cancellation_token": cancel_token
            })
            
            assert result["status"] == "cancelled"
            assert result["command"] == "sleep 100"
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_multiple_commands_session_state(self, mock_system_config, mock_server_config, mock_status):
        """Test that session state does NOT persist between commands (each command runs in separate subprocess)."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        try:
            # Set a variable
            result1 = await server.execute_command({
                "command": "export MY_VAR=test123",
                "_status": mock_status
            })
            assert result1["status"] == "success"
            
            # Variable should NOT persist (separate subprocess)
            result2 = await server.execute_command({
                "command": "echo $MY_VAR",
                "_status": mock_status
            })
            assert result2["status"] == "success"
            # Variable not set, so output is just newline
            assert "test123" not in result2["stdout"]
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_output_truncation(self, mock_system_config, mock_server_config, mock_status):
        """Test that large output is truncated."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        # Set low limit and update executor
        server.max_output_kb = 1
        server.executor.max_output_kb = 1  # Also update executor
        
        try:
            # Generate more than 1KB of output
            result = await server.execute_command({
                "command": "python -c 'print(\"x\" * 2000)'",
                "_status": mock_status
            })
            
            if result["status"] == "success":
                # Should be truncated
                assert result.get("truncated", False) or len(result.get("stdout", "")) <= 1024 + 200
        
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_get_template_vars(self, mock_system_config, mock_server_config):
        """Test template variable generation."""
        server = TerminalServer("test", mock_system_config, mock_server_config)
        
        try:
            vars = server.get_template_vars()
            
            assert "name" in vars
            assert vars["name"] == "test"
            assert "max_timeout" in vars
            assert vars["max_timeout"] == 3600
            assert "default_timeout" in vars
            assert vars["default_timeout"] == 300
        
        finally:
            await server.cleanup()
