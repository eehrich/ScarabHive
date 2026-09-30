"""BasicOperations Tool Server implementation.

This module provides basic utility operations including wait, countdown,
echo functionality, and ping operations.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agent_system.core.session_presence import presence_for, wake_blocked, wake_depth, wake_session
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.tools.status import current_request_id

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

# Below this a wake costs more than it saves: the woken turn reads the whole
# conversation again, while waiting here costs nothing but the seconds.
WAKE_MIN_SECONDS = 60.0
# The schema's maxLength, which nothing enforces: the message is echoed in the
# answer and repeated in every status line.
MESSAGE_MAX_CHARS = 100


def _flag(value: Any) -> bool:
    """A boolean argument; the text "false" is false, not a non-empty string."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


class BasicOperationsServer(SchemaBasedToolServer):
    """BasicOperations tool server providing utility operations.

    This server provides:
    - Wait operation with countdown status updates
    - Ping operation for connectivity testing
    
    All tools are automatically loaded from schema.yaml by SchemaBasedToolServer.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, server_config)
        
        # Extract configuration with sensible defaults from server_config
        self.max_wait_seconds = float(getattr(server_config, 'max_wait_seconds', 3600))
        self.default_update_interval = float(getattr(server_config, 'default_update_interval', 1.0))
        # A task with no reference can be collected mid-sleep.
        self._wakes: set[asyncio.Task] = set()
        
        logger.info(
            f"BasicOperations server '{name}' initialized - max_wait_seconds={self.max_wait_seconds}, "
            f"default_update_interval={self.default_update_interval}"
        )

    def get_template_vars(self) -> dict[str, Any]:
        """Provide custom template variables for schema rendering."""
        return {
            "name": self.name,
            "max_wait_seconds": self.max_wait_seconds
        }

    async def _wake_refused(self, session_id: str, user_id: str) -> str:
        """Why this session cannot be woken when the wait is over, or ""."""
        if not session_id:
            return "this call has no session to wake"
        if wake_depth():
            return "this run was itself woken and ends with its turn"
        blocked = wake_blocked(self.system_config, session_id, user_id)
        if blocked:
            return blocked
        # wake_blocked leaves this out on purpose (session_presence.py): reading
        # it parses the session file, so it is asked here, off the loop.
        presence = presence_for(self.system_config)
        try:
            state = await asyncio.to_thread(presence.get, session_id, user_id) if presence else None
        except Exception as exc:  # noqa: BLE001 - an unreadable session is no reason to fail the wait
            logger.warning("Could not tell whether session %s is a sub-agent's: %s", session_id, exc)
            return ""
        return "a sub-agent's session is never woken" if state and state.get("sub_agent") else ""

    async def stop_plugin(self) -> None:
        """An armed wake is nothing but a sleeping task: it goes with the
        process. Named in the log, or it is only asyncio's "task destroyed"."""
        for task in list(self._wakes):
            task.cancel()
            logger.warning("BasicOperations '%s': a wake was armed and is dropped with this process", self.name)
        await asyncio.gather(*self._wakes, return_exceptions=True)

    async def _wake_after(self, seconds: float, session_id: str, user_id: str,
                          message: str, started_by: str) -> None:
        await asyncio.sleep(seconds)
        await wake_session(self.system_config, session_id, user_id,
                           what=f"the wait is over: {message}", started_by=started_by)

    async def wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Handle wait operation with countdown status.
        
        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml.
        """
        try:
            try:
                seconds = float(params.get("seconds"))
            except (TypeError, ValueError):  # missing, null, a list, "abc"
                seconds = math.nan
            # Always use server-configured default_update_interval. Ignore caller-supplied update_interval.
            update_interval = float(self.default_update_interval)
            message = str(params.get("message") or "Waiting")[:MESSAGE_MAX_CHARS]
            wake = _flag(params.get("wake"))

            # Get status context for updates (mandatory from framework)
            status = params["_status"]
            
            # Validate parameters
            if not seconds > 0:  # NaN too: it never counts down and would wait forever
                return {"status": "error", "error": "seconds must be a number above 0"}
            if seconds > self.max_wait_seconds:
                return {"status": "error", "error": f"Wait time exceeds maximum of {self.max_wait_seconds} seconds"}
            if update_interval <= 0:
                return {"status": "error", "error": "Update interval must be positive"}
            
            session_id = str(params.get("_session_id") or "")
            user_id = str(params.get("_user_id") or "")
            refused = ""
            # A long wait need not hold the turn: the session can be woken when
            # it is over. Only where a wake reaches it -- otherwise waiting here
            # is the only thing that works.
            if wake and seconds >= WAKE_MIN_SECONDS:
                refused = await self._wake_refused(session_id, user_id)
                if not refused:
                    task = asyncio.create_task(self._wake_after(
                        seconds, session_id, user_id, message[:60], current_request_id.get() or ""))
                    self._wakes.add(task)
                    task.add_done_callback(self._wakes.discard)
                    await status.end(f"{message}: waking the session in {seconds:.1f}s")
                    return {
                        "status": "success",
                        "waiting": True,
                        "requested_seconds": seconds,
                        "user_message": message,
                        "note": f"end your turn now -- you are woken in {seconds:.0f} s. The wake lives in "
                                f"this process: a one-shot agent-cli run, or a restart before the time is up, "
                                f"drops it. Then nobody rings, and waiting without wake is the way.",
                    }

            start_time = time.time()
            end_time = start_time + seconds
            
            logger.info(f"Starting wait for {seconds} seconds with message: '{message}' using update_interval: {update_interval}")
            
            # Check for cancellation before starting the wait
            cancellation_token = params.get("_cancellation_token")
            if cancellation_token and cancellation_token.is_cancelled:
                await status.error(f"{message}: cancelled before start")
                return {
                    "status": "cancelled", 
                    "requested_seconds": seconds,
                    "actual_seconds": 0,
                    "user_message": message,
                    "cancelled": True
                }
            
            # Initial status update (English) - always include message (user message or "Waiting")
            await status.progress(f"{message}: starting countdown - {seconds:.1f}s")
            
            last_update_time = start_time  # Track when we last sent an update
            status_update_interval = 10.0  # Send status updates every 10 seconds
            
            while True:
                current_time = time.time()
                remaining = end_time - current_time
                
                if remaining <= 0:
                    break
                
                # Check for cancellation using the new cancellation token system
                cancellation_token = params.get("_cancellation_token")
                if cancellation_token and cancellation_token.is_cancelled:
                    elapsed = time.time() - start_time
                    await status.error(f"{message}: cancelled after {elapsed:.1f}s")
                    return {
                        "status": "cancelled", 
                        "requested_seconds": seconds,
                        "actual_seconds": elapsed,
                        "user_message": message,
                        "cancelled": True
                    }
                
                # Only send status update every 10 seconds
                if current_time - last_update_time >= status_update_interval:
                    await status.progress(f"{message}: {remaining:.1f}s remaining")
                    last_update_time = current_time
                
                # Sleep for update interval or remaining time, whichever is smaller
                sleep_time = min(update_interval, remaining)
                
                # For responsive cancellation, break sleep into smaller chunks (max 1 second)
                # This allows more frequent cancellation checks during long waits
                max_chunk = 1.0
                if sleep_time > max_chunk:
                    # Sleep in chunks, checking for cancellation between each chunk
                    chunks = int(sleep_time / max_chunk)
                    remainder = sleep_time % max_chunk
                    
                    for i in range(chunks):
                        # Check for cancellation before each sleep chunk
                        if cancellation_token and cancellation_token.is_cancelled:
                            elapsed = time.time() - start_time
                            await status.error(f"{message}: cancelled after {elapsed:.1f}s")
                            return {
                                "status": "cancelled", 
                                "requested_seconds": seconds,
                                "actual_seconds": elapsed,
                                "user_message": message,
                                "cancelled": True
                            }
                        await asyncio.sleep(max_chunk)
                    
                    # Sleep the remainder if any
                    if remainder > 0:
                        if cancellation_token and cancellation_token.is_cancelled:
                            elapsed = time.time() - start_time
                            await status.error(f"{message}: cancelled after {elapsed:.1f}s")
                            return {
                                "status": "cancelled", 
                                "requested_seconds": seconds,
                                "actual_seconds": elapsed,
                                "user_message": message,
                                "cancelled": True
                            }
                        await asyncio.sleep(remainder)
                else:
                    # Short sleep, no need to chunk
                    await asyncio.sleep(sleep_time)
                
                # Debug log to show the effective sleep_time and configured update_interval
                logger.debug(
                    "BasicOperations '%s' sleeping for %.3fs (update_interval=%.3fs, remaining=%.3fs)",
                    self.name, sleep_time, update_interval, remaining
                )
            
            # end, not progress: this was the last thing said, so the scope's
            # default END overwrote it with a bare "completed" and the elapsed
            # time was lost.
            elapsed = time.time() - start_time
            await status.end(
                f"Waited {elapsed:.1f}s of {seconds:.1f}s -- {message[:60]}")
            
            logger.info(f"Wait completed after {elapsed:.1f} seconds")
            
            return {
                "status": "success",
                "message": "Wait completed successfully",
                "requested_seconds": seconds,
                "actual_seconds": elapsed,
                "user_message": message,
                # Asked to be woken and waited anyway: say why, or the model reads this as a wake that worked.
                **({"wake_note": refused or f"a wake needs at least {WAKE_MIN_SECONDS:.0f} s"}
                   if wake else {})
            }
            
        except Exception as e:
            error_msg = f"Wait operation failed: {e}"
            logger.error(error_msg)
            return {"status": "error", "error": error_msg}

    async def ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Handle ping operation.
        
        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml.
        """
        try:
            include_details = _flag(params.get("include_details"))
            
            timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            
            result = {
                "status": "success",
                "message": "Pong!",
                "timestamp": timestamp,
                "server_name": self.name
            }
            
            if include_details:
                import platform
                import sys
                
                result["details"] = {
                    "python_version": sys.version,
                    "platform": platform.platform(),
                    "server_config": {
                        "max_wait_seconds": self.max_wait_seconds,
                        "default_update_interval": self.default_update_interval
                    }
                }
            
            logger.debug(f"Ping from {self.name} at {timestamp}")

            # ping never said anything at all, so the line read "completed".
            status = params.get("_status")
            if status:
                await status.end(f"Pong from {self.name} at {timestamp}")

            return result
            
        except Exception as e:
            error_msg = f"Ping operation failed: {e}"
            logger.error(error_msg)
            return {"status": "error", "error": error_msg}