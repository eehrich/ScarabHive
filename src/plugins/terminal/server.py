"""Terminal MCP Server implementation.

This module provides secure bash command execution with persistent sessions,
background process management, and output capture.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agent_system.mcp.schema_based import SchemaBasedMCPServer

from .executor import CommandExecutor
from .platform_detect import PlatformDetector
from .process_manager import ProcessManager
from .security import CommandSecurityValidator

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class TerminalServer(SchemaBasedMCPServer):
    """Terminal MCP server for executing shell commands with persistent sessions.

    This server provides:
    - execute_command: Execute commands and wait for completion
    - execute_background: Start long-running commands in background
    - get_output: Retrieve output from background processes
    - kill_process: Terminate background processes

    All tools are automatically loaded from schema.yaml by SchemaBasedMCPServer.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """
        Initialize terminal server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)

        # Extract security configuration
        security_config = getattr(mcp_config, 'security', {})
        if isinstance(security_config, dict):
            whitelist = security_config.get('whitelist')
            blacklist = security_config.get('blacklist')
            allow_command_chains = security_config.get('allow_command_chains', True)
        else:
            whitelist = None
            blacklist = None
            allow_command_chains = True

        # Extract limits configuration
        limits_config = getattr(mcp_config, 'limits', {})
        if isinstance(limits_config, dict):
            self.max_output_kb = limits_config.get('max_output_size_kb', 60)
            self.default_timeout = limits_config.get('default_timeout_seconds', 300)
            self.max_timeout = limits_config.get('max_timeout_seconds', 3600)
            # max_concurrent_background is available for future use
        else:
            self.max_output_kb = 60
            self.default_timeout = 300
            self.max_timeout = 3600

        # Extract platform configuration
        platform_config = getattr(mcp_config, 'platform', {})
        if isinstance(platform_config, dict):
            bash_path_config = platform_config.get('bash_path', 'auto')
            initial_cwd = platform_config.get('initial_cwd')
        else:
            bash_path_config = 'auto'
            initial_cwd = None

        # Detect bash path
        detector = PlatformDetector()
        if bash_path_config == 'auto':
            bash_path, shell_name = detector.detect_bash()
            logger.info(f"Auto-detected bash: {bash_path} ({shell_name})")
        else:
            bash_path = bash_path_config
            shell_name = "custom"
            logger.info(f"Using configured bash: {bash_path}")

        self.bash_path = bash_path
        self.shell_name = shell_name

        # Initialize security validator
        self.security = CommandSecurityValidator(
            whitelist=whitelist,
            blacklist=blacklist,
            allow_command_chains=allow_command_chains
        )

        # Initialize command executor
        self.executor = CommandExecutor(
            bash_path=bash_path,
            security_validator=self.security,
            initial_cwd=initial_cwd,
            max_output_kb=self.max_output_kb
        )

        # Initialize process manager for background processes
        self.process_manager = ProcessManager(max_buffer_lines=1000)

        logger.info(
            f"Terminal server '{name}' initialized - bash={bash_path}, "
            f"max_output_kb={self.max_output_kb}, default_timeout={self.default_timeout}s"
        )

    def get_template_vars(self) -> dict[str, Any]:
        """Provide custom template variables for schema rendering."""
        return {
            "name": self.name,
            "max_timeout": self.max_timeout,
            "default_timeout": self.default_timeout
        }

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a shell command either synchronously or as background process.
        
        Unified tool method that routes to foreground or background execution.
        
        Args:
            params: Tool parameters including:
                - command: Shell command to execute
                - background: Run as background process (default: false)
                - cwd: Working directory
                - timeout: Timeout for foreground commands
                - env_vars: Environment variables
                - process_id: Custom process ID for background
        
        Returns:
            dict: Execution result (foreground) or process info (background)
        """
        background = params.get("background", False)
        
        if background:
            # Route to background execution
            return await self.execute_background(params)
        else:
            # Route to foreground execution
            return await self.execute_command(params)

    async def execute_command(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a shell command and wait for completion.

        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml (with underscore conversion).

        Args:
            params: Tool parameters including command, cwd, timeout, env_vars

        Returns:
            dict: Execution result with status, stdout, stderr, exit_code
        """
        command = params["command"]
        cwd = params.get("cwd")
        timeout = params.get("timeout", self.default_timeout)
        env_vars = params.get("env_vars")
        # capture_output is available in schema but not currently used in executor
        # params.get("capture_output", True)

        # Get status context for updates
        status = params["_status"]

        # Validate timeout
        if timeout > self.max_timeout:
            error_msg = f"Timeout {timeout}s exceeds maximum of {self.max_timeout}s"
            await status.error(error_msg)
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "TimeoutExceeded"
            }

        # Check for cancellation
        cancellation_token = params.get("_cancellation_token")
        if cancellation_token and cancellation_token.is_cancelled:
            await status.error(f"Command cancelled before execution: {command}")
            return {
                "status": "cancelled",
                "command": command
            }

        await status.progress(f"Executing: {command}")

        # Execute command
        result = await self.executor.execute(
            command=command,
            cwd=cwd,
            timeout=timeout,
            env=env_vars,
            is_background=False
        )

        if result["status"] == "success":
            exit_code = result.get("exit_code", 0)
            exec_time = result.get("execution_time", 0)
            await status.end(
                f"Command completed (exit code {exit_code})",
                meta={"execution_time": exec_time}
            )
        else:
            error_msg = result.get("error", "Unknown error")
            await status.error(f"Command failed: {error_msg}")

        return result

    async def execute_background(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a long-running command in background.

        Args:
            params: Tool parameters including command, cwd, env_vars, process_id

        Returns:
            dict: Process information with process_id, pid, command
        """
        command = params["command"]
        cwd = params.get("cwd")
        env_vars = params.get("env_vars")
        custom_process_id = params.get("process_id")

        # Get status context
        status = params["_status"]

        await status.progress(f"Starting background process: {command}")

        # Execute in background (creates separate process, not persistent terminal)
        result = await self.executor.execute_background(
            command=command,
            cwd=cwd,
            env=env_vars
        )

        if result["status"] != "success":
            await status.error(f"Failed to start background process: {result.get('error')}")
            return result

        # Register with process manager
        process = result["process"]
        process_id = await self.process_manager.register_process(
            process=process,
            command=command,
            cwd=cwd,
            process_id=custom_process_id
        )

        await status.end(f"Background process started: {process_id}")

        return {
            "status": "success",
            "process_id": process_id,
            "pid": process.pid,
            "command": command,
            "cwd": cwd or self.executor.initial_cwd,
            "started_at": self.process_manager.processes[process_id]["started_at"]
        }

    async def get_output(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Get stdout/stderr from a background process.

        Args:
            params: Tool parameters including process_id, stream, clear_buffer

        Returns:
            dict: Output data with stdout, stderr, is_running, exit_code
        """
        process_id = params["process_id"]
        stream = params.get("stream", "both")
        clear_buffer = params.get("clear_buffer", False)

        # Get status context
        status = params["_status"]

        await status.progress(f"Retrieving output from process: {process_id}")

        result = await self.process_manager.get_output(
            process_id=process_id,
            stream=stream,
            clear_buffer=clear_buffer
        )

        if result["status"] == "success":
            is_running = result.get("is_running", False)
            status_msg = "running" if is_running else "finished"
            await status.end(f"Retrieved output from {status_msg} process {process_id}")
        else:
            await status.error(f"Failed to get output: {result.get('error')}")

        return result

    async def kill_process(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Terminate a background process.

        Args:
            params: Tool parameters including process_id, force

        Returns:
            dict: Status with killed flag and signal used
        """
        process_id = params["process_id"]
        force = params.get("force", False)

        # Get status context
        status = params["_status"]

        signal_type = "SIGKILL" if force else "SIGTERM"
        await status.progress(f"Killing process {process_id} with {signal_type}")

        result = await self.process_manager.kill_process(
            process_id=process_id,
            force=force
        )

        if result["status"] == "success":
            signal_used = result.get("signal", signal_type)
            await status.end(f"Process {process_id} killed with {signal_used}")
        else:
            await status.error(f"Failed to kill process: {result.get('error')}")

        return result

    async def cleanup(self):
        """Clean up all resources."""
        logger.info(f"Cleaning up terminal server '{self.name}'")
        await self.executor.cleanup()
        await self.process_manager.cleanup()
