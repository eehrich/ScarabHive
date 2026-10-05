"""What the tool answers and records when a server cannot be asked, or a finished job is asked about again.

* Every ``status`` or ``result`` call on a finished job stamped it finished anew: the panel's finish time moved and its
  duration grew with each call.
* ``wait_for_completion`` saw a job finish without recording it, so the panel's times were those of a later call.
* ``result`` against a server that could not be reached answered "not found in queue or history" -- an agent reads
  that as a lost job and runs it again.
* ``status`` against such a server ended its status line green, "Job ...: unknown".
* ``load`` with ``file_path`` "." attached the output folder as if it were a file.

The network is refused by the conftest: a real client is used wherever the test says so.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_system.tools.status import StatusPhase, get_status_bus
from plugins.comfyui.server import ComfyUIServer

OLD_END = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def server(tmp_path: Path) -> ComfyUIServer:
    config = MagicMock()
    config.host = "comfy.invalid"
    config.port = 8188
    config.timeout_seconds = 5
    config.unknown_threshold_seconds = 60
    config.cleanup_age_hours = 0
    config.servers = None
    config.upload_source_dirs = []
    config.output_dir = str(tmp_path / "outputs")
    config.workflow_files_dir = str(tmp_path / "workflows")
    config.workflows = []
    server = ComfyUIServer("comfyui", MagicMock(), config)
    server._startup_sync_done = True
    server.job_tracker.register_job("job-1", "wf", "Workflow", {})
    return server


def finished_long_ago(server: ComfyUIServer, status: str) -> None:
    server.job_tracker.update_status("job-1", status)
    with sqlite3.connect(server.job_tracker.db_path) as conn:
        conn.execute("UPDATE jobs SET completed_at = ?, duration_seconds = 12.5 WHERE prompt_id = 'job-1'", (OLD_END,))


def test_a_finished_job_reported_again_keeps_its_end(server):
    finished_long_ago(server, "completed")
    server.job_tracker.update_status("job-1", "completed")
    job = server.job_tracker.get_job("job-1")
    assert (job["completed_at"], job["duration_seconds"]) == (OLD_END, 12.5)


def test_a_new_outcome_is_still_recorded(server):
    finished_long_ago(server, "cancelled")
    server.job_tracker.update_status("job-1", "failed", "interrupted")
    job = server.job_tracker.get_job("job-1")
    assert job["status"] == "failed" and job["error"] == "interrupted" and job["completed_at"] != OLD_END


async def test_a_status_call_on_a_finished_job_leaves_its_times(server):
    finished_long_ago(server, "completed")
    client = MagicMock(get_status=AsyncMock(return_value={"status": "completed", "prompt_id": "job-1"}))
    with patch.object(server, "_client_for_job", AsyncMock(return_value=client)):
        await server.workflow({"operation": "status", "prompt_id": "job-1"})
    assert server.job_tracker.get_job("job-1")["completed_at"] == OLD_END


@pytest.mark.parametrize("seen, recorded", [
    ({"status": "completed"}, ("completed", None)),
    ({"status": "failed", "error": ["out of memory"]}, ("failed", "['out of memory']")),
])
async def test_waiting_records_the_outcome_it_sees(server, seen, recorded):
    client = MagicMock(get_status=AsyncMock(return_value={"prompt_id": "job-1", **seen}))
    with patch.object(server, "_client_for_job", AsyncMock(return_value=client)):
        await server.workflow({"operation": "wait_for_completion", "prompt_id": "job-1"})
    job = server.job_tracker.get_job("job-1")
    assert (job["status"], job["error"]) == recorded
    assert job["completed_at"]


async def test_a_result_from_an_unreachable_server_is_no_lost_job(server):
    result = await server.workflow({"operation": "result", "prompt_id": "job-1"})
    assert "could not be fetched" in result["error"] and "Cannot connect" in result["error"], result
    assert "not found" not in result["error"]


async def test_a_status_from_an_unreachable_server_is_an_error_line(server):
    bus = get_status_bus()
    queue = await bus.subscribe(server="comfyui.workflow()")
    try:
        result = await server.call_with_status("comfyui_workflow", {"operation": "status", "prompt_id": "job-1"})
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    closing = [e for e in events if e.phase in (StatusPhase.END, StatusPhase.ERROR)]
    assert result["status"] == "unknown" and result["error"], result
    assert len(closing) == 1 and closing[0].phase is StatusPhase.ERROR, [(e.phase, e.message) for e in events]
    assert "unavailable" in closing[0].message


@pytest.mark.parametrize("file_path", [".", "sub"])
async def test_load_attaches_no_folder(server, file_path):
    (server.output_dir / "sub").mkdir()
    result = await server.workflow({"operation": "load", "file_path": file_path})
    assert "error" in result and "_multimodal_content" not in result, result


# ---------------------------------------------------------------- review round

def refusing_session(error: BaseException | None = None, status_code: int = 200):
    """An aiohttp.ClientSession stand-in: every request raises ``error``, or answers ``status_code`` with {}."""
    class Answer:
        status = status_code

        async def json(self):
            return {}

        async def __aenter__(self):
            if error is not None:
                raise error
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, *args, **kwargs):
            return Answer()

        post = get

    return Session


@pytest.fixture
def silent_server(server, monkeypatch):
    """A server that accepts and never answers: aiohttp raises a TimeoutError without text."""
    import asyncio

    import aiohttp

    from plugins.comfyui import comfyui_client
    monkeypatch.setattr(comfyui_client, "aiohttp", MagicMock(
        ClientSession=refusing_session(asyncio.TimeoutError()), ClientTimeout=aiohttp.ClientTimeout,
        ClientConnectorError=aiohttp.ClientConnectorError))
    return server


async def test_a_server_that_never_answers_names_the_timeout(silent_server):
    assert "TimeoutError after 5" in (await silent_server.client.get_queue())["error"]
    assert "TimeoutError after 5" in (await silent_server.client.get_history("job-1"))["error"]


async def test_a_status_from_a_silent_server_is_an_error_with_its_reason(silent_server):
    ends = []
    status = MagicMock(progress=AsyncMock(), end=AsyncMock(side_effect=lambda m: ends.append(("END", m))),
                       error=AsyncMock(side_effect=lambda m: ends.append(("ERROR", m))))
    await silent_server.workflow({"operation": "status", "prompt_id": "job-1", "_status": status})
    assert len(ends) == 1 and ends[0][0] == "ERROR" and "TimeoutError" in ends[0][1], ends


async def test_a_ping_answered_with_an_http_error_says_so(server, monkeypatch):
    import aiohttp

    from plugins.comfyui import comfyui_client
    monkeypatch.setattr(comfyui_client, "aiohttp", MagicMock(
        ClientSession=refusing_session(status_code=503), ClientTimeout=aiohttp.ClientTimeout,
        ClientConnectorError=aiohttp.ClientConnectorError))
    assert await server.client.ping() == {"status": "error", "code": 503, "error": "HTTP 503"}


def test_a_finished_job_reported_again_takes_a_new_error(server):
    finished_long_ago(server, "failed")
    server.job_tracker.update_status("job-1", "failed", "lost")
    server.job_tracker.update_status("job-1", "failed", "out of memory")
    server.job_tracker.update_status("job-1", "failed")  # no text: the one there stays
    job = server.job_tracker.get_job("job-1")
    assert (job["error"], job["completed_at"], job["duration_seconds"]) == ("out of memory", OLD_END, 12.5)


EVIL = [r"\\evil.example\share\x.png", "//evil.example/share/x.png"]


@pytest.fixture
def touched(monkeypatch):
    """Every file system call on a path naming evil.example, or one named escaped or secret (out of the output folder).

    The spy raises too; code that swallows the raise still leaves the call recorded."""
    calls = []
    for name in ("exists", "is_file", "is_dir", "resolve", "stat", "rglob", "read_bytes", "read_text", "mkdir",
                 "write_bytes", "write_text", "open"):
        original = getattr(Path, name)

        def spy(self, *args, _original=original, _name=name, **kwargs):
            if any(marker in str(self) for marker in ("evil.example", "escaped", "secret")):
                calls.append((_name, str(self)))
                raise AssertionError(f"{_name} on {self}")
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(Path, name, spy)
    return calls


@pytest.mark.parametrize("value", EVIL)
async def test_a_unc_path_is_refused_before_the_file_system_is_asked(server, touched, value):
    server._upload_source_dirs = server._upload_source_roots_as_named = [server.output_dir.resolve()]
    for params in ({"operation": "load", "file_path": value}, {"operation": "load", "filename": value},
                   {"operation": "upload_image", "file_path": value}):
        result = await server.workflow(params)
        assert "error" in result, (params, result)
    assert server._resolve_local_image_source(value) is None
    assert touched == []


async def test_an_anchored_filename_is_not_hunted_for(server):
    missing = str(server.output_dir.resolve() / "nowhere.png")
    result = await server.workflow({"operation": "load", "filename": missing})
    assert result["error"] == f"File not found: {missing}"
    result = await server.workflow({"operation": "load", "filename": "C:/elsewhere/x.png"})
    assert "outside" in result["error"]


def test_a_misspelt_field_is_not_added(server):
    workflow = {"3": {"inputs": {"text": "original"}}}
    server._inject_value(workflow, "3", "inputs.txet", "new")
    assert workflow == {"3": {"inputs": {"text": "original"}}}


async def test_a_result_without_download_keeps_the_saved_outputs(server):
    history = {"job-1": {"outputs": {"9": {"images": [{"filename": "x.png"}]}}, "status": {"status_str": "success"}}}
    client = MagicMock(get_history=AsyncMock(return_value=history), get_file=AsyncMock(return_value=b"png"))
    with patch.object(server, "_client_for_job", AsyncMock(return_value=client)):
        await server.workflow({"operation": "result", "prompt_id": "job-1"})
        await server.workflow({"operation": "result", "prompt_id": "job-1", "download": False})
    assert server.job_tracker.get_job("job-1")["outputs"] == {"images": ["comfy_x.png"]}
    result = await server.workflow({"operation": "load", "prompt_id": "job-1"})
    assert result["count"] == 1, result


async def test_a_result_whose_queue_check_fails_is_not_a_lost_job(server):
    client = MagicMock(get_history=AsyncMock(return_value={}), get_status=AsyncMock(
        return_value={"status": "unknown", "prompt_id": "job-1", "error": "Cannot connect"}))
    with patch.object(server, "_client_for_job", AsyncMock(return_value=client)):
        result = await server.workflow({"operation": "result", "prompt_id": "job-1"})
    assert "could not be fetched" in result["error"] and "not found" not in result["error"], result


async def test_list_filters_general_as_it_shows_it(server):
    server.workflows = {"a": {"id": "a"}, "b": {"id": "b", "category": "audio"}}
    result = await server.workflow({"operation": "list", "category": "general"})
    assert [w["id"] for w in result["workflows"]] == ["a"]


@pytest.mark.parametrize("error", ["", "Cannot connect"])
async def test_waiting_on_an_unreachable_server_records_nothing(server, error):
    server.unknown_threshold = 1
    client = MagicMock(get_status=AsyncMock(return_value={"status": "unknown", "prompt_id": "job-1", "error": error}))
    with patch.object(server, "_client_for_job", AsyncMock(return_value=client)):
        result = await server.workflow({"operation": "wait_for_completion", "prompt_id": "job-1", "poll_interval": 1})
    assert result["status"] == "unreachable" and result["hint"], result
    job = server.job_tracker.get_job("job-1")
    assert (job["status"], job["completed_at"], job["error"]) == ("queued", None, None)


# ---------------------------------------------------------------- review round 3

def history_client(outputs: dict, get_file=None) -> MagicMock:
    history = {"job-1": {"outputs": outputs, "status": {"status_str": "success"}}}
    return MagicMock(get_history=AsyncMock(return_value=history),
                     get_file=get_file or AsyncMock(return_value=b"png"))


@pytest.mark.parametrize("prefix", [r"\\evil.example\share\x", r"..\..\escaped", "../escaped", "a/b", "", 7])
async def test_an_output_prefix_that_is_no_plain_name_is_refused(server, touched, prefix):
    client = history_client({"9": {"images": [{"filename": "x.png"}], "text": ["hello"]}})
    with patch.object(server, "_client_for_job", AsyncMock(return_value=client)):
        result = await server.workflow({"operation": "result", "prompt_id": "job-1", "output_prefix": prefix})
        assert "output_prefix" in result["error"], result
        result = await server.workflow({"operation": "wait_for_completion", "prompt_id": "job-1",
                                        "include_content": True, "output_prefix": prefix})
        assert "output_prefix" in result["error"], result
    client.get_file.assert_not_called()
    assert touched == []


async def test_an_output_name_from_the_server_that_leads_out_is_not_saved(server, touched):
    client = history_client({"9": {"images": [{"filename": "x/../../../escaped.png"}]},
                             "../../../escaped": {"text": "hello"}})
    with patch.object(server, "_client_for_job", AsyncMock(return_value=client)):
        result = await server.workflow({"operation": "result", "prompt_id": "job-1"})
    assert "outside the output folder" in result["outputs"]["images"][0]["download_error"]
    assert "outside the output folder" in result["outputs"]["text"][0]["save_error"]
    client.get_file.assert_not_called()
    assert touched == []
    assert not server.job_tracker.get_job("job-1").get("outputs")


async def test_load_skips_a_recorded_path_outside_the_output_folder_unasked(server, touched):
    server.job_tracker.set_outputs("job-1", {"images": [r"\\evil.example\share\x.png", "../escaped.png"]})
    result = await server.workflow({"operation": "load", "prompt_id": "job-1"})
    assert result == {"error": "No valid media files found to load"}
    assert touched == []


@pytest.mark.parametrize("seen, said", [
    ({"status": "completed"}, "has just finished"),
    ({"status": "failed", "error": ["out of memory"]}, "failed: ['out of memory']"),
])
async def test_a_job_that_finished_between_the_lookups_is_not_lost(server, seen, said):
    client = MagicMock(get_history=AsyncMock(return_value={}),
                       get_status=AsyncMock(return_value={"prompt_id": "job-1", **seen}))
    with patch.object(server, "_client_for_job", AsyncMock(return_value=client)):
        result = await server.workflow({"operation": "result", "prompt_id": "job-1"})
    assert said in result["error"] and "not found" not in result["error"], result


async def test_a_partly_failed_download_keeps_the_outputs_saved_before(server):
    outputs = {"9": {"images": [{"filename": "x.png"}], "text": ["hello"]}}
    with patch.object(server, "_client_for_job", AsyncMock(return_value=history_client(outputs))):
        await server.workflow({"operation": "result", "prompt_id": "job-1"})
    broken = history_client(outputs, get_file=AsyncMock(side_effect=ValueError("Failed to get file: 500")))
    with patch.object(server, "_client_for_job", AsyncMock(return_value=broken)):
        await server.workflow({"operation": "result", "prompt_id": "job-1"})
    assert server.job_tracker.get_job("job-1")["outputs"] == {"images": ["comfy_x.png"], "text": ["comfy_text_9_0.txt"]}


async def test_load_judges_the_path_it_opens(tmp_path, monkeypatch, touched):
    if not tmp_path.drive:
        pytest.skip("drive-relative paths exist on Windows only")
    monkeypatch.chdir(tmp_path)
    with open(tmp_path / "secret.txt", "w") as f:  # not through Path: the spy is on
        f.write("SECRET")
    config = MagicMock(host="comfy.invalid", port=8188, timeout_seconds=5, unknown_threshold_seconds=60,
                       cleanup_age_hours=0, servers=None, upload_source_dirs=[], output_dir="rel/out",
                       workflow_files_dir="wf", workflows=[])
    server = ComfyUIServer("comfyui", MagicMock(), config)
    for params in ({"file_path": f"{tmp_path.drive}secret.txt"}, {"filename": f"{tmp_path.drive}secret.txt"}):
        result = await server.workflow({"operation": "load", **params})
        assert "outside" in result["error"], (params, result)
    assert touched == []  # refused on the path it would open, before the file system is asked


async def test_a_long_text_is_cut_in_the_answer_and_whole_in_the_file(server):
    from plugins.comfyui.server import TEXT_CONTENT_MAX_CHARS
    long = "ä" * (TEXT_CONTENT_MAX_CHARS + 1)
    client = history_client({"9": {"text": [long, "short"]}})
    with patch.object(server, "_client_for_job", AsyncMock(return_value=client)):
        result = await server.workflow({"operation": "result", "prompt_id": "job-1"})
    cut, short = result["outputs"]["text"]
    assert (len(cut["content"]), cut["truncated"], cut["size_bytes"]) == (TEXT_CONTENT_MAX_CHARS, True, 2 * len(long))
    assert short["content"] == "short" and "truncated" not in short
    assert (server.output_dir / "comfy_text_9_0.txt").read_text(encoding="utf-8") == long
    loaded = (await server.workflow({"operation": "load", "prompt_id": "job-1"}))["loaded_files"]
    assert [(len(f["content"]), f.get("truncated")) for f in loaded] == [(TEXT_CONTENT_MAX_CHARS, True), (5, None)]
