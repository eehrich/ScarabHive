"""Batch Monitor web endpoints: the Batch Queues panel and the two calls it makes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

# a finished job stays listed this long, so its outcome can be seen
FINISHED_JOB_RETENTION_SECONDS = 180


def job_row(job, now: datetime) -> dict:
    end = job.completed_at or now
    return {
        "job_id": job.job_id,
        "provider_job_id": job.provider_job_id,
        "status": job.status.value,
        "total_requests": len(job.requests),
        "completed_count": job.completed_count,
        "failed_count": job.failed_count,
        "elapsed_seconds": int((end - job.submitted_at).total_seconds()) if job.submitted_at else None,
        "estimated_input_tokens": job.estimated_input_tokens,
    }


class BatchMonitorWebFactory:
    """Web UI factory for batch queue monitoring."""

    def __init__(self, name: str, system_config: "AgentSystemConfig", server_config: "ToolServerConfig") -> None:
        self.name = name
        self.system_config = system_config
        self.server_config = server_config
        self.plugin_dir = Path(__file__).parent
        self.templates = ui_templates(self.plugin_dir / "templates")
        self._load_schema()

    def _load_schema(self) -> None:
        from agent_system.plugins.schema_loader import load_schema_from_dir

        try:
            self.schema = load_schema_from_dir(self.plugin_dir, template_vars={"name": self.name})
        except Exception as e:
            logger.error(f"Failed to load schema for {self.name}: {e}")
            self.schema = {}

    def get_schema_data(self) -> dict:
        return self.schema

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.name, schema=self.schema, handler_class=self)

    def get_static_assets(self) -> Path:
        """The panel's script and stylesheet, served under /plugins/<name>/static/."""
        return self.plugin_dir / "static"

    def _get_batch_manager(self):
        """The running batch queue manager; without one there is nothing to show, which is an error, not an empty list."""
        from agent_system.llm.batch.initialization import get_batch_queue_manager

        manager = get_batch_queue_manager()
        if manager is None:
            raise HTTPException(status_code=503, detail="No batch queue manager is running: batch processing is set up in llm.yaml")
        return manager

    async def get_panel(self, request: Request):
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.name})

    async def get_queues(self, request: Request) -> dict:
        """Each queue with requests waiting to be batched or jobs running or finished a moment ago."""
        manager = self._get_batch_manager()
        now = datetime.now(timezone.utc)
        recent = [job for job in manager._completed_jobs.values()
                  if job.completed_at and (now - job.completed_at).total_seconds() <= FINISHED_JOB_RETENTION_SECONDS]
        jobs: dict[str, list[dict]] = {}
        for job in [*manager._active_jobs.values(), *recent]:
            jobs.setdefault(f"{job.provider}:{job.model}", []).append(job_row(job, now))
        keys = set(jobs) | {key for key, pending in manager._queues.items() if pending}
        queues = []
        for key in sorted(keys):
            pending = manager._queues.get(key, [])
            queues.append({
                "queue_key": key,
                "pending_requests": len(pending),
                "pending_estimated_tokens": sum(item.estimate_input_tokens() for item in pending),
                "jobs": jobs.get(key, []),
            })
        return {"queues": queues, "collection_window_seconds": manager._collection_window}

    async def get_metrics(self, request: Request) -> dict:
        """Totals since the manager started; they outlive the jobs they count."""
        metrics = self._get_batch_manager()._metrics
        rounded = lambda seconds: None if seconds is None else round(seconds, 1)  # noqa: E731
        return {
            "completed_jobs": metrics.completed_jobs,
            "failed_jobs": metrics.failed_jobs,
            "total_completed_requests": metrics.completed_requests,
            "total_failed_requests": metrics.failed_requests,
            "processing_time": {
                "min": rounded(metrics.min_processing_time),
                "max": rounded(metrics.max_processing_time),
                "mean": rounded(metrics.mean_processing_time),
            },
        }


PLUGIN_FACTORY = BatchMonitorWebFactory
