"""The status line of the comfyui tool must not claim success on a failure.

Four branches reported a green END for a failed call, audited 2026-09-02:

* ``queue`` counted the (absent) lists of an ``{"error": ...}`` answer and
  handed back ``status: success`` with 0/0 -- an unreachable server read as
  an idle, healthy queue.
* ``cancel`` ended with "Job cancelled" even when the interrupt was refused.
* ``status`` pushed every answer through ``result.get("status", "unknown")``,
  so a missing prompt_id and a dead connection both ended as "Job status:
  unknown".
* ``wait_for_completion`` with include_content published its END *before*
  fetching the results, and the fetch runs with status=None.

Every test drives the real dispatch (``call_with_status`` -> StatusScope ->
status bus), so a regression in the wiring fails them too.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_system.tools.status import StatusPhase, get_status_bus
from plugins.comfyui.server import ComfyUIServer


@pytest.fixture
def server(tmp_path: Path) -> ComfyUIServer:
    config = MagicMock()
    config.host = "127.0.0.1"
    config.port = 8188
    config.timeout_seconds = 5
    config.cleanup_age_hours = 0
    config.output_dir = str(tmp_path / "outputs")
    config.workflow_files_dir = str(tmp_path / "workflows")
    config.workflows = []
    return ComfyUIServer("comfyui", MagicMock(), config)


async def _run(server: ComfyUIServer, params: dict):
    """One real tool call; returns (result, published events)."""
    bus = get_status_bus()
    queue = await bus.subscribe(server="comfyui.workflow()")
    try:
        result = await server.call_with_status("comfyui_workflow", params)
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events, "no status events arrived -- the subscription is vacuous"
    return result, events


def _terminal(events):
    """The one event that closes the scope."""
    closing = [e for e in events if e.phase in (StatusPhase.END, StatusPhase.ERROR)]
    assert len(closing) == 1, [(e.phase, e.message) for e in events]
    return closing[0]


class TestQueue:
    async def test_unreachable_server_is_not_an_empty_queue(self, server):
        error = "Cannot connect to ComfyUI server at 127.0.0.1:8188"
        with patch.object(server.client, "get_queue",
                          AsyncMock(return_value={"error": error})):
            result, events = await _run(server, {"operation": "queue"})

        assert result["status"] == "error", result
        assert result["error"] == error
        closing = _terminal(events)
        assert closing.phase is StatusPhase.ERROR
        assert error in closing.message

    async def test_a_reachable_server_still_reports_its_counts(self, server):
        """Counter-check: the guard must not swallow healthy answers."""
        with patch.object(server.client, "get_queue", AsyncMock(return_value={
                "queue_running": [1], "queue_pending": [1, 2]})):
            result, events = await _run(server, {"operation": "queue"})

        assert result["status"] == "success"
        assert (result["running"], result["pending"]) == (1, 2)
        closing = _terminal(events)
        assert closing.phase is StatusPhase.END
        assert "1 running, 2 pending" in closing.message


class TestCancel:
    async def test_a_refused_interrupt_is_not_a_cancelled_job(self, server):
        with patch.object(server, "_client_for_job", AsyncMock(return_value=MagicMock(
                cancel=AsyncMock(return_value={"status": "error",
                                               "error": "HTTP 500 from ComfyUI"})))):
            result, events = await _run(
                server, {"operation": "cancel", "prompt_id": "abc123"})

        assert result["status"] == "error"
        closing = _terminal(events)
        assert closing.phase is StatusPhase.ERROR
        assert "abc123" in closing.message and "HTTP 500" in closing.message

    async def test_a_real_cancellation_still_ends(self, server):
        with patch.object(server, "_client_for_job", AsyncMock(return_value=MagicMock(
                cancel=AsyncMock(return_value={"status": "cancelled"})))):
            _, events = await _run(
                server, {"operation": "cancel", "prompt_id": "abc123"})

        closing = _terminal(events)
        assert closing.phase is StatusPhase.END
        assert "abc123" in closing.message and "cancelled" in closing.message


class TestJobStatus:
    async def test_an_error_is_not_job_status_unknown(self, server):
        with patch.object(server, "_op_status", AsyncMock(return_value={
                "error": "Cannot connect to ComfyUI server"})):
            _, events = await _run(
                server, {"operation": "status", "prompt_id": "abc123"})

        closing = _terminal(events)
        assert closing.phase is StatusPhase.ERROR
        assert "Cannot connect" in closing.message
        assert "unknown" not in closing.message

    async def test_a_real_status_names_the_job(self, server):
        with patch.object(server, "_op_status", AsyncMock(return_value={
                "status": "running"})):
            _, events = await _run(
                server, {"operation": "status", "prompt_id": "abc123"})

        closing = _terminal(events)
        assert closing.phase is StatusPhase.END
        assert "abc123" in closing.message and "running" in closing.message

    async def test_a_failed_job_is_an_error_not_an_unavailable_status(self, server):
        """A job that really failed carries BOTH status and error -- the query
        itself succeeded, so 'unavailable' would be the wrong word, and an END
        would be the wrong phase."""
        with patch.object(server, "_op_status", AsyncMock(return_value={
                "status": "failed", "error": ["CUDA out of memory"]})):
            _, events = await _run(
                server, {"operation": "status", "prompt_id": "abc123"})

        closing = _terminal(events)
        assert closing.phase is StatusPhase.ERROR
        assert "abc123" in closing.message and "failed" in closing.message
        assert "unavailable" not in closing.message


class TestWaitForCompletion:
    """The END used to be published BEFORE the results were fetched.

    The fetch (``_op_result``) runs with status=None, so when it failed the
    green line stayed and its error never reached the stream. This is the
    riskiest edit of the batch -- code moved across a return.
    """

    @staticmethod
    def _job_done(server):
        """A job the poll loop sees as completed on its first look.

        The loop asks ``_client_for_job(...).get_status(prompt_id)`` -- patch
        that, not the plugin's own client, or the loop keeps polling a server
        that is not there.
        """
        return patch.object(server, "_client_for_job", AsyncMock(
            return_value=MagicMock(
                get_status=AsyncMock(return_value={"status": "completed"}))))

    async def test_a_failed_fetch_is_not_a_completed_render(self, server):
        with self._job_done(server), \
             patch.object(server, "_op_result", AsyncMock(return_value={
                 "error": "Job abc123 not found in queue or history"})):
            result, events = await _run(server, {
                "operation": "wait_for_completion",
                "prompt_id": "abc123",
                "include_content": True,
            })

        assert result["error"], result
        closing = _terminal(events)
        assert closing.phase is StatusPhase.ERROR, closing.message
        assert "not found in queue" in closing.message

    async def test_a_good_fetch_ends_with_the_file_count(self, server):
        with self._job_done(server), \
             patch.object(server, "_op_result", AsyncMock(return_value={
                 "status": "completed", "prompt_id": "abc123",
                 "outputs": {"images": [1, 2], "videos": [3]},
                 "total_files": 3})):
            _, events = await _run(server, {
                "operation": "wait_for_completion",
                "prompt_id": "abc123",
                "include_content": True,
            })

        closing = _terminal(events)
        assert closing.phase is StatusPhase.END, closing.message
        # total_files, not len(outputs) -- the latter would say 2 (categories)
        assert "3 output file(s)" in closing.message

    async def test_without_include_content_the_end_is_unchanged(self, server):
        with self._job_done(server):
            result, events = await _run(server, {
                "operation": "wait_for_completion",
                "prompt_id": "abc123",
                "include_content": False,
            })

        assert result["status"] == "completed"
        closing = _terminal(events)
        assert closing.phase is StatusPhase.END
        assert "completed" in closing.message
