"""BasicOperations Tool Server implementation.

This module provides basic utility operations including wait, countdown,
echo functionality, and ping operations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


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

    async def wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Handle wait operation with countdown status.
        
        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml.
        """
        try:
            seconds = float(params["seconds"])
            # Always use server-configured default_update_interval. Ignore caller-supplied update_interval.
            update_interval = float(self.default_update_interval)
            message = params.get("message", "Waiting")
            
            # Get status context for updates (mandatory from framework)
            status = params["_status"]
            
            # Validate parameters
            if seconds <= 0:
                return {"status": "error", "error": "Wait time must be positive"}
            if seconds > self.max_wait_seconds:
                return {"status": "error", "error": f"Wait time exceeds maximum of {self.max_wait_seconds} seconds"}
            if update_interval <= 0:
                return {"status": "error", "error": "Update interval must be positive"}
            
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
            # str(): `params.get("message", "Waiting")` returns None when the
            # model sends "message": null, and nothing validates tool params
            # against the schema at runtime. The old line interpolated it
            # ("None: completed"), this one subscripts it -- so without this
            # a completed wait would return an error.
            await status.end(
                f"Waited {elapsed:.1f}s of {seconds:.1f}s -- {str(message)[:60]}")
            
            logger.info(f"Wait completed after {elapsed:.1f} seconds")
            
            return {
                "status": "success",
                "message": "Wait completed successfully",
                "requested_seconds": seconds,
                "actual_seconds": elapsed,
                "user_message": message
            }
            
        except ValueError as e:
            error_msg = f"Invalid numeric parameter: {e}"
            logger.error(error_msg)
            return {"status": "error", "error": error_msg}
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
            include_details = params.get("include_details", False)
            
            current_time = datetime.now()
            timestamp = current_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # Include milliseconds
            
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