"""Command execution with persistent sessions."""

import asyncio
import logging
import os
import time
from typing import Dict, Optional

from agent_system.utils.process_sandbox import ProcessSandbox, SandboxUnavailable
from .security import CommandSecurityValidator

logger = logging.getLogger(__name__)


def _restore_console_mode() -> None:
    """Undo the console-mode reset a spawned shell inflicts on Windows.

    Console modes belong to the CONSOLE, not the process: an MSYS bash (or
    cmd.exe) child inherits our console and clears its VT-processing flag on
    startup, even when its std handles are pipes. From that moment every ANSI
    escape the CLI prints renders literally. Whoever spawns the shell puts the
    flag back -- best effort, and a no-op outside a Windows console.
    """
    try:
        from agent_system.cli_utils.common import reassert_vt
        reassert_vt()
    except Exception:
        logger.debug("Could not restore console mode", exc_info=True)


class PersistentTerminal:
    """
    Persistent bash terminal session (like GitHub Copilot's run_in_terminal).

    Key Features:
    - Environment variables persist across commands
    - Working directory changes persist (cd commands)
    - Activated virtual environments stay active
    - Output from multiple commands accumulates
    """

    def __init__(
        self,
        bash_path: str,
        initial_cwd: Optional[str] = None,
        max_output_kb: int = 60,
        sandbox: Optional[ProcessSandbox] = None,
    ):
        """
        Initialize persistent terminal.

        Args:
            bash_path: Path to bash executable
            initial_cwd: Initial working directory (defaults to current directory)
            max_output_kb: Maximum output size in KB (default: 60, like GitHub Copilot)
        """
        self.bash_path = bash_path
        self.sandbox = sandbox or ProcessSandbox()
        self.cwd = initial_cwd or os.getcwd()
        self.env = os.environ.copy()
        self.max_output_kb = max_output_kb
        self.process: Optional[asyncio.subprocess.Process] = None
        self._output_buffer: list[str] = []
        self._lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start persistent bash session."""
        if self.process is not None:
            logger.warning("Terminal already started")
            return

        logger.info(f"Starting persistent terminal with bash: {self.bash_path}")

        # The whole session is confined, not each command: everything the
        # session spawns inherits the cage, which is the point.
        confined = self.sandbox.confine([self.bash_path], cwd=self.cwd)
        self.process = await asyncio.create_subprocess_exec(
            *confined.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # Merge stderr into stdout for interleaved output
            cwd=self.cwd,
            env=self.env
        )

        # Start output reader task
        self._reader_task = asyncio.create_task(self._read_output())
        logger.info(f"Persistent terminal started (PID: {self.process.pid})")
        # bash start resets the inherited console's VT flag
        _restore_console_mode()

    async def execute(
        self,
        command: str,
        timeout: Optional[float] = None,
        is_background: bool = False
    ) -> Dict:
        """
        Execute command in persistent session.

        Args:
            command: Command to execute
            timeout: Timeout in seconds (None = no timeout)
            is_background: If True, don't wait for completion

        Returns:
            dict: Execution result with status, output, exit_code, etc.
        """
        if self.process is None:
            await self.start()

        async with self._lock:
            start_time = time.time()

            # Clear previous output buffer
            self._output_buffer.clear()

            # Send command to bash with completion marker
            command_with_marker = f"{command}\necho '__CMD_DONE__'\n"
            self.process.stdin.write(command_with_marker.encode('utf-8'))
            await self.process.stdin.drain()

            if is_background:
                # Don't wait, return immediately
                return {
                    "status": "success",
                    "background": True,
                    "command": command,
                    "message": "Command sent to background terminal session"
                }

            # Wait for completion marker or timeout
            try:
                output = await asyncio.wait_for(
                    self._wait_for_completion(),
                    timeout=timeout
                )

                execution_time = time.time() - start_time

                # Check for output size limit
                truncated = len(output) > (self.max_output_kb * 1024)
                if truncated:
                    output = output[:self.max_output_kb * 1024]
                    output += "\n... [OUTPUT TRUNCATED - exceeded 60KB limit. Use filters like 'head', 'tail', 'grep' to limit output]"

                return {
                    "status": "success",
                    "exit_code": 0,  # TODO: Determine by checking $? in future impl
                    "stdout": output,
                    "stderr": "",  # Merged into stdout
                    "execution_time": execution_time,
                    "truncated": truncated,
                    "cwd": self.cwd,
                    "command": command
                }

            except asyncio.TimeoutError:
                partial_output = "".join(self._output_buffer)
                return {
                    "status": "error",
                    "error": f"Command timed out after {timeout}s",
                    "error_type": "TimeoutError",
                    "partial_output": partial_output,
                    "command": command
                }

    async def _read_output(self):
        """Continuously read output from bash process."""
        while True:
            try:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode('utf-8', errors='replace')
                self._output_buffer.append(decoded)
            except Exception as e:
                logger.error(f"Error reading terminal output: {e}")
                break

    async def _wait_for_completion(self) -> str:
        """
        Wait for command completion marker.

        Returns:
            str: Command output (without completion marker)
        """
        output_lines = []
        while True:
            if self._output_buffer:
                line = self._output_buffer.pop(0)
                if '__CMD_DONE__' in line:
                    # Remove the marker line
                    break
                output_lines.append(line)
            await asyncio.sleep(0.01)

        return ''.join(output_lines)

    async def get_output(self) -> str:
        """
        Get accumulated output from terminal.

        Returns:
            str: Current output buffer contents
        """
        return "".join(self._output_buffer)

    async def cleanup(self):
        """Clean up terminal session."""
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            except Exception as e:
                logger.error(f"Error cleaning up terminal: {e}")

        if self._reader_task:
            self._reader_task.cancel()


class CommandExecutor:
    """Manages command execution with persistent terminals and security validation."""

    def __init__(
        self,
        bash_path: str,
        security_validator: CommandSecurityValidator,
        initial_cwd: Optional[str] = None,
        max_output_kb: int = 60,
        sandbox: Optional[ProcessSandbox] = None,
    ):
        """
        Initialize command executor.

        Args:
            bash_path: Path to bash executable
            security_validator: Security validator instance
            initial_cwd: Initial working directory
            max_output_kb: Maximum output size in KB
        """
        self.bash_path = bash_path
        self.security = security_validator
        self.initial_cwd = initial_cwd or os.getcwd()
        self.max_output_kb = max_output_kb
        # danger-full-access by default: confine() then returns the argv
        # untouched, so an unconfigured deployment behaves exactly as before.
        self.sandbox = sandbox or ProcessSandbox()
        self.default_terminal: Optional[PersistentTerminal] = None

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
        is_background: bool = False
    ) -> Dict:
        """
        Execute command (simplified - use subprocess instead of persistent terminal for now).

        Args:
            command: Command to execute
            cwd: Working directory (if different from current)
            timeout: Timeout in seconds
            env: Environment variables to set
            is_background: Run in background without waiting

        Returns:
            dict: Execution result
        """
        # Validate security
        is_safe, msg = self.security.validate_command(command)
        if not is_safe:
            return {
                "status": "error",
                "error": msg,
                "error_type": "SecurityError",
                "command": command
            }

        try:
            # For now, use simple subprocess execution instead of persistent terminal
            # This avoids the complexity of marker-based completion detection
            
            # Prepare environment
            exec_env = os.environ.copy()
            if env:
                exec_env.update(env)

            # Create subprocess
            confined = self.sandbox.confine(
                [self.bash_path, "-c", command], cwd=cwd or self.initial_cwd)
            process = await asyncio.create_subprocess_exec(
                *confined.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # Merge for interleaved output
                cwd=cwd or self.initial_cwd,
                env=exec_env
            )

            if is_background:
                # Return process for background execution
                return {
                    "status": "success",
                    "process": process,
                    "command": command,
                    "cwd": cwd or self.initial_cwd,
                    "pid": process.pid
                }

            # Wait for completion with timeout
            try:
                start_time = time.time()
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
                execution_time = time.time() - start_time

                output = stdout.decode('utf-8', errors='replace')

                # Check for output size limit (in bytes, not characters)
                max_bytes = self.max_output_kb * 1024
                truncated = False
                
                # Check if output exceeds max_bytes when encoded
                output_encoded = output.encode('utf-8', errors='replace')
                if len(output_encoded) > max_bytes:
                    # Truncate at byte boundary, then decode
                    truncated_bytes = output_encoded[:max_bytes]
                    # Decode safely, ignoring incomplete sequences at end
                    output = truncated_bytes.decode('utf-8', errors='ignore')
                    output += "\n... [OUTPUT TRUNCATED - exceeded size limit. Use filters like 'head', 'tail', 'grep' to limit output]"
                    truncated = True

                return {
                    "status": "success",
                    "exit_code": process.returncode,
                    "stdout": output,
                    "stderr": "",  # Merged into stdout
                    "execution_time": execution_time,
                    "truncated": truncated,
                    "cwd": cwd or self.initial_cwd,
                    "command": command
                }

            except asyncio.TimeoutError:
                # Kill process on timeout
                process.kill()
                await process.wait()
                return {
                    "status": "error",
                    "error": f"Command timeout after {timeout}s",
                    "error_type": "TimeoutError",
                    "command": command
                }

        except SandboxUnavailable as e:
            # Expected outcome of a configured policy, not an incident: log it
            # without a traceback, or a confined deployment drowns its own log.
            logger.warning("Refusing to run unconfined: %s", e)
            return {
                "status": "error",
                "error": str(e),
                "error_type": "SandboxUnavailable",
                "command": command,
            }
        except Exception as e:
            logger.error(f"Error executing command '{command}': {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "command": command
            }
        finally:
            # Every bash -c spawn resets the console's VT flag; put it back
            # before the status lines for this very command get printed.
            _restore_console_mode()

    async def execute_background(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None
    ) -> Dict:
        """
        Execute command as a separate background process (not in persistent terminal).

        Args:
            command: Command to execute
            cwd: Working directory
            env: Environment variables

        Returns:
            dict: Process information with process handle
        """
        # Validate security
        is_safe, msg = self.security.validate_command(command)
        if not is_safe:
            return {
                "status": "error",
                "error": msg,
                "error_type": "SecurityError",
                "command": command
            }

        try:
            # Prepare environment
            exec_env = os.environ.copy()
            if env:
                exec_env.update(env)

            # Create subprocess
            confined = self.sandbox.confine(
                [self.bash_path, "-c", command], cwd=cwd or self.initial_cwd)
            process = await asyncio.create_subprocess_exec(
                *confined.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or self.initial_cwd,
                env=exec_env
            )

            # Best effort -- the child may reset the console mode a moment
            # AFTER this returns; the chat renderer re-asserts per line.
            _restore_console_mode()

            return {
                "status": "success",
                "process": process,
                "command": command,
                "cwd": cwd or self.initial_cwd,
                "pid": process.pid
            }

        except SandboxUnavailable as e:
            # Expected outcome of a configured policy, not an incident: log it
            # without a traceback, or a confined deployment drowns its own log.
            logger.warning("Refusing to run unconfined: %s", e)
            return {
                "status": "error",
                "error": str(e),
                "error_type": "SandboxUnavailable",
                "command": command,
            }
        except Exception as e:
            logger.error(f"Error starting background process '{command}': {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "command": command
            }

    async def cleanup(self):
        """Clean up all resources."""
        if self.default_terminal:
            await self.default_terminal.cleanup()
