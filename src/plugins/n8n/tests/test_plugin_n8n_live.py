"""Against a real n8n -- opt-in, for after every n8n upgrade (design §10.3).

Each test holds one measured fact the plugin stands on. When one fails after
an upgrade, docs/n8n_facts.md is re-measured before anything else changes --
and a check that n8n now does itself is dropped from validate.py, never kept
"to be safe".

Runs only with N8N_LIVE=1 and N8N_BASE_URL, N8N_API_KEY, N8N_MCP_KEY plus
N8N_TEST_API_KEY: a SEPARATE public API key with workflow:read/list/delete,
used only to delete what these tests create. The runtime key cannot delete,
by design. Every workflow here is named zz-probe-live-*.
"""
import os
import uuid

import httpx
import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.n8n.client import N8nClient
from plugins.n8n.server import N8nServer
from plugins.n8n.watch import wait_for_end

NEEDED = ("N8N_BASE_URL", "N8N_API_KEY", "N8N_MCP_KEY", "N8N_TEST_API_KEY")
pytestmark = pytest.mark.skipif(
    os.environ.get("N8N_LIVE") != "1" or not all(os.environ.get(v) for v in NEEDED),
    reason="live n8n tests need N8N_LIVE=1 and " + ", ".join(NEEDED))

PREFIX = "zz-probe-live-"


class Status:
    def __init__(self):
        self.lines = []

    async def progress(self, message, meta=None):
        pass

    async def end(self, message, meta=None):
        self.lines.append(("end", message))

    async def error(self, message, meta=None):
        self.lines.append(("error", message))


def sdk(name, *nodes, chain):
    """SDK code: nodes are (var, kind, type, version, name, parameters)."""
    body = "\n".join(f"const {var} = {kind}({{ type: '{t}', version: {v}, config: {{ name: '{n}', "
                     f"parameters: {p} }} }});" for var, kind, t, v, n, p in nodes)
    return (f"import {{ workflow, node, trigger, expr }} from '@n8n/workflow-sdk';\n{body}\n"
            f"export default workflow('{name}', '{name}').add({chain[0]})"
            + "".join(f".to({c})" for c in chain[1:]) + ";\n")


@pytest.fixture
async def n8n():
    base = os.environ["N8N_BASE_URL"].rstrip("/")
    client = N8nClient(base, os.environ["N8N_API_KEY"], os.environ["N8N_MCP_KEY"])
    yield client
    await client.aclose()
    async with httpx.AsyncClient(base_url=base, headers={"X-N8N-API-KEY": os.environ["N8N_TEST_API_KEY"]}) as c:
        left = [w for w in (await c.get("/api/v1/workflows", params={"limit": 250})).json()["data"]
                if w["name"].startswith(PREFIX)]
        for w in left:
            await c.delete(f"/api/v1/workflows/{w['id']}")
        assert not [w for w in (await c.get("/api/v1/workflows", params={"limit": 250})).json()["data"]
                    if w["name"].startswith(PREFIX)], "live probes left behind"


async def create(n8n, code, name):
    result = await n8n.mcp_call("create_workflow_from_code", {"code": code, "name": name})
    return result.payload["workflowId"]


async def output(n8n, execution_id, node):
    execution = await n8n.api_get(f"/executions/{execution_id}", {"includeData": "true"})
    return execution["data"]["resultData"]["runData"][node][0]["data"]["main"][0][0]["json"]


async def test_the_plugin_proves_a_workflow_end_to_end(n8n):
    cfg = ToolServerConfig()
    server = N8nServer("n8n", AgentSystemConfig(), cfg)
    name = PREFIX + uuid.uuid4().hex[:6]
    code = sdk(name,
               ("hook", "trigger", "n8n-nodes-base.webhook", 2.1, "Hook",
                f"{{ httpMethod: 'POST', path: '{name}', responseMode: 'responseNode' }}"),
               ("shape", "node", "n8n-nodes-base.set", 3.4, "Shape",
                "{ assignments: { assignments: [{ id: 'a', name: 'greeting', "
                "value: expr('Hello {{ $json.body.name }}'), type: 'string' }] } }"),
               ("respond", "node", "n8n-nodes-base.respondToWebhook", 1.1, "Respond",
                "{ respondWith: 'json', responseBody: expr('{{ JSON.stringify({ greeting: $json.greeting }) }}') }"),
               chain=["hook", "shape", "respond"])
    try:
        created = await server.create_workflow({"code": code, "name": name, "_status": Status()})
        assert created["status"] == "success" and created["ok"], created
        tested = await server.test_workflow({"workflow_id": created["workflow_id"], "_status": Status(),
                                             "trigger_input": [{"body": {"name": "Ada"}}]})
        assert tested["tested"], tested
        shape = next(n for n in tested["nodes"]["content"] if n["name"] == "Shape")
        assert shape["run"] == "live" and "Hello Ada" in shape["sample"]
    finally:
        await server.stop_plugin()


async def test_publish_trigger_and_take_offline_through_the_plugin(n8n):
    """M-MCP-65 to M-MCP-68: an untested version is refused, the tested one goes
    live by its version id, a webhook run is found while it still runs, and
    it is taken offline again. If a step breaks after an upgrade, re-measure
    before changing the plugin."""
    cfg = ToolServerConfig()
    cfg.allow_publish = True
    server = N8nServer("n8n", AgentSystemConfig(), cfg)
    name = PREFIX + uuid.uuid4().hex[:6]
    code = sdk(name,
               ("hook", "trigger", "n8n-nodes-base.webhook", 2.1, "Hook", f"{{ httpMethod: 'POST', path: '{name}' }}"),
               ("pause", "node", "n8n-nodes-base.wait", 1.1, "Pause", "{ amount: 3, unit: 'seconds' }"),
               ("shape", "node", "n8n-nodes-base.set", 3.4, "Shape",
                "{ assignments: { assignments: [{ id: 'a', name: 'got', value: expr('{{ $json.body.x }}'), "
                "type: 'string' }] } }"),
               chain=["hook", "pause", "shape"])
    try:
        created = await server.create_workflow({"code": code, "name": name, "_status": Status()})
        assert created["status"] == "success" and created["ok"], created
        wid = created["workflow_id"]
        untested = await server.publish_workflow({"workflow_id": wid, "_status": Status()})
        assert "no successful test" in untested.get("error", ""), untested
        tested = await server.test_workflow({"workflow_id": wid, "_status": Status(), "mocks": {"Pause": [{"json": {"body": {"x": "T"}}}]},
                                             "trigger_input": [{"json": {"body": {"x": "T"}}}]})
        assert tested["tested"], tested
        published = await server.publish_workflow({"workflow_id": wid, "_status": Status()})
        assert published["status"] == "success", published
        assert published["webhooks"]["content"][0]["url"].endswith(f"/webhook/{name}")
        fired = await server.trigger_workflow({"workflow_id": wid, "payload": {"x": "LIVE"}, "wait": "none",
                                               "_status": Status()})
        assert fired["http_status"] == 200 and fired["correlation"] == "heuristic", fired
        assert fired["execution_status"] == "running", "the run ended before it was found; the lookup of a running run is unproven"
        ended = await wait_for_end(lambda: server._n8n().api_get(f"/executions/{fired['execution_id']}"), max_s=60)
        assert ended == {"status": "success"}
        offline = await server.unpublish_workflow({"workflow_id": wid, "_status": Status()})
        assert offline["was_published"] is True, offline
        archived = await server.archive_workflow({"workflow_id": wid, "_status": Status()})
        assert archived["archived"] is True, archived
    finally:
        await server.stop_plugin()


async def test_caller_pins_are_honoured_and_unpinned_http_runs_live(n8n):
    """M-MCP-3/39: pins win. M-MCP-30: without a pin, HTTP runs for real --
    if this ever flips, the pin plan is re-checked, not loosened."""
    base = os.environ["N8N_BASE_URL"].rstrip("/")
    name = PREFIX + uuid.uuid4().hex[:6]
    wid = await create(n8n, sdk(name,
                                ("start", "trigger", "n8n-nodes-base.manualTrigger", 1, "Start", "{}"),
                                ("pinned", "node", "n8n-nodes-base.httpRequest", 4.2, "Pinned",
                                 "{ url: 'http://localhost:1/never' }"),
                                ("live", "node", "n8n-nodes-base.httpRequest", 4.2, "Live",
                                 f"{{ url: '{base}/healthz' }}"),
                                chain=["start", "pinned", "live"]), name)
    result = await n8n.mcp_call("test_workflow", {"workflowId": wid, "pinData": {
        "Start": [{"json": {}}], "Pinned": [{"json": {"pinned": "HTTP_PINNED"}}]}})
    execution_id = result.payload["executionId"]
    assert await output(n8n, execution_id, "Pinned") == {"pinned": "HTTP_PINNED"}
    assert (await output(n8n, execution_id, "Live")).get("status") == "ok", "unpinned HTTP no longer runs live"


async def test_a_pinned_sub_workflow_call_does_not_start_the_sub_workflow(n8n):
    """M-MCP-53: why a sub-workflow is always pinned. If a pinned call ever
    starts the callee, its nodes run unpinned and the pin plan must change."""
    name = PREFIX + uuid.uuid4().hex[:6]
    callee = await create(n8n, sdk(name + "-callee",
                                   ("start", "trigger", "n8n-nodes-base.executeWorkflowTrigger", 1.1, "Start",
                                    "{ inputSource: 'passthrough' }"),
                                   ("end", "node", "n8n-nodes-base.noOp", 1, "End", "{}"),
                                   chain=["start", "end"]), name + "-callee")
    caller = await create(n8n, sdk(name,
                                   ("start", "trigger", "n8n-nodes-base.manualTrigger", 1, "Start", "{}"),
                                   ("call", "node", "n8n-nodes-base.executeWorkflow", 1.3, "Call",
                                    f"{{ source: 'database', workflowId: {{ __rl: true, value: '{callee}', "
                                    f"mode: 'id' }} }}"),
                                   chain=["start", "call"]), name)
    result = await n8n.mcp_call("test_workflow", {"workflowId": caller, "pinData": {
        "Start": [{"json": {}}], "Call": [{"json": {"pinned": "CALL_PINNED"}}]}})
    assert await output(n8n, result.payload["executionId"], "Call") == {"pinned": "CALL_PINNED"}
    started = (await n8n.api_get("/executions", {"workflowId": callee}))["data"]
    assert not started, "a pinned call started the sub-workflow"


async def test_code_still_reaches_the_network(n8n):
    """M-MCP-38: why Code never runs live in a test. If this flips, the
    never-live rule stays until someone decides otherwise."""
    base = os.environ["N8N_BASE_URL"].rstrip("/")
    name = PREFIX + uuid.uuid4().hex[:6]
    js = (f"const r = await this.helpers.httpRequest({{ url: '{base}/healthz', json: true }}); "
          f"return [{{ json: {{ net: r.status }} }}];")
    wid = await create(n8n, sdk(name,
                                ("start", "trigger", "n8n-nodes-base.manualTrigger", 1, "Start", "{}"),
                                ("net", "node", "n8n-nodes-base.code", 2, "Net", f"{{ jsCode: {js!r} }}"),
                                chain=["start", "net"]), name)
    result = await n8n.mcp_call("test_workflow", {"workflowId": wid, "pinData": {"Start": [{"json": {}}]}})
    assert await output(n8n, result.payload["executionId"], "Net") == {"net": "ok"}


async def test_n8n_still_misses_the_gaps_validate_py_closes(n8n):
    """M-MCP-H8/H9: a version n8n does not know and an empty webhook path pass
    n8n's own validator. When one is caught by n8n, drop our check."""
    for version, path in ((99, "p"), (2.1, "")):
        code = sdk(PREFIX + "gap",
                   ("hook", "trigger", "n8n-nodes-base.webhook", version, "Hook",
                    f"{{ httpMethod: 'POST', path: '{path}' }}"),
                   ("end", "node", "n8n-nodes-base.noOp", 1, "End", "{}"), chain=["hook", "end"])
        verdict = (await n8n.mcp_call("validate_workflow", {"code": code})).payload
        assert verdict.get("valid") is True, f"n8n now catches version={version} path={path!r}: {verdict}"
