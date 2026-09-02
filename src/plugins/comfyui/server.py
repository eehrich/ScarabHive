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
        # Threshold for detecting lost jobs (unknown status) - fail early if job stays unknown
        self.unknown_threshold = getattr(mcp_config, 'unknown_threshold_seconds', 60)
        self._lb_strategy = getattr(mcp_config, 'strategy', 'least_loaded') or 'least_loaded'

        # Build server list for load balancing.
        # If 'servers' list is configured, use it; otherwise fall back to single host/port.
        raw_servers = getattr(mcp_config, 'servers', None)
        if raw_servers and isinstance(raw_servers, list) and len(raw_servers) > 0:
            self._servers: list[dict[str, Any]] = [
                {"host": s["host"], "port": int(s.get("port", 8188))}
                for s in raw_servers
                if isinstance(s, dict) and "host" in s
            ]
            logger.info(
                "ComfyUI load balancing enabled: %d server(s) configured",
                len(self._servers),
            )
        else:
            self._servers = [{"host": self.host, "port": self.port}]
        
        # Output directory - supports {session_id} template for session isolation
        self._output_dir_template = getattr(mcp_config, 'output_dir', "data/comfyui/outputs")
        # Base output dir (without session_id substitution) for cleanup and fallback
        self._output_dir_base = Path(self._output_dir_template.replace("{session_id}", "").rstrip("/\\"))
        self._output_dir_base.mkdir(parents=True, exist_ok=True)
        # Legacy: self.output_dir for backward compatibility (uses base path)
        self.output_dir = self._output_dir_base
        self.cleanup_age_hours = int(getattr(mcp_config, 'cleanup_age_hours', 48))

        # upload_image security allowlist — list of absolute roots that
        # _op_upload_image is allowed to read from. LLM-controlled
        # file_path values are rejected if they don't resolve inside one
        # of these roots, preventing confused-deputy reads of arbitrary
        # host files. An empty list disables upload_image entirely.
        #
        # Default scope is INTENTIONALLY narrow — the plugin's own
        # output_dir (re-upload of generated images) and an
        # image-asset subtree under data/. The bare project ``data/``
        # tree contains databases (writer.db, users.db, message_debugger
        # debugger.db, comfyui jobs.db, lessons_learned), agent traces,
        # and other non-image state. Setting the default to ``data/``
        # would re-open the confused-deputy class the allowlist was
        # introduced to close. Operators who need broader access opt in
        # explicitly via the ``upload_source_dirs`` config.
        raw_allowlist = getattr(mcp_config, 'upload_source_dirs', None)
        if raw_allowlist is None:
            raw_allowlist = [
                str(self._output_dir_base),
                "data/comfyui",
                "data/writer/assets",
            ]
        # Normalise a string config to a single-entry list so a misset
        # YAML value (e.g. upload_source_dirs: "data/img") doesn't get
        # iterated character-by-character into a silently-empty list.
        if isinstance(raw_allowlist, (str, Path)):
            raw_allowlist = [raw_allowlist]
        self._upload_source_dirs: list[Path] = []
        for p in raw_allowlist:
            try:
                self._upload_source_dirs.append(Path(p).resolve())
            except (TypeError, OSError) as e:
                logger.warning(
                    "Skipping invalid comfyui.upload_source_dirs entry %r: %s", p, e,
                )

        # Permitted file extensions for upload_image (case-insensitive).
        # Defense-in-depth against exfil of non-image bytes (DB files,
        # JSON traces, .env, etc.) even if they sit inside an allowed
        # root. Override via mcp_config.upload_image_extensions.
        raw_exts = getattr(mcp_config, 'upload_image_extensions', None)
        if raw_exts is None:
            raw_exts = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"]
        if isinstance(raw_exts, str):
            raw_exts = [raw_exts]
        self._upload_image_extensions: set[str] = {
            e.lower() if e.startswith(".") else f".{e.lower()}"
            for e in raw_exts
            if isinstance(e, str)
        }
        
        # Parse workflow configurations
        self.workflows: dict[str, dict[str, Any]] = {}
        self.workflow_files_dir = Path(getattr(mcp_config, 'workflow_files_dir', "config/comfyui_workflows"))
        
        for wf_config in getattr(mcp_config, 'workflows', []):
            wf_id = wf_config.get("id") if isinstance(wf_config, dict) else None
            if wf_id:
                self.workflows[wf_id] = wf_config
        
        # Initialize client (primary server — used for monitoring, fallback, upload_image)
        self.client = ComfyUIClient(
            host=self._servers[0]["host"],
            port=self._servers[0]["port"],
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
        
        # Flag for lazy startup sync (will run on first tool call)
        self._startup_sync_done = False
        
        # Flag for cleanup task - will be started lazily when event loop is available
        self._cleanup_task_started = False
        # Strong reference to the running cleanup task so the event loop
        # doesn't GC it mid-execution (asyncio docs warn fire-and-forget
        # tasks can disappear, producing "Task was destroyed but it is
        # pending" warnings and partial cleanups).
        self._cleanup_task: asyncio.Task[int] | None = None
        
        logger.info(
            "ComfyUI plugin initialized: %s:%s with %d workflows",
            self.host, self.port, len(self.workflows)
        )

    # =========================================================================
    # Load-balancing helpers
    # =========================================================================

    # Health cache: maps "host:port" → time.monotonic() of last offline probe.
    # Servers are skipped for _HEALTH_CACHE_TTL seconds to avoid repeated
    # connection timeouts when a server is down.
    _health_cache: dict[str, float] = {}
    _HEALTH_CACHE_TTL = 60.0

    def _build_client(self, host: str, port: int, output_dir: Path) -> "ComfyUIClient":
        """Build a ComfyUIClient for the given server."""
        return ComfyUIClient(
            host=host,
            port=port,
            output_dir=output_dir,
            timeout=float(self.timeout),
        )

    async def _pick_client(self, output_dir: Path) -> "ComfyUIClient":
        """Return a client pointing at a ComfyUI server.

        Uses the ``strategy`` config (``least_loaded`` or ``random``).
        Offline servers are cached for 60s to avoid repeated probe timeouts.
        """
        if len(self._servers) == 1:
            return self.client

        import time
        now = time.monotonic()
        strategy = self._lb_strategy

        if strategy == "random":
            import random as _rng
            healthy = [
                s for s in self._servers
                if f"{s['host']}:{s.get('port', 8188)}" not in self._health_cache
                or (now - self._health_cache[f"{s['host']}:{s.get('port', 8188)}"]) >= self._HEALTH_CACHE_TTL
            ]
            if not healthy:
                healthy = list(self._servers)
            best_srv = _rng.choice(healthy)
            logger.debug(
                "ComfyUI load balancer (random): selected %s:%s",
                best_srv["host"], best_srv.get("port", 8188),
            )
            return self._build_client(best_srv["host"], best_srv["port"], output_dir)

        # --- least_loaded strategy ---
        async def _probe(srv: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            key = f"{srv['host']}:{srv.get('port', 8188)}"
            last_fail = self._health_cache.get(key)
            if last_fail is not None and (now - last_fail) < self._HEALTH_CACHE_TTL:
                return (999999, srv)
            try:
                probe = self._build_client(srv["host"], srv["port"], output_dir)
                info = await probe.ping()
                if info.get("status") != "online":
                    self._health_cache[key] = now
                    return (999999, srv)
                self._health_cache.pop(key, None)
                depth = info.get("queue_pending", 0) + info.get("queue_running", 0)
                return (depth, srv)
            except Exception:
                self._health_cache[key] = now
                return (999999, srv)

        results = await asyncio.gather(*[_probe(s) for s in self._servers])
        best_depth, best_srv = min(results, key=lambda x: x[0])

        if best_depth == 999999:
            logger.warning(
                "ComfyUI load balancer: all servers appear offline — "
                "falling back to primary %s:%s",
                self._servers[0]["host"], self._servers[0]["port"],
            )
            best_srv = self._servers[0]

        logger.debug(
            "ComfyUI load balancer: selected %s:%s (queue depth %s)",
            best_srv["host"], best_srv.get("port", 8188),
            best_depth if best_depth < 999999 else "offline",
        )
        return self._build_client(best_srv["host"], best_srv["port"], output_dir)

    async def _client_for_job(
        self, prompt_id: str, output_dir: Path
    ) -> "ComfyUIClient":
        """Return a client for the server that originally handled *prompt_id*.

        Looks up the ``server_url`` stored in the job tracker at submit-time.
        Falls back to the primary client if no record is found.
        """
        server_url = self.job_tracker.get_server_url(prompt_id)
        if server_url:
            try:
                # server_url is stored as "http://host:port"
                from urllib.parse import urlparse
                parsed = urlparse(server_url)
                return self._build_client(
                    parsed.hostname or self.host,
                    parsed.port or self.port,
                    output_dir,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to parse server_url %r for job %s: %s",
                    server_url, prompt_id, exc,
                )
        return self.client  # fallback: primary server
    
    def _resolve_output_dir(self, session_id: str | None = None) -> Path:
        """Resolve output directory, substituting {session_id} if present in template.
        
        This enables session-based isolation of output files. When multiple agents
        run concurrently, each gets its own subdirectory to avoid file conflicts.
        
        Args:
            session_id: Session ID to substitute into the path template.
                       If None and template contains {session_id}, returns base path.
        
        Returns:
            Resolved Path object with {session_id} substituted if applicable.
            
        Example:
            Template: "data/writer/audio/temp/{session_id}"
            Session ID: "abc123"
            Result: Path("data/writer/audio/temp/abc123")
        """
        if "{session_id}" not in self._output_dir_template:
            # No template - return base path
            return self._output_dir_base

        if not session_id:
            # Template exists but no session_id provided - use base path
            logger.debug("output_dir template contains {session_id} but no session_id provided, using base path")
            return self._output_dir_base

        # SECURITY: session_id is harness-supplied by convention, but a
        # tainted value (`..`, `..\Windows`, absolute path) would let
        # template substitution escape the configured output root and
        # turn every subsequent file write into an arbitrary-write
        # primitive. Reject anything that isn't a strict identifier
        # and fall back to the base dir.
        import re
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", session_id):
            logger.warning(
                "Rejecting suspicious session_id %r; falling back to base output dir",
                session_id,
            )
            return self._output_dir_base

        # Substitute session_id into template
        resolved_path = Path(self._output_dir_template.replace("{session_id}", session_id))
        resolved_path.mkdir(parents=True, exist_ok=True)
        return resolved_path

    async def _startup_sync(self) -> None:
        """Sync job tracker with ComfyUI queue on startup.
        
        Marks stale jobs (queued/running in DB but not in ComfyUI) as failed.
        This handles the case where the API server was restarted.
        
        Note: We check history before marking as failed to avoid race conditions
        where a job finished between queue removal and history addition.
        
        Also starts the cleanup task on first call (lazy initialization).
        """
        if self._startup_sync_done:
            return
        self._startup_sync_done = True
        
        # Start cleanup task now that we have an event loop
        if self.cleanup_age_hours > 0 and not self._cleanup_task_started:
            self._cleanup_task_started = True
            self._cleanup_task = asyncio.create_task(self._cleanup_old_files())
        
        try:
            # Aggregate queue across all configured servers so jobs
            # queued on a non-primary host are not silently flagged
            # stale by the sync pass.
            queue_data = await self._get_aggregated_queue()
            if queue_data is None:
                logger.debug("No ComfyUI server online, skipping startup sync")
            else:
                updated = await self._sync_stale_jobs_with_history(queue_data)
                if updated > 0:
                    logger.info("Startup sync: updated %d stale jobs", updated)
        except Exception as e:
            logger.debug("Failed to sync jobs on startup: %s", e)

    async def _get_aggregated_queue(self) -> dict[str, Any] | None:
        """Aggregate queue_pending + queue_running across every configured
        ComfyUI server.

        Returns ``None`` if every server probe fails (caller treats as "no
        live data — do not touch DB job statuses"). Otherwise returns a
        dict shaped like ComfyUIClient.get_queue() PLUS a private
        ``_dead_servers`` set listing URLs whose queue we could not
        observe. Callers (specifically _sync_stale_jobs_with_history)
        use that to skip stale-classification of jobs whose owning
        server is currently unreachable — without it a transient blip
        on one of N servers would mark every in-flight job on it as
        failed.

        ``get_stale_job_ids`` ignores unknown keys so the extra field
        does not affect existing logic.
        """
        if len(self._servers) <= 1:
            try:
                ping = await self.client.ping()
                if ping.get("status") != "online":
                    return None
                q = await self.client.get_queue()
                q["_dead_servers"] = set()
                return q
            except Exception as e:
                logger.debug("Primary ComfyUI server unreachable: %s", e)
                return None

        async def _probe(srv: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
            url = f"http://{srv['host']}:{srv.get('port', 8188)}"
            try:
                probe = self._build_client(srv["host"], srv["port"], self.output_dir)
                ping = await probe.ping()
                if ping.get("status") != "online":
                    return (url, None)
                return (url, await probe.get_queue())
            except Exception as e:
                logger.debug(
                    "ComfyUI server %s:%s unreachable: %s",
                    srv["host"], srv.get("port", 8188), e,
                )
                return (url, None)

        results = await asyncio.gather(*[_probe(s) for s in self._servers])
        merged_pending: list = []
        merged_running: list = []
        dead_servers: set[str] = set()
        any_alive = False
        for url, r in results:
            if r is None:
                dead_servers.add(url)
                continue
            any_alive = True
            merged_pending.extend(r.get("queue_pending", []))
            merged_running.extend(r.get("queue_running", []))

        if not any_alive:
            return None
        return {
            "queue_pending": merged_pending,
            "queue_running": merged_running,
            "_dead_servers": dead_servers,
        }
    
    async def _sync_stale_jobs_with_history(self, queue_data: dict[str, Any]) -> int:
        """Sync stale jobs by checking history before marking as failed.
        
        This handles the race condition where a job finishes and is removed
        from the queue but not yet visible in history polling.
        
        Args:
            queue_data: Queue data from ComfyUI API
            
        Returns:
            Number of jobs updated
        """
        stale_ids = self.job_tracker.get_stale_job_ids(queue_data)
        
        if not stale_ids:
            return 0
        
        # Skip stale-classification of jobs whose owning server is
        # currently down — _get_aggregated_queue couldn't see that
        # server's queue, so absence from the merged dict says nothing
        # about the job. Without this guard a transient blip would
        # false-fail every in-flight job on the affected server.
        dead_servers: set[str] = queue_data.get("_dead_servers", set()) if isinstance(queue_data, dict) else set()

        updated = 0
        for prompt_id in stale_ids:
            if dead_servers:
                job_server = self.job_tracker.get_server_url(prompt_id)
                if job_server and job_server in dead_servers:
                    logger.debug(
                        "Skipping stale-check for %s (owning server %s currently unreachable)",
                        prompt_id, job_server,
                    )
                    continue

            # Check if job is in history (completed or failed). Use the
            # per-job client so jobs queued on a non-primary server are
            # probed against THEIR server, not the primary's history.
            try:
                job_client = await self._client_for_job(prompt_id, self.output_dir)
                history = await job_client.get_history(prompt_id)
                # comfyui_client.get_history returns {'error': '...'} on
                # a network failure instead of raising — without this
                # guard a transient network blip lands in the "truly
                # lost → mark failed" branch below.
                if isinstance(history, dict) and "error" in history:
                    logger.debug(
                        "Skipping stale-check for %s — get_history error: %s",
                        prompt_id, history.get("error"),
                    )
                    continue
                if prompt_id in history:
                    # Job completed - check for errors
                    job_data = history[prompt_id]
                    status_data = job_data.get("status", {})
                    if status_data.get("status_str") == "error" or "exception_message" in job_data:
                        error_msg = job_data.get("exception_message") or status_data.get("exception_message", "Unknown error")
                        self.job_tracker.update_status(prompt_id, "failed", error_msg)
                        logger.debug("Job %s marked as failed (from history)", prompt_id)
                    else:
                        self.job_tracker.update_status(prompt_id, "completed")
                        logger.debug("Job %s marked as completed (from history)", prompt_id)
                    updated += 1
                else:
                    # Not in queue AND not in history - truly lost
                    self.job_tracker.update_status(
                        prompt_id, "failed", "Job lost (server restart or queue cleared)"
                    )
                    logger.debug("Job %s marked as failed (not in queue or history)", prompt_id)
                    updated += 1
            except Exception as e:
                logger.debug("Failed to check history for job %s: %s", prompt_id, e)
                # On error, mark as failed
                self.job_tracker.update_status(prompt_id, "failed", f"Failed to check status: {e}")
                updated += 1
        
        return updated
    
    async def _cleanup_old_files(self) -> int:
        """Delete output files older than cleanup_age_hours.

        Also removes empty directories that are older than cleanup_age_hours.

        The actual filesystem walk runs in a worker thread via
        ``asyncio.to_thread`` so a large output tree (recursive rglob +
        per-file stat) doesn't block the event loop for seconds and
        starve other tool calls.

        Returns:
            Number of files deleted
        """
        if self.cleanup_age_hours <= 0:
            return 0

        import time

        cutoff_time = time.time() - (self.cleanup_age_hours * 3600)

        def _do_cleanup() -> int:
            deleted_files = 0
            deleted_dirs = 0
            try:
                # First pass: Delete old files
                for file_path in self.output_dir.rglob('*'):
                    if not file_path.is_file():
                        continue

                    # Check file age
                    file_mtime = file_path.stat().st_mtime
                    if file_mtime < cutoff_time:
                        try:
                            file_path.unlink()
                            deleted_files += 1
                            logger.debug(f"Deleted old file: {file_path.name} (age: {(time.time() - file_mtime) / 3600:.1f}h)")
                        except Exception as e:
                            logger.warning(f"Failed to delete {file_path}: {e}")

                # Second pass: Delete empty old directories (bottom-up to handle nested empty dirs)
                # Sort by depth (deepest first) to delete child dirs before parents
                all_dirs = [d for d in self.output_dir.rglob('*') if d.is_dir()]
                all_dirs.sort(key=lambda p: len(p.parts), reverse=True)

                for dir_path in all_dirs:
                    try:
                        # Check if directory is empty
                        if not any(dir_path.iterdir()):
                            # Check directory age (only delete old empty dirs)
                            dir_mtime = dir_path.stat().st_mtime
                            if dir_mtime < cutoff_time:
                                dir_path.rmdir()
                                deleted_dirs += 1
                                logger.debug(f"Deleted empty old directory: {dir_path.name} (age: {(time.time() - dir_mtime) / 3600:.1f}h)")
                    except Exception:
                        # Ignore errors (dir might not be empty anymore, race condition, etc.)
                        pass

                if deleted_files > 0 or deleted_dirs > 0:
                    logger.info(f"Cleanup: Deleted {deleted_files} files and {deleted_dirs} empty directories older than {self.cleanup_age_hours}h")
            except Exception as e:
                logger.error(f"Failed to cleanup old files: {e}")

            return deleted_files

        return await asyncio.to_thread(_do_cleanup)
    
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
        # Lazy startup sync on first tool call
        await self._startup_sync()
        
        status = params.get("_status")
        operation = params.get("operation")
        
        if not operation:
            if status:
                await status.error("Operation parameter is required")
            return {"error": "operation is required"}
        
        # ===== LIST =====
        if operation == "list":
            result = await self._op_list(params)
            if status:
                count = result.get("count", 0)
                await status.end(f"Listed {count} available workflow(s)")
            return result
        
        # ===== EXECUTE =====
        elif operation == "execute":
            return await self._op_execute(params, status)
        
        # ===== STATUS =====
        elif operation == "status":
            result = await self._op_status(params)
            if status:
                # Three cases, and only the presence of "status" separates
                # them: a missing prompt_id and a dead connection come back as
                # {"error": ...} with NO status (the .get() default turned both
                # into a green END "Job status: unknown"), while a job that
                # really failed carries status "failed" AND an error -- keying
                # on "error" alone would have called that one "unavailable".
                prompt_id = params.get("prompt_id")
                job_status = result.get("status")
                if job_status is None:
                    await status.error(
                        f"Job status unavailable: {result.get('error', 'unknown reason')}")
                elif job_status == "failed":
                    await status.error(f"Job {prompt_id} failed: {result.get('error')}")
                else:
                    await status.end(f"Job {prompt_id}: {job_status}")
            return result
        
        # ===== RESULT =====
        elif operation == "result":
            return await self._op_result(params, status)
        
        # ===== WAIT_FOR_COMPLETION =====
        elif operation == "wait_for_completion":
            return await self._op_wait_for_completion(params, status)
        
        # ===== SERVER_STATUS =====
        elif operation == "server_status":
            result = await self.client.ping()
            if status:
                is_online = result.get("status") == "online"
                await status.end(f"ComfyUI server is {'online' if is_online else 'offline'}")
            return result
        
        # ===== QUEUE =====
        elif operation == "queue":
            queue_data = await self.client.get_queue()
            # get_queue() reports an unreachable server as {"error": ...} —
            # counting its (absent) lists would turn that into a healthy,
            # empty queue and hand the caller status: success.
            if "error" in queue_data:
                if status:
                    await status.error(f"Queue unavailable: {queue_data['error']}")
                return {"status": "error", "error": queue_data["error"]}
            result = {
                "status": "success",
                "pending": len(queue_data.get("queue_pending", [])),
                "running": len(queue_data.get("queue_running", [])),
                "queue": queue_data
            }
            if status:
                pending = result["pending"]
                running = result["running"]
                await status.end(f"Queue status: {running} running, {pending} pending")
            return result
        
        # ===== CANCEL =====
        elif operation == "cancel":
            prompt_id = params.get("prompt_id")
            if not prompt_id:
                if status:
                    await status.error("Prompt ID is required for cancel operation")
                return {"error": "prompt_id is required"}

            job_client = await self._client_for_job(prompt_id, self.output_dir)
            result = await job_client.cancel(prompt_id)
            # Only update tracker if cancel was successful — mirrors the
            # web router guard. ComfyUIClient.cancel returns
            # {"status": "error", ...} on failure; without this guard we
            # would lie to consumers about the live job state.
            cancelled = result.get("status") in ("cancelled", "already_finished")
            if cancelled:
                self.job_tracker.update_status(prompt_id, "cancelled")
            if status:
                # The guard above already knows cancel can fail — the status
                # line said "Job cancelled" regardless, so a refused interrupt
                # read as a success while the job kept rendering.
                if cancelled:
                    await status.end(f"Job {prompt_id}: {result.get('status')}")
                else:
                    await status.error(
                        f"Job {prompt_id} not cancelled: "
                        f"{result.get('error') or result.get('status') or 'unknown reason'}")
            return result
        
        # ===== LOAD =====
        elif operation == "load":
            return await self._op_load(params, status)

        # ===== UPLOAD_IMAGE =====
        elif operation == "upload_image":
            return await self._op_upload_image(params, status)
        
        if status:
            await status.error(f"Unknown operation: {operation}")
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
            if status:
                await status.error("workflow_id is required")
            return {"error": "workflow_id is required"}
        
        if workflow_id not in self.workflows:
            if status:
                await status.error(f"Unknown workflow: {workflow_id}")
            return {
                "error": f"Unknown workflow: {workflow_id}",
                "available": list(self.workflows.keys())
            }
        
        wf_config = self.workflows[workflow_id]
        workflow_file = self.workflow_files_dir / wf_config.get("workflow_file", f"{workflow_id}.json")
        
        # Check if workflow file exists
        if not workflow_file.exists():
            if status:
                await status.error(f"Workflow file not found: {workflow_file}")
            return {
                "error": f"Workflow file not found: {workflow_file}",
                "hint": "Export workflow from ComfyUI using 'Save (API Format)'"
            }
        
        # Load workflow JSON
        try:
            with open(workflow_file, "r", encoding="utf-8") as f:
                workflow_json = json.load(f)
        except Exception as e:
            if status:
                await status.error(f"Failed to load workflow: {e}")
            return {"error": f"Failed to load workflow: {e}"}
        
        # Inject parameters — accept JSON string (LLM callers, per schema)
        # or dict (internal/test callers). String is schema-declared because
        # Gemini's constrained decoder collapses on freeform objects
        # (additionalProperties: true) — emitting JSON-as-string sidesteps
        # MALFORMED_FUNCTION_CALL.
        user_params = params.get("parameters", {})
        if isinstance(user_params, str):
            try:
                user_params = json.loads(user_params) if user_params.strip() else {}
            except json.JSONDecodeError as e:
                if status:
                    await status.error(f"parameters is not valid JSON: {e}")
                return {"error": f"parameters is not valid JSON: {e}"}
        if not isinstance(user_params, dict):
            if status:
                await status.error("parameters must be a JSON object or JSON string")
            return {"error": "parameters must be a JSON object or JSON string"}
        # Pick the execution server BEFORE parameter injection: LoadImage
        # auto-uploads must land on the server that will run this workflow
        # (a manual upload_image goes to the primary — desyncs on multi-server).
        session_id = params.get("_session_id")
        exec_output_dir = self._resolve_output_dir(session_id)
        exec_client = await self._pick_client(exec_output_dir)

        for param_def in wf_config.get("parameters", []):
            param_name = param_def["name"]
            node_id = param_def.get("node_id")
            field_path = param_def.get("field", "")

            # Get value: user-provided or default
            if param_name in user_params:
                value = user_params[param_name]
            elif param_def.get("required"):
                if status:
                    await status.error(f"Required parameter missing: {param_name}")
                return {"error": f"Required parameter missing: {param_name}"}
            else:
                value = param_def.get("default")

            # Inject into workflow
            if node_id and field_path and value is not None:
                # LoadImage inputs read ComfyUI's INPUT folder only. When the
                # value resolves to a local file (fresh outputs are downloaded
                # locally), upload it to the execution server and inject the
                # server-side name — no manual upload_image round-trip needed.
                target_node = workflow_json.get(node_id) or {}
                if (isinstance(value, str) and field_path == "inputs.image"
                        and str(target_node.get("class_type", "")).startswith("LoadImage")):
                    uploaded = await self._ensure_image_on_server(value, exec_client, status)
                    if uploaded:
                        value = uploaded
                self._inject_value(workflow_json, node_id, field_path, value)

        if status:
            await status.progress(f"Executing workflow: {wf_config.get('name', workflow_id)}")

        queue_result = await exec_client.queue_prompt(workflow_json)

        if "error" in queue_result:
            error_payload: dict[str, Any] = {"error": queue_result["error"]}
            if queue_result.get("node_errors"):
                error_payload["node_errors"] = queue_result["node_errors"]
                # LoadImage validation failure: tell the agent HOW to fix the
                # call instead of letting it retry the same request blind.
                node_err_text = json.dumps(queue_result["node_errors"], ensure_ascii=False)
                if "Invalid image file" in node_err_text:
                    error_payload["hint"] = (
                        "The referenced image is not in ComfyUI's input folder and "
                        "could not be resolved to a local file. Pass the LOCAL path "
                        "of the image (e.g. the output_path from wait_for_completion) "
                        "as the image parameter — it will be uploaded automatically."
                    )
            if status:
                await status.error(f"Failed to queue workflow: {queue_result['error']}")
            return error_payload

        prompt_id = queue_result.get("prompt_id")
        if not prompt_id:
            if status:
                await status.error("No prompt_id returned from ComfyUI")
            return {"error": "No prompt_id returned from ComfyUI"}

        # Register job in tracker (store which server handled it)
        self.job_tracker.register_job(
            prompt_id=prompt_id,
            workflow_id=workflow_id,
            workflow_name=wf_config.get("name", workflow_id),
            parameters=user_params,
            output_prefix=params.get("output_prefix", "comfy"),
            server_url=exec_client.base_url,
        )
        
        if status:
            wf_name = wf_config.get("name", workflow_id)
            await status.end(f"Queued '{wf_name}'")
        
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
            # Note: _op_status is called from workflow() which handles status reporting
            # for this operation, so no status parameter needed here
            return {"error": "prompt_id is required"}
        
        # Get live status from ComfyUI (use the server that handled this job)
        job_client = await self._client_for_job(prompt_id, self.output_dir)
        live_status = await job_client.get_status(prompt_id)
        
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
            if status:
                await status.error("prompt_id is required")
            return {"error": "prompt_id is required"}
        
        download = params.get("download", True)
        include_content = params.get("include_content", False)
        output_prefix = params.get("output_prefix", "comfy")
        
        # Resolve output directory with session isolation if configured
        session_id = params.get("_session_id")
        effective_output_dir = self._resolve_output_dir(session_id)
        
        if status:
            await status.progress(f"Fetching results for {prompt_id}")
        
        # Get history from ComfyUI (use the server that handled this job)
        job_client = await self._client_for_job(prompt_id, effective_output_dir)
        history = await job_client.get_history(prompt_id)

        if prompt_id not in history:
            # History only contains *completed* jobs. Distinguish "still running"
            # (job exists in queue, just not done yet — agent called result too
            # early) from genuinely "not found" (never queued or already cleared).
            live = await job_client.get_status(prompt_id)
            live_state = live.get("status", "unknown")
            if live_state in ("pending", "running"):
                msg = f"Job {prompt_id} is still {live_state}, result not available yet"
                hint = "Use operation='wait_for_completion' (with include_content=true) instead of polling result manually."
            else:
                msg = f"Job {prompt_id} not found in queue or history"
                hint = "The job was never queued, was cleared, or the prompt_id is wrong."
            if status:
                await status.error(msg)
            return {
                "error": msg,
                "prompt_id": prompt_id,
                "live_status": live_state,
                "hint": hint,
            }
        
        job_data = history[prompt_id]
        
        # Check for errors
        if job_data.get("status", {}).get("status_str") == "error":
            self.job_tracker.update_status(
                prompt_id, "failed",
                str(job_data.get("status", {}).get("messages", []))
            )
            if status:
                await status.error(f"Job {prompt_id} failed")
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
            "text": [],
            "other": []
        }
        
        for node_id, node_output in job_data.get("outputs", {}).items():
            # Process media outputs (images, audio, video, gifs)
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
                                file_data = await job_client.get_file(
                                    file_info["filename"],
                                    file_info.get("subfolder", ""),
                                    file_info.get("type", "output")
                                )
                                local_filename = f"{output_prefix}_{file_info['filename']}"
                                local_path = effective_output_dir / local_filename
                                local_path.parent.mkdir(parents=True, exist_ok=True)
                                local_path.write_bytes(file_data)
                                # Return filename only (relative to output_dir) for LLM/audio_ops compatibility
                                file_record["local_path"] = local_filename
                                # Store full path for multimodal content encoding
                                file_record["full_path"] = str(local_path)
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
            
            # Process text outputs (from ShowText, SaveText, etc.)
            # Text can appear as 'text' list or direct string values in outputs
            if "text" in node_output:
                text_data = node_output["text"]
                # Handle both list and single value formats
                if isinstance(text_data, list):
                    for idx, text_item in enumerate(text_data):
                        text_content = text_item if isinstance(text_item, str) else str(text_item)
                        text_record: dict[str, Any] = {
                            "content": text_content,
                            "node_id": node_id,
                            "index": idx
                        }
                        
                        if download:
                            # Save text to file
                            try:
                                text_filename = f"{output_prefix}_text_{node_id}_{idx}.txt"
                                local_path = effective_output_dir / text_filename
                                local_path.parent.mkdir(parents=True, exist_ok=True)
                                local_path.write_text(text_content, encoding="utf-8")
                                # Return filename only (relative to output_dir) for LLM compatibility
                                text_record["local_path"] = text_filename
                                text_record["filename"] = text_filename
                                # Store full path for multimodal content encoding
                                text_record["full_path"] = str(local_path)
                            except Exception as e:
                                logger.error("Failed to save text output: %s", e)
                                text_record["save_error"] = str(e)
                        
                        outputs["text"].append(text_record)
                elif isinstance(text_data, str):
                    text_record = {
                        "content": text_data,
                        "node_id": node_id,
                        "index": 0
                    }
                    
                    if download:
                        try:
                            text_filename = f"{output_prefix}_text_{node_id}.txt"
                            local_path = effective_output_dir / text_filename
                            local_path.parent.mkdir(parents=True, exist_ok=True)
                            local_path.write_text(text_data, encoding="utf-8")
                            # Return filename only (relative to output_dir) for LLM compatibility
                            text_record["local_path"] = text_filename
                            text_record["filename"] = text_filename
                            # Store full path for multimodal content encoding
                            text_record["full_path"] = str(local_path)
                        except Exception as e:
                            logger.error("Failed to save text output: %s", e)
                            text_record["save_error"] = str(e)
                    
                    outputs["text"].append(text_record)
        
        # Update tracker
        self.job_tracker.update_status(prompt_id, "completed")
        
        # Cleanup old files after job completion. Single-flight: if a
        # previous cleanup is still running, skip — otherwise a burst of
        # concurrent jobs spawns overlapping rglob walks competing on
        # the same filesystem. Store the reference so the loop can't GC
        # the task mid-walk.
        if self.cleanup_age_hours > 0 and (
            self._cleanup_task is None or self._cleanup_task.done()
        ):
            self._cleanup_task = asyncio.create_task(self._cleanup_old_files())
        
        # Store output paths
        output_paths = {
            k: [f.get("local_path", f["filename"]) for f in v]
            for k, v in outputs.items() if v
        }
        self.job_tracker.set_outputs(prompt_id, output_paths)
        
        if status:
            total_files = sum(len(v) for v in outputs.values())
            await status.end(f"Retrieved {total_files} output file(s)")
        
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
            outputs: Dict with keys 'images', 'audio', 'video', 'text', 'other', each containing
                    file records with 'local_path', 'filename', 'content' (for text), etc.
        
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
                # Use full_path for multimodal encoding (needs actual file system path)
                full_path = file_record.get("full_path")
                if full_path:
                    filename = file_record.get("filename", "")
                    mime_type = self._guess_mime_type(filename, content_type)
                    
                    multimodal.append({
                        "type": content_type,
                        "path": full_path,
                        "mime_type": mime_type,
                        "description": f"Generated {content_type}: {filename}"
                    })
        
        # Add text outputs - include content directly for LLM
        for text_record in outputs.get("text", []):
            # Use full_path for multimodal encoding (needs actual file system path)
            full_path = text_record.get("full_path")
            content = text_record.get("content", "")
            filename = text_record.get("filename", f"text_{text_record.get('node_id', 'unknown')}.txt")
            
            text_item: dict[str, Any] = {
                "type": "text",
                "mime_type": "text/plain",
                "description": f"Generated text: {filename}",
                "content": content,  # Include text content directly
            }
            if full_path:
                text_item["path"] = full_path
            
            multimodal.append(text_item)
        
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
        
        # Resolve output directory with session isolation if configured
        session_id = params.get("_session_id")
        effective_output_dir = self._resolve_output_dir(session_id)
        
        if not any([prompt_id, file_path, filename]):
            if status:
                await status.error("One of prompt_id, file_path, or filename is required")
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
                if status:
                    await status.error(f"Job not found: {prompt_id}")
                return {"error": f"Job not found: {prompt_id}"}
            
            outputs = job.get("outputs", {})
            if not outputs:
                if status:
                    await status.error(f"No outputs found for job {prompt_id}")
                return {
                    "error": f"No outputs found for job {prompt_id}",
                    "hint": "Job may not have completed yet. Use operation='result' first to download outputs."
                }
            
            for output_type, paths in outputs.items():
                content_type = {
                    "images": "image",
                    "audio": "audio",
                    "video": "video",
                    "text": "text"
                }.get(output_type)
                
                if not content_type:
                    continue
                
                for path in paths:
                    # Stored paths are bare filenames relative to the
                    # job's effective_output_dir (set_outputs persists
                    # `f"{output_prefix}_{filename}"`). Resolve against
                    # the output dir; otherwise Path(filename).exists()
                    # evaluates against the process CWD and never matches.
                    path_obj = Path(path)
                    if not path_obj.is_absolute():
                        path_obj = effective_output_dir / path_obj
                    # Defensive containment check — the stored local_path
                    # is server-set, but resolve anyway in case the output
                    # dir contains symlinks pointing elsewhere.
                    try:
                        resolved = path_obj.resolve()
                        resolved.relative_to(effective_output_dir.resolve())
                        path_obj = resolved
                    except (ValueError, OSError):
                        logger.warning(
                            "Stored output path %s escapes output dir %s; skipping",
                            path_obj, effective_output_dir,
                        )
                        continue
                    if path_obj.exists():
                        mime_type = self._guess_mime_type(path_obj.name, content_type)
                        
                        # For text files, also load content directly
                        if content_type == "text":
                            try:
                                text_content = path_obj.read_text(encoding="utf-8")
                                multimodal.append({
                                    "type": content_type,
                                    "path": str(path_obj),
                                    "mime_type": "text/plain",
                                    "content": text_content,
                                    "description": f"Generated text from job {prompt_id}: {path_obj.name}"
                                })
                            except Exception as e:
                                logger.warning("Failed to read text file %s: %s", path_obj, e)
                                multimodal.append({
                                    "type": content_type,
                                    "path": str(path_obj),
                                    "mime_type": "text/plain",
                                    "description": f"Generated text from job {prompt_id}: {path_obj.name}",
                                    "read_error": str(e)
                                })
                        else:
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
                path_obj = effective_output_dir / file_path
            else:
                # Search by filename
                path_obj = effective_output_dir / filename
                if not path_obj.exists():
                    # Try to find in subdirectories
                    matches = list(effective_output_dir.rglob(filename))
                    if matches:
                        path_obj = matches[0]

            # SECURITY: contain the resolved path inside the output dir. The
            # file_path/filename are LLM-controlled; an absolute path or '..'
            # segments would otherwise escape effective_output_dir and read
            # arbitrary host files into the model context.
            try:
                path_obj = path_obj.resolve()
                path_obj.relative_to(effective_output_dir.resolve())
            except ValueError:
                if status:
                    await status.error("File path escapes the output directory")
                return {
                    "error": "Invalid file path (outside the allowed output directory)",
                    "searched_in": str(effective_output_dir),
                }

            if not path_obj.exists():
                if status:
                    await status.error(f"File not found: {file_path or filename}")
                return {
                    "error": f"File not found: {file_path or filename}",
                    "searched_in": str(effective_output_dir),
                    "hint": "Provide a valid file path or use prompt_id to load job outputs"
                }
            
            # Determine content type from extension
            content_type = self._get_content_type_from_path(path_obj)
            mime_type = self._guess_mime_type(path_obj.name, content_type)
            
            # For text files, also load content directly
            if content_type == "text":
                try:
                    text_content = path_obj.read_text(encoding="utf-8")
                    multimodal.append({
                        "type": content_type,
                        "path": str(path_obj),
                        "mime_type": "text/plain",
                        "content": text_content,
                        "description": f"Loaded text: {path_obj.name}"
                    })
                except Exception as e:
                    logger.warning("Failed to read text file %s: %s", path_obj, e)
                    multimodal.append({
                        "type": content_type,
                        "path": str(path_obj),
                        "mime_type": "text/plain",
                        "description": f"Loaded text: {path_obj.name}",
                        "read_error": str(e)
                    })
            else:
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
            if status:
                await status.error("No valid media files found to load")
            return {"error": "No valid media files found to load"}
        
        if status:
            await status.end(f"Loaded {len(multimodal)} file(s)")
        
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
        text_exts = {".txt", ".md", ".json", ".yaml", ".yml", ".xml", ".csv", ".log", ".html", ".htm"}
        
        if suffix in image_exts:
            return "image"
        elif suffix in audio_exts:
            return "audio"
        elif suffix in video_exts:
            return "video"
        elif suffix in text_exts:
            return "text"
        else:
            return "other"
    
    async def _op_wait_for_completion(self, params: dict[str, Any], status: Any) -> dict[str, Any]:
        """Wait synchronously until a job completes or times out.
        
        This operation polls the job status internally, eliminating the need
        for the agent to manually poll with wait + status operations.
        
        If include_content is True and the job completes successfully, this
        also retrieves outputs and includes multimodal content for LLM analysis.
        
        Unknown Job Detection:
        - If a job stays "unknown" (not in queue, not in history) for 60 seconds,
          it's considered lost and marked as failed immediately.
        - This handles cases where ComfyUI didn't receive the job (network issues,
          dropped connections, etc.)
        """
        prompt_id = params.get("prompt_id")
        if not prompt_id:
            if status:
                await status.error("prompt_id is required")
            return {"error": "prompt_id is required"}
        
        # Timeout only from plugin config, not from tool params
        timeout = self.timeout
        # Clamp poll_interval to a sane lower bound: it is LLM-controlled and
        # unbounded in the schema, so poll_interval=0 would busy-loop two HTTP
        # round-trips per iteration against ComfyUI for the full timeout window.
        try:
            poll_interval = max(1, int(params.get("poll_interval", 2)))
        except (TypeError, ValueError):
            poll_interval = 2
        include_content = params.get("include_content", False)
        
        # Unknown status threshold - if job stays unknown for this long, fail early
        # Jobs should appear in queue within seconds of submission
        # Configurable via unknown_threshold_seconds in plugin config
        unknown_threshold = self.unknown_threshold
        
        import time
        start_time = time.time()
        first_unknown_time: float | None = None  # Track when we first saw "unknown"
        
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
            
            # Get job status (use the server that handled this job)
            job_status = await (await self._client_for_job(prompt_id, self.output_dir)).get_status(prompt_id)
            current_status = job_status.get("status", "unknown")
            
            # Track unknown status duration
            if current_status == "unknown":
                if first_unknown_time is None:
                    first_unknown_time = time.time()
                    logger.debug("Job %s first seen as unknown", prompt_id)
                
                unknown_duration = time.time() - first_unknown_time
                
                # If job has been unknown for too long, it's lost
                if unknown_duration >= unknown_threshold:
                    error_msg = (
                        f"Job not found in ComfyUI queue or history after {int(unknown_duration)}s. "
                        f"The job may have been lost, rejected, or never received by ComfyUI."
                    )
                    if status:
                        await status.error(f"Job {prompt_id} lost: {error_msg}")
                    
                    # Update tracker
                    self.job_tracker.update_status(prompt_id, "failed", error_msg)
                    
                    return {
                        "status": "failed",
                        "prompt_id": prompt_id,
                        "elapsed_seconds": elapsed,
                        "unknown_duration": unknown_duration,
                        "error": error_msg
                    }
            else:
                # Job is known (pending/running/completed/failed) - reset unknown timer
                if first_unknown_time is not None:
                    logger.debug("Job %s found after being unknown for %.1fs", 
                                prompt_id, time.time() - first_unknown_time)
                first_unknown_time = None
            
            # Update status message periodically (every 10s)
            if status and int(elapsed) % 10 == 0:
                status_msg = f"Job {prompt_id}: {current_status}"
                if current_status == "unknown" and first_unknown_time:
                    unknown_dur = int(time.time() - first_unknown_time)
                    status_msg += f" (unknown for {unknown_dur}s/{unknown_threshold}s)"
                status_msg += f" (elapsed: {int(elapsed)}s, timeout in: {int(timeout - elapsed)}s)"
                await status.progress(status_msg)
            
            # Check if completed or failed
            if current_status == "completed":
                job_info = self.job_tracker.get_job(prompt_id)
                wf_name = job_info.get("workflow_name", "workflow") if job_info else "workflow"

                # If include_content, fetch results to get multimodal content
                if include_content:
                    result_params = {
                        "prompt_id": prompt_id,
                        "download": True,
                        "include_content": True,
                        "output_prefix": params.get("output_prefix", "comfy"),
                        "_session_id": params.get("_session_id")  # Pass through session_id for isolation
                    }
                    # The end line used to be published BEFORE this call, which
                    # runs with status=None: a failed fetch left the green line
                    # standing and never reported its own error.
                    result = await self._op_result(result_params, None)
                    result["elapsed_seconds"] = elapsed
                    if status:
                        if result.get("error"):
                            await status.error(
                                f"'{wf_name}' rendered ({int(elapsed)}s) but the "
                                f"results could not be fetched: {result['error']}")
                        else:
                            # total_files, not len(outputs): outputs is a dict
                            # of lists per category, so len() would count
                            # categories.
                            await status.end(
                                f"'{wf_name}' completed ({int(elapsed)}s), "
                                f"{result.get('total_files', 0)} output file(s)")
                    return result

                if status:
                    await status.end(f"'{wf_name}' completed ({int(elapsed)}s)")
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

        for idx, part in enumerate(parts[:-1]):
            if not isinstance(obj, dict) or part not in obj:
                traversed = ".".join(parts[: idx + 1])
                logger.warning(
                    "Workflow field path not present: node %s missing '%s' "
                    "(field=%s) — check workflow YAML parameter mapping",
                    node_id, traversed, field_path,
                )
                return
            obj = obj[part]

        if not isinstance(obj, dict):
            logger.warning(
                "Workflow field path not present: node %s parent of '%s' "
                "is %s, not dict (field=%s)",
                node_id, parts[-1], type(obj).__name__, field_path,
            )
            return

        obj[parts[-1]] = value

    @staticmethod
    def _is_contained(path: Path, root: Path) -> bool:
        """True if *path* (resolved) is inside *root* (resolved)."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _resolve_local_image_source(self, value: str) -> Path | None:
        """Resolve an LLM-provided image reference to a local file — safely.

        Accepts a path (absolute or CWD-relative) or a bare filename. Bare
        filenames are searched inside the upload_source_dirs allowlist (the
        plugin's own downloaded outputs live there), newest match wins —
        agents typically reference the file they just generated.

        Same security envelope as _op_upload_image: result must live inside
        upload_source_dirs and carry an allowed image extension. Returns None
        when nothing local matches (the value may simply be a filename that
        already exists in ComfyUI's input folder).
        """
        if not value or "\x00" in value or not self._upload_source_dirs:
            return None

        def _permitted(p: Path) -> bool:
            if not any(self._is_contained(p, root) for root in self._upload_source_dirs):
                return False
            if self._upload_image_extensions and p.suffix.lower() not in self._upload_image_extensions:
                return False
            return True

        raw = Path(value)
        candidates = [raw] if raw.is_absolute() else [raw, Path.cwd() / value]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved.exists() and resolved.is_file() and _permitted(resolved):
                return resolved

        # Bare filename: search the allowlisted roots (downloaded outputs,
        # writer assets). Reject values with separators — those were paths
        # that simply don't exist, not names to hunt for.
        if raw.name != value:
            return None
        matches: list[Path] = []
        for root in self._upload_source_dirs:
            if not root.exists():
                continue
            try:
                matches.extend(p for p in root.rglob(value) if p.is_file() and _permitted(p.resolve()))
            except OSError as e:
                logger.debug("rglob failed under %s: %s", root, e)
        if not matches:
            return None
        return max(matches, key=lambda p: p.stat().st_mtime)

    async def _ensure_image_on_server(
        self, value: str, exec_client: "ComfyUIClient", status: Any
    ) -> str | None:
        """Auto-upload for LoadImage parameters — the structural fix for the
        recurring "Invalid image file" failures.

        Agents referenced freshly generated outputs by filename, but
        LoadImage only reads ComfyUI's INPUT folder — and even a dutiful
        manual upload_image lands on the PRIMARY server while execute picks
        the least-loaded one, so multi-server setups desynced. Uploading the
        locally downloaded copy to the server that will run THIS workflow
        removes both failure modes.

        Returns the server-side filename to inject, or None to leave the
        parameter untouched (e.g. already-uploaded input names).
        """
        local = self._resolve_local_image_source(value)
        if local is None:
            return None
        try:
            image_data = local.read_bytes()
        except OSError as e:
            logger.warning("Auto-upload: cannot read %s: %s", local, e)
            return None
        result = await exec_client.upload_image(
            image_data=image_data, filename=local.name, overwrite=True)
        if "error" in result:
            logger.warning("Auto-upload of %s failed: %s", local.name, result["error"])
            return None
        uploaded = result.get("name", local.name)
        if status:
            await status.progress(f"Auto-uploaded '{local.name}' to execution server")
        logger.info("Auto-uploaded %s -> '%s' for LoadImage parameter", local, uploaded)
        return uploaded

    async def _op_upload_image(self, params: dict[str, Any], status: Any) -> dict[str, Any]:
        """Upload a local image file to ComfyUI's input folder.

        The returned filename can be used as the ``reference_image`` (or
        ``image``) parameter in workflows that accept an input image
        (e.g. ``sdxl_img2img``, ``sdxl_ipadapter``).

        Args:
            params: Must contain ``file_path`` — path to the local image file.

        Returns:
            Dict with ``filename`` (ComfyUI-internal name) on success, or
            ``error`` on failure.
        """
        file_path_str = params.get("file_path")
        if not file_path_str:
            if status:
                await status.error("file_path is required for upload_image")
            return {"error": "file_path is required"}

        # SECURITY: file_path comes from the LLM. Constrain it to the
        # configured upload_source_dirs allowlist so the tool cannot be
        # tricked into reading arbitrary host files (e.g. /etc/passwd,
        # SSH keys, .env). Reject paths with null bytes outright.
        if "\x00" in file_path_str:
            err = "file_path contains null bytes"
            if status:
                await status.error(err)
            return {"error": err}

        if not self._upload_source_dirs:
            err = "upload_image disabled (no upload_source_dirs configured)"
            if status:
                await status.error(err)
            return {"error": err}

        raw = Path(file_path_str)
        candidates = [raw] if raw.is_absolute() else [raw, Path.cwd() / file_path_str]
        file_path: Path | None = None
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError):
                continue
            if not resolved.exists() or not resolved.is_file():
                continue
            if any(self._is_contained(resolved, root) for root in self._upload_source_dirs):
                file_path = resolved
                break

        if file_path is None:
            roots = ", ".join(str(r) for r in self._upload_source_dirs)
            err = (
                f"file_path {file_path_str!r} not found inside the configured "
                f"upload_source_dirs allowlist ({roots})"
            )
            if status:
                await status.error(err)
            return {"error": err}

        # Defense-in-depth: even if a non-image file (DB, .env, JSON
        # trace) sits inside an allowed root, refuse to upload it as
        # an "image". Closes the exfil window that a broad allowlist
        # would otherwise leave open.
        if self._upload_image_extensions:
            ext = file_path.suffix.lower()
            if ext not in self._upload_image_extensions:
                allowed_exts = ", ".join(sorted(self._upload_image_extensions))
                err = (
                    f"file_path {file_path.name!r} extension {ext!r} not in "
                    f"the allowed image extensions ({allowed_exts})"
                )
                if status:
                    await status.error(err)
                return {"error": err}

        try:
            image_data = file_path.read_bytes()
        except OSError as e:
            if status:
                await status.error(f"Cannot read file: {e}")
            return {"error": f"Cannot read file: {e}"}

        if status:
            await status.progress(f"Uploading {file_path.name} to ComfyUI…")

        result = await self.client.upload_image(
            image_data=image_data,
            filename=file_path.name,
            overwrite=True,
        )

        if "error" in result:
            if status:
                await status.error(f"Upload failed: {result['error']}")
            return result

        filename = result.get("name", file_path.name)
        if status:
            await status.end(f"Uploaded as '{filename}'")
        return {
            "status": "uploaded",
            "filename": filename,
            "subfolder": result.get("subfolder", ""),
            "message": f"Use filename='{filename}' as reference_image parameter in workflows",
        }

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
                    "plugin_name": self.name,
                    "title": f"ComfyUI Monitor - {self.name}",
                    "host": self.host,
                    "port": self.port
                }
            )
        
        @router.get("/jobs")
        async def get_jobs() -> JSONResponse:
            """Get all tracked jobs."""
            server_status = await self.client.ping()

            # Sync DB with live queue and history to update stale jobs.
            # Aggregate across all configured servers so load-balanced jobs
            # on non-primary hosts are not flagged stale.
            queue_data = await self._get_aggregated_queue()
            if queue_data is not None:
                await self._sync_stale_jobs_with_history(queue_data)

                # Check live status for all active jobs IN PARALLEL — each
                # against the server that actually owns it, via
                # _client_for_job.
                active_jobs = self.job_tracker.get_active_jobs()
                if active_jobs:
                    async def check_and_update_job(job: dict) -> None:
                        """Check live status and update DB if needed."""
                        prompt_id = job.get("prompt_id")
                        if not prompt_id:
                            return
                        try:
                            job_client = await self._client_for_job(
                                prompt_id, self.output_dir
                            )
                            live = await job_client.get_status(prompt_id)
                            live_status = live.get("status", "unknown")
                            db_status = job.get("status")
                            # Update DB if status changed (e.g. queued/running -> completed)
                            if live_status != db_status and live_status in ["completed", "failed"]:
                                error_msg = live.get("error") if live_status == "failed" else None
                                if isinstance(error_msg, list):
                                    error_msg = str(error_msg)
                                self.job_tracker.update_status(prompt_id, live_status, error_msg)
                        except Exception as e:
                            logger.warning(f"Failed to check status for job {prompt_id}: {e}")

                    # Run all status checks in parallel
                    await asyncio.gather(*[check_and_update_job(job) for job in active_jobs])
            
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

            # Get live status from the server that handled this job
            job_client = await self._client_for_job(prompt_id, self.output_dir)
            live_status = await job_client.get_status(prompt_id)
            job["live_status"] = live_status.get("status", "unknown")

            return JSONResponse({"status": "success", "job": job})

        @router.post("/jobs/{prompt_id}/cancel")
        async def cancel_job(prompt_id: str) -> JSONResponse:
            """Cancel a job."""
            job_client = await self._client_for_job(prompt_id, self.output_dir)
            result = await job_client.cancel(prompt_id)
            # Only update tracker if cancel was successful
            if result.get("status") in ("cancelled", "already_finished"):
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
