"""ComfyUI HTTP/WebSocket Client for AgentSystem.

Provides async client for interacting with ComfyUI's REST API.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

import aiohttp

logger = logging.getLogger(__name__)


class ComfyUIClient:
    """Async client for ComfyUI API.
    
    Handles communication with a ComfyUI server including:
    - Server status checks
    - Workflow queue submission
    - Job status polling
    - Output file retrieval
    """
    
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8188,
        output_dir: Path | None = None,
        timeout: float = 30.0
    ) -> None:
        """Initialize ComfyUI client.
        
        Args:
            host: ComfyUI server hostname
            port: ComfyUI server port
            output_dir: Directory for downloaded output files
            timeout: Default request timeout in seconds
        """
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self.output_dir = output_dir or Path("data/comfyui/outputs")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.client_id = str(uuid.uuid4())
    
    async def ping(self) -> dict[str, Any]:
        """Check if ComfyUI server is reachable.
        
        Returns:
            Dict with status and queue information
        """
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(f"{self.base_url}/queue") as resp:
                    if resp.status == 200:
                        queue_data = await resp.json()
                        return {
                            "status": "online",
                            "host": f"{self.host}:{self.port}",
                            "queue_pending": len(queue_data.get("queue_pending", [])),
                            "queue_running": len(queue_data.get("queue_running", []))
                        }
                    return {"status": "error", "code": resp.status}
        except asyncio.TimeoutError:
            return {"status": "offline", "error": "Connection timeout"}
        except aiohttp.ClientConnectorError as e:
            return {"status": "offline", "error": f"Connection failed: {e}"}
        except Exception as e:
            logger.exception("Unexpected error pinging ComfyUI")
            return {"status": "offline", "error": str(e)}
    
    async def queue_prompt(self, workflow: dict[str, Any]) -> dict[str, Any]:
        """Submit workflow to execution queue.
        
        Args:
            workflow: ComfyUI workflow in API format
            
        Returns:
            Dict with prompt_id and status on success, or error details
        """
        data = {
            "prompt": workflow,
            "client_id": self.client_id
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    f"{self.base_url}/prompt",
                    json=data,
                    headers={"Content-Type": "application/json"}
                ) as resp:
                    result = await resp.json()
                    if resp.status != 200:
                        logger.error("Failed to queue prompt: %s", result)
                        return {"status": "error", "error": result.get("error", "Unknown error")}
                    # Add status field for consistency
                    result["status"] = "queued"
                    return result
        except asyncio.TimeoutError:
            logger.warning("Timeout queuing prompt to ComfyUI")
            return {"status": "error", "error": "Connection timeout - ComfyUI server may be busy"}
        except aiohttp.ClientConnectorError as e:
            logger.warning("Cannot connect to ComfyUI server at %s:%s", self.host, self.port)
            return {"status": "error", "error": f"Cannot connect to ComfyUI server at {self.host}:{self.port}. Is it running?"}
        except Exception as e:
            logger.exception("Unexpected error queuing prompt")
            return {"status": "error", "error": str(e)}
    
    async def get_history(self, prompt_id: str) -> dict[str, Any]:
        """Get execution history/results for a prompt.
        
        Args:
            prompt_id: The prompt ID to query
            
        Returns:
            Dict with execution history and outputs
        """
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(f"{self.base_url}/history/{prompt_id}") as resp:
                    return await resp.json()
        except aiohttp.ClientConnectorError:
            logger.warning("Cannot connect to ComfyUI server for history lookup")
            return {"error": f"Cannot connect to ComfyUI server at {self.host}:{self.port}"}
        except Exception as e:
            logger.exception("Unexpected error getting history for %s", prompt_id)
            return {"error": str(e)}
    
    async def get_status(self, prompt_id: str) -> dict[str, Any]:
        """Get status of a specific job.
        
        Args:
            prompt_id: The prompt ID to check
            
        Returns:
            Dict with status (pending/running/completed/unknown)
        """
        # Check queue first
        queue = await self.get_queue()
        
        if "error" in queue:
            return {"status": "unknown", "prompt_id": prompt_id, "error": queue["error"]}
        
        # Check if running
        for item in queue.get("queue_running", []):
            if len(item) > 1 and item[1] == prompt_id:
                return {"status": "running", "prompt_id": prompt_id}
        
        # Check if pending
        for idx, item in enumerate(queue.get("queue_pending", [])):
            if len(item) > 1 and item[1] == prompt_id:
                return {"status": "pending", "prompt_id": prompt_id, "position": idx + 1}
        
        # Check history for completion
        history = await self.get_history(prompt_id)
        if prompt_id in history:
            job_data = history[prompt_id]
            # Check if there was an error
            if job_data.get("status", {}).get("status_str") == "error":
                return {
                    "status": "failed",
                    "prompt_id": prompt_id,
                    "error": job_data.get("status", {}).get("messages", [])
                }
            return {"status": "completed", "prompt_id": prompt_id}
        
        return {"status": "unknown", "prompt_id": prompt_id}
    
    async def get_queue(self) -> dict[str, Any]:
        """Get current queue state.
        
        Returns:
            Dict with queue_pending and queue_running lists
        """
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(f"{self.base_url}/queue") as resp:
                    return await resp.json()
        except aiohttp.ClientConnectorError:
            logger.warning("Cannot connect to ComfyUI server for queue status")
            return {"error": f"Cannot connect to ComfyUI server at {self.host}:{self.port}"}
        except Exception as e:
            logger.exception("Unexpected error getting queue")
            return {"error": str(e)}
    
    async def get_file(
        self,
        filename: str,
        subfolder: str = "",
        folder_type: str = "output"
    ) -> bytes:
        """Download generated file (image, audio, video).
        
        Args:
            filename: Name of the file
            subfolder: Optional subfolder path
            folder_type: Type of folder (output, input, temp)
            
        Returns:
            Raw file bytes
        """
        params = {
            "filename": filename,
            "subfolder": subfolder,
            "type": folder_type
        }
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(f"{self.base_url}/view", params=params) as resp:
                if resp.status != 200:
                    raise ValueError(f"Failed to get file: {resp.status}")
                return await resp.read()
    
    async def cancel(self, prompt_id: str) -> dict[str, Any]:
        """Cancel a queued or running job.
        
        Args:
            prompt_id: The prompt ID to cancel
            
        Returns:
            Dict with cancellation result
        """
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                # Interrupt current execution
                await session.post(f"{self.base_url}/interrupt")
                
                # Delete from queue
                data = {"delete": [prompt_id]}
                async with session.post(f"{self.base_url}/queue", json=data):
                    return {"status": "cancelled", "prompt_id": prompt_id}
        except Exception as e:
            logger.exception("Error cancelling job %s", prompt_id)
            return {"status": "error", "error": str(e)}
    
    async def get_system_stats(self) -> dict[str, Any]:
        """Get ComfyUI system statistics.
        
        Returns:
            Dict with system stats (GPU memory, etc.)
        """
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(f"{self.base_url}/system_stats") as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return {"error": f"Status {resp.status}"}
        except Exception as e:
            return {"error": str(e)}
    
    async def get_object_info(self) -> dict[str, Any]:
        """Get available node types from ComfyUI.
        
        Returns:
            Dict with all available node classes and their definitions
        """
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(f"{self.base_url}/object_info") as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return {"error": f"Status {resp.status}"}
        except Exception as e:
            return {"error": str(e)}
    
    async def upload_image(
        self,
        image_data: bytes,
        filename: str,
        subfolder: str = "",
        overwrite: bool = False
    ) -> dict[str, Any]:
        """Upload an image to ComfyUI.
        
        Args:
            image_data: Raw image bytes
            filename: Name for the uploaded file
            subfolder: Optional subfolder
            overwrite: Whether to overwrite existing file
            
        Returns:
            Dict with upload result
        """
        try:
            data = aiohttp.FormData()
            data.add_field(
                "image",
                image_data,
                filename=filename,
                content_type="image/png"
            )
            if subfolder:
                data.add_field("subfolder", subfolder)
            data.add_field("overwrite", str(overwrite).lower())
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    f"{self.base_url}/upload/image",
                    data=data
                ) as resp:
                    return await resp.json()
        except Exception as e:
            logger.exception("Error uploading image")
            return {"error": str(e)}
    
    async def wait_for_completion(
        self,
        prompt_id: str,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
        on_progress: Callable[[dict[str, Any]], None] | None = None
    ) -> dict[str, Any]:
        """Poll until workflow execution completes.
        
        Args:
            prompt_id: The prompt ID to wait for
            timeout: Max seconds to wait
            poll_interval: Seconds between status checks
            on_progress: Optional callback for status updates
            
        Returns:
            Final status dict
        """
        start_time = asyncio.get_event_loop().time()
        
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                return {
                    "status": "timeout",
                    "prompt_id": prompt_id,
                    "error": f"Workflow execution timed out after {timeout}s"
                }
            
            status = await self.get_status(prompt_id)
            
            if on_progress:
                on_progress(status)
            
            if status["status"] in ["completed", "failed", "unknown"]:
                return status
            
            await asyncio.sleep(poll_interval)
