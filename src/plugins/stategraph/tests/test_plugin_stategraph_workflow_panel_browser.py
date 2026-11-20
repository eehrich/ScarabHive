"""A dynamic workflow's run in the real panel, in a real browser, served by the real plugin.

A workflow is never saved, so the panel has no machine file to open: it opens the run on the machine its snapshot
holds. Only the LLM boundary is replaced (FakeBackend); route, service, template, panel.js and kit are the real ones.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agent_system.config.models import AgentSystemConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, tool_config
from plugins.stategraph.tests.test_plugin_stategraph_workflows import (REVIEW, SERVERS, SETTINGS, fake_backends,
                                                                        settled)
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.filterwarnings(FASTAPI_PY314), pytest.mark.timeout(180)]

#: The panel in a frame of its own page: what it drew is read from there once it shows the run's machine.
PROBE = """<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">
<iframe id="panel" src="/plugins/stategraph/?run=%s" style="width:1200px;height:800px;border:0"></iframe>
<script>
const frame = document.getElementById('panel');
const started = Date.now();
function look() {
  const doc = frame.contentDocument;
  const head = doc && doc.getElementById('machineHead');
  const nodes = doc ? [...doc.querySelectorAll('.sg-node')].map((n) => n.dataset.state) : [];
  const ready = head && head.textContent.includes('quick_review') && nodes.length;
  if (!ready && Date.now() - started < 30000) {
    setTimeout(look, 100);
    return;
  }
  const seen = { head: head ? head.textContent : null, nodes,
    startHidden: doc ? doc.getElementById('startBox').hidden : null, url: doc ? doc.location.search : null };
  // a refresh as the refresh button sends it: the snapshot never changes, and no saved machine is asked for
  if (doc) doc.dispatchEvent(new CustomEvent('refresh', { detail: { auto: false } }));
  setTimeout(() => fetch('/__results', { method: 'POST', body: JSON.stringify(seen) }), 1500);
}
frame.addEventListener('load', look);
</script></body></html>"""


def workflow_app(root: Path) -> tuple[FastAPI, str]:
    """The plugin with one workflow run that ended, read from disk by a server of its own (as the app's would)."""
    system = AgentSystemConfig(plugins={"servers": SERVERS})

    async def seed() -> str:
        server = StateGraphServer("stategraph", system, tool_config(root, workflows=dict(SETTINGS)))
        fake_backends(server, {"find": "a bug", "verify/0": "stands"})
        started = await server.service.start_workflow(REVIEW, user_id="ann", caller="lead", wake=False)
        row = await settled(server, started["run_id"])
        assert row["status"] == "succeeded", row.get("error")
        await server.stop_plugin()
        return started["run_id"]

    run_id = asyncio.run(seed())
    server = StateGraphServer("stategraph", system, tool_config(root, workflows=dict(SETTINGS)))
    assert server.machines.find("quick_review") is None, "fixture: the workflow must have no saved machine"
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    registry = PluginWebRegistry()
    registry.register_web_plugin("stategraph", server)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.asked = []

    @app.middleware("http")
    async def record(request, call_next):
        app.state.asked.append(request.url.path)
        return await call_next(request)

    @app.get("/__probe")
    async def probe() -> HTMLResponse:
        return HTMLResponse(PROBE % run_id)
    return app, run_id


def test_a_workflows_run_opens_in_the_panel_on_its_own_graph_read_only(tmp_path):
    app, run_id = workflow_app(tmp_path)

    seen = run_app_test_page(BROWSER, app, "__probe", timeout=90)

    assert "quick_review" in seen["head"] and "run snapshot" in seen["head"], seen
    assert "Save" not in seen["head"], seen
    assert {"find", "verify", "done"} <= set(seen["nodes"]), seen
    assert seen["startHidden"] is True, seen
    assert run_id in seen["url"], seen
    asked = app.state.asked
    assert f"/plugins/stategraph/api/runs/{run_id}/machine" in asked, asked
    assert asked.count("/plugins/stategraph/api/machines") >= 2, f"fixture: the refresh did not run: {asked}"
    assert "/plugins/stategraph/api/machines/quick_review" not in asked, asked
