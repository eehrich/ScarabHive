"""Terminal Tool Server implementation.

This module provides bash command execution -- every command in its own
`bash -c` -- background process management, and output capture.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_system.core.session_presence import wake_blocked, wake_session
from agent_system.plugins.cache import PluginCache
from agent_system.paths import launch_dir
from agent_system.tools.schema_based import SchemaBasedToolServer

from .executor import CommandExecutor
from .platform_detect import PlatformDetector
from .process_manager import ProcessManager
from agent_system.utils.process_sandbox import ProcessSandbox
from .security import CommandSecurityValidator

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

# Status lines are read at a glance, in a stream that is also carrying the
# model's tokens. A full pipeline with quotes, escapes and newlines wraps over
# several lines and pushes everything around it out of view -- so commands get
# folded to one line and capped here before they go into a status message.
_CMD_DISPLAY_LIMIT = 70

#: What a recorded result may carry per stream. terminal caps a live answer at
#: max_output_size_kb (60 KB) -- half of that each keeps the recorded one in the
#: same order of magnitude instead of writing a whole build log to disk.
_RECORDED_STREAM_CAP = 30_000


def _short_cmd(command: str, limit: int = _CMD_DISPLAY_LIMIT) -> str:
    """Fold a command to a single line of at most `limit` characters.

    The end line gets the same budget as the progress line on purpose: the WebUI
    writes both into the same row, so the end message REPLACES the progress
    message and is all that survives (static/js/chat_module.js, phase 'end').
    """
    single_line = " ".join(command.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[:limit - 3] + "..."


def _output_size(result: dict[str, Any]) -> str:
    """Line count of what a command produced -- the part the reader wants.

    The executor caps stdout at max_output_kb, so this counts what survived,
    not what was produced. Saying so beats a number that is silently wrong by
    orders of magnitude.
    """
    stdout = result.get("stdout") or ""
    if not stdout:
        return "no output"
    lines = stdout.count("\n") + (0 if stdout.endswith("\n") else 1)
    plural = "s" if lines != 1 else ""
    if result.get("truncated"):
        return f"{lines}+ lines (truncated)"
    return f"{lines} line{plural}"


class TerminalServer(SchemaBasedToolServer):
    """Terminal tool server for executing shell commands, each in its own ``bash -c``.

    This server provides:
    - execute_command: Execute commands and wait for completion
    - execute_background: Start long-running commands in background
    - get_output: Retrieve output from background processes
    - kill_process: Terminate background processes

    All tools are automatically loaded from schema.yaml by SchemaBasedToolServer.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """
        Initialize terminal server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, server_config)

        # Extract security configuration
        security_config = getattr(server_config, 'security', {})
        if isinstance(security_config, dict):
            whitelist = security_config.get('whitelist')
            blacklist = security_config.get('blacklist')
            allow_command_chains = security_config.get('allow_command_chains', True)
            extra_dangerous_patterns = security_config.get('dangerous_patterns')
        else:
            whitelist = None
            blacklist = None
            allow_command_chains = True
            extra_dangerous_patterns = None

        # Extract limits configuration
        limits_config = getattr(server_config, 'limits', {})
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
        platform_config = getattr(server_config, 'platform', {})
        if isinstance(platform_config, dict):
            bash_path_config = platform_config.get('bash_path', 'auto')
            initial_cwd = platform_config.get('initial_cwd')
        else:
            bash_path_config = 'auto'
            initial_cwd = None

        # The same value serves as the working directory of every command and
        # as the base the sandbox resolves its workspace root against, and the
        # harness writes it relative ("data/workspace"). A relative base puts
        # that segment into the cage path twice, so resolve it once, here.
        if initial_cwd:
            initial_cwd = str(Path(initial_cwd).resolve())
        else:
            # No directory configured means the one the person started in --
            # not the working directory, which since the CLIs enter the project
            # at startup is the checkout. Started from the project, as
            # everything was until now, the two are the same.
            initial_cwd = str(launch_dir())

        # Process confinement. Absent or unset, the mode is
        # danger-full-access and nothing about spawning changes; an
        # unknown mode raises here rather than silently not confining.
        sandbox_config = getattr(server_config, 'sandbox', None)
        if not isinstance(sandbox_config, dict):
            sandbox_config = {}
        self.sandbox = ProcessSandbox.from_config(
            sandbox_config, base=initial_cwd)
        if self.sandbox.confines:
            logger.info("Terminal confinement: mode=%s workspace=%s",
                        self.sandbox.mode, self.sandbox.workspace_root)

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
            allow_command_chains=allow_command_chains,
            extra_dangerous_patterns=extra_dangerous_patterns
        )

        # Initialize command executor
        self.executor = CommandExecutor(
            bash_path=bash_path,
            security_validator=self.security,
            initial_cwd=initial_cwd,
            max_output_kb=self.max_output_kb,
            sandbox=self.sandbox,
        )

        # Where a finished process's outcome survives THIS process: a woken
        # run is a new one and has none of these in memory.
        self._recorded = PluginCache(name)
        # process_ids between their check and their registration, see
        # execute_background.
        self._starting: set[str] = set()

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
            "default_timeout": self.default_timeout,
            # A whitelisted terminal offers no cwd and no env_vars (CommandExecutor.refusal).
            "whitelisted": self._whitelisted(),
        }

    def _whitelisted(self) -> bool:
        security = getattr(self, "security", None)
        return bool(getattr(security, "whitelist_patterns", None))

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
                - wake: Wake this session when a background process ends
        
        Returns:
            dict: Execution result (foreground) or process info (background)
        """
        background = params.get("background", False)
        
        if background:
            # Route to background execution
            return await self.execute_background(params)
        # Route to foreground execution
        result = await self.execute_command(params)
        if params.get("wake"):
            # The one case where asking for a wake used to be answered with
            # nothing at all. There is nothing to wake for: the result is here.
            result["wake"] = False
            result["wake_note"] = ("wake applies to background=true only; this ran in "
                                   "the foreground and its result is in this answer")
        return result

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
            await status.error(f"Cancelled before execution: {_short_cmd(command)}")
            return {
                "status": "cancelled",
                "command": command
            }

        await status.progress(f"Executing: {_short_cmd(command)}")

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
            # The end line has to stand on its own -- in the WebUI it overwrites
            # the progress line, so it is the only record of what ran.
            await status.end(
                f"{_short_cmd(command)} -- exit {exit_code}, "
                f"{exec_time:.2f}s, {_output_size(result)}",
                meta={"execution_time": exec_time, "exit_code": exit_code}
            )
        else:
            error_msg = result.get("error", "Unknown error")
            await status.error(f"{_short_cmd(command)} failed: {error_msg}")

        return result

    def _wake_callback(self, params: dict[str, Any]):
        """What runs when a background process ends -- or the reason nothing will.

        Returns (callback, note); exactly one of them is set. The note is what
        the caller is told INSTEAD of a wake: a model that asked to be woken
        and silently was not would end its turn and wait for a message that
        never comes. Both reasons are known before the process starts, which
        is why this is asked then and not at the end.
        """
        session_id = params.get("_session_id") or ""
        user_id = params.get("_user_id") or ""
        blocked = wake_blocked(self.system_config, session_id, user_id)
        if blocked:
            return None, blocked

        async def on_finish(process_id: str, info: dict) -> None:
            if info["read_after_finish"]:
                # Read, killed or its id handed to a later run while the
                # capture task was still draining the pipes: nothing left to
                # hand over and nobody to ring. Recording it anyway would
                # leave the copy in the cache for its full hour.
                return
            # The ring holds the ENTRY, not the id. An id is free again once
            # its process is over, so a later run can hold it -- and a lookup
            # by id would then let that fresh process answer for this ring.
            await self._record(process_id, info)
            await wake_session(
                self.system_config, session_id, user_id,
                what=f"background process {process_id} "
                     f"(exit {info.get('exit_code')})",
                still_needed=lambda: not info["read_after_finish"])

        return on_finish, ""

    async def execute_background(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a long-running command in background.

        Args:
            params: Tool parameters including command, cwd, env_vars, process_id, wake

        Returns:
            dict: Process information with process_id, pid, command
        """
        process_id = params.get("process_id")
        if not process_id:
            return await self._start_background(params)
        # Reserved from the check to the registration: several awaits lie
        # between them (the record, the spawn), so two calls with the same id
        # both passed the check -- and the second registration replaced the
        # entry of a process that then ran on, unreadable and unkillable.
        if process_id in self._starting:
            message = (f"process_id {process_id} is being started by another call; "
                       f"choose another one or leave it out")
            await params["_status"].error(message[:140])
            return {"status": "error", "error": message,
                    "error_type": "ProcessIdInUse"}
        self._starting.add(process_id)
        try:
            return await self._start_background(params)
        finally:
            self._starting.discard(process_id)

    async def _start_background(self, params: dict[str, Any]) -> dict[str, Any]:
        command = params["command"]
        cwd = params.get("cwd")
        env_vars = params.get("env_vars")
        custom_process_id = params.get("process_id")
        # Refused before the takeover below changes anything: taking over a
        # finished process's id drops its recorded outcome, and a command that
        # will not run must not cost that. The spawn asks the same again.
        refused = self.executor.refusal(command, cwd, env_vars)
        if refused:
            await params["_status"].error(refused["error"][:140])
            return refused
        # Before the process is spawned, not after: replacing the entry of a
        # running process would leave it running with nobody able to read its
        # output or kill it. A FINISHED entry is a different thing: the tool
        # invites a stable id ("build", "dev"), nothing ever removes an entry,
        # and refusing the second run because the first one ENDED would make
        # that invitation a trap -- for the rest of the process's life.
        taken = (self.process_manager.processes.get(custom_process_id)
                 if custom_process_id else None)
        # Another session's id stays theirs, finished or not: taking it over
        # would disarm THEIR wake and delete THEIR recorded result.
        refusal = custom_process_id and await self._held_by_another_session(
            taken, custom_process_id, params.get("_session_id"))
        if refusal:
            message = refusal
            await params["_status"].error(message[:140])
            return {"status": "error", "error": message,
                    "error_type": "ProcessIdInUse"}
        if taken is not None and taken["process"].returncode is None:
            message = (f"process_id {custom_process_id} is in use by a running process; "
                       f"choose another one or leave it out")
            await params["_status"].error(message[:140])
            return {"status": "error", "error": message,
                    "error_type": "ProcessIdInUse"}
        if taken is not None:
            # Its armed wake goes with it: once the id belongs to another run
            # the old outcome is unreachable under it either way, and nobody
            # will mutate the entry the ring holds any more.
            taken["read_after_finish"] = True
            # The recording goes with it. A record left under this id would
            # answer get_output for the NEW process with the OLD result, and
            # that is worse than the refusal this branch just lifted. The entry
            # itself needs no removal: register_process replaces it below.
            try:
                await self._recorded.delete(custom_process_id)
            except Exception as e:  # noqa: BLE001 - reusing the id must not fail over it
                logger.warning(f"Could not drop the record of {custom_process_id}: {e}")
        on_finish, wake_note = (self._wake_callback(params) if params.get("wake")
                                else (None, ""))

        # Get status context
        status = params["_status"]

        await status.progress(f"Starting background process: {_short_cmd(command)}")

        # Execute in background (a separate process of its own)
        result = await self.executor.execute_background(
            command=command,
            cwd=cwd,
            env=env_vars
        )

        if result["status"] != "success":
            await status.error(f"Failed to start background process: {result.get('error')}")
            return result

        # Register with process manager, tagging the owning session so other
        # sessions can't read/kill this process (cross-user isolation).
        process = result["process"]
        process_id = await self.process_manager.register_process(
            process=process,
            command=command,
            cwd=cwd,
            process_id=custom_process_id,
            owner_session=params.get("_session_id"),
            on_finish=on_finish
        )

        # process_id is what the follow-up tools take, so it leads. The wake
        # goes in front of the command, not behind it: the WebUI cuts the row
        # at the right edge, and the command is the part that may be cut.
        wake_tag = " (wakes this session)" if on_finish is not None else ""
        # process_id may be one the caller chose, of any length, so it gets
        # its own budget: a blind cut of the whole row would drop the PID and
        # the wake note and keep only the id. The command is last and is the
        # part that may lose characters.
        head = (f"Background process {_short_cmd(process_id, 30)} started "
                f"(PID {process.pid}){wake_tag}")
        await status.end(f"{head}: {_short_cmd(command, 138 - len(head))}")

        result = {
            "status": "success",
            "process_id": process_id,
            "pid": process.pid,
            "command": command,
            "cwd": cwd or self.executor.initial_cwd,
            "started_at": self.process_manager.processes[process_id]["started_at"]
        }
        # Only when it was asked for: an answer about a wake nobody wanted is
        # noise in every single background call.
        if params.get("wake"):
            result["wake"] = on_finish is not None
            if wake_note:
                result["wake_note"] = wake_note
        return result

    async def _held_by_another_session(self, taken: Any, process_id: str,
                                       session_id: Any) -> str | None:
        """Why *process_id* may not be taken over, or None when it may.

        Another session's id stays theirs, live or recorded. The record counts
        as much as the entry: it outlives it (another process, a restart, an
        entry reclaimed after enough later runs) and it is what that session's
        woken run reads.
        """
        theirs = (f"process_id {process_id} belongs to another session; "
                  f"choose another one or leave it out")
        if taken is not None and self.process_manager._owner_mismatch(taken, session_id):
            return theirs
        try:
            recorded = await self._recorded.get(process_id)
        except Exception as e:  # noqa: BLE001 - unreadable is not "nobody's"
            # Taking it over would delete a record we could not look at. Every
            # id hits the same read, so "choose another one" would not help.
            logger.warning(f"Could not read the record of {process_id}, "
                           f"so it is not taken over: {e}")
            return (f"could not check who holds process_id {process_id} "
                    f"(its record is unreadable); leave process_id out")
        if isinstance(recorded, dict) and self.process_manager._owner_mismatch(recorded, session_id):
            return theirs
        return None

    async def _record(self, process_id: str, info: dict[str, Any]) -> None:
        """Put the outcome where another process can read it.

        Only for a wake: without one the caller polls from this process and
        never needs the file. A failure here costs the recording, not the
        process -- which is over either way.
        """
        if not info:
            return
        try:
            await self._recorded.set(process_id, {
                "process_id": process_id,
                "command": info.get("command"),
                "exit_code": info.get("exit_code"),
                "started_at": info.get("started_at"),
                "finished_at": info.get("finished_at"),
                "owner_session": info.get("owner_session"),
                "stdout": "".join(info.get("stdout_buffer", []))[-_RECORDED_STREAM_CAP:],
                "stderr": "".join(info.get("stderr_buffer", []))[-_RECORDED_STREAM_CAP:],
            })
        except Exception as e:  # noqa: BLE001 - reporting must not break the process
            logger.warning(f"Could not record the result of {process_id}: {e}")

    async def _recall(self, process_id: str, requester_session: str | None,
                      stream: str = "both") -> dict | None:
        """A result recorded by the process that ran it, for the run that was
        woken for it. Read once: it is handed over, not kept.

        ``stream`` is honoured here for the same reason the live reader
        honours it: a woken run that asks for stderr alone is avoiding the
        60 000 characters of stdout, and answering with both anyway spends
        exactly the context it was trying to save."""
        try:
            record = await self._recorded.get(process_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read the recorded result of {process_id}: {e}")
            return None
        if not record:
            return None
        # Same rule as the live registry: a foreign-owned process is not this
        # session's business, and an ownerless one stays readable.
        owner = record.get("owner_session")
        if owner and requester_session and owner != requester_session:
            return None
        await self._recorded.delete(process_id)
        return {
            "status": "success",
            "process_id": process_id,
            "stdout": record.get("stdout", "") if stream in ("stdout", "both") else "",
            "stderr": record.get("stderr", "") if stream in ("stderr", "both") else "",
            "is_running": False,
            "exit_code": record.get("exit_code"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "source": "recorded",
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
            clear_buffer=clear_buffer,
            requester_session=params.get("_session_id")
        )

        if result["status"] == "success" and not result.get("is_running", True):
            # The live registry answered for a process that is over, so the
            # caller has the outcome and the copy written for a wake is spent.
            # Dropping it was _recall's job alone -- and _recall runs only when
            # the live registry MISSES, which is never the case in the process
            # that ran the command. Its record sat in data/cache for the full
            # hour, one per armed wake, up to 60 000 characters each.
            try:
                await self._recorded.delete(process_id)
            except Exception as e:  # noqa: BLE001 - the answer is already built
                logger.debug(f"Could not drop the record of {process_id}: {e}")

        if result["status"] != "success":
            # Not in THIS process's memory. A run woken for this process is a
            # new one and never has it, so the outcome the old process recorded
            # is the answer here.
            recorded = await self._recall(process_id, params.get("_session_id"),
                                          stream)
            if recorded is not None:
                result = recorded

        if result["status"] == "success":
            is_running = result.get("is_running", False)
            status_msg = "running" if is_running else "finished"
            stdout_len = len(result.get("stdout", ""))
            stderr_len = len(result.get("stderr", ""))
            # Include process_id and output size in end message
            await status.end(
                f"Output retrieved from process '{process_id}' ({status_msg}): "
                f"{stdout_len} bytes stdout, {stderr_len} bytes stderr"
            )
        else:
            await status.error(f"Failed to get output from '{process_id}': {result.get('error')}")

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
            force=force,
            requester_session=params.get("_session_id")
        )

        if result["status"] == "success":
            signal_used = result.get("signal", signal_type)
            # Include process_id and signal in end message
            await status.end(f"Process '{process_id}' terminated with {signal_used}")
        else:
            await status.error(f"Failed to kill process '{process_id}': {result.get('error')}")

        return result

    async def cleanup(self):
        """Clean up all resources."""
        logger.info(f"Cleaning up terminal server '{self.name}'")
        await self.process_manager.cleanup()

    async def stop_plugin(self) -> None:
        """The name the framework actually calls at shutdown.

        ``cleanup()`` waits for the capture tasks and cancels what is left, but
        nothing reached it: ``plugins/capabilities.stop_plugin`` is the only
        shutdown hook the adapter knows (tool_adapter.py:364, :381), and it looks for
        THIS name. Every background process therefore outlived the run that
        started it, with its capture task still attached.
        """
        await self.cleanup()
