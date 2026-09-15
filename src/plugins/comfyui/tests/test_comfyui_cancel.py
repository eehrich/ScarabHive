"""Cancelling a job: what the tracker records, what the client sends to ComfyUI, and how the panel's route answers.

* A job that finished before the cancel reached it was recorded as cancelled, by the tool and the panel alike.
* A job neither queued, running nor in the history made the client post /interrupt, which stops whichever job runs.
* Every status poll that said "running" moved ``started_at``, so a job's duration was the time since the last poll.
* A job that had already failed was recorded failed without its error.
* Two instances with different servers share one tracker database: each took the other's jobs, absent from its own
  queues, for lost and failed them.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from plugins.comfyui.comfyui_client import ComfyUIClient
from plugins.comfyui.job_tracker import ComfyUIJobTracker
from plugins.comfyui.server import ComfyUIServer
from plugins.comfyui.web_endpoints import ComfyUIWebEndpoints


@pytest.fixture
def server(tmp_path: Path) -> ComfyUIServer:
    config = MagicMock()
    config.host = "comfyui.invalid"
    config.port = 8188
    config.timeout_seconds = 5
    config.cleanup_age_hours = 0
    config.servers = None
    config.output_dir = str(tmp_path / "outputs")
    config.workflow_files_dir = str(tmp_path / "workflows")
    config.workflows = []
    server = ComfyUIServer("comfyui", MagicMock(), config)
    server._startup_sync_done = True  # its queue probe would reach for a real ComfyUI
    server.job_tracker.register_job("job-1", "wf", "Workflow", {})
    return server


async def test_a_job_finished_before_the_cancel_keeps_its_outcome(server):
    server.job_tracker.update_status("job-1", "running")
    server.client.cancel = AsyncMock(return_value={"status": "already_finished", "job_status": "completed"})
    result = await server.workflow({"operation": "cancel", "prompt_id": "job-1"})
    assert result["status"] == "already_finished"
    assert server.job_tracker.get_job("job-1")["status"] == "completed"


async def test_a_job_the_tracker_already_closed_is_left_as_recorded(server):
    server.job_tracker.update_status("job-1", "failed", "out of memory")
    recorded = server.job_tracker.get_job("job-1")
    server.client.cancel = AsyncMock(return_value={"status": "already_finished", "job_status": "completed"})
    await server.workflow({"operation": "cancel", "prompt_id": "job-1"})
    assert server.job_tracker.get_job("job-1") == recorded


async def test_the_client_interrupts_nothing_for_a_job_it_cannot_find():
    client = ComfyUIClient(host="gpu.test", port=8188)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    with patch.object(client, "get_status", AsyncMock(return_value={"status": "unknown", "prompt_id": "job-1"})), \
            patch("aiohttp.ClientSession", return_value=session):
        result = await client.cancel("job-1")
    assert result["status"] == "error"
    assert "not in the queue or history of gpu.test:8188" in result["error"]
    session.post.assert_not_called()


def test_a_run_keeps_the_start_of_its_first_report(tmp_path):
    tracker = ComfyUIJobTracker(tmp_path / "jobs.db")
    tracker.register_job("job-1", "wf", "Workflow", {})
    tracker.update_status("job-1", "running")
    started = tracker.get_job("job-1")["started_at"]
    tracker.update_status("job-1", "running")
    assert tracker.get_job("job-1")["started_at"] == started


@pytest.mark.parametrize("prepare, cancel, status, detail", [
    (None, None, 404, "Job nope not found"),
    ("completed", None, 409, "Job job-1 is already completed"),
    ("running", {"status": "already_finished", "job_status": "failed", "error": "CUDA out of memory"}, 409,
     "Job job-1 has already failed"),
    ("running", {"status": "error", "error": "interrupt returned HTTP 500"}, 502, "interrupt returned HTTP 500"),
])
async def test_the_panel_route_answers_a_refusal_as_an_error(server, prepare, cancel, status, detail):
    if prepare:
        server.job_tracker.update_status("job-1", prepare)
    server.client.cancel = AsyncMock(return_value=cancel)
    with pytest.raises(HTTPException) as refused:
        await ComfyUIWebEndpoints(server).cancel_job(None, "nope" if prepare is None else "job-1")
    assert (refused.value.status_code, refused.value.detail) == (status, detail)
    if cancel is None:
        server.client.cancel.assert_not_called()
    if cancel and cancel["status"] == "already_finished":
        job = server.job_tracker.get_job("job-1")
        assert (job["status"], job["error"]) == ("failed", "CUDA out of memory")


async def test_the_client_hands_on_why_a_finished_job_failed():
    client = ComfyUIClient(host="gpu.test", port=8188)
    failed = {"status": "failed", "prompt_id": "job-1", "error": ["execution_error", "CUDA out of memory"]}
    with patch.object(client, "get_status", AsyncMock(return_value=failed)):
        result = await client.cancel("job-1")
    assert result == {"status": "already_finished", "prompt_id": "job-1", "job_status": "failed",
                      "error": "['execution_error', 'CUDA out of memory']"}


def instance(name: str, host: str, output_dir: Path) -> ComfyUIServer:
    config = MagicMock()
    config.host, config.port, config.servers = host, 8188, None
    config.timeout_seconds, config.cleanup_age_hours, config.workflows = 5, 0, []
    config.output_dir = str(output_dir)
    config.workflow_files_dir = str(output_dir.parent / "workflows")
    return ComfyUIServer(name, MagicMock(), config)


async def test_an_instance_leaves_the_jobs_of_another_instance_on_the_same_tracker_alone(tmp_path):
    mine = instance("comfyui", "a.test", tmp_path / "outputs")
    other = instance("writer_comfyui", "b.test", tmp_path / "outputs")
    assert mine.job_tracker.db_path == other.job_tracker.db_path
    other.job_tracker.register_job("b-job", "cover_animate", "Cover", {}, server_url="http://b.test:8188")
    other.job_tracker.update_status("b-job", "running")
    mine.job_tracker.register_job("a-job", "flux", "Flux", {}, server_url="http://a.test:8188")
    mine.client.ping = AsyncMock(return_value={"status": "online", "queue_pending": 0, "queue_running": 0})
    mine.client.get_queue = AsyncMock(return_value={"queue_running": [], "queue_pending": []})
    # every server answers that it never saw the job: only a.test's answer counts for this instance
    with patch.object(ComfyUIClient, "get_history", AsyncMock(return_value={})):
        await ComfyUIWebEndpoints(mine).list_jobs(None)
    assert mine.job_tracker.get_job("b-job")["status"] == "running"
    assert mine.job_tracker.get_job("a-job")["status"] == "failed"
