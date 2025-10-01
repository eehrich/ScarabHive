"""BasicOperations MCP Server implementation.

This module provides basic utility operations including wait, countdown,
echo functionality, and ping operations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class BasicOperationsServer(SchemaBasedMCPServer):
    """BasicOperations MCP server providing utility operations.

    This server provides:
    - Wait operation with countdown status updates
    - Ping operation for connectivity testing
    
    All tools are automatically loaded from schema.yaml by SchemaBasedMCPServer.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Extract configuration with sensible defaults from mcp_config
        self.max_wait_seconds = float(getattr(mcp_config, 'max_wait_seconds', 3600))
        self.default_update_interval = float(getattr(mcp_config, 'default_update_interval', 1.0))
        
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
            
            return result
            
        except Exception as e:
            error_msg = f"Ping operation failed: {e}"
            logger.error(error_msg)
            return {"status": "error", "error": error_msg}