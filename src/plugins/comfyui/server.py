"""ComfyUI Plugin Server.

MCP server for executing ComfyUI workflows with web monitoring interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from .comfyui_client import ComfyUIClient
from .job_tracker import ComfyUIJobTracker

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class ComfyUIServer(SchemaBasedMCPServer):
    """MCP Server for ComfyUI workflow execution.
    
    Provides tools for:
    - Listing configured workflows
    - Executing workflows with parameters
    - Checking job status
    - Retrieving results
    - Server status monitoring
    
    Also provides a web UI for job monitoring.
    """
    
    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPConfig"
    ) -> None:
        """Initialize ComfyUI server.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Server configuration - directly from mcp_config attributes
        self.host = getattr(mcp_config, 'host', "127.0.0.1")
        self.port = getattr(mcp_config, 'port', 8188)
        self.timeout = getattr(mcp_config, 'timeout_seconds', 300)
        self.output_dir = Path(getattr(mcp_config, 'output_dir', "data/comfyui/outputs"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Parse workflow configurations
        self.workflows: dict[str, dict[str, Any]] = {}
        self.workflow_files_dir = Path(getattr(mcp_config, 'workflow_files_dir', "config/comfyui_workflows"))
        
        for wf_config in getattr(mcp_config, 'workflows', []):
            wf_id = wf_config.get("id") if isinstance(wf_config, dict) else None
            if wf_id:
                self.workflows[wf_id] = wf_config
        
        # Initialize client
        self.client = ComfyUIClient(
            host=self.host,
            port=self.port,
            output_dir=self.output_dir,
            timeout=float(self.timeout)
        )
        
        # Initialize job tracker
        db_path = self.output_dir.parent / "jobs.db"
        self.job_tracker = ComfyUIJobTracker(db_path)
        
        # Templates for web UI
        self.templates_dir = Path(__file__).parent / "templates"
        self.templates: Jinja2Templates | None = None
        if self.templates_dir.exists():
            self.templates = Jinja2Templates(directory=str(self.templates_dir))
        
        logger.info(
            "ComfyUI plugin initialized: %s:%s with %d workflows",
            self.host, self.port, len(self.workflows)
        )
    
    # =========================================================================
    # MCP Tool: workflow
    # =========================================================================
    
    async def workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        """Main workflow management tool.
        
        Operations:
        - list: Show all configured workflows
        - execute: Run a workflow with parameters
        - status: Check execution status of a job
        - result: Get output files from completed job
        - load: Load previously generated media for LLM analysis
        - server_status: Check if ComfyUI server is online
        - queue: View current queue
        - cancel: Cancel a running/queued job
        
        Args:
            params: Tool parameters including operation and args
            
        Returns:
            Dict with operation result
        """
        status = params.get("_status")
        operation = params.get("operation")
        
        if not operation:
            return {"error": "operation is required"}
        
        # ===== LIST =====
        if operation == "list":
            return await self._op_list(params)
        
        # ===== EXECUTE =====
        elif operation == "execute":
            return await self._op_execute(params, status)
        
        # ===== STATUS =====
        elif operation == "status":
            return await self._op_status(params)
        
        # ===== RESULT =====
        elif operation == "result":
            return await self._op_result(params, status)
        
        # ===== WAIT_FOR_COMPLETION =====
        elif operation == "wait_for_completion":
            return await self._op_wait_for_completion(params, status)
        
        # ===== SERVER_STATUS =====
        elif operation == "server_status":
            return await self.client.ping()
        
        # ===== QUEUE =====
        elif operation == "queue":
            queue_data = await self.client.get_queue()
            return {
                "status": "success",
                "pending": len(queue_data.get("queue_pending", [])),
                "running": len(queue_data.get("queue_running", [])),
                "queue": queue_data
            }
        
        # ===== CANCEL =====
        elif operation == "cancel":
            prompt_id = params.get("prompt_id")
            if not prompt_id:
                return {"error": "prompt_id is required"}
            
            result = await self.client.cancel(prompt_id)
            self.job_tracker.update_status(prompt_id, "cancelled")
            return result
        
        # ===== LOAD =====
        elif operation == "load":
            return await self._op_load(params, status)
        
        return {"error": f"Unknown operation: {operation}"}
    
    async def _op_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """List available workflows."""
        category = params.get("category")
        workflows = []
        
        for wf_id, wf_config in self.workflows.items():
            if category and wf_config.get("category") != category:
                continue
            
            workflows.append({
                "id": wf_id,
                "name": wf_config.get("name", wf_id),
                "description": wf_config.get("description", ""),
                "category": wf_config.get("category", "general"),
                "parameters": [
                    {
                        "name": p["name"],
                        "type": p.get("type", "string"),
                        "required": p.get("required", False),
                        "default": p.get("default"),
                        "description": p.get("description", "")
                    }
                    for p in wf_config.get("parameters", [])
                ]
            })
        
        return {"workflows": workflows, "count": len(workflows)}
    
    async def _op_execute(self, params: dict[str, Any], status: Any) -> dict[str, Any]:
        """Execute a workflow."""
        workflow_id = params.get("workflow_id")
        if not workflow_id:
            return {"error": "workflow_id is required"}
        
        if workflow_id not in self.workflows:
            return {
                "error": f"Unknown workflow: {workflow_id}",
                "available": list(self.workflows.keys())
            }
        
        wf_config = self.workflows[workflow_id]
        workflow_file = self.workflow_files_dir / wf_config.get("workflow_file", f"{workflow_id}.json")
        
        # Check if workflow file exists
        if not workflow_file.exists():
            return {
                "error": f"Workflow file not found: {workflow_file}",
                "hint": "Export workflow from ComfyUI using 'Save (API Format)'"
            }
        
        # Load workflow JSON
        try:
            with open(workflow_file, "r", encoding="utf-8") as f:
                workflow_json = json.load(f)
        except Exception as e:
            return {"error": f"Failed to load workflow: {e}"}
        
        # Inject parameters
        user_params = params.get("parameters", {})
        for param_def in wf_config.get("parameters", []):
            param_name = param_def["name"]
            node_id = param_def.get("node_id")
            field_path = param_def.get("field", "")
            
            # Get value: user-provided or default
            if param_name in user_params:
                value = user_params[param_name]
            elif param_def.get("required"):
                return {"error": f"Required parameter missing: {param_name}"}
            else:
                value = param_def.get("default")
            
            # Inject into workflow
            if node_id and field_path and value is not None:
                self._inject_value(workflow_json, node_id, field_path, value)
        
        if status:
            await status.progress(f"Executing workflow: {wf_config.get('name', workflow_id)}")
        
        # Queue workflow
        queue_result = await self.client.queue_prompt(workflow_json)
        
        if "error" in queue_result:
            return {"error": queue_result["error"]}
        
        prompt_id = queue_result.get("prompt_id")
        if not prompt_id:
            return {"error": "No prompt_id returned from ComfyUI"}
        
        # Register job in tracker
        self.job_tracker.register_job(
            prompt_id=prompt_id,
            workflow_id=workflow_id,
            workflow_name=wf_config.get("name", workflow_id),
            parameters=user_params,
            output_prefix=params.get("output_prefix", "comfy")
        )
        
        if status:
            await status.end(f"Workflow queued: {prompt_id}")
        
        return {
            "status": "queued",
            "prompt_id": prompt_id,
            "workflow_id": workflow_id,
            "workflow_name": wf_config.get("name", workflow_id),
            "message": f"Job queued. Check status with operation='status', prompt_id='{prompt_id}'"
        }
    
    async def _op_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get job status."""
        prompt_id = params.get("prompt_id")
        if not prompt_id:
            return {"error": "prompt_id is required"}
        
        # Get live status from ComfyUI
        live_status = await self.client.get_status(prompt_id)
        
        # Update tracker if status changed
        if live_status["status"] == "running":
            self.job_tracker.update_status(prompt_id, "running")
        elif live_status["status"] == "completed":
            self.job_tracker.update_status(prompt_id, "completed")
        elif live_status["status"] == "failed":
            error_msg = str(live_status.get("error", "Unknown error"))
            self.job_tracker.update_status(prompt_id, "failed", error_msg)
        
        # Get tracker data
        job = self.job_tracker.get_job(prompt_id)
        
        if job:
            return {
                "status": live_status["status"],
                "prompt_id": prompt_id,
                "workflow_id": job.get("workflow_id"),
                "workflow_name": job.get("workflow_name"),
                "submitted_at": job.get("submitted_at"),
                "started_at": job.get("started_at"),
                "completed_at": job.get("completed_at"),
                "duration_seconds": job.get("duration_seconds"),
                "error": live_status.get("error"),
                "position": live_status.get("position")
            }
        
        return live_status
    
    async def _op_result(self, params: dict[str, Any], status: Any) -> dict[str, Any]:
        """Get job results."""
        prompt_id = params.get("prompt_id")
        if not prompt_id:
            return {"error": "prompt_id is required"}
        
        download = params.get("download", True)
        include_content = params.get("include_content", False)
        output_prefix = params.get("output_prefix", "comfy")
        
        if status:
            await status.progress(f"Fetching results for {prompt_id}")
        
        # Get history from ComfyUI
        history = await self.client.get_history(prompt_id)
        
        if prompt_id not in history:
            return {
                "error": "Job not found or not completed",
                "prompt_id": prompt_id,
                "hint": "Use operation='status' to check job state"
            }
        
        job_data = history[prompt_id]
        
        # Check for errors
        if job_data.get("status", {}).get("status_str") == "error":
            self.job_tracker.update_status(
                prompt_id, "failed",
                str(job_data.get("status", {}).get("messages", []))
            )
            return {
                "status": "failed",
                "prompt_id": prompt_id,
                "error": job_data.get("status", {}).get("messages", [])
            }
        
        # Process outputs
        outputs: dict[str, list[dict[str, Any]]] = {
            "images": [],
            "audio": [],
            "video": [],
            "other": []
        }
        
        for node_id, node_output in job_data.get("outputs", {}).items():
            for output_type in ["images", "audio", "video", "gifs"]:
                if output_type in node_output:
                    for file_info in node_output[output_type]:
                        file_record: dict[str, Any] = {
                            "filename": file_info["filename"],
                            "subfolder": file_info.get("subfolder", ""),
                            "type": file_info.get("type", "output")
                        }
                        
                        if download:
                            # Download file locally
                            try:
                                file_data = await self.client.get_file(
                                    file_info["filename"],
                                    file_info.get("subfolder", ""),
                                    file_info.get("type", "output")
                                )
                                local_filename = f"{output_prefix}_{file_info['filename']}"
                                local_path = self.output_dir / local_filename
                                local_path.parent.mkdir(parents=True, exist_ok=True)
                                local_path.write_bytes(file_data)
                                file_record["local_path"] = str(local_path)
                            except Exception as e:
                                logger.error("Failed to download %s: %s", file_info["filename"], e)
                                file_record["download_error"] = str(e)
                        
                        # Categorize
                        if output_type == "images":
                            outputs["images"].append(file_record)
                        elif output_type == "audio":
                            outputs["audio"].append(file_record)
                        elif output_type in ["video", "gifs"]:
                            outputs["video"].append(file_record)
                        else:
                            outputs["other"].append(file_record)
        
        # Update tracker
        self.job_tracker.update_status(prompt_id, "completed")
        
        # Store output paths
        output_paths = {
            k: [f.get("local_path", f["filename"]) for f in v]
            for k, v in outputs.items() if v
        }
        self.job_tracker.set_outputs(prompt_id, output_paths)
        
        if status:
            total_files = sum(len(v) for v in outputs.values())
            await status.end(f"Retrieved {total_files} output files")
        
        result: dict[str, Any] = {
            "status": "completed",
            "prompt_id": prompt_id,
            "outputs": outputs,
            "total_files": sum(len(v) for v in outputs.values())
        }
        
        # Include multimodal content for LLM analysis if requested
        if include_content:
            multimodal = self._build_multimodal_content(outputs)
            if multimodal:
                result["_multimodal_content"] = multimodal
        
        return result
    
    def _build_multimodal_content(self, outputs: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Build multimodal content list from outputs for LLM analysis.
        
        Args:
            outputs: Dict with keys 'images', 'audio', 'video', 'other', each containing
                    file records with 'local_path', 'filename', etc.
        
        Returns:
            List of multimodal content items: [{type, path, mime_type, description}, ...]
        """
        multimodal: list[dict[str, Any]] = []
        
        # Map output types to content types
        type_mapping = {
            "images": "image",
            "audio": "audio",
            "video": "video",
        }
        
        for output_type, content_type in type_mapping.items():
            for file_record in outputs.get(output_type, []):
                local_path = file_record.get("local_path")
                if local_path:
                    filename = file_record.get("filename", "")
                    mime_type = self._guess_mime_type(filename, content_type)
                    
                    multimodal.append({
                        "type": content_type,
                        "path": local_path,
                        "mime_type": mime_type,
                        "description": f"Generated {content_type}: {filename}"
                    })
        
        return multimodal
    
    def _guess_mime_type(self, filename: str, content_type: str) -> str:
        """Guess MIME type from filename extension."""
        filename_lower = filename.lower()
        
        # Image types
        if filename_lower.endswith(".jpg") or filename_lower.endswith(".jpeg"):
            return "image/jpeg"
        elif filename_lower.endswith(".png"):
            return "image/png"
        elif filename_lower.endswith(".gif"):
            return "image/gif"
        elif filename_lower.endswith(".webp"):
            return "image/webp"
        # Audio types
        elif filename_lower.endswith(".mp3"):
            return "audio/mpeg"
        elif filename_lower.endswith(".wav"):
            return "audio/wav"
        elif filename_lower.endswith(".flac"):
            return "audio/flac"
        elif filename_lower.endswith(".ogg"):
            return "audio/ogg"
        # Video types
        elif filename_lower.endswith(".mp4"):
            return "video/mp4"
        elif filename_lower.endswith(".webm"):
            return "video/webm"
        elif filename_lower.endswith(".avi"):
            return "video/x-msvideo"
        
        # Default fallbacks
        defaults = {
            "image": "image/png",
            "audio": "audio/wav",
            "video": "video/mp4",
        }
        return defaults.get(content_type, "application/octet-stream")
    
    async def _op_load(self, params: dict[str, Any], status: Any) -> dict[str, Any]:
        """Load previously generated media for LLM analysis.
        
        This allows the LLM to view images, audio, or video that were generated
        earlier (e.g., from a previous job) without re-generating them.
        
        Can load by:
        - prompt_id: Load all outputs from a completed job
        - file_path: Load a specific file by path (absolute or relative to output_dir)
        - filename: Load by filename (searches in output_dir)
        
        Args:
            params: Must include one of: prompt_id, file_path, or filename
            status: Status reporter
            
        Returns:
            Dict with file info and _multimodal_content for LLM analysis
        """
        prompt_id = params.get("prompt_id")
        file_path = params.get("file_path")
        filename = params.get("filename")
        
        if not any([prompt_id, file_path, filename]):
            return {
                "error": "One of prompt_id, file_path, or filename is required",
                "hint": "Use prompt_id to load outputs from a job, or file_path/filename to load a specific file"
            }
        
        if status:
            await status.progress("Loading media for analysis...")
        
        multimodal: list[dict[str, Any]] = []
        loaded_files: list[dict[str, Any]] = []
        
        # Load by prompt_id - get all outputs from job
        if prompt_id:
            job = self.job_tracker.get_job(prompt_id)
            if not job:
                return {"error": f"Job not found: {prompt_id}"}
            
            outputs = job.get("outputs", {})
            if not outputs:
                return {
                    "error": f"No outputs found for job {prompt_id}",
                    "hint": "Job may not have completed yet. Use operation='result' first to download outputs."
                }
            
            for output_type, paths in outputs.items():
                content_type = {
                    "images": "image",
                    "audio": "audio",
                    "video": "video"
                }.get(output_type)
                
                if not content_type:
                    continue
                
                for path in paths:
                    path_obj = Path(path)
                    if path_obj.exists():
                        mime_type = self._guess_mime_type(path_obj.name, content_type)
                        multimodal.append({
                            "type": content_type,
                            "path": str(path_obj),
                            "mime_type": mime_type,
                            "description": f"Generated {content_type} from job {prompt_id}: {path_obj.name}"
                        })
                        loaded_files.append({
                            "path": str(path_obj),
                            "filename": path_obj.name,
                            "type": content_type,
                            "size_bytes": path_obj.stat().st_size
                        })
        
        # Load by file_path or filename
        else:
            # Determine path
            if file_path:
                path_obj = Path(file_path)
                if not path_obj.is_absolute():
                    path_obj = self.output_dir / path_obj
            else:
                # Search by filename
                path_obj = self.output_dir / filename
                if not path_obj.exists():
                    # Try to find in subdirectories
                    matches = list(self.output_dir.rglob(filename))
                    if matches:
                        path_obj = matches[0]
            
            if not path_obj.exists():
                return {
                    "error": f"File not found: {file_path or filename}",
                    "searched_in": str(self.output_dir),
                    "hint": "Provide a valid file path or use prompt_id to load job outputs"
                }
            
            # Determine content type from extension
            content_type = self._get_content_type_from_path(path_obj)
            mime_type = self._guess_mime_type(path_obj.name, content_type)
            
            multimodal.append({
                "type": content_type,
                "path": str(path_obj),
                "mime_type": mime_type,
                "description": f"Loaded {content_type}: {path_obj.name}"
            })
            loaded_files.append({
                "path": str(path_obj),
                "filename": path_obj.name,
                "type": content_type,
                "size_bytes": path_obj.stat().st_size
            })
        
        if not multimodal:
            return {"error": "No valid media files found to load"}
        
        if status:
            await status.end(f"Loaded {len(multimodal)} media file(s) for analysis")
        
        return {
            "status": "success",
            "loaded_files": loaded_files,
            "count": len(loaded_files),
            "message": f"Loaded {len(loaded_files)} file(s). You can now analyze the content.",
            "_multimodal_content": multimodal
        }
    
    def _get_content_type_from_path(self, path: Path) -> str:
        """Determine content type from file extension."""
        suffix = path.suffix.lower()
        
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
        audio_exts = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}
        video_exts = {".mp4", ".webm", ".avi", ".mov", ".mkv"}
        
        if suffix in image_exts:
            return "image"
        elif suffix in audio_exts:
            return "audio"
        elif suffix in video_exts:
            return "video"
        else:
            return "other"
    
    async def _op_wait_for_completion(self, params: dict[str, Any], status: Any) -> dict[str, Any]:
        """Wait synchronously until a job completes or times out.
        
        This operation polls the job status internally, eliminating the need
        for the agent to manually poll with wait + status operations.
        
        If include_content is True and the job completes successfully, this
        also retrieves outputs and includes multimodal content for LLM analysis.
        """
        prompt_id = params.get("prompt_id")
        if not prompt_id:
            return {"error": "prompt_id is required"}
        
        timeout = params.get("timeout", 300)  # Default 5 minutes
        poll_interval = params.get("poll_interval", 2)  # Default 2 seconds
        include_content = params.get("include_content", False)
        
        import time
        start_time = time.time()
        
        if status:
            await status.progress(f"Waiting for job {prompt_id} to complete (timeout: {timeout}s)")
        
        while True:
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                if status:
                    await status.error(f"Job {prompt_id} timed out after {timeout}s")
                return {
                    "status": "timeout",
                    "prompt_id": prompt_id,
                    "elapsed_seconds": elapsed,
                    "message": f"Job did not complete within {timeout} seconds"
                }
            
            # Check cancellation
            cancellation_token = params.get("_cancellation_token")
            if cancellation_token and cancellation_token.is_cancelled:
                if status:
                    await status.error(f"Job {prompt_id} wait cancelled by user")
                return {
                    "status": "cancelled",
                    "prompt_id": prompt_id,
                    "elapsed_seconds": elapsed
                }
            
            # Get job status
            job_status = await self.client.get_status(prompt_id)
            current_status = job_status.get("status", "unknown")
            
            # Update status message periodically (every 10s)
            if status and int(elapsed) % 10 == 0:
                await status.progress(
                    f"Job {prompt_id}: {current_status} "
                    f"(elapsed: {int(elapsed)}s, timeout in: {int(timeout - elapsed)}s)"
                )
            
            # Check if completed or failed
            if current_status == "completed":
                if status:
                    await status.end(f"Job {prompt_id} completed after {int(elapsed)}s")
                
                # If include_content, fetch results to get multimodal content
                if include_content:
                    result_params = {
                        "prompt_id": prompt_id,
                        "download": True,
                        "include_content": True,
                        "output_prefix": params.get("output_prefix", "comfy")
                    }
                    result = await self._op_result(result_params, None)
                    result["elapsed_seconds"] = elapsed
                    return result
                
                return {
                    "status": "completed",
                    "prompt_id": prompt_id,
                    "elapsed_seconds": elapsed
                }
            elif current_status == "failed":
                if status:
                    await status.error(f"Job {prompt_id} failed after {int(elapsed)}s")
                return {
                    "status": "failed",
                    "prompt_id": prompt_id,
                    "elapsed_seconds": elapsed,
                    "error": job_status.get("error", "Unknown error")
                }
            
            # Wait before next poll
            await asyncio.sleep(poll_interval)
        
        # Timeout reached (unreachable code but kept for completeness)
        if status:
            await status.error(f"Job {prompt_id} did not complete within {timeout}s")
        
        return {
            "status": "timeout",
            "prompt_id": prompt_id,
            "elapsed_seconds": elapsed,
            "error": f"Job did not complete within {timeout} seconds"
        }
    
    def _inject_value(
        self,
        workflow: dict[str, Any],
        node_id: str,
        field_path: str,
        value: Any
    ) -> None:
        """Inject a value into the workflow at the specified path.
        
        Args:
            workflow: Workflow dict to modify
            node_id: Node ID in the workflow
            field_path: Dot-separated path (e.g., "inputs.text")
            value: Value to inject
        """
        if node_id not in workflow:
            logger.warning("Node %s not found in workflow", node_id)
            return
        
        parts = field_path.split(".")
        obj = workflow[node_id]
        
        for part in parts[:-1]:
            if part not in obj:
                obj[part] = {}
            obj = obj[part]
        
        obj[parts[-1]] = value
    
    # =========================================================================
    # Web UI Router
    # =========================================================================
    
    def get_web_router(self) -> APIRouter:
        """Get FastAPI router for web monitoring endpoints.
        
        Returns:
            APIRouter with monitoring endpoints
        """
        router = APIRouter(prefix=f"/plugins/{self.name}")
        
        @router.get("/", response_class=HTMLResponse)
        async def monitor_panel(request: Request) -> HTMLResponse:
            """Render the job monitoring panel."""
            if not self.templates:
                return HTMLResponse(
                    "<h1>ComfyUI Monitor</h1><p>Templates not found</p>",
                    status_code=500
                )
            return self.templates.TemplateResponse(
                request=request,
                name="monitor.html",
                context={
                    "plugin_name": "comfyui",
                    "title": "ComfyUI Job Monitor",
                    "host": self.host,
                    "port": self.port
                }
            )
        
        @router.get("/jobs")
        async def get_jobs() -> JSONResponse:
            """Get all tracked jobs."""
            server_status = await self.client.ping()
            active = self.job_tracker.get_active_jobs()
            recent = self.job_tracker.get_recent_completed(limit=10)
            stats = self.job_tracker.get_stats()
            
            return JSONResponse({
                "status": "success",
                "server_online": server_status.get("status") == "online",
                "server_host": f"{self.host}:{self.port}",
                "queue_pending": server_status.get("queue_pending", 0),
                "queue_running": server_status.get("queue_running", 0),
                "jobs": active,
                "recent_completed": recent,
                "stats": stats
            })
        
        @router.get("/jobs/{prompt_id}")
        async def get_job_detail(prompt_id: str) -> JSONResponse:
            """Get details for a specific job."""
            job = self.job_tracker.get_job(prompt_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            
            # Also get live status
            live_status = await self.client.get_status(prompt_id)
            job["live_status"] = live_status.get("status", "unknown")
            
            return JSONResponse({"status": "success", "job": job})
        
        @router.post("/jobs/{prompt_id}/cancel")
        async def cancel_job(prompt_id: str) -> JSONResponse:
            """Cancel a job."""
            result = await self.client.cancel(prompt_id)
            self.job_tracker.update_status(prompt_id, "cancelled")
            return JSONResponse(result)
        
        @router.get("/workflows")
        async def list_workflows() -> JSONResponse:
            """List configured workflows."""
            result = await self._op_list({})
            return JSONResponse(result)
        
        @router.get("/stats")
        async def get_stats() -> JSONResponse:
            """Get job statistics."""
            server_status = await self.client.ping()
            job_stats = self.job_tracker.get_stats()
            
            return JSONResponse({
                "status": "success",
                "server": server_status,
                "jobs": job_stats
            })
        
        return router
    
    def get_panels(self) -> list[dict[str, Any]]:
        """Return UI panel configuration for ComfyUI monitor.
        
        Returns:
            List with panel configuration dict
        """
        return [
            {
                "id": "comfyui_monitor",
                "title": "ComfyUI Monitor",
                "icon": "🎨",
                "url": f"/plugins/{self.name}/",
                "position": "right",
                "width": "650px",
                "height": "500px"
            }
        ]
    
    def get_menu_items(self) -> list[dict[str, Any]]:
        """Return menu items for ComfyUI.
        
        Returns:
            List of menu item configuration dicts
        """
        return [
            {
                "id": "comfyui_monitor",
                "menu_id": "tools",
                "label": "ComfyUI Monitor",
                "icon": "🎨",
                "url": f"/plugins/{self.name}/",
                "order": 50,
                "target": "_blank"
            }
        ]


# Plugin factory for discovery
PLUGIN_FACTORY = ComfyUIServer
