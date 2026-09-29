"""Policy, gap checks and the pin plan -- pure functions, no network.

Each gap check is built from a case n8n measurably lets through
(docs/n8n_facts.md M-MCP-9/10/11/34, M-MCP-H8/H9); each pin-plan rule from a
way a test could act outside the execution (M-MCP-30, M-MCP-38, M-MCP-40,
M-MCP-43).
"""
import pytest

from plugins.n8n.validate import (DEFAULT_BLOCKED_NODE_TYPES, DEFAULT_REVIEW_NODE_TYPES,
                                  blocking_save_settings, check_workflow,
                                  classify_workflow_validation, code_precheck, node_type_errors,
                                  normalize_type, normalized, pin_plan)

BLOCKED = normalized(DEFAULT_BLOCKED_NODE_TYPES)
REVIEW = normalized(DEFAULT_REVIEW_NODE_TYPES)


def node(name, node_type, **parameters):
    return {"name": name, "type": node_type, "typeVersion": 1, "parameters": parameters}


def chain(*nodes, ai=()):
    """Nodes connected main-to-main in order; ai = [(subnode, root, kind)]."""
    connections = {}
    for a, b in zip(nodes, nodes[1:]):
        connections.setdefault(a["name"], {}).setdefault("main", [[]])[0].append(
            {"node": b["name"], "type": "main", "index": 0})
    for sub, root, kind in ai:
        connections.setdefault(sub["name"], {})[kind] = [[{"node": root["name"], "type": kind, "index": 0}]]
    return {"nodes": list(nodes) + [s for s, _, _ in ai], "connections": connections}


def codes(findings, level=None):
    return sorted(f["code"] for f in findings if level is None or f["level"] == level)


def check(workflow, **kw):
    return check_workflow(workflow, blocked=BLOCKED, review=REVIEW, **kw)


# ── types ─────────────────────────────────────────────────────────────────

def test_a_tool_variant_is_its_base_type():
    """M-MCP-42: gitTool must fall under a list that names git."""
    assert normalize_type("n8n-nodes-base.gitTool") == "n8n-nodes-base.git"
    assert codes(check(chain(node("G", "n8n-nodes-base.gitTool")))) == ["BLOCKED_NODE"]


def test_review_types_warn_and_blocked_types_fail():
    wf = chain(node("H", "n8n-nodes-base.httpRequest", url="https://x.org"),
               node("S", "n8n-nodes-base.ssh"))
    assert codes(check(wf), "warning") == ["REVIEW_NODE"]
    assert codes(check(wf), "error") == ["BLOCKED_NODE"]


def test_both_mcp_client_nodes_need_a_human_look():
    """They call any MCP endpoint URL (live search_nodes 21.09.: mcpClient,
    mcpClientTool); the registry variant is internal."""
    wf = chain(node("C", "@n8n/n8n-nodes-langchain.mcpClient"),
               node("CT", "@n8n/n8n-nodes-langchain.mcpClientTool"))
    assert codes(check(wf), "warning") == ["REVIEW_NODE", "REVIEW_NODE"]


# ── gap checks ────────────────────────────────────────────────────────────

def test_a_ghost_credential_and_a_wrong_type_are_caught():
    """M-MCP-10/11: n8n stores both unchecked."""
    slack = node("Post", "n8n-nodes-base.slack")
    slack["credentials"] = {"slackApi": {"id": "ghost", "name": "Ghost"}}
    wrong = node("Post2", "n8n-nodes-base.slack")
    wrong["credentials"] = {"slackApi": {"id": "c1", "name": "GitHub"}}
    found = check(chain(slack, wrong), credentials={"c1": "githubApi"})
    assert codes(found) == ["CREDENTIAL_TYPE_MISMATCH", "CREDENTIAL_UNKNOWN_ID"]


def test_a_known_credential_passes_and_none_is_not_checked():
    slack = node("Post", "n8n-nodes-base.slack")
    slack["credentials"] = {"slackApi": {"id": "c1", "name": "Bot"}}
    assert check(chain(slack), credentials={"c1": "slackApi"}) == []
    assert check(chain(slack), credentials=None) == []


def test_respond_node_mode_needs_a_respond_node_after_the_webhook():
    """M-MCP-34: green in a test, HTTP 500 in production."""
    hook = node("Hook", "n8n-nodes-base.webhook", path="p", responseMode="responseNode")
    assert codes(check(chain(hook, node("Set", "n8n-nodes-base.set")))) == ["RESPOND_NODE_MISSING"]
    respond = node("Respond", "n8n-nodes-base.respondToWebhook")
    assert check(chain(hook, node("Set", "n8n-nodes-base.set"), respond)) == []


def test_an_empty_webhook_path_is_caught():
    assert codes(check(chain(node("Hook", "n8n-nodes-base.webhook", path="  ")))) == ["WEBHOOK_PATH_EMPTY"]


@pytest.mark.parametrize("path,unsafe", [
    ("../victim", True), ("a/../b", True), (".", True), ("victim?x=1", True), ("victim#x", True),
    ("a%2e%2e", True), ("a\\b", True), ("a b", True), ("a//b", True), ("orders ", True), (" orders", True),
    ("orders", False), ("orders/new", False), ("/orders/", False), ("orders/:id", False), ("v1.2", False),
])
def test_a_webhook_path_that_would_reach_another_url_is_caught(path, unsafe):
    """The path becomes part of the production URL the trigger calls."""
    found = codes(check(chain(node("Hook", "n8n-nodes-base.webhook", path=path))))
    assert found == (["WEBHOOK_PATH_UNSAFE"] if unsafe else [])


def test_an_open_expression_is_caught_but_a_closed_one_passes():
    """M-MCP-11: '={{ $json.a' evaluates to null silently."""
    assert codes(check(chain(node("S", "n8n-nodes-base.set", value="={{ $json.a")))) == ["EXPRESSION_UNBALANCED"]
    assert check(chain(node("S", "n8n-nodes-base.set", value="={{ $json.a }}"))) == []
    assert check(chain(node("S", "n8n-nodes-base.set", value="{{ not an expression"))) == []


def test_a_sub_workflow_must_come_from_the_database_with_a_fixed_id():
    """M-MCP-40: inline JSON runs nodes no type check sees."""
    inline = node("Sub", "n8n-nodes-base.executeWorkflow", source="parameter", workflowJson="{}")
    dynamic = node("Sub2", "n8n-nodes-base.executeWorkflow", workflowId="={{ $json.id }}")
    fixed = node("Sub3", "n8n-nodes-base.executeWorkflow", workflowId={"__rl": True, "value": "w9"})
    found = check(chain(inline, dynamic, fixed))
    assert [f["node"] for f in found if f["code"] == "EXECUTE_WORKFLOW_SOURCE"] == ["Sub", "Sub2"]


def test_version_errors_are_attributed_to_their_node():
    hook = node("Hook", "n8n-nodes-base.webhook", path="p")
    hook["typeVersion"] = 99
    errors = {("n8n-nodes-base.webhook", "99"): "Version '99' not found for node 'n8n-nodes-base.webhook'"}
    assert codes(check(chain(hook), type_errors=errors)) == ["UNKNOWN_TYPE_VERSION"]


def test_errors_section_is_found_anywhere_in_the_text():
    """M-MCP-44: behind a valid definition, not at the start."""
    pairs = [("n8n-nodes-base.set", "3.4"), ("n8n-nodes-base.webhook", "99")]
    alone = "# Errors\n- Version '99' not found for node 'n8n-nodes-base.webhook'"
    mixed = "# TypeScript Type Definitions\n" + "x" * 6000 + "\n" + alone
    for text in (alone, mixed):
        assert set(node_type_errors(text, pairs)) == {("n8n-nodes-base.webhook", "99")}
    assert node_type_errors("# TypeScript Type Definitions\nfine", pairs) == {}


def test_valid_true_with_a_real_warning_is_not_ok():
    """M-MCP-8: an AI agent without a model is 'valid'."""
    errors, warnings = classify_workflow_validation({"valid": True, "warnings": [
        {"code": "MISSING_REQUIRED_INPUT", "message": "no model", "nodeName": "Agent"},
        {"code": "AGENT_STATIC_PROMPT", "message": "static", "nodeName": "Agent"}]})
    assert codes(errors) == ["MISSING_REQUIRED_INPUT"] and codes(warnings) == ["AGENT_STATIC_PROMPT"]
    assert [f["level"] for f in errors] == ["error"] and [f["level"] for f in warnings] == ["warning"], \
        "a finding's list and its level must agree"


def test_code_precheck_names_blocked_types_and_inline_sources():
    code = "node({ type: 'n8n-nodes-base.ssh' }); node({ type: \"n8n-nodes-base.set\", source: 'parameter' })"
    assert codes(code_precheck(code, BLOCKED)) == ["BLOCKED_NODE", "EXECUTE_WORKFLOW_SOURCE"]


def test_only_a_setting_that_leaves_no_stored_test_run_blocks():
    """M-MCP-41: saveManualExecutions false -> the execution is a 404 (140);
    saveData*Execution 'none' still stores a test run (137, 139)."""
    assert blocking_save_settings({"settings": {"saveManualExecutions": False}}) == ["saveManualExecutions"]
    assert blocking_save_settings({"settings": {"saveDataSuccessExecution": "none",
                                                "saveDataErrorExecution": "none"}}) == []


# ── the pin plan ──────────────────────────────────────────────────────────

def runs(plan):
    return {name: r["run"] for name, r in plan.runs.items()}


def test_trigger_pinned_to_its_input_local_nodes_live():
    wf = chain(node("Hook", "n8n-nodes-base.webhook", path="p"), node("Set", "n8n-nodes-base.set"),
               node("Respond", "n8n-nodes-base.respondToWebhook"))
    plan = pin_plan(wf, trigger_input=[{"body": {"name": "Ada"}}])
    assert runs(plan) == {"Hook": "pinned", "Set": "live", "Respond": "live"}
    assert plan.pin_data == {"Hook": [{"json": {"body": {"name": "Ada"}}}]}


def test_everything_else_is_pinned_by_default_and_warned_without_a_mock():
    """M-MCP-30: the server pins nothing itself."""
    wf = chain(node("Start", "n8n-nodes-base.manualTrigger"),
               node("Http", "n8n-nodes-base.httpRequest", url="https://api.example.org/x"),
               node("Slack", "n8n-nodes-base.slack"))
    plan = pin_plan(wf, mocks={"Http": {"ok": 1}})
    assert runs(plan) == {"Start": "pinned", "Http": "pinned", "Slack": "pinned"}
    assert plan.pin_data["Http"] == [{"json": {"ok": 1}}]
    assert plan.pin_data["Slack"] == [{"json": {}}]
    assert any("'Slack'" in w for w in plan.warnings)


def test_http_runs_live_only_with_a_fixed_url_on_an_allowed_host():
    hosts = frozenset({"api.example.org"})
    ok = node("Ok", "n8n-nodes-base.httpRequest", url="https://api.example.org/x")
    expr = node("Expr", "n8n-nodes-base.httpRequest", url="={{ $json.url }}")
    other = node("Other", "n8n-nodes-base.httpRequest", url="https://evil.test/x")
    tool = node("AsTool", "n8n-nodes-base.httpRequestTool", url="https://evil.test/x")
    plan = pin_plan(chain(node("T", "n8n-nodes-base.manualTrigger"), ok, expr, other, tool),
                    live_nodes=["Ok", "Expr", "Other", "AsTool"], allowed_hosts=hosts)
    assert runs(plan) == {"T": "pinned", "Ok": "live", "Expr": "pinned", "Other": "pinned", "AsTool": "pinned"}


def test_a_url_that_can_lead_elsewhere_is_never_live():
    """Python reads allowed.example.org in "http://evil.test\\@allowed.example.org",
    n8n's WHATWG parser reads evil.test. User info and any backslash are refused
    outright ("Port" has no parser disagreement; it holds the backslash rule on
    its own). A proxy, pagination or a credential sent on redirect leave the
    host too; a credential needs the operator's type approval."""
    hosts = frozenset({"allowed.example.org"})
    url = "https://allowed.example.org/x"
    nodes = [node("Slash", "n8n-nodes-base.httpRequest", url="http://evil.test\\@allowed.example.org/x"),
             node("User", "n8n-nodes-base.httpRequest", url="http://me@allowed.example.org/x"),
             node("Port", "n8n-nodes-base.httpRequest", url="http://allowed.example.org:80\\x"),
             node("Proxy", "n8n-nodes-base.httpRequest", url=url, options={"proxy": "http://evil.test:3128"}),
             node("Pages", "n8n-nodes-base.httpRequest", url=url,
                  options={"pagination": {"pagination": {"nextURL": "https://evil.test/"}}}),
             node("Redirect", "n8n-nodes-base.httpRequest", url=url,
                  options={"sendCredentialsOnCrossOriginRedirect": True}),
             node("Cred", "n8n-nodes-base.httpRequest", url=url),
             node("Query", "n8n-nodes-base.httpRequest", url="https://allowed.example.org/s?q=München a"),
             node("Plain", "n8n-nodes-base.httpRequest", url=url, options={"timeout": 5000})]
    nodes[-3]["credentials"] = {"googleOAuth2Api": {"id": "c7", "name": "Google"}}
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"), *nodes)
    plan = pin_plan(wf, live_nodes=[n["name"] for n in nodes], allowed_hosts=hosts)
    assert runs(plan) == {"T": "pinned", "Slash": "pinned", "User": "pinned", "Port": "pinned",
                          "Proxy": "pinned", "Pages": "pinned", "Redirect": "pinned", "Cred": "pinned",
                          "Query": "live", "Plain": "live"}
    approved = pin_plan(wf, live_nodes=["Cred"], allowed_hosts=hosts,
                        live_node_types=["n8n-nodes-base.httpRequest"])
    assert runs(approved)["Cred"] == "live"


def test_allowed_hosts_compare_as_hostnames():
    """Upper case, a trailing dot or IPv6 brackets in an entry must not make it dead."""
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"),
               node("A", "n8n-nodes-base.httpRequest", url="https://api.example.org./x"),
               node("B", "n8n-nodes-base.httpRequest", url="http://[::1]:5678/healthz"))
    plan = pin_plan(wf, live_nodes=["A", "B"], allowed_hosts=["API.Example.org.", "[::1]"])
    assert runs(plan) == {"T": "pinned", "A": "live", "B": "live"}


def test_code_never_runs_live_not_even_when_the_operator_lists_it():
    """M-MCP-38: Code reaches the network through this.helpers.httpRequest."""
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"), node("Code", "n8n-nodes-base.code"))
    plan = pin_plan(wf, live_nodes=["Code"], live_node_types=["n8n-nodes-base.code"])
    assert runs(plan)["Code"] == "pinned"


def test_an_operator_approved_type_runs_live_only_when_named():
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"), node("Slack", "n8n-nodes-base.slack"))
    assert runs(pin_plan(wf, live_nodes=["Slack"]))["Slack"] == "pinned"
    assert runs(pin_plan(wf, live_node_types=["n8n-nodes-base.slack"]))["Slack"] == "pinned"
    assert runs(pin_plan(wf, live_nodes=["Slack"], live_node_types=["n8n-nodes-base.slack"]))["Slack"] == "live"


def test_a_sub_workflow_never_runs_live():
    """Its own nodes would run outside the pin plan (executions 135/136);
    pinned, it does not start at all (138)."""
    sub = node("Sub", "n8n-nodes-base.executeWorkflow", workflowId="w9")
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"), sub)
    plan = pin_plan(wf, live_nodes=["Sub"], live_node_types=["n8n-nodes-base.executeWorkflow"])
    assert runs(plan)["Sub"] == "pinned" and "Sub" in plan.pin_data


def test_sort_with_code_and_merge_with_sql_are_not_local():
    """M-MCP-43: a Sort with type 'code' ran its own JavaScript live."""
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"),
               node("SortCode", "n8n-nodes-base.sort", type="code"),
               node("SortSimple", "n8n-nodes-base.sort", type="simple"),
               node("Sql", "n8n-nodes-base.merge", mode="combineBySql"))
    assert runs(pin_plan(wf)) == {"T": "pinned", "SortCode": "pinned", "SortSimple": "live", "Sql": "pinned"}


def test_an_ai_root_is_pinned_unless_every_subnode_may_run():
    agent = node("Agent", "@n8n/n8n-nodes-langchain.agent")
    model = node("Model", "@n8n/n8n-nodes-langchain.lmChatOpenAi")
    wf = chain(node("T", "@n8n/n8n-nodes-langchain.chatTrigger"), agent,
               ai=[(model, agent, "ai_languageModel")])
    plan = pin_plan(wf)
    assert runs(plan) == {"T": "pinned", "Agent": "pinned", "Model": "sub"}
    assert "Model" not in plan.pin_data, "a subnode is never pinned on its own"
    types = ["@n8n/n8n-nodes-langchain.agent", "@n8n/n8n-nodes-langchain.lmChatOpenAi"]
    assert runs(pin_plan(wf, live_nodes=["Model"], live_node_types=types))["Agent"] == "pinned", \
        "the root itself must be allowed too"
    assert runs(pin_plan(wf, live_nodes=["Agent"], live_node_types=types))["Agent"] == "pinned", \
        "and so must every subnode"
    assert runs(pin_plan(wf, live_nodes=["Agent", "Model"], live_node_types=types))["Agent"] == "live"


def test_a_stray_ai_edge_does_not_make_a_node_live():
    """Measured (execution 131): a disabled tool wired as ai_tool into an HTTP
    node made it run live, with no live_nodes and no allowed host."""
    fake = node("Fake", "@n8n/n8n-nodes-langchain.toolCalculator")
    fake["disabled"] = True
    http = node("Http", "n8n-nodes-base.httpRequest", url="http://localhost:5678/healthz")
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"), http, ai=[(fake, http, "ai_tool")])
    plan = pin_plan(wf)
    assert runs(plan)["Http"] == "pinned" and "Http" in plan.pin_data


def test_a_root_is_checked_with_every_node_below_it():
    """A vector store inserts on its own; a tool's own tools count too."""
    types = ["@n8n/n8n-nodes-langchain.embeddingsOpenAi", "@n8n/n8n-nodes-langchain.agent",
             "@n8n/n8n-nodes-langchain.lmChatOpenAi", "@n8n/n8n-nodes-langchain.agentTool"]
    store = node("Store", "@n8n/n8n-nodes-langchain.vectorStorePinecone", mode="insert")
    emb = node("Emb", "@n8n/n8n-nodes-langchain.embeddingsOpenAi")
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"), store, ai=[(emb, store, "ai_embedding")])
    assert runs(pin_plan(wf, live_nodes=["Emb"], live_node_types=types))["Store"] == "pinned"

    agent = node("Agent", "@n8n/n8n-nodes-langchain.agent")
    model = node("Model", "@n8n/n8n-nodes-langchain.lmChatOpenAi")
    inner = node("Inner", "@n8n/n8n-nodes-langchain.agentTool")
    evil = node("Evil", "n8n-nodes-base.httpRequestTool", url="https://evil.test/x")
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"), agent,
               ai=[(model, agent, "ai_languageModel"), (inner, agent, "ai_tool"), (evil, inner, "ai_tool")])
    plan = pin_plan(wf, live_nodes=["Agent", "Model", "Inner"], live_node_types=types)
    assert runs(plan)["Agent"] == "pinned" and "'Evil'" in plan.runs["Agent"]["reason"]


def test_a_trigger_is_pinned_even_with_an_ai_edge():
    """A trigger wired as a subnode must still be pinned: unpinned, a polling
    trigger reads the real mailbox."""
    mail = node("Mail", "n8n-nodes-base.gmailTrigger")
    agent = node("Agent", "@n8n/n8n-nodes-langchain.agent")
    wf = chain(agent, ai=[(mail, agent, "ai_tool")])
    plan = pin_plan(wf, trigger_node="Mail", trigger_input=[{"id": 1}])
    assert runs(plan)["Mail"] == "pinned" and plan.pin_data["Mail"] == [{"json": {"id": 1}}]
    assert plan.trigger == "Mail" and not plan.errors
    mocked = pin_plan(wf, mocks={"Mail": {"id": 2}})
    assert mocked.pin_data["Mail"] == [{"json": {"id": 2}}]
    assert not any("ignored" in w for w in mocked.warnings), "a trigger's mock is used, not ignored"


def test_an_imap_read_is_a_trigger():
    """n8n counts emailReadImap as a trigger though its name does not say so."""
    wf = chain(node("Mail", "n8n-nodes-base.emailReadImap"), node("Set", "n8n-nodes-base.set"))
    plan = pin_plan(wf, trigger_input=[{"subject": "hi"}])
    assert runs(plan)["Mail"] == "pinned" and plan.trigger == "Mail"
    assert plan.pin_data["Mail"] == [{"json": {"subject": "hi"}}]


def test_the_plan_names_its_trigger_and_warns_about_a_mock_on_a_subnode():
    agent = node("Agent", "@n8n/n8n-nodes-langchain.agent")
    model = node("Model", "@n8n/n8n-nodes-langchain.lmChatOpenAi")
    wf = chain(node("A", "n8n-nodes-base.manualTrigger"), agent, ai=[(model, agent, "ai_languageModel")])
    wf["nodes"].append(node("B", "n8n-nodes-base.scheduleTrigger"))
    plan = pin_plan(wf, mocks={"Model": {"text": "hi"}})
    assert plan.trigger == "A"
    assert any("'Model'" in w and "ignored" in w for w in plan.warnings)
    assert pin_plan(wf, trigger_node="B").trigger == "B"


def test_a_subnode_on_the_main_path_is_planned_like_any_node():
    tool = node("Tool", "n8n-nodes-base.httpRequestTool", url="https://evil.test/x")
    agent = node("Agent", "@n8n/n8n-nodes-langchain.agent")
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"), tool, agent, ai=[(tool, agent, "ai_tool")])
    plan = pin_plan(wf)
    assert runs(plan)["Tool"] == "pinned" and "Tool" in plan.pin_data


def test_unknown_names_and_triggers_are_reported():
    wf = chain(node("T", "n8n-nodes-base.manualTrigger"))
    plan = pin_plan(wf, mocks={"Nope": []}, trigger_node="Missing")
    assert any("'Nope'" in w for w in plan.warnings)
    assert plan.errors and "Missing" in plan.errors[0]
