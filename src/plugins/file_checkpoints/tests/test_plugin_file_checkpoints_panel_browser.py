"""The Files panel (the plugin's web.py and static/panel.js) in a real browser: checkpoints newest first, a diff per file on demand, and
putting files back -- asked once more when files changed since stood in the way, never when a
run still going did. A refresh with nothing new leaves an open diff alone.

The page is the plugin's own route, template and script, built from the plugin and mounted as the app
mounts plugins; the chat endpoints behind it are a stub.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from agent_system.config.models import AgentSystemConfig, AuthConfig, ToolServerConfig
from agent_system.file_rewind import unregister_file_rewinder
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from plugins.file_checkpoints.plugin import PLUGIN_FACTORY
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent

# By the test machine's clock and calendar, as the panel groups them: yesterday at noon, and ten minutes ago.
_NOW = datetime.now().astimezone()
YESTERDAY = (_NOW - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
RECENT = (_NOW - timedelta(minutes=10)).replace(microsecond=0).isoformat()
CHECKPOINTS = [
    {"number": 3, "turn": 1, "after_turn": None, "dropped": False, "question": "fix the parser", "changed_at": YESTERDAY,
     "files": [{"path": "/w/a.txt", "change": "modified", "changed_at": YESTERDAY},
               {"path": "/w/logo.bin", "change": "created", "changed_at": YESTERDAY}],
     "untracked": [{"tool": "terminal/execute", "count": 2},
                   {"tool": "builder/build (partly not recorded: files the work tree ignores)", "count": 3}]},
    # A turn that only counted a call which may change files (a coding CLI's run): no file of its own.
    {"number": 4, "turn": 2, "after_turn": None, "dropped": False, "question": "let it run", "changed_at": YESTERDAY,
     "files": [], "untracked": [{"tool": "coder_cli/run_task", "count": 1}]},
    # A turn whose only call changed what the look leaves out (an install into a venv): no file, and no warning.
    {"number": 6, "turn": 3, "after_turn": None, "dropped": False, "question": "install it", "changed_at": YESTERDAY,
     "files": [], "untracked": [{"tool": "terminal/execute (partly not recorded: .venv directories)", "count": 1}]},
    {"number": 5, "turn": None, "after_turn": 1, "dropped": True, "question": "try again", "changed_at": RECENT,
     "files": [{"path": "/w/c.txt", "change": "created", "changed_at": RECENT}], "untracked": []},
]
DIFFS = {
    "/w/a.txt": {"diff": "--- before\n+++ after\n@@ -3,2 +4,2 @@\n-old line\n+new line\n same\n", "note": "",
                 "truncated": False, "change": "modified"},
    "/w/logo.bin": {"diff": None, "note": "Binary file (0 -> 6 bytes).", "truncated": False, "change": "created"},
}


def stub_app(store: Path) -> FastAPI:
    app = FastAPI()
    calls: list = []
    diffs: list = []

    @app.get("/chat/checkpoints")
    async def checkpoints(session_id: str):
        return {"session_id": session_id, "checkpoints": CHECKPOINTS, "text": ""}

    @app.get("/chat/checkpoints/diff")
    async def diff(session_id: str, checkpoint: int, path: str):
        diffs.append(path)
        if checkpoint != 3 or path not in DIFFS:
            raise HTTPException(status_code=404, detail="no such change")
        return {"session_id": session_id, "checkpoint": checkpoint, "path": path, **DIFFS[path]}

    @app.post("/chat/rewind")
    async def rewind(request: Request):
        body = await request.json()
        calls.append(body)
        if body.get("only") or body.get("path"):   # a revert of one checkpoint or one file
            return {"status": "rewound", "text": "Reverted it."}
        if body["checkpoint"] == 5:   # what held_for_a_write answers: no files stood in the way
            raise HTTPException(status_code=409, detail="Session s1 is running. Try again once it is done.")
        if not body.get("overwrite"):
            raise HTTPException(status_code=409, detail="Files not rewound: /w/a.txt was changed outside the agent.",
                                headers={"X-Rewind-Refusal": "files"})
        return {"status": "rewound", "text": "Put back 1 file."}

    @app.get("/stub/calls")
    async def stub_calls():
        return calls

    @app.get("/stub/diffs")
    async def stub_diffs():
        return diffs

    plugin = PLUGIN_FACTORY("file_checkpoints", AgentSystemConfig(), ToolServerConfig(
        type="file_checkpoints", enabled=True, config={"storage_path": str(store)}))
    unregister_file_rewinder(plugin)   # the panel's page only: the chat endpoints here are the stub's
    web = PluginWebRegistry()
    web.register_web_plugin("file_checkpoints", plugin)
    web.apply_to_app(app, AuthConfig(enabled=False))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/file_checkpoints", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return run_app_test_page(BROWSER, stub_app(tmp_path_factory.mktemp("fc_panel")),
                             "tests/file_checkpoints/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the checkpoints are listed newest first with where they stand',
    'a file opened shows its diff with the numbers of the new file',
    'a file without a diff says why',
    'a refresh with nothing new leaves an open diff alone',
    'the checkpoints stand under the span of their newest change, every change with its moment',
    'a file marked reviewed keeps its diff closed and its mark across a redraw',
    'all files of a checkpoint are marked and unmarked at once',
    'hiding the reviewed hides their checkpoint and a span left empty',
    'a checkpoint reviewed whole offers no put back until a mark comes off',
    'a refusal for a running session offers no overwrite',
    'a refused rewind is asked once more and then sent with overwrite',
    'a checkpoint that only counted calls is one line with nothing to put back',
    'a checkpoint whose calls changed only what the look does not see is one line without a warning',
    'a call recorded except for a part is named apart from the calls that were only counted',
    'a checkpoint is reverted alone, and a file of it alone',
    'what is reviewed offers no revert',
    'without storage a mark still holds for the page',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_files_panel(results, name):
    assert results.get(name) == "ok", results
