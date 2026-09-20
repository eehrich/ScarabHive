"""Background process management."""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ProcessManager:
    """Manages background processes."""

    def __init__(self, max_buffer_lines: int = 1000):
        # Held, not fired and forgotten: a task nobody references can be
        # collected mid-flight, and cleanup has to be able to wait for it.
        self._tasks: set[asyncio.Task] = set()
        """
        Initialize process manager.

        Args:
            max_buffer_lines: Maximum lines to keep in output buffers
        """
        self.processes: Dict[str, Dict] = {}
        self.max_buffer_lines = max_buffer_lines

    def generate_process_id(self) -> str:
        """Generate unique process ID."""
        return f"bg_proc_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _owner_mismatch(proc_info: Dict, requester_session: Optional[str]) -> bool:
        """True if the requester is not allowed to touch this process.

        Deny only when the process has a known owner AND the requester is a
        different session. A None owner (legacy entry) or a None requester
        (internal/CLI call, never LLM-reachable) is allowed - the framework
        injects _session_id on every LLM tool call, so the cross-user case
        (both present, different) is the one that matters.
        """
        owner = proc_info.get("owner_session")
        return bool(owner and requester_session and owner != requester_session)

    async def register_process(
        self,
        process: asyncio.subprocess.Process,
        command: str,
        cwd: Optional[str] = None,
        process_id: Optional[str] = None,
        owner_session: Optional[str] = None,
        on_finish: Optional[Callable[[str], Awaitable[None]]] = None
    ) -> str:
        """
        Register a background process.

        Args:
            process: Subprocess instance
            command: Command being executed
            cwd: Working directory
            process_id: Optional custom process ID
            owner_session: Session that owns this process (for isolation)
            on_finish: Awaited once with the process id when the process has
                ended and its output is captured. Its failure is logged and
                dropped: the process is over either way.

        Returns:
            str: Process ID
        """
        if process_id is None:
            process_id = self.generate_process_id()

        self.processes[process_id] = {
            "process": process,
            "command": command,
            "cwd": cwd,
            "owner_session": owner_session,
            "started_at": datetime.now().isoformat(),
            "stdout_buffer": [],
            "stderr_buffer": [],
            "finished_at": None,
            "exit_code": None,
            "read_after_finish": False,
            "on_finish": on_finish
        }

        # Start output capture task
        task = asyncio.create_task(self._capture_output(process_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        logger.info(f"Registered background process {process_id}: {command}")
        return process_id

    async def _capture_output(self, process_id: str):
        """
        Capture output from background process.

        Args:
            process_id: Process ID to capture output from
        """
        if process_id not in self.processes:
            return

        proc_info = self.processes[process_id]
        process = proc_info["process"]

        async def read_stream(stream, buffer: List[str]):
            """Read from stream and append to buffer."""
            while True:
                try:
                    line = await stream.readline()
                    if not line:
                        break
                    decoded = line.decode('utf-8', errors='replace')
                    buffer.append(decoded)

                    # Enforce buffer size limit
                    if len(buffer) > self.max_buffer_lines:
                        buffer.pop(0)
                except Exception as e:
                    logger.error(f"Error reading stream for {process_id}: {e}")
                    break

        # Capture stdout and stderr concurrently
        await asyncio.gather(
            read_stream(process.stdout, proc_info["stdout_buffer"]),
            read_stream(process.stderr, proc_info["stderr_buffer"]),
            return_exceptions=True
        )

        # Mark as finished. EOF on both pipes is NOT the process ending: the
        # return code stays None until the child is reaped. Without this wait,
        # get_output answered for a finished process with finished_at set AND
        # is_running true, and the recorded exit_code stayed None for good --
        # which is what a session woken by on_finish reads first.
        await process.wait()
        proc_info["finished_at"] = datetime.now().isoformat()
        proc_info["exit_code"] = process.returncode
        logger.info(f"Background process {process_id} finished with exit code {process.returncode}")

        # Whoever asked to hear about the end hears about it here -- also when
        # the process failed: a caller waiting on it waits just the same.
        if proc_info["on_finish"] is not None:
            try:
                await proc_info["on_finish"](process_id)
            except Exception as e:
                logger.warning(f"on_finish for {process_id} failed: {e}")

    async def get_output(
        self,
        process_id: str,
        stream: str = "both",
        clear_buffer: bool = False,
        requester_session: Optional[str] = None
    ) -> Dict:
        """
        Get output from a background process.

        Args:
            process_id: Process ID
            stream: Which stream to get ("stdout", "stderr", "both")
            clear_buffer: Clear buffer after reading
            requester_session: Calling session (for ownership check)

        Returns:
            dict: Output data with status, stdout, stderr, is_running, exit_code
        """
        proc_info = self.processes.get(process_id)
        # Treat a foreign-owned process as not-found: don't leak its existence
        # or its captured stdout/stderr to another session.
        if proc_info is None or self._owner_mismatch(proc_info, requester_session):
            return {
                "status": "error",
                "error": f"Process {process_id} not found",
                "error_type": "ProcessNotFound"
            }

        process = proc_info["process"]
        # Whoever asked for a wake stops being rung once the result has been
        # read here: the session dealt with it by itself and starting a run of
        # it would cost a turn for nothing.
        if proc_info["finished_at"] is not None:
            proc_info["read_after_finish"] = True

        # Get output based on stream parameter
        stdout = ""
        stderr = ""

        if stream in ("stdout", "both"):
            stdout = "".join(proc_info["stdout_buffer"])
            if clear_buffer:
                proc_info["stdout_buffer"].clear()

        if stream in ("stderr", "both"):
            stderr = "".join(proc_info["stderr_buffer"])
            if clear_buffer:
                proc_info["stderr_buffer"].clear()

        return {
            "status": "success",
            "process_id": process_id,
            "stdout": stdout,
            "stderr": stderr,
            "is_running": process.returncode is None,
            "exit_code": process.returncode,
            "started_at": proc_info["started_at"],
            "finished_at": proc_info["finished_at"]
        }

    async def kill_process(
        self,
        process_id: str,
        force: bool = False,
        requester_session: Optional[str] = None
    ) -> Dict:
        """
        Kill a background process.

        Args:
            process_id: Process ID to kill
            force: Use SIGKILL instead of SIGTERM
            requester_session: Calling session (for ownership check)

        Returns:
            dict: Status with killed flag and signal used
        """
        proc_info = self.processes.get(process_id)
        # Foreign-owned process -> not-found: a session must not be able to kill
        # another session's process (cross-user control / DoS).
        if proc_info is None or self._owner_mismatch(proc_info, requester_session):
            return {
                "status": "error",
                "error": f"Process {process_id} not found",
                "error_type": "ProcessNotFound"
            }

        process = proc_info["process"]

        if process.returncode is not None:
            return {
                "status": "error",
                "error": f"Process {process_id} already terminated with exit code {process.returncode}",
                "error_type": "ProcessAlreadyTerminated"
            }

        signal_used = "SIGKILL" if force else "SIGTERM"

        try:
            if force:
                process.kill()
            else:
                process.terminate()

            # Wait for process to die (with timeout)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # Force kill if terminate didn't work
                logger.warning(f"Process {process_id} did not terminate, forcing kill")
                process.kill()
                await process.wait()
                signal_used = "SIGKILL (forced)"

            logger.info(f"Killed process {process_id} with {signal_used}")

            return {
                "status": "success",
                "process_id": process_id,
                "killed": True,
                "signal": signal_used,
                "exit_code": process.returncode
            }

        except Exception as e:
            logger.error(f"Error killing process {process_id}: {e}")
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    def list_processes(self, requester_session: Optional[str] = None) -> List[Dict]:
        """
        List registered background processes.

        Args:
            requester_session: If given, only processes owned by this session
                (plus legacy ownerless ones) are returned. None lists all
                (internal/CLI use).

        Returns:
            list: List of process information dicts
        """
        result = []
        for process_id, proc_info in self.processes.items():
            if self._owner_mismatch(proc_info, requester_session):
                continue
            result.append({
                "process_id": process_id,
                "command": proc_info["command"],
                "cwd": proc_info["cwd"],
                "started_at": proc_info["started_at"],
                "finished_at": proc_info["finished_at"],
                "is_running": proc_info["process"].returncode is None,
                "exit_code": proc_info["exit_code"],
                "pid": proc_info["process"].pid
            })
        return result

    async def cleanup(self):
        """Stop the processes AND let go of the tasks watching them.

        A capture task carries the wake that reports the end, and that can
        take minutes. A server that is closing has nothing left to report
        from, so the wait is short and what is left is cancelled.
        """
        for process_id in list(self.processes.keys()):
            await self.kill_process(process_id, force=True)
        if self._tasks:
            done, pending = await asyncio.wait(set(self._tasks), timeout=5.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5.0)
                logger.info(f"Gave up waiting for {len(pending)} capture task(s)")
