"""Command execution: every command in its own ``bash -c``."""

import asyncio
import codecs
import logging
import os
import signal
import time
import weakref
from typing import Dict, Optional, Tuple

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


#: POSIX: every shell in a session of its own, so that signal_tree reaches all
#: it starts. It also takes the command off the terminal the server runs in:
#: a Ctrl+C there no longer reaches it directly (the cancelled tool call kills
#: it), and it cannot read that terminal (/dev/tty) any more.
#: Everywhere: nothing to read on stdin (a background command: an input that
#: stays open, see execute_background). A command that asks reads the end of
#: its input at once instead of prompting in the terminal agent-cli runs in,
#: where it took the person's keys, Ctrl+C included (measured: PowerShell's
#: "Path[0]:" waited there). Most then end with their error; Read-Host, `read`
#: and `set /p` answer empty, and a [Y/n] prompt takes its default. Not a
#: console of its own on Windows: its codepage is the OEM one (850, not the
#: terminal's 65001), and a native tool's output would change with it.
_SPAWN_OPTIONS = {"stdin": asyncio.subprocess.DEVNULL,
                  **({} if os.name == "nt" else {"start_new_session": True})}


def _join_windows_job(process) -> None:
    """Put a spawned shell into a job object of its own, so kill_tree can end
    everything it starts.

    Ending bash.exe alone leaves what Git Bash started running, and taskkill /T
    does not reach it either: in a chain or a pipeline the Windows parent of
    the command is an intermediate process that is already gone (measured).
    Every descendant joins the job, though. The shell may start a child in the
    moment before it is assigned; that child escapes, as all of them did
    before. A failure leaves the process without a job, i.e. as before.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        handle = kernel32.OpenProcess(0x0100 | 0x0001, False, process.pid)
        assigned = bool(handle) and kernel32.AssignProcessToJobObject(job, handle)
        if handle:
            kernel32.CloseHandle(handle)
        if not assigned:
            kernel32.CloseHandle(job)
            return
        # Closing the handle does not end the job's processes; it goes with
        # the process object, or earlier with release_job.
        close = weakref.finalize(process, kernel32.CloseHandle, job)
        process._terminal_job = (kernel32, job, close)
    except Exception:  # noqa: BLE001 - without a job the process runs as before
        logger.debug("Could not put process %s into a job object", process.pid, exc_info=True)


def signal_tree(process, sig: int) -> None:
    """Send *sig* to the shell and everything it started (POSIX).

    Every shell is spawned in a session of its own (_SPAWN_OPTIONS), so its
    pid is the process group of all it starts. Signalling the shell alone left
    a chain's or a pipeline's commands running (measured on Linux:
    ``echo a; sleep 32; echo b`` with timeout 1 left the sleep behind). The
    group is signalled even when the shell has already ended: a command it
    put in the background may still hold the group.
    """
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # the group is gone
    except AttributeError:  # no killpg (Windows)
        pass


def release_job(process) -> None:
    """Close the job handle and the input of a process whose shell has ended.

    A finished background entry stays in the registry, and so did its handle
    (measured: one more per entry). What the shell left running is not ended
    by this -- it was not before the job object either; it reads the end of
    its input from now on.
    """
    job = getattr(process, "_terminal_job", None)
    if job is not None:
        del process._terminal_job
        job[2]()  # the finalizer: closes once, never again at collection
    held = getattr(process, "_terminal_input", None)
    if held is not None:
        del process._terminal_input
        held()  # the write end of its input, closed once


async def kill_tree(process) -> None:
    """Kill *process* and every process it started, and wait for it, at most
    five seconds.

    Measured before: on Windows a 1 s timeout on ``sleep 8`` answered after
    8 s and the sleep ran on -- the wait after the kill also waits for the
    pipes the orphan still held; on Linux the same for a chain or a pipeline.
    The wait is bounded so that a child still holding the pipes cannot hold up
    the answer.
    """
    job = getattr(process, "_terminal_job", None)
    if job is not None:
        kernel32, handle, _close = job
        kernel32.TerminateJobObject(handle, 1)
    if os.name != "nt":
        signal_tree(process, signal.SIGKILL)
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass  # already gone
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("Process %s was killed, but its pipes are still open", process.pid)


class CommandExecutor:
    """Runs each command in its own ``bash -c``, after CommandExecutor.refusal has passed it."""

    def __init__(
        self,
        bash_path: str,
        security_validator: CommandSecurityValidator,
        initial_cwd: Optional[str] = None,
        max_output_kb: int = 60,
        sandbox: Optional[ProcessSandbox] = None,
        pass_secrets_env: bool = False,
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
        self.pass_secrets_env = pass_secrets_env

    def _environment(self, env: Optional[Dict[str, str]]) -> Dict[str, str]:
        """The server's environment for a command, *env* laid over it.

        Without ``pass_secrets_env`` the variables the server took from its
        secrets files (config/secrets.env and the local one) are left out, so
        ``env`` and a logged environment do not show them. Not a boundary:
        reads are not confined, and the files stay readable to the command.
        """
        exec_env = os.environ.copy()
        if not self.pass_secrets_env:
            from agent_system.config.settings import SECRETS_FROM_FILE_ENV, set_by_the_environment
            for name in list(exec_env):
                if not set_by_the_environment(name):
                    del exec_env[name]
            # It names every one of them with a 12-hex SHA-256 prefix of its
            # value: enough to confirm a guessed short secret.
            exec_env.pop(SECRETS_FROM_FILE_ENV, None)
        if env:
            exec_env.update(env)
        return exec_env

    @staticmethod
    async def _read_capped(process, max_bytes: int, kept: Optional[bytearray] = None,
                           more: Optional[list] = None) -> Tuple[bytes, bool]:
        """The merged output up to *max_bytes*, and whether there was more.

        Reads to the end either way -- a pipe nobody drains blocks the command
        -- but keeps no more than the cap. communicate() held the whole output
        in memory before it was cut: 150 MB at the peak for a 50 MB output.

        *kept* and *more* (a one-element list) belong to the caller, so what
        was read survives a timeout or a cancel that ends this task.
        """
        kept = bytearray() if kept is None else kept
        more = [False] if more is None else more
        while chunk := await process.stdout.read(65536):
            room = max_bytes - len(kept)
            if len(chunk) > room:
                more[0] = True
            kept += chunk[:room]
        await process.wait()
        return bytes(kept), more[0]

    @staticmethod
    def _decoded(kept: bytes, truncated: bool) -> str:
        # Not final when cut: a UTF-8 sequence split at the cap is held back
        # instead of becoming a replacement character.
        output = codecs.getincrementaldecoder('utf-8')(errors='replace').decode(
            bytes(kept), final=not truncated)
        if truncated:
            output += "\n... [OUTPUT TRUNCATED - exceeded size limit. Use filters like 'head', 'tail', 'grep' to limit output]"
        return output

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
        is_background: bool = False,
        cancellation_token=None,
    ) -> Dict:
        """
        Execute command in its own ``bash -c``.

        Args:
            command: Command to execute
            cwd: Working directory (if different from current)
            timeout: Timeout in seconds
            env: Environment variables to set
            is_background: Run in background without waiting
            cancellation_token: The tool call's token; a graceful cancel
                kills the command at once instead of at the force-cancel

        Returns:
            dict: Execution result
        """
        refused = self.refusal(command, cwd, env)
        if refused:
            return refused
        try:
            # A relative cwd is taken from where the commands run, as a `cd`
            # in the command would take it -- not from the server's working
            # directory, which is the checkout since the CLIs enter it.
            cwd = os.path.join(self.initial_cwd, cwd) if cwd else self.initial_cwd
            # Prepare environment
            exec_env = self._environment(env)

            # Create subprocess
            # to_thread: the first confine() probes the sandbox backend with a
            # synchronous subprocess.run (up to 10s); that must not freeze the loop.
            confined = await asyncio.to_thread(
                self.sandbox.confine,
                [self.bash_path, "-c", command], cwd=cwd)
            process = await asyncio.create_subprocess_exec(
                *confined.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # Merge for interleaved output
                cwd=cwd,
                env=exec_env,
                **_SPAWN_OPTIONS,
            )
            _join_windows_job(process)

            if is_background:
                # Return process for background execution
                return {
                    "status": "success",
                    "process": process,
                    "command": command,
                    "cwd": cwd,
                    "pid": process.pid
                }

            # Wait for completion, the timeout or a graceful cancel. Before,
            # only the force-cancel (tool_cleanup_timeout, 30 s) ended a
            # command a Stop had asked to end.
            # Held here, not in the reading task: a timeout or a cancel ends
            # that task, and what the command wrote until then is the answer.
            kept, more = bytearray(), [False]
            reading = asyncio.ensure_future(
                self._read_capped(process, self.max_output_kb * 1024, kept, more))
            waiters = {reading}
            if cancellation_token is not None:
                waiters.add(asyncio.ensure_future(cancellation_token.wait_for_cancellation()))
            try:
                start_time = time.time()
                done, _ = await asyncio.wait(
                    waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if reading not in done:
                    await kill_tree(process)
                    partial = {
                        "stdout": self._decoded(kept, more[0]),
                        "stderr": "",  # Merged into stdout
                        "truncated": more[0],
                        "command": command
                    }
                    if done:
                        return {"status": "cancelled", "error": "Command cancelled",
                                "error_type": "Cancelled", **partial}
                    return {"status": "error", "error": f"Command timeout after {timeout}s",
                            "error_type": "TimeoutError", **partial}
                kept_bytes, truncated = reading.result()
                execution_time = time.time() - start_time
                output = self._decoded(kept_bytes, truncated)

                return {
                    "status": "success",
                    "exit_code": process.returncode,
                    "stdout": output,
                    "stderr": "",  # Merged into stdout
                    "execution_time": execution_time,
                    "truncated": truncated,
                    "cwd": cwd,
                    "command": command
                }

            except asyncio.CancelledError:
                # The tool call was cancelled (the framework force-cancels it
                # after the agent's tool_cleanup_timeout). The command must not
                # run on unsupervised: no registry holds it, so kill_process
                # could never reach it.
                await kill_tree(process)
                raise
            finally:
                for waiter in waiters:
                    waiter.cancel()

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
            # A relative cwd is taken from where the commands run, as a `cd`
            # in the command would take it -- not from the server's working
            # directory, which is the checkout since the CLIs enter it.
            cwd = os.path.join(self.initial_cwd, cwd) if cwd else self.initial_cwd
            # Prepare environment
            exec_env = self._environment(env)

            # Create subprocess
            # to_thread: the first confine() probes the sandbox backend with a
            # synchronous subprocess.run (up to 10s); that must not freeze the loop.
            confined = await asyncio.to_thread(
                self.sandbox.confine,
                [self.bash_path, "-c", command], cwd=cwd)
            # An input that stays open and that nobody writes to: a watcher
            # that stops once its input closes (esbuild, tailwind --watch)
            # keeps running, and what the person types reaches it no more than
            # a foreground command. Closed when the shell has ended (release_job).
            reading, writing = os.pipe()
            try:
                process = await asyncio.create_subprocess_exec(
                    *confined.argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=exec_env,
                    **{**_SPAWN_OPTIONS, "stdin": reading},
                )
            except BaseException:
                os.close(writing)
                raise
            finally:
                os.close(reading)
            process._terminal_input = weakref.finalize(process, os.close, writing)
            _join_windows_job(process)

            # Best effort -- the child may reset the console mode a moment
            # AFTER this returns; the chat renderer re-asserts per line.
            _restore_console_mode()

            return {
                "status": "success",
                "process": process,
                "command": command,
                "cwd": cwd,
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
