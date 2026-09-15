"""ComfyUI HTTP/WebSocket Client for AgentSystem.

Provides async client for interacting with ComfyUI's REST API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
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
                        # Surface node_errors too — they carry the actionable
                        # detail (e.g. LoadImage "Invalid image file: x.png");
                        # the top-level error alone ("prompt_outputs_failed_
                        # validation") leaves the calling agent retrying blind.
                        err: dict[str, Any] = {"status": "error", "error": result.get("error", "Unknown error")}
                        if result.get("node_errors"):
                            err["node_errors"] = result["node_errors"]
                        return err
                    # Add status field for consistency
                    result["status"] = "queued"
                    return result
        except asyncio.TimeoutError:
            logger.warning("Timeout queuing prompt to ComfyUI")
            return {"status": "error", "error": "Connection timeout - ComfyUI server may be busy"}
        except aiohttp.ClientConnectorError:
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

        # If the history lookup itself failed (e.g. ComfyUI hiccup), surface
        # the error so callers can distinguish "truly not in history" from
        # "history endpoint unreachable".
        if "error" in history:
            return {
                "status": "unknown",
                "prompt_id": prompt_id,
                "error": history["error"],
            }

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
            # First, check the job status to determine the right action
            status = await self.get_status(prompt_id)
            job_status = status.get("status", "unknown")
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                if job_status == "running":
                    # Job is currently running - interrupt it
                    # Note: /interrupt stops the CURRENT job, so we verify it's ours
                    logger.info(f"Interrupting running job {prompt_id}")
                    async with session.post(f"{self.base_url}/interrupt") as resp:
                        if resp.status != 200:
                            logger.warning(f"Interrupt returned status {resp.status}")
                            return {
                                "status": "error",
                                "prompt_id": prompt_id,
                                "error": f"interrupt returned HTTP {resp.status}",
                            }
                    return {"status": "cancelled", "prompt_id": prompt_id, "was_running": True}

                elif job_status in ("pending", "queued"):
                    # Job is in queue - delete it from queue
                    # Note: ComfyUI API returns "pending", job_tracker uses "queued"
                    logger.info(f"Removing pending job {prompt_id} from queue")
                    data = {"delete": [prompt_id]}
                    async with session.post(f"{self.base_url}/queue", json=data) as resp:
                        if resp.status != 200:
                            logger.warning(f"Queue delete returned status {resp.status}")
                            return {
                                "status": "error",
                                "prompt_id": prompt_id,
                                "error": f"queue delete returned HTTP {resp.status}",
                            }
                    return {"status": "cancelled", "prompt_id": prompt_id, "was_pending": True}
                    
                elif job_status in ("completed", "failed"):
                    # Job already finished
                    finished = {"status": "already_finished", "prompt_id": prompt_id, "job_status": job_status}
                    if job_status == "failed":
                        finished["error"] = str(status.get("error", "Unknown error"))  # stored as _op_status does
                    return finished
                    
                else:
                    # Neither queued, running nor finished: /interrupt would stop whichever job runs now.
                    return {
                        "status": "error",
                        "prompt_id": prompt_id,
                        "error": status.get("error")
                        or f"Job {prompt_id} is not in the queue or history of {self.host}:{self.port}",
                    }
                    
        except Exception as e:
            logger.exception("Error cancelling job %s", prompt_id)
            return {"status": "error", "error": str(e)}
    
    async def free_memory(self) -> dict[str, Any]:
        """Unload ComfyUI-managed models and free VRAM/RAM.

        Note: This only frees models tracked by ComfyUI's model manager.
        Custom nodes that load models directly (e.g. VibeVoice) are NOT
        affected.  Use :meth:`restart_service` for a full VRAM cleanup.
        """
        try:
            payload = {"unload_models": True, "free_memory": True}
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    f"{self.base_url}/free",
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        logger.info("ComfyUI: VRAM freed (models unloaded)")
                        return {"status": "ok"}
                    text = await resp.text()
                    logger.warning("ComfyUI /free returned %d: %s", resp.status, text)
                    return {"status": "error", "error": f"HTTP {resp.status}"}
        except Exception as e:
            logger.warning("ComfyUI /free failed: %s", e)
            return {"status": "error", "error": str(e)}

    async def restart_service(
        self,
        service_name: str = "comfyui",
        ssh_host: str | None = None,
        ssh_user: str = "operator",
        ready_timeout: float = 60.0,
        poll_interval: float = 2.0,
        vram_threshold_gb: float = 2.0,
    ) -> dict[str, Any]:
        """Restart the ComfyUI systemd service via SSH and wait until ready.

        This is the only reliable way to free VRAM held by custom nodes
        that bypass ComfyUI's model management (e.g. VibeVoice).

        Includes coordination for concurrent callers:
        - Checks VRAM first; skips restart if already below threshold.
        - Uses a file lock so only one process restarts at a time.
        - After acquiring the lock, re-checks VRAM (another process may
          have already restarted while we were waiting).

        Args:
            service_name: Name of the systemd service.
            ssh_host: SSH host (defaults to ``self.host``).
            ssh_user: SSH user on the remote machine.
            ready_timeout: Seconds to wait for ComfyUI to become reachable.
            poll_interval: Seconds between readiness checks.
            vram_threshold_gb: Skip restart if torch VRAM usage is below
                this value (in GB).

        Returns:
            ``{"status": "ok"}`` or ``{"status": "error", "error": ...}``
        """
        import subprocess as _sp

        threshold_bytes = int(vram_threshold_gb * 1_000_000_000)

        # --- quick check: is a restart even necessary? ---
        if await self._vram_is_clean(threshold_bytes):
            logger.info("ComfyUI VRAM already clean — skipping restart")
            return {"status": "ok", "skipped": True}

        # --- file lock to serialise concurrent restarts ---
        lock_path = Path(tempfile.gettempdir()) / "comfyui_restart.lock"
        lock_fd: int | None = None
        try:
            lock_fd = await self._acquire_restart_lock(
                lock_path, wait_timeout=ready_timeout + 30,
            )
        except TimeoutError:
            logger.warning("Could not acquire restart lock — proceeding anyway")

        try:
            # --- double-check after acquiring the lock ---
            if await self._vram_is_clean(threshold_bytes):
                logger.info(
                    "ComfyUI VRAM already clean after lock — "
                    "another process restarted it"
                )
                return {"status": "ok", "skipped": True}

            # --- perform the restart ---
            host = ssh_host or self.host
            ssh_target = f"{ssh_user}@{host}"
            restart_cmd = [
                "ssh", ssh_target,
                f"sudo systemctl restart {service_name}",
            ]

            logger.info("Restarting ComfyUI service on %s …", ssh_target)
            try:
                # Run the blocking subprocess in a thread so we do not
                # stall the event loop for up to 30s (other coroutines,
                # FastAPI handlers, pings would freeze).
                proc = await asyncio.to_thread(
                    _sp.run,
                    restart_cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout or "").strip()
                    logger.error(
                        "SSH restart failed (rc=%d): %s", proc.returncode, err,
                    )
                    return {"status": "error", "error": err}
            except Exception as e:
                logger.error("SSH restart exception: %s", e)
                return {"status": "error", "error": str(e)}

            # --- wait for ComfyUI to come back online ---
            logger.info("Waiting for ComfyUI to become ready …")
            elapsed = 0.0
            while elapsed < ready_timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    stats = await self.get_system_stats()
                    if "error" not in stats:
                        logger.info(
                            "ComfyUI back online after %.1fs", elapsed,
                        )
                        return {"status": "ok"}
                except Exception:
                    pass  # not ready yet

            logger.warning(
                "ComfyUI did not become ready within %.0fs", ready_timeout,
            )
            return {"status": "error", "error": "timeout waiting for ComfyUI"}
        finally:
            self._release_restart_lock(lock_fd, lock_path)

    # -- restart helpers --------------------------------------------------

    async def _vram_is_clean(self, threshold_bytes: int) -> bool:
        """Return True if torch VRAM usage is below *threshold_bytes*."""
        try:
            stats = await self.get_system_stats()
            devices = stats.get("devices", [])
            if devices:
                d = devices[0]
                used = d.get("torch_vram_total", 0) - d.get("torch_vram_free", 0)
                logger.debug("VRAM used: %.0fMB (threshold: %.0fMB)",
                             used / 1e6, threshold_bytes / 1e6)
                return used < threshold_bytes
        except Exception:
            pass
        return False  # can't tell → assume dirty

    @staticmethod
    async def _acquire_restart_lock(
        lock_path: Path,
        wait_timeout: float = 90.0,
        poll: float = 2.0,
    ) -> int:
        """Atomically create a lock file.  Wait if another process holds it."""
        deadline = asyncio.get_running_loop().time() + wait_timeout
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                # Write PID so stale locks can be diagnosed.
                os.write(fd, str(os.getpid()).encode())
                return fd
            except FileExistsError:
                # Check for stale lock (older than 2 minutes).
                try:
                    age = asyncio.get_running_loop().time() - lock_path.stat().st_mtime
                    if age > 120:
                        logger.warning(
                            "Removing stale ComfyUI restart lock (%.0fs old)", age,
                        )
                        lock_path.unlink(missing_ok=True)
                        continue
                except Exception:
                    pass
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("restart lock wait timed out")
                logger.debug("Waiting for ComfyUI restart lock …")
                await asyncio.sleep(poll)

    @staticmethod
    def _release_restart_lock(fd: int | None, lock_path: Path) -> None:
        """Release the restart lock file.

        Only unlinks the lock file when ``fd`` is not None — i.e. when we
        actually acquired the lock. Otherwise another process owns it and
        we must not remove their lock.
        """
        if fd is None:
            return
        try:
            os.close(fd)
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass

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
    
    async def get_object_info(
        self, node_class: str | None = None
    ) -> dict[str, Any]:
        """Get available node types from ComfyUI.

        Args:
            node_class: Optional single class name. ComfyUI serves
                ``/object_info/<class>`` for it, which returns the same
                ``{class_name: definition}`` shape as the full map but is
                a few KB instead of several MB — worth it for a capability
                probe. An unknown class yields an empty dict (or a 404,
                reported as ``{"error": ...}``), never a raise.

        Returns:
            Dict with the available node class(es) and their definitions
        """
        path = f"/object_info/{node_class}" if node_class else "/object_info"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(f"{self.base_url}{path}") as resp:
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

    async def upload_audio(
        self,
        audio_data: bytes,
        filename: str,
        subfolder: str = "",
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Upload an audio file to ComfyUI input folder.

        ComfyUI's ``/upload/image`` endpoint accepts any file type despite
        its name — it just copies the payload into the ``input/`` directory.

        Args:
            audio_data: Raw audio bytes (FLAC, WAV, etc.)
            filename: Target filename on server (e.g. ``book_1_narrator.flac``)
            subfolder: Optional subfolder inside ``input/``
            overwrite: Whether to overwrite an existing file (default True)

        Returns:
            Dict with upload result or ``{"error": ...}``
        """
        try:
            data = aiohttp.FormData()
            data.add_field(
                "image",  # ComfyUI expects field name "image" for all uploads
                audio_data,
                filename=filename,
                content_type="application/octet-stream",
            )
            if subfolder:
                data.add_field("subfolder", subfolder)
            data.add_field("overwrite", str(overwrite).lower())

            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    f"{self.base_url}/upload/image",
                    data=data,
                ) as resp:
                    return await resp.json()
        except Exception as e:
            logger.exception("Error uploading audio to ComfyUI")
            return {"error": str(e)}

    async def wait_for_completion(
        self,
        prompt_id: str,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        unknown_grace_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Poll until workflow execution completes.

        Args:
            prompt_id: The prompt ID to wait for
            timeout: Max seconds to wait
            poll_interval: Seconds between status checks
            on_progress: Optional callback for status updates
            unknown_grace_seconds: How long to tolerate "unknown" status
                before treating it as terminal. Right after queue submission
                a job can briefly be absent from both /queue and /history
                (race window), so an immediate "unknown" must not be treated
                as failure.

        Returns:
            Final status dict
        """
        start_time = asyncio.get_running_loop().time()
        first_unknown_time: float | None = None

        while True:
            now = asyncio.get_running_loop().time()
            elapsed = now - start_time
            if elapsed > timeout:
                return {
                    "status": "timeout",
                    "prompt_id": prompt_id,
                    "error": f"Workflow execution timed out after {timeout}s"
                }

            status = await self.get_status(prompt_id)

            if on_progress:
                on_progress(status)

            if status["status"] in ["completed", "failed"]:
                return status

            if status["status"] == "unknown":
                # Treat "unknown" as transient during the grace window —
                # the job may exist in the queue but not yet be visible in
                # /queue or /history (race right after submission).
                if first_unknown_time is None:
                    first_unknown_time = now
                elif (now - first_unknown_time) >= unknown_grace_seconds:
                    return status
            else:
                # Any other status resets the unknown grace timer
                first_unknown_time = None

            await asyncio.sleep(poll_interval)
