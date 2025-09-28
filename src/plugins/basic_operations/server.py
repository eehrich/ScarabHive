"""BasicOperations MCP Server implementation.

This module provides basic utility operations including wait            # Ini                # U            # Final status update (English) and include 'waiting' and optional message
            elapsed = time.time() - start_time
            if message:
                await status.progress(f"Waiting ({message}): completed after {elapsed:.1f}s")
            else:
                await status.progress(f"Waiting: completed after {elapsed:.1f}s")status with countdown (English) and include 'waiting' and optional message
                if message:
                    await status.progress(f"Waiting ({message}): {remaining:.1f}s remaining")
                else:
                    await status.progress(f"Waiting: {remaining:.1f}s remaining")status update (English) - include 'waiting' keyword and optional user message
            if message:
                await status.progress(f"Waiting ({message}): starting countdown - {seconds:.1f}s")
            else:
                await status.progress(f"Waiting: starting countdown - {seconds:.1f}s") countdown,
echo functionality, and ping operations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from agent_system.mcp.schema_based import SchemaBasedMCPServer

logger = logging.getLogger(__name__)


class BasicOperationsServer(SchemaBasedMCPServer):
    """BasicOperations MCP server providing utility operations.

    This server provides:
    - Wait operation with countdown status updates
    - Ping operation for connectivity testing
    
    All tools are automatically loaded from schema.yaml by SchemaBasedMCPServer.
    """

    def __init__(self, name: str, config: dict[str, Any] | None = None, ssl_verify: bool = True):
        """Initialize the BasicOperations server."""
        super().__init__(name, config, ssl_verify)
        
        # Extract configuration with sensible defaults
        self.max_wait_seconds = float(self.config.get("max_wait_seconds", 3600))
        self.default_update_interval = float(self.config.get("default_update_interval", 1.0))
        # Log effective configuration for debugging lifecycle issues where
        # the registry may create instances with incomplete config.
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

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Route tool calls to appropriate handlers."""
        logger.debug(f"Calling tool '{tool}' with params: {params}")
        
        if tool == "wait":
            return await self._handle_wait(params)
        elif tool == "ping":
            return await self._handle_ping(params)
        else:
            return {"status": "error", "error": f"Unknown tool: {tool}"}

    async def _handle_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle wait operation with countdown status."""
        try:
            seconds = float(params["seconds"])
            # Always use server-configured default_update_interval. Ignore caller-supplied update_interval.
            update_interval = float(self.default_update_interval)
            message = params.get("message", "Waiting")
            
            # Get status context for updates
            status = params.get("_status")
            
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
                
                # Update status with countdown (English) - always include message
                await status.progress(f"{message}: {remaining:.1f}s remaining")
                
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
            
            # Final status update (English) - always include message
            elapsed = time.time() - start_time
            await status.progress(f"{message}: completed after {elapsed:.1f}s")
            
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

    async def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ping operation."""
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
            
            return result
            
        except Exception as e:
            error_msg = f"Ping operation failed: {e}"
            logger.error(error_msg)
            return {"status": "error", "error": error_msg}

    def get_default_action(self) -> str:
        """Return the default action for this server."""
        return "ping"