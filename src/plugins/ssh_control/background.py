"""Remote commands that outlive the call that started them.

Same vocabulary as terminal's ProcessManager -- ``process_id``, ``get_output``,
``kill_process`` -- because a model that learned one should not have to learn
the other. What differs is what a remote process COSTS: it holds a connection
out of the machine's pool for its whole life, and a pool is three connections
by default. That is why ``start`` refuses before it takes the last one.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def _text(line: Any) -> str:
    """asyncssh reads str while an encoding is set (utf-8 by default) and bytes
    when it is not. The buffers hold text either way."""
    return line if isinstance(line, str) else line.decode("utf-8", errors="replace")


class RemoteProcessManager:
    """Background commands on remote machines, one held connection each."""

    def __init__(self, connection_manager, max_buffer_lines: int = 1000,
                 keep_finished: int = 50):
        self._connections = connection_manager
        self.max_buffer_lines = max_buffer_lines
        self.keep_finished = keep_finished
        self.processes: Dict[str, Dict] = {}
        # Held, not fired and forgotten: cleanup has to wait for them, and a
        # task nobody references can be collected mid-flight.
        self._tasks: set[asyncio.Task] = set()

    def generate_process_id(self) -> str:
        return f"ssh_proc_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _owner_mismatch(proc_info: Dict, requester_session: Optional[str]) -> bool:
        """Whether this process belongs to somebody else. Same rule as terminal:
        a process registered without an owner stays readable by everyone, so
        processes started before ownership existed do not become unreachable."""
        owner = proc_info.get("owner_session")
        return bool(owner and requester_session and owner != requester_session)

    def _forget_old_finished(self) -> None:
        """Finished commands stay readable, but not for the life of the
        process: each one holds up to two full line buffers, and a job that
        starts one every few minutes would grow this dict without end.

        By the time they FINISHED, not the time they started: a long command
        started first can end last, and dropping it in start order would take
        the one that just ended -- whose caller is about to read it.
        """
        finished = sorted((info["finished_at"], pid)
                          for pid, info in self.processes.items()
                          if info["finished_at"] is not None)
        for _, pid in finished[:max(0, len(finished) - self.keep_finished)]:
            del self.processes[pid]

    def running_on(self, machine_name: str) -> int:
        """How many background commands hold, or are about to hold, a
        connection of that machine."""
        return sum(1 for info in self.processes.values()
                   if info["machine"] == machine_name and info["finished_at"] is None)

    async def start(
        self,
        machine_name: str,
        command: str,
        process_id: Optional[str] = None,
        owner_session: Optional[str] = None,
        on_finish: Optional[Callable[[str, Dict], Awaitable[None]]] = None
    ) -> Dict[str, Any]:
        """Start a command and return once it RUNS, not once it is done.

        A command that cannot even be started is reported here rather than in a
        background task nobody awaits: the caller can still act on it.
        """
        # Everything that decides whether this command may run happens with no
        # await in between. Tool calls run in PARALLEL, so a check that is
        # followed by an await and only then by the registration is read by
        # every simultaneous call as "nothing is running yet".
        taken = self.processes.get(process_id) if process_id else None
        if taken is not None and self._owner_mismatch(taken, owner_session):
            return {
                "status": "error",
                "error": f"process_id {process_id} belongs to another session; "
                         f"choose another one or leave it out",
                "error_type": "ProcessIdInUse"
            }
        if taken is not None and taken["finished_at"] is None:
            return {
                "status": "error",
                "error": f"process_id {process_id} is in use by a running command; "
                         f"choose another one or leave it out",
                "error_type": "ProcessIdInUse"
            }
        if taken is not None:
            # A FINISHED entry does not own the id: the tool invites a stable
            # one, and _forget_old_finished reclaims it only after 50 further
            # commands have ended -- for a recurring id, never. Its armed wake
            # goes with it; the old outcome is unreachable under this id now.
            taken["read_after_finish"] = True
        self._forget_old_finished()
        process_id = process_id or self.generate_process_id()
        self.processes[process_id] = {
            "process": None,   # filled in once the channel is open
            "machine": machine_name,
            "command": command,
            "owner_session": owner_session,
            "started_at": datetime.now().isoformat(),
            "stdout_buffer": deque(maxlen=self.max_buffer_lines),
            "stderr_buffer": deque(maxlen=self.max_buffer_lines),
            "finished_at": None,
            "exit_code": None,
            "error": None,
            "read_after_finish": False,
            "on_finish": on_finish
        }

        try:
            pool = await self._connections.pool_for(machine_name)
            # One slot has to stay free, or an ordinary command to this machine
            # waits 30 s for a connection and then times out. With
            # max_connections=1 that leaves nothing, and saying so is more use
            # than a background command that starves every other one. This entry
            # counts itself, which is what makes the check hold under parallel
            # calls -- each one sees the places the others have taken.
            limit = pool.config.max_connections - 1
            running = self.running_on(machine_name)
            if running > limit:
                del self.processes[process_id]
                return {
                    "status": "error",
                    "error": (f"{machine_name} allows {limit} background command(s) at a "
                              f"time ({running - 1} running or starting); one connection "
                              f"of max_connections={pool.config.max_connections} stays "
                              f"free for ordinary commands. Raise max_connections for "
                              f"this machine or wait for one to finish."),
                    "error_type": "BackgroundLimitReached"
                }
            conn = await pool.acquire()
        except BaseException:
            self.processes.pop(process_id, None)
            raise

        try:
            process = await conn.create_process(command)
        except BaseException:
            # The slot goes back even when this is a cancellation: a connection
            # left in `in_use` is gone from the pool until the process restarts.
            self.processes.pop(process_id, None)
            await pool.release(conn)
            raise

        if process_id not in self.processes:
            # Stopped while the channel was opening -- kill_process and cleanup
            # take the entry away, because there is no process to signal yet and
            # no capture task for cleanup to wait on.
            process.close()
            await pool.release(conn)
            return {"status": "error",
                    "error": f"{process_id} was stopped before it started",
                    "error_type": "StoppedBeforeStart"}

        self.processes[process_id]["process"] = process
        task = asyncio.create_task(
            self._capture_output(process_id, self.processes[process_id], pool, conn))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(f"Started background command {process_id} on {machine_name}: {command}")
        return {"status": "success", "process_id": process_id,
                "machine": machine_name, "command": command,
                "started_at": self.processes[process_id]["started_at"]}

    async def _read_stream(self, stream, buffer: deque, process_id: str) -> None:
        while True:
            try:
                line = await stream.readline()
                if not line:
                    break
                buffer.append(_text(line))
            except Exception as e:
                logger.debug(f"Stream ended for {process_id}: {e}")
                break

    async def _capture_output(self, process_id: str, proc_info: Dict, pool, conn) -> None:
        """Read both streams to their end, record the outcome, hand the
        connection back -- and only then tell whoever asked to hear about it.
        The entry is held, not looked up: once the command is over its id may
        be handed to a later one."""
        process = proc_info["process"]
        try:
            await asyncio.gather(
                self._read_stream(process.stdout, proc_info["stdout_buffer"], process_id),
                self._read_stream(process.stderr, proc_info["stderr_buffer"], process_id),
                return_exceptions=True
            )
            # EOF on the streams is not the command ending: the exit status
            # arrives with the channel's close.
            await process.wait()
            proc_info["exit_code"] = process.exit_status
        except Exception as e:
            proc_info["error"] = str(e)
            logger.warning(f"Background command {process_id} ended badly: {e}")
        finally:
            proc_info["finished_at"] = datetime.now().isoformat()
            # The slot goes back before anyone is told, so a woken session that
            # immediately runs another command finds the pool as it should be.
            try:
                await pool.release(conn)
            except Exception as e:  # pragma: no cover - release is best effort
                logger.warning(f"Could not release the connection of {process_id}: {e}")
            logger.info(f"Background command {process_id} finished "
                        f"with exit code {proc_info['exit_code']}")

        if proc_info["on_finish"] is not None:
            try:
                # The entry this task holds, not a lookup by id: the id may
                # already name a later run (a finished id can be reused).
                await proc_info["on_finish"](process_id, proc_info)
            except Exception as e:
                logger.warning(f"on_finish for {process_id} failed: {e}")

    def _found(self, process_id: str, requester_session: Optional[str]) -> Optional[Dict]:
        """The process, or None when it is not this caller's to see. A foreign
        process is treated as missing: its existence and its output are not
        another session's business."""
        proc_info = self.processes.get(process_id)
        if proc_info is None or self._owner_mismatch(proc_info, requester_session):
            return None
        return proc_info

    async def get_output(
        self,
        process_id: str,
        stream: str = "both",
        clear_buffer: bool = False,
        requester_session: Optional[str] = None
    ) -> Dict[str, Any]:
        proc_info = self._found(process_id, requester_session)
        if proc_info is None:
            return {"status": "error", "error": f"Process {process_id} not found",
                    "error_type": "ProcessNotFound"}

        # Whoever asked for a wake stops being rung once the result has been
        # read here: the session dealt with it by itself.
        if proc_info["finished_at"] is not None:
            proc_info["read_after_finish"] = True

        stdout = stderr = ""
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
            "machine": proc_info["machine"],
            "command": proc_info["command"],
            "stdout": stdout,
            "stderr": stderr,
            "is_running": proc_info["finished_at"] is None,
            "exit_code": proc_info["exit_code"],
            "started_at": proc_info["started_at"],
            "finished_at": proc_info["finished_at"],
            "error": proc_info["error"]
        }

    async def kill_process(
        self,
        process_id: str,
        force: bool = False,
        requester_session: Optional[str] = None
    ) -> Dict[str, Any]:
        proc_info = self._found(process_id, requester_session)
        if proc_info is None:
            return {"status": "error", "error": f"Process {process_id} not found",
                    "error_type": "ProcessNotFound"}
        if proc_info["finished_at"] is not None:
            # Asking for it to stop is dealing with it, so an armed wake has
            # nothing left to ring about -- see the signal path below.
            proc_info["read_after_finish"] = True
            return {"status": "success", "process_id": process_id,
                    "signal": "none", "note": "already finished",
                    "exit_code": proc_info["exit_code"]}

        if proc_info["process"] is None:
            # It has claimed its place but has no channel yet. Taking the entry
            # away IS the stop: start() finds it gone and gives the connection
            # back itself.
            del self.processes[process_id]
            logger.info(f"Stopped {process_id} before its channel was open")
            return {"status": "success", "process_id": process_id,
                    "signal": "none", "note": "stopped before it started"}

        process = proc_info["process"]
        signal_used = "SIGKILL" if force else "SIGTERM"
        try:
            process.kill() if force else process.terminate()
        except Exception as e:
            return {"status": "error", "error": f"Could not signal {process_id}: {e}",
                    "error_type": "KillFailed"}

        # The capture task notices the end, records it and releases the
        # connection; it is not raced here.
        #
        # Killing it IS dealing with it. get_output was the only thing that set
        # this flag, and nobody reads the output of a command they just ended --
        # so an armed wake went on ringing for its full five minutes and then
        # started a whole agent-cli run to report a death the caller ordered.
        proc_info["read_after_finish"] = True
        logger.info(f"Signalled background command {process_id} with {signal_used}")
        return {"status": "success", "process_id": process_id, "signal": signal_used}

    async def cleanup(self) -> None:
        """Stop what still runs AND wait for the capture tasks to let go.

        Signalling alone was not enough: kill_process returns as soon as the
        signal is out, and whoever called this goes on to close the pools --
        replacing the semaphore a capture task is still about to release.
        """
        for process_id, proc_info in list(self.processes.items()):
            if proc_info["finished_at"] is None:
                await self.kill_process(process_id, force=True)
        if self._tasks:
            # The wait is for the connection to be handed back. What may still
            # be running after it is the wake's ring loop, which lives in the
            # same task and can take minutes -- a server that is closing has
            # nothing left to wake anybody about.
            done, pending = await asyncio.wait(set(self._tasks), timeout=5.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5.0)
                logger.info(f"Gave up waiting for {len(pending)} capture task(s)")
