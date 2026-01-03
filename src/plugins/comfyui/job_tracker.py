"""ComfyUI Job Tracker - Persistent job state management.

Tracks ComfyUI job execution state in SQLite for monitoring and history.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ComfyUIJobTracker:
    """Track ComfyUI job execution state.
    
    Provides persistent storage and retrieval of job information
    including status, parameters, outputs, and timing.
    """
    
    def __init__(self, db_path: Path) -> None:
        """Initialize job tracker.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    prompt_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    workflow_name TEXT,
                    status TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_seconds REAL,
                    parameters TEXT,
                    output_prefix TEXT,
                    outputs TEXT,
                    error_message TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_submitted_at ON jobs(submitted_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_workflow_id ON jobs(workflow_id)"
            )
            conn.commit()
    
    def register_job(
        self,
        prompt_id: str,
        workflow_id: str,
        workflow_name: str,
        parameters: dict[str, Any],
        output_prefix: str = ""
    ) -> None:
        """Register a new job.
        
        Args:
            prompt_id: Unique job identifier from ComfyUI
            workflow_id: ID of the workflow being executed
            workflow_name: Human-readable workflow name
            parameters: Parameters passed to the workflow
            output_prefix: Prefix for output files
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs 
                (prompt_id, workflow_id, workflow_name, status, 
                 submitted_at, parameters, output_prefix)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prompt_id,
                    workflow_id,
                    workflow_name,
                    "queued",
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(parameters),
                    output_prefix
                )
            )
            conn.commit()
        logger.debug("Registered job %s for workflow %s", prompt_id, workflow_id)
    
    def update_status(
        self,
        prompt_id: str,
        status: str,
        error_message: str | None = None
    ) -> None:
        """Update job status.
        
        Args:
            prompt_id: Job identifier
            status: New status (queued/running/completed/failed/cancelled)
            error_message: Optional error message for failed jobs
        """
        now = datetime.now(timezone.utc).isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            if status == "running":
                conn.execute(
                    "UPDATE jobs SET status = ?, started_at = ? WHERE prompt_id = ?",
                    (status, now, prompt_id)
                )
            elif status in ["completed", "failed", "cancelled"]:
                # Calculate duration
                cursor = conn.execute(
                    "SELECT submitted_at FROM jobs WHERE prompt_id = ?",
                    (prompt_id,)
                )
                row = cursor.fetchone()
                duration = None
                if row and row[0]:
                    try:
                        submitted = datetime.fromisoformat(row[0])
                        duration = (datetime.now(timezone.utc) - submitted).total_seconds()
                    except ValueError:
                        pass
                
                conn.execute(
                    """
                    UPDATE jobs 
                    SET status = ?, completed_at = ?, duration_seconds = ?, error_message = ?
                    WHERE prompt_id = ?
                    """,
                    (status, now, duration, error_message, prompt_id)
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status = ? WHERE prompt_id = ?",
                    (status, prompt_id)
                )
            conn.commit()
        logger.debug("Updated job %s status to %s", prompt_id, status)
    
    def set_outputs(self, prompt_id: str, outputs: dict[str, list[str]]) -> None:
        """Store output file paths.
        
        Args:
            prompt_id: Job identifier
            outputs: Dict mapping output type to list of file paths
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET outputs = ? WHERE prompt_id = ?",
                (json.dumps(outputs), prompt_id)
            )
            conn.commit()
    
    def get_job(self, prompt_id: str) -> dict[str, Any] | None:
        """Get job details.
        
        Args:
            prompt_id: Job identifier
            
        Returns:
            Job dict or None if not found
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM jobs WHERE prompt_id = ?",
                (prompt_id,)
            )
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
        return None
    
    def get_active_jobs(self) -> list[dict[str, Any]]:
        """Get all queued/running jobs.
        
        Returns:
            List of active job dicts
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM jobs 
                WHERE status IN ('queued', 'running', 'pending')
                ORDER BY submitted_at ASC
                """
            )
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def get_recent_completed(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get recently completed/failed jobs.
        
        Args:
            limit: Maximum number of jobs to return
            
        Returns:
            List of completed job dicts
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM jobs 
                WHERE status IN ('completed', 'failed', 'cancelled')
                ORDER BY completed_at DESC
                LIMIT ?
                """,
                (limit,)
            )
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def get_all_jobs(
        self,
        limit: int = 100,
        offset: int = 0,
        status_filter: str | None = None,
        workflow_filter: str | None = None
    ) -> list[dict[str, Any]]:
        """Get jobs with optional filtering.
        
        Args:
            limit: Maximum number of jobs
            offset: Number of jobs to skip
            status_filter: Optional status to filter by
            workflow_filter: Optional workflow_id to filter by
            
        Returns:
            List of job dicts
        """
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        
        if status_filter:
            query += " AND status = ?"
            params.append(status_filter)
        
        if workflow_filter:
            query += " AND workflow_id = ?"
            params.append(workflow_filter)
        
        query += " ORDER BY submitted_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, params)
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def get_stats(self) -> dict[str, Any]:
        """Get job statistics.
        
        Returns:
            Dict with job counts by status
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT status, COUNT(*) as count
                FROM jobs
                GROUP BY status
                """
            )
            stats = {row[0]: row[1] for row in cursor.fetchall()}
            
            # Get total
            cursor = conn.execute("SELECT COUNT(*) FROM jobs")
            total = cursor.fetchone()[0]
            
            return {
                "total": total,
                "queued": stats.get("queued", 0),
                "running": stats.get("running", 0),
                "pending": stats.get("pending", 0),
                "completed": stats.get("completed", 0),
                "failed": stats.get("failed", 0),
                "cancelled": stats.get("cancelled", 0)
            }
    
    def cleanup_old_jobs(self, days: int = 7) -> int:
        """Delete jobs older than N days.
        
        Args:
            days: Number of days to keep
            
        Returns:
            Number of deleted jobs
        """
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                DELETE FROM jobs 
                WHERE status IN ('completed', 'failed', 'cancelled') 
                AND completed_at < ?
                """,
                (cutoff_iso,)
            )
            conn.commit()
            deleted = cursor.rowcount
        
        if deleted > 0:
            logger.info("Cleaned up %d old jobs", deleted)
        return deleted
    
    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert database row to dict with parsed JSON fields.
        
        Args:
            row: SQLite Row object
            
        Returns:
            Dict with parsed fields
        """
        result = dict(row)
        
        # Parse JSON fields
        if result.get("parameters"):
            try:
                result["parameters"] = json.loads(result["parameters"])
            except json.JSONDecodeError:
                pass
        
        if result.get("outputs"):
            try:
                result["outputs"] = json.loads(result["outputs"])
            except json.JSONDecodeError:
                pass
        
        # Map error_message to error for API consistency
        if "error_message" in result:
            result["error"] = result.pop("error_message")
        
        return result
