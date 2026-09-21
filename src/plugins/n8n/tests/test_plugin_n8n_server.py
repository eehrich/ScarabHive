"""Every lock of the server against a fake n8n.

The fake answers with the shapes measured on n8n 2.39.9. What these tests pin
down is what the plugin refuses and forwards -- the policy layer -- not n8n.
"""
import copy
import json

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.n8n.client import McpResult, N8nNotFound
from plugins.n8n.server import N8nServer

TAG = "scarabhive"


def workflow(wid="w1", *, tags=(TAG,), archived=False, exposed=True, nodes=None, settings=None):
    return {"id": wid, "name": f"flow {wid}", "versionId": "v1", "isArchived": archived,
            "tags": [{"id": f"t-{t}", "name": t} for t in tags],
            "settings": {"availableInMCP": exposed, **(settings or {})},
            "nodes": nodes if nodes is not None else [
                {"name": "Start", "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "parameters": {}},
                {"name": "Set", "type": "n8n-nodes-base.set", "typeVersion": 3.4, "parameters": {}}],
            "connections": {"Start": {"main": [[{"node": "Set", "type": "main", "index": 0}]]}}}


class FakeClient:
    """api_get / mcp_call as the server uses them, with measured answer shapes."""

    def __init__(self):
        self.workflows = {"w1": workflow()}
        self.executions = {"e1": {"id": "e1", "data": {"resultData": {"runData": {
            "Start": [{"executionStatus": "success", "data": {"main": [[{"json": {"a": 1}}]]}}],
            "Set": [{"executionStatus": "success", "data": {"main": [[{"json": {"b": 2}}]]}}]}}}}}
        self.credentials = [{"id": "c1", "name": "Bot", "type": "slackApi"}]
        self.calls = []
        self.answers = {}
        self.closed = False

    async def api_get(self, path, params=None):
        self.calls.append(("GET", path))
        parts = path.strip("/").split("/")
        if parts[0] == "workflows" and len(parts) == 2:
            if parts[1] not in self.workflows:
                raise N8nNotFound(path)
            return copy.deepcopy(self.workflows[parts[1]])
        if parts[0] == "workflows":
            return {"data": list(self.workflows.values()), "nextCursor": None}
        if parts[0] == "executions" and len(parts) == 2:
            if parts[1] not in self.executions:
                raise N8nNotFound(path)
            return self.executions[parts[1]]
        return {"data": [], "nextCursor": None}

    async def mcp_call(self, tool, arguments, timeout=None):
        self.calls.append((tool, copy.deepcopy(arguments)))
        if tool in self.answers:
            answer = self.answers[tool]
            return answer(arguments) if callable(answer) else answer
        if tool == "list_credentials":
            answer = {"data": self.credentials, "count": len(self.credentials)}
            return McpResult(answer, json.dumps(answer), False)
        if tool == "get_node_types":
            return McpResult("# TypeScript Type Definitions\nok", "# TypeScript Type Definitions\nok", False)
        if tool == "validate_workflow":
            return McpResult({"valid": True, "nodeCount": 2}, "", False)
        if tool == "create_workflow_from_code":
            self.workflows["w2"] = workflow("w2", tags=())
            return McpResult({"workflowId": "w2", "autoAssignedCredentials": []}, "", False)
        if tool == "update_workflow":
            wid = arguments["workflowId"]
            for op in arguments["operations"]:
                if op["type"] == "addTags":
                    self.workflows[wid]["tags"] += [{"id": "t", "name": n} for n in op["names"]]
            return McpResult({"workflowId": wid, "validationWarnings": []}, "", False)
        if tool == "test_workflow":
            return McpResult({"executionId": "e1", "status": "success"}, "", False)
        return McpResult({}, "{}", False)

    async def aclose(self):
        self.closed = True

    def tools_called(self):
        return [c[0] for c in self.calls]


class Status:
    def __init__(self):
        self.lines = []

    async def progress(self, message, meta=None):
        pass

    async def end(self, message, meta=None):
        self.lines.append(("end", message))

    async def error(self, message, meta=None):
        self.lines.append(("error", message))


def make_server(**config):
    cfg = ToolServerConfig()
    cfg.base_url, cfg.api_key, cfg.mcp_key = "http://n8n.test", "api-key", "mcp-key"
    for key, value in config.items():
        setattr(cfg, key, value)
    server = N8nServer("n8n", AgentSystemConfig(), cfg)
    server._client = FakeClient()
    return server


async def call(server, tool, **params):
    status = Status()
    result = await getattr(server, tool)({**params, "_status": status})
    assert len(status.lines) == 1, f"{tool}: one closing line expected, got {status.lines}"
    kind, line = status.lines[0]
    assert kind == ("end" if result["status"] == "success" else "error"), (tool, result, status.lines)
    assert len(line) <= 140
    return result


# ── the managed lock ──────────────────────────────────────────────────────

@pytest.mark.parametrize("change,needle", [
    (dict(tags=("other",)), "not managed"),
    (dict(archived=True), "archived"),
    (dict(exposed=False), "not exposed"),
])
async def test_acting_tools_refuse_a_workflow_outside_the_lock(change, needle):
    server = make_server()
    server._client.workflows["w1"] = workflow(**change)
    for tool, params in (("update_workflow", {"operations": [{"type": "setNodeDisabled", "nodeName": "Set"}]}),
                         ("test_workflow", {})):
        result = await call(server, tool, workflow_id="w1", **params)
        assert result["status"] == "error" and needle in result["error"]
    assert "update_workflow" not in server._client.tools_called()
    assert "test_workflow" not in server._client.tools_called()


# ── update filters ────────────────────────────────────────────────────────

@pytest.mark.parametrize("operation,needle", [
    ({"type": "removeTags", "names": [TAG]}, "marks the workflow as ours"),
    ({"type": "addNode", "node": {"name": "X", "type": "n8n-nodes-base.executeCommand"}}, "blocked"),
    ({"type": "addNode", "node": {"name": "X", "type": "n8n-nodes-base.gitTool"}}, "blocked"),
    ({"type": "setWorkflowSettings", "settings": {"saveManualExecutions": False,
                                                  "saveDataSuccessExecution": "none"}}, "saveManualExecutions"),
    ({"type": "setNodeCredential", "nodeName": "Set", "credentialKey": "slackApi", "credentialId": "ghost"},
     "does not exist"),
    ({"type": "setNodeCredential", "nodeName": "Set", "credentialKey": "githubApi", "credentialId": "c1"},
     "not a githubApi"),
])
async def test_update_refuses_what_the_builder_may_not_do(operation, needle):
    server = make_server()
    result = await call(server, "update_workflow", workflow_id="w1", operations=[operation])
    assert result["status"] == "error" and needle in result["error"]
    assert "update_workflow" not in server._client.tools_called()


async def test_update_refuses_when_the_workflow_moved():
    server = make_server()
    result = await call(server, "update_workflow", workflow_id="w1", expected_version_id="v0",
                        operations=[{"type": "setNodeDisabled", "nodeName": "Set", "disabled": True}])
    assert result["status"] == "error" and "changed since you read it" in result["error"]
    assert "update_workflow" not in server._client.tools_called(), "checked before anything is applied"


async def test_an_allowed_update_is_forwarded_and_rechecked():
    server = make_server()
    result = await call(server, "update_workflow", workflow_id="w1", expected_version_id="v1",
                        operations=[{"type": "setNodeCredential", "nodeName": "Set",
                                     "credentialKey": "slackApi", "credentialId": "c1"}])
    assert result["status"] == "success" and result["ok"]
    assert "update_workflow" in server._client.tools_called()


async def test_settings_that_still_store_a_test_run_may_be_changed():
    """Only saveManualExecutions false leaves a test run unstored (M-MCP-41);
    true is the repair."""
    server = make_server()
    result = await call(server, "update_workflow", workflow_id="w1", operations=[
        {"type": "setWorkflowSettings", "settings": {"saveManualExecutions": True,
                                                     "saveDataSuccessExecution": "none"}}])
    assert result["status"] == "success"


@pytest.mark.parametrize("tool,params,done", [
    ("create_workflow", {"code": "x", "name": "flow"}, "created and tagged"),
    ("update_workflow", {"workflow_id": "w1", "operations": [{"type": "setNodeDisabled", "nodeName": "Set"}]},
     "operations applied"),
])
async def test_a_failed_check_after_a_write_says_the_write_happened(tool, params, done):
    """Otherwise the model creates the workflow, or applies the operations, twice."""
    from plugins.n8n.client import N8nError
    server = make_server()

    fake_call = server._client.mcp_call

    async def mcp_call(tool_name, arguments, timeout=None):
        if tool_name == "list_credentials":
            raise N8nError("n8n MCP rate limit reached; retry in 30 s")
        return await fake_call(tool_name, arguments, timeout)
    server._client.mcp_call = mcp_call
    result = await call(server, tool, **params)
    assert result["status"] == "error" and done in result["error"] and "do not repeat" in result["error"]
    assert result["workflow_id"] == ("w2" if tool == "create_workflow" else "w1")


# ── create ────────────────────────────────────────────────────────────────

async def test_create_validates_first_and_creates_nothing_on_errors():
    server = make_server()
    server._client.answers["validate_workflow"] = McpResult(
        {"valid": True, "warnings": [{"code": "MISSING_REQUIRED_INPUT", "message": "no model"}]}, "", False)
    result = await call(server, "create_workflow", code="x", name="flow")
    assert result["status"] == "error" and result["errors"][0]["code"] == "MISSING_REQUIRED_INPUT"
    assert "create_workflow_from_code" not in server._client.tools_called()


async def test_create_refuses_a_blocked_type_before_n8n_sees_it():
    server = make_server()
    result = await call(server, "create_workflow", code="node({ type: 'n8n-nodes-base.ssh' })", name="flow")
    assert result["status"] == "error"
    assert "create_workflow_from_code" not in server._client.tools_called()


async def test_create_tags_the_new_workflow_and_checks_what_was_stored():
    server = make_server()
    result = await call(server, "create_workflow", code="x", name="flow")
    assert result["status"] == "success" and result["workflow_id"] == "w2"
    assert TAG in {t["name"] for t in server._client.workflows["w2"]["tags"]}
    assert result["editor_url"] == "http://n8n.test/workflow/w2"
    called = server._client.tools_called()
    assert called.index("create_workflow_from_code") < called.index("update_workflow") < \
        called.index("get_node_types"), "tag first, then check the stored workflow"


async def test_a_workflow_that_cannot_be_tagged_is_reported_with_its_id():
    server = make_server()
    server._client.answers["update_workflow"] = McpResult({"error": "Invalid operations"}, "", False)
    result = await call(server, "create_workflow", code="x", name="flow")
    assert result["status"] == "error" and "w2" in result["error"] and "not tagged" in result["error"]


# ── test runs ─────────────────────────────────────────────────────────────

async def test_a_test_sends_the_pin_plan_and_reports_per_node():
    server = make_server()
    result = await call(server, "test_workflow", workflow_id="w1", trigger_input=[{"x": 1}])
    sent = next(args for tool, args in server._client.calls if tool == "test_workflow")
    assert sent["pinData"] == {"Start": [{"json": {"x": 1}}]}, "only the trigger; Set is local"
    assert result["tested"] is True
    assert {n["name"]: n["run"] for n in result["nodes"]["content"]} == {"Start": "pinned", "Set": "live"}


async def test_a_sub_workflow_named_live_is_pinned_all_the_same():
    """Its own nodes would run outside the pin plan (executions 135/136)."""
    server = make_server(live_node_types=["n8n-nodes-base.executeWorkflow"])
    server._client.workflows["w9"] = workflow("w9")
    server._client.workflows["w1"]["nodes"].append(
        {"name": "Sub", "type": "n8n-nodes-base.executeWorkflow", "typeVersion": 1.3,
         "parameters": {"source": "database", "workflowId": {"__rl": True, "value": "w9", "mode": "id"}}})
    result = await call(server, "test_workflow", workflow_id="w1", live_nodes=["Sub"])
    sent = next(args for tool, args in server._client.calls if tool == "test_workflow")
    assert "Sub" in sent["pinData"]
    sub = next(n for n in result["nodes"]["content"] if n["name"] == "Sub")
    assert "never runs live" in sub["reason"]


async def test_the_summary_counts_every_output_and_names_nodes_not_reached():
    """Measured (execution 129): an IF's item on the false branch sits in output 1."""
    server = make_server()
    server._client.workflows["w1"]["nodes"] += [
        {"name": "Check", "type": "n8n-nodes-base.if", "typeVersion": 2, "parameters": {}},
        {"name": "Later", "type": "n8n-nodes-base.noOp", "typeVersion": 1, "parameters": {}}]
    server._client.executions["e1"]["data"]["resultData"]["runData"]["Check"] = [
        {"executionStatus": "success", "data": {"main": [[], [{"json": {"n": 1}}]]}}]
    result = await call(server, "test_workflow", workflow_id="w1")
    nodes = {n["name"]: n for n in result["nodes"]["content"]}
    assert nodes["Check"]["items_out"] == 1 and nodes["Check"]["items_per_output"] == [0, 1]
    assert nodes["Check"]["sample"] == '{"n": 1}'
    assert nodes["Later"]["run"] == "not_reached"


async def test_a_subnode_is_live_only_when_it_ran_and_the_trigger_is_named():
    """A subnode that never ran must not look tested; n8n must start from the
    trigger the plan fed."""
    server = make_server()
    wf = server._client.workflows["w1"]
    wf["nodes"] += [{"name": "Agent", "type": "@n8n/n8n-nodes-langchain.agent", "typeVersion": 2, "parameters": {}},
                    {"name": "Model", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi", "typeVersion": 1,
                     "parameters": {}},
                    {"name": "Memory", "type": "@n8n/n8n-nodes-langchain.memoryBufferWindow", "typeVersion": 1,
                     "parameters": {}}]
    wf["connections"]["Model"] = {"ai_languageModel": [[{"node": "Agent", "type": "ai_languageModel", "index": 0}]]}
    wf["connections"]["Memory"] = {"ai_memory": [[{"node": "Agent", "type": "ai_memory", "index": 0}]]}
    server._client.executions["e1"]["data"]["resultData"]["runData"]["Model"] = [
        {"executionStatus": "success", "data": {"ai_languageModel": [[{"json": {"text": "hi"}}]]}}]
    result = await call(server, "test_workflow", workflow_id="w1")
    nodes = {n["name"]: n["run"] for n in result["nodes"]["content"]}
    assert nodes["Model"] == "live" and nodes["Memory"] == "not_reached"
    sent = next(args for tool, args in server._client.calls if tool == "test_workflow")
    assert sent["triggerNodeName"] == "Start"


@pytest.mark.parametrize("tool,params,needle", [
    ("test_workflow", {"workflow_id": "w1", "timeout_s": "90s"}, "timeout_s"),
    ("list_workflows", {"limit": "many"}, "limit"),
    ("list_executions", {"limit": [3]}, "limit"),
    ("list_workflows", {"limit": float("inf")}, "limit"),
    ("test_workflow", {"workflow_id": "w1", "live_nodes": [{"name": "Set"}]}, "mocks is"),
    ("explore_node_resources", {"node_type": "n8n-nodes-base.slack", "version": "nan", "method_name": "m",
                                "method_type": "listSearch", "credential_type": "slackApi",
                                "credential_id": "c1"}, "version"),
    ("explore_node_resources", {"node_type": "n8n-nodes-base.slack", "version": 10 ** 400, "method_name": "m",
                                "method_type": "listSearch", "credential_type": "slackApi",
                                "credential_id": "c1"}, "version"),
    ("explore_node_resources", {"node_type": "n8n-nodes-base.slack", "version": "two", "method_name": "m",
                                "method_type": "listSearch", "credential_type": "slackApi",
                                "credential_id": "c1"}, "version"),
])
async def test_an_argument_that_is_no_number_is_named(tool, params, needle):
    """The framework does not check arguments against the schema."""
    server = make_server()
    result = await call(server, tool, **params)
    assert result["status"] == "error" and result["error"].startswith(needle)


async def test_blocking_findings_stop_the_test_before_n8n_runs_anything():
    server = make_server()
    server._client.workflows["w1"]["nodes"].append(
        {"name": "Shell", "type": "n8n-nodes-base.executeCommand", "typeVersion": 1, "parameters": {}})
    result = await call(server, "test_workflow", workflow_id="w1")
    assert result["status"] == "error" and result["findings"][0]["code"] == "BLOCKED_NODE"
    assert "test_workflow" not in server._client.tools_called()


async def test_a_setting_that_stores_no_execution_stops_the_test():
    server = make_server()
    server._client.workflows["w1"]["settings"]["saveManualExecutions"] = False
    result = await call(server, "test_workflow", workflow_id="w1")
    assert result["status"] == "error" and "saveManualExecutions" in result["error"]
    assert "test_workflow" not in server._client.tools_called()


async def test_an_execution_that_was_not_stored_proves_nothing():
    """M-MCP-41: success from test_workflow, 404 on the execution."""
    server = make_server()
    server._client.answers["test_workflow"] = McpResult({"executionId": "gone", "status": "success"}, "", False)
    result = await call(server, "test_workflow", workflow_id="w1")
    assert result["status"] == "error" and "not stored" in result["error"]


async def test_a_failed_run_is_a_result_not_tested():
    server = make_server()
    server._client.answers["test_workflow"] = McpResult(
        {"executionId": "e1", "status": "error", "error": "boom"}, "", False)
    result = await call(server, "test_workflow", workflow_id="w1")
    assert result["status"] == "success" and result["tested"] is False
    assert result["execution_status"] == "error"


# ── reading, and what is offered ──────────────────────────────────────────

async def test_validate_reports_ok_false_on_a_warning_that_is_an_error():
    server = make_server()
    server._client.answers["validate_workflow"] = McpResult(
        {"valid": True, "warnings": [{"code": "INVALID_PARAMETER", "message": "bad"}]}, "", False)
    result = await call(server, "validate_workflow", code="x")
    assert result["ok"] is False


async def test_node_types_name_the_versions_n8n_does_not_know():
    """M-MCP-44: the errors stand in an otherwise valid answer."""
    server = make_server()
    text = "# TypeScript Type Definitions\n...\n# Errors\n- Version '99' not found for node 'n8n-nodes-base.set'"
    server._client.answers["get_node_types"] = McpResult(text, text, False)
    result = await call(server, "get_node_types", nodes=[{"node_id": "n8n-nodes-base.set", "version": 99}])
    assert result["errors"] == ["Version '99' not found for node 'n8n-nodes-base.set'"]
    assert result["data"]["untrusted"] is True, "a community node's text is not ours"


async def test_node_config_errors_come_from_every_result():
    """The measured shape: results[] with errors[{path, message}]."""
    server = make_server()
    server._client.answers["validate_node_config"] = McpResult({"valid": False, "results": [
        {"index": 0, "name": "Slack", "type": "n8n-nodes-base.slack", "valid": False,
         "errors": [{"path": "/channelId", "message": "required"}]}]}, "", True)
    result = await call(server, "validate_node_config", nodes=[{"type": "n8n-nodes-base.slack"}])
    assert result["ok"] is False
    assert result["errors"][0]["node"] == "Slack" and result["errors"][0]["fix_hint"] == "/channelId"


async def test_get_workflow_reads_in_full_and_refuses_what_it_cannot_show_whole():
    """'execution' measured without nodes and connections; a cut workflow
    would look like a whole one."""
    server = make_server()
    server._client.answers["get_workflow_details"] = McpResult({}, "{}", False)
    result = await call(server, "get_workflow", workflow_id="w1")
    assert result["status"] == "success"
    assert server._client.calls[-1][1]["detailLevel"] == "full"
    big = "x" * 40_001
    server._client.answers["get_workflow_details"] = McpResult(big, big, False)
    result = await call(server, "get_workflow", workflow_id="w1")
    assert result["status"] == "error" and "over 40000" in result["error"]


async def test_list_workflows_asks_for_the_managed_tag_only():
    server = make_server()
    seen = {}

    async def api_get(path, params=None):
        seen.update(params or {})
        return {"data": [workflow()], "nextCursor": None}

    server._client.api_get = api_get
    result = await call(server, "list_workflows")
    assert seen["tags"] == TAG and result["count"] == 1


@pytest.mark.parametrize("config,expected", [
    (dict(base_url="", api_key="", mcp_key=""), 0),
    (dict(api_key=""), 6),
    ({}, 15),
])
async def test_tools_are_offered_only_as_far_as_n8n_is_configured(monkeypatch, config, expected):
    for var in ("N8N_BASE_URL", "N8N_API_KEY", "N8N_MCP_KEY"):
        monkeypatch.delenv(var, raising=False)
    server = make_server(**config)
    tools = await server.list_tools()
    assert len(tools) == expected


async def test_stop_plugin_closes_the_client():
    server = make_server()
    client = server._client
    await server.stop_plugin()
    assert client.closed and server._client is None
