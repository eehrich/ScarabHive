"""Command execution: every command in its own ``bash -c``."""

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


class CommandExecutor:
    """Runs each command in its own ``bash -c``, after CommandExecutor.refusal has passed it."""

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

    def refusal(self, command: str, cwd: Optional[str] = None,
                env: Optional[Dict[str, str]] = None) -> Optional[Dict]:
        """Why *command* may not be spawned as asked, or None.

        Every spawn asks it (execute, execute_background): no entry into the
        server reaches a process without passing here. Asked earlier as well
        by a caller that would otherwise change something first.

        With a whitelist the command also runs in the configured working
        directory and environment. A whitelist checks the command string and
        nothing else, while the directory decides which files the command
        reads and writes and the environment how it runs (PYTHONPATH,
        PYTHONSTARTUP, PATH, LD_PRELOAD all put code of the caller's choosing
        into the one allowed command). Taken from the model, they would undo
        what the whitelist is for.
        """
        is_safe, msg = self.security.validate_command(command)
        if not is_safe:
            return {
                "status": "error",
                "error": msg,
                "error_type": "SecurityError",
                "command": command
            }
        if self.security.whitelist_patterns:
            given = [name for name, value in (("cwd", cwd), ("env_vars", env)) if value]
            if given:
                named = " and ".join(given)
                return {
                    "status": "error",
                    "error": (f"This terminal runs only the commands its configuration allows, in its "
                              f"configured working directory and environment -- {named} cannot be set "
                              f"here. Send the command again without {named}."),
                    "error_type": "ConfiguredOnly",
                    "command": command
                }
        return None

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
        is_background: bool = False
    ) -> Dict:
        """
        Execute command in its own ``bash -c``.

        Args:
            command: Command to execute
            cwd: Working directory (if different from current)
            timeout: Timeout in seconds
            env: Environment variables to set
            is_background: Run in background without waiting

        Returns:
            dict: Execution result
        """
        refused = self.refusal(command, cwd, env)
        if refused:
            return refused

        try:
            # Prepare environment
            exec_env = os.environ.copy()
            if env:
                exec_env.update(env)

            # Create subprocess
            # to_thread: the first confine() probes the sandbox backend with a
            # synchronous subprocess.run (up to 10s); that must not freeze the loop.
            confined = await asyncio.to_thread(
                self.sandbox.confine,
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
        Execute command as a separate background process, in its own ``bash -c``.

        Args:
            command: Command to execute
            cwd: Working directory
            env: Environment variables

        Returns:
            dict: Process information with process handle
        """
        refused = self.refusal(command, cwd, env)
        if refused:
            return refused

        try:
            # Prepare environment
            exec_env = os.environ.copy()
            if env:
                exec_env.update(env)

            # Create subprocess
            # to_thread: the first confine() probes the sandbox backend with a
            # synchronous subprocess.run (up to 10s); that must not freeze the loop.
            confined = await asyncio.to_thread(
                self.sandbox.confine,
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
