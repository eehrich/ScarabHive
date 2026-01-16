"""Batch Monitor Web Endpoints.

Provides web UI for monitoring batch queue status.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agent_system.plugins.schema_router import create_schema_router

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class BatchMonitorWebFactory:
    """Web UI factory for batch queue monitoring."""
    
    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPConfig"
    ) -> None:
        """Initialize batch monitor web factory.
        
        Args:
            name: Plugin name
            system_config: System configuration
            mcp_config: MCP/plugin configuration
        """
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        self.plugin_dir = Path(__file__).parent
        self.templates_dir = self.plugin_dir / "templates"
        self.templates = Jinja2Templates(directory=str(self.templates_dir))
        
        # Load schema for router generation
        self._load_schema()
    
    def _load_schema(self) -> None:
        """Load schema.yaml for endpoint definitions with template variable substitution."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        
        try:
            self.schema = load_schema_from_dir(
                self.plugin_dir,
                template_vars={"name": self.name}
            )
        except Exception as e:
            logger.error(f"Failed to load schema for {self.name}: {e}")
            self.schema = {}
    
    def get_schema_data(self) -> dict:
        """Get loaded schema data."""
        return self.schema
    
    def get_web_router(self) -> APIRouter:
        """Get FastAPI router for web endpoints."""
        return create_schema_router(
            plugin_name=self.name,
            schema=self.schema,
            handler_class=self
        )
    
    def _get_batch_manager(self):
        """Get the global batch queue manager."""
        from agent_system.llm.batch.initialization import get_batch_queue_manager
        return get_batch_queue_manager()
    
    async def get_panel(self, request: Request) -> HTMLResponse:
        """Render the batch queue monitoring panel.
        
        Args:
            request: FastAPI request object
            
        Returns:
            HTML response with panel content
        """
        return self.templates.TemplateResponse(
            request=request,
            name="panel.html",
            context={
                "plugin_name": "batch_monitor",
                "title": "Batch Queue Monitor",
            }
        )
    
    async def get_queues(self, request: Request) -> JSONResponse:
        """Get status of all batch queues.
        
        Args:
            request: FastAPI request object
        
        Returns:
            JSON response with queue statuses
        """
        manager = self._get_batch_manager()
        
        if not manager:
            return JSONResponse({
                "status": "unavailable",
                "message": "Batch queue manager not initialized",
                "queues": []
            })
        
        queues = []
        now = datetime.now(timezone.utc)
        
        # Retention time for finished jobs (show for 180 seconds after completion)
        FINISHED_JOB_RETENTION_SECONDS = 60*3
        
        # _queues is Dict[str, List[BatchRequest]] - queue_key -> pending requests
        # _active_jobs is Dict[str, BatchJob] - job_id -> active job
        # _completed_jobs is Dict[str, BatchJob] - job_id -> recently completed job
        
        # Collect all known queue keys (from pending + active jobs + recent completed jobs)
        all_queue_keys = set(manager._queues.keys())
        for job in manager._active_jobs.values():
            queue_key = f"{job.provider}:{job.model}"
            all_queue_keys.add(queue_key)
        
        # Also include recently completed jobs
        for job in manager._completed_jobs.values():
            if job.completed_at:
                age_seconds = (now - job.completed_at).total_seconds()
                if age_seconds <= FINISHED_JOB_RETENTION_SECONDS:
                    queue_key = f"{job.provider}:{job.model}"
                    all_queue_keys.add(queue_key)
        
        for queue_key in all_queue_keys:
            # Parse provider:model from queue_key
            parts = queue_key.split(":", 1)
            provider = parts[0] if len(parts) > 0 else "unknown"
            model = parts[1] if len(parts) > 1 else "unknown"
            
            # Get pending requests for this queue
            pending_requests = manager._queues.get(queue_key, [])
            
            # Find ALL active jobs for this queue (can be multiple parallel)
            active_jobs = []
            for job in manager._active_jobs.values():
                if f"{job.provider}:{job.model}" == queue_key:
                    elapsed_seconds = None
                    if job.submitted_at:
                        # For terminal states, use completed_at if available (job being finalized)
                        # Otherwise use current time (job still running)
                        end_time = job.completed_at if job.completed_at else now
                        elapsed_seconds = int((end_time - job.submitted_at).total_seconds())
                    
                    active_jobs.append({
                        "job_id": job.job_id,
                        "provider_job_id": job.provider_job_id,
                        "status": job.status.value,
                        "total_requests": len(job.requests) if job.requests else 0,
                        "completed_count": job.completed_count,
                        "failed_count": job.failed_count,
                        "submitted_at": job.submitted_at.isoformat() if job.submitted_at else None,
                        "elapsed_seconds": elapsed_seconds,
                        "estimated_input_tokens": job.estimated_input_tokens,
                    })
            
            # Also include recently completed jobs (finished within retention period)
            for job in manager._completed_jobs.values():
                if f"{job.provider}:{job.model}" == queue_key:
                    if job.completed_at:
                        age_seconds = (now - job.completed_at).total_seconds()
                        if age_seconds <= FINISHED_JOB_RETENTION_SECONDS:
                            elapsed_seconds = None
                            if job.submitted_at:
                                elapsed_seconds = int((job.completed_at - job.submitted_at).total_seconds())
                            
                            active_jobs.append({
                                "job_id": job.job_id,
                                "provider_job_id": job.provider_job_id,
                                "status": job.status.value,
                                "total_requests": len(job.requests) if job.requests else 0,
                                "completed_count": job.completed_count,
                                "failed_count": job.failed_count,
                                "submitted_at": job.submitted_at.isoformat() if job.submitted_at else None,
                                "elapsed_seconds": elapsed_seconds,
                                "estimated_input_tokens": job.estimated_input_tokens,
                            })
            
            # Determine queue status from jobs
            if active_jobs:
                # Use the "most active" status (in_progress > submitted > pending)
                statuses = [j["status"] for j in active_jobs]
                if "in_progress" in statuses:
                    queue_status = "in_progress"
                elif "submitted" in statuses:
                    queue_status = "submitted"
                else:
                    queue_status = statuses[0]
            else:
                queue_status = "idle"
            
            # Estimate tokens for pending requests (not yet batched)
            pending_estimated_tokens = sum(
                req.estimate_input_tokens() for req in pending_requests
            )
            
            queue_data = {
                "queue_key": queue_key,
                "provider": provider,
                "model": model,
                "pending_requests": len(pending_requests),
                "pending_estimated_tokens": pending_estimated_tokens,
                "status": queue_status,
                "active_jobs": active_jobs,
                # Keep backward compat: active_job = first job or None
                "active_job": active_jobs[0] if active_jobs else None,
            }
            
            # Skip empty idle queues (no pending requests and no active jobs)
            if not active_jobs and len(pending_requests) == 0:
                continue
            
            queues.append(queue_data)
        
        return JSONResponse({
            "status": "success",
            "queue_count": len(queues),
            "queues": queues,
            "collection_window_seconds": manager._collection_window,
            "poll_interval_seconds": manager._poll_interval,
        })
    
    async def get_queue_detail(self, request: Request, queue_key: str) -> JSONResponse:
        """Get detailed status of a specific queue.
        
        Args:
            request: FastAPI request object
            queue_key: Queue key in format "provider:model"
        
        Returns:
            JSON response with queue details
        """
        manager = self._get_batch_manager()
        
        if not manager:
            return JSONResponse({
                "status": "unavailable",
                "message": "Batch queue manager not initialized"
            })
        
        # Parse provider:model from queue_key
        parts = queue_key.split(":", 1)
        provider = parts[0] if len(parts) > 0 else "unknown"
        model = parts[1] if len(parts) > 1 else "unknown"
        
        # Get pending requests for this queue
        pending_requests = manager._queues.get(queue_key, [])
        
        # Check if queue exists (either pending requests or active job)
        active_job = None
        for job in manager._active_jobs.values():
            if f"{job.provider}:{job.model}" == queue_key:
                active_job = job
                break
        
        if queue_key not in manager._queues and not active_job:
            raise HTTPException(status_code=404, detail=f"Queue '{queue_key}' not found")
        
        queue_data = {
            "status": "success",
            "queue_key": queue_key,
            "provider": provider,
            "model": model,
            "pending_requests": len(pending_requests),
            "queue_status": "idle",
            "active_job": None,
        }
        
        if active_job:
            queue_data["queue_status"] = active_job.status.value
            
            now = datetime.now(timezone.utc)
            elapsed_seconds = None
            if active_job.submitted_at:
                # For terminal states, use completed_at if available
                end_time = active_job.completed_at if active_job.completed_at else now
                elapsed_seconds = int((end_time - active_job.submitted_at).total_seconds())
            
            queue_data["active_job"] = {
                "job_id": active_job.job_id,
                "provider_job_id": active_job.provider_job_id,
                "status": active_job.status.value,
                "total_requests": len(active_job.requests) if active_job.requests else 0,
                "completed_count": active_job.completed_count,
                "failed_count": active_job.failed_count,
                "submitted_at": active_job.submitted_at.isoformat() if active_job.submitted_at else None,
                "elapsed_seconds": elapsed_seconds,
            }
        
        return JSONResponse(queue_data)
    
    async def get_metrics(self, request: Request) -> JSONResponse:
        """Get overall batch system metrics.
        
        Args:
            request: FastAPI request object
        
        Returns:
            JSON response with metrics
        """
        manager = self._get_batch_manager()
        
        if not manager:
            return JSONResponse({
                "status": "unavailable",
                "message": "Batch queue manager not initialized"
            })
        
        # Use manager._metrics for cumulative totals (survives job completion)
        metrics = manager._metrics
        
        # Count active queues (queues with pending requests or active jobs)
        active_queue_keys: set[str] = set()
        for job in manager._active_jobs.values():
            active_queue_keys.add(f"{job.provider}:{job.model}")
        for queue_key, requests in manager._queues.items():
            if requests:  # has pending requests
                active_queue_keys.add(queue_key)
        
        total_pending = sum(len(requests) for requests in manager._queues.values())
        
        return JSONResponse({
            "status": "success",
            "total_queues": len(active_queue_keys),
            "active_jobs": len(manager._active_jobs),
            "total_pending_requests": total_pending,
            "total_completed_requests": metrics.completed_requests,
            "total_failed_requests": metrics.failed_requests,
            "total_jobs": metrics.total_jobs,
            "completed_jobs": metrics.completed_jobs,
            "failed_jobs": metrics.failed_jobs,
            # Processing time statistics (in seconds)
            "processing_time": {
                "min": round(metrics.min_processing_time, 1) if metrics.min_processing_time else None,
                "max": round(metrics.max_processing_time, 1) if metrics.max_processing_time else None,
                "mean": round(metrics.mean_processing_time, 1) if metrics.mean_processing_time else None,
                "sample_count": len(metrics._processing_times),
            },
        })


# Plugin factory export
PLUGIN_FACTORY = BatchMonitorWebFactory
