"""The client against a fake n8n: sessions, answer formats, limits, errors.

Every shape here was measured on n8n 2.39.9 (docs/n8n_facts.md): JSON and SSE
answers, a stateless MCP that sends no session header, a 429 with
retry-after, the text of a disabled MCP, and the tool answers that report
failure without isError -- or report isError on what is really a result.
"""
import json

import httpx
import pytest

from plugins.n8n import client as client_module
from plugins.n8n.client import (McpResult, N8nClient, N8nError, N8nNoAnswer, N8nNotFound, N8nUnavailable, mcp_error,
                               to_result)

API_KEY, MCP_KEY = "api-secret-123", "mcp-secret-456"


class FakeN8n:
    """Speaks just enough of the instance MCP and the public API."""

    def __init__(self, *, sse=False, session=None):
        self.sse = sse
        self.initializes = 0
        self.calls = []
        self.next_status = []          # queued (status, headers, body) for tools/call
        self.next_init_status = []     # the same, for initialize
        self.session = session         # None: stateless, as n8n 2.39.9 is

    def reply(self, message):
        if self.sse:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=f"event: message\ndata: {json.dumps(message)}\n\n")
        return httpx.Response(200, json=message)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1"):
            self.calls.append(("api", request.url.path, request.headers.get("x-n8n-api-key")))
            if request.url.path == "/api/v1/missing":
                return httpx.Response(404, json={"message": "Not Found"})
            if request.url.path == "/api/v1/broken":
                return httpx.Response(500, text="<html>boom</html>")
            if request.url.path == "/api/v1/denied":
                return httpx.Response(401, json={"message": "Unauthorized"})
            if request.url.path == "/api/v1/forbidden":
                return httpx.Response(403, json={"message": "Forbidden"})
            if request.url.path == "/api/v1/login-page":
                return httpx.Response(200, text="<html>sign in</html>")
            if request.url.path == "/api/v1/list-error":
                return httpx.Response(400, json=["odd"])
            return httpx.Response(200, json={"data": [], "nextCursor": None, "query": dict(request.url.params)})
        body = json.loads(request.content)
        self.calls.append((body.get("method"), request.headers.get("mcp-session-id"),
                           request.headers.get("authorization")))
        if body.get("method") == "initialize":
            if self.next_init_status:
                status, headers, content = self.next_init_status.pop(0)
                return httpx.Response(status, headers=headers, text=content)
            self.initializes += 1
            response = self.reply({"jsonrpc": "2.0", "id": body["id"],
                                   "result": {"serverInfo": {"name": "n8n MCP Server", "version": "1.1.0"}}})
            if self.session:
                response.headers["mcp-session-id"] = self.session
            return response
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if self.next_status:
            status, headers, content = self.next_status.pop(0)
            return httpx.Response(status, headers=headers, text=content)
        if body["params"]["name"] == "rpc_error":
            return self.reply({"jsonrpc": "2.0", "id": body["id"],
                               "error": {"code": -32602, "message": "Tool rpc_error not found"}})
        return self.reply({"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps({"echo": body["params"]["name"]})}],
            "structuredContent": {"echo": body["params"]["name"]}}})


def make(fake):
    return N8nClient("http://n8n.test", API_KEY, MCP_KEY, transport=httpx.MockTransport(fake))


@pytest.mark.parametrize("sse", [False, True])
async def test_json_and_sse_answers_read_the_same(sse):
    fake = FakeN8n(sse=sse)
    client = make(fake)
    result = await client.mcp_call("search_nodes", {"queries": ["x"]})
    assert result.payload == {"echo": "search_nodes"}
    assert client.server_info["name"] == "n8n MCP Server"
    await client.aclose()


@pytest.mark.parametrize("session", [None, "sess-1"])
async def test_one_handshake_serves_many_calls(session):
    """A handshake per call costs three of the hundred requests per five
    minutes instead of one (M-MCP-24). n8n 2.39.9 sends no session id at all,
    so the handshake cannot hang on one (measured 21.09.)."""
    fake = FakeN8n(session=session)
    client = make(fake)
    for _ in range(4):
        await client.mcp_call("search_nodes", {})
    assert fake.initializes == 1
    tool_calls = [c for c in fake.calls if c[0] == "tools/call"]
    assert len(tool_calls) == 4 and all(c[1] == session for c in tool_calls)
    assert all(c[2] == f"Bearer {MCP_KEY}" for c in tool_calls)
    await client.aclose()


async def test_a_stale_session_does_not_drop_its_replacement():
    """Two calls that both carried the old id: the second must not throw away
    the session the first one has just rebuilt."""
    fake = FakeN8n(session="sess-1")
    client = make(fake)
    await client.mcp_call("search_nodes", {})
    fake.session = "sess-2"
    await client._forget_session("sess-1")
    await client.mcp_call("search_nodes", {})
    await client._forget_session("sess-1")
    await client.mcp_call("search_nodes", {})
    assert fake.initializes == 2 and fake.calls[-1][1] == "sess-2"
    await client.aclose()


async def test_a_forgotten_session_is_rebuilt_once():
    fake = FakeN8n(session="sess-1")
    client = make(fake)
    await client.mcp_call("search_nodes", {})
    fake.session = "sess-2"
    fake.next_status.append((404, {}, '{"message":"Session not found"}'))
    result = await client.mcp_call("search_nodes", {})
    assert result.payload == {"echo": "search_nodes"}
    assert fake.initializes == 2
    await client.aclose()


async def test_a_short_rate_limit_is_waited_out(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    fake = FakeN8n()
    fake.next_status.append((429, {"retry-after": "3"}, '{"message":"Too many requests"}'))
    client = make(fake)
    result = await client.mcp_call("search_nodes", {})
    assert result.payload == {"echo": "search_nodes"} and slept == [3.0]
    await client.aclose()


async def test_a_long_rate_limit_goes_back_to_the_model(monkeypatch):
    async def no_sleep(seconds):
        raise AssertionError("a long wait must not be slept")

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    fake = FakeN8n()
    fake.next_status.append((429, {"retry-after": "102"}, '{"message":"Too many requests"}'))
    client = make(fake)
    with pytest.raises(N8nError, match="retry in 102 s"):
        await client.mcp_call("search_nodes", {})
    await client.aclose()


async def test_a_rate_limit_on_the_handshake_says_when_to_retry(monkeypatch):
    """The handshake is the first request a call makes, so it meets the limit first."""
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    fake = FakeN8n()
    fake.next_init_status.append((429, {"retry-after": "4"}, '{"message":"Too many requests"}'))
    client = make(fake)
    assert (await client.mcp_call("search_nodes", {})).payload == {"echo": "search_nodes"}
    assert slept == [4.0]
    await client.aclose()
    fake = FakeN8n()
    fake.next_init_status.append((429, {"retry-after": "26"}, '{"message":"Too many requests"}'))
    client = make(fake)
    with pytest.raises(N8nError, match="retry in 26 s"):
        await client.mcp_call("search_nodes", {})
    await client.aclose()


async def test_a_refused_key_names_the_variable_not_the_key():
    fake = FakeN8n()
    client = make(fake)
    await client.mcp_call("search_nodes", {})       # opens the session first
    fake.next_status.append((401, {}, '{"message":"Unauthorized"}'))
    with pytest.raises(N8nError) as info:
        await client.mcp_call("search_nodes", {})
    assert "N8N_MCP_KEY" in str(info.value) and MCP_KEY not in str(info.value)
    with pytest.raises(N8nError) as info:
        await client.api_get("/denied")
    assert "N8N_API_KEY" in str(info.value) and API_KEY not in str(info.value)
    await client.aclose()


async def test_a_disabled_mcp_says_how_to_switch_it_on():
    fake = FakeN8n(session="sess-1")
    client = make(fake)
    await client.mcp_call("search_nodes", {})
    fake.next_status.append((404, {}, '{"message":"MCP access is disabled"}'))
    with pytest.raises(N8nError, match="N8N_MCP_ACCESS_ENABLED"):
        await client.mcp_call("search_nodes", {})
    assert fake.initializes == 1, "a disabled MCP is not a lost session"
    await client.aclose()


async def test_public_api_answers_and_failures():
    fake = FakeN8n()
    client = make(fake)
    assert (await client.api_get("/workflows"))["data"] == []
    assert fake.calls[-1][2] == API_KEY
    with pytest.raises(N8nNotFound):
        await client.api_get("/missing")
    with pytest.raises(N8nError, match="no JSON"):
        await client.api_get("/broken")
    with pytest.raises(N8nError, match="lacks the scope"):
        await client.api_get("/forbidden")
    with pytest.raises(N8nError, match="HTTP 400"):
        await client.api_get("/list-error")
    answer = await client.api_get("/workflows", {"name": None, "limit": 5})
    assert answer["query"] == {"limit": "5"}, "an unset filter must not reach n8n as 'None'"
    await client.aclose()


async def test_only_n8ns_own_404_means_not_found():
    """A proxy answers a plain 404 while n8n restarts (M-MCP-70: n8n's is JSON);
    a watch must wait on that, not give the run up as gone."""
    def proxy(request):
        return httpx.Response(404, text="404 page not found")

    client = N8nClient("http://n8n.test", API_KEY, MCP_KEY, transport=httpx.MockTransport(proxy))
    with pytest.raises(N8nUnavailable):
        await client.api_get("/executions/7")
    await client.aclose()


async def test_an_answer_that_is_not_json_names_the_likely_cause():
    """A proxy or login page in front of n8n answers 200 with HTML."""
    fake = FakeN8n()
    client = make(fake)
    with pytest.raises(N8nError, match="not JSON"):
        await client.api_get("/login-page")
    fake.next_status.append((200, {"content-type": "text/html"}, "<html>sign in</html>"))
    with pytest.raises(N8nError, match="not JSON"):
        await client.mcp_call("search_nodes", {})
    await client.aclose()


async def test_an_argument_json_cannot_carry_is_named():
    client = make(FakeN8n())
    with pytest.raises(N8nError, match="NaN or Infinity"):
        await client.mcp_call("search_nodes", {"x": float("nan")})
    await client.aclose()


async def test_a_json_rpc_answer_that_is_no_object_is_refused():
    fake = FakeN8n()
    client = make(fake)
    await client.mcp_call("search_nodes", {})
    fake.next_status.append((200, {"content-type": "application/json"}, "[]"))
    with pytest.raises(N8nError, match="not JSON"):
        await client.mcp_call("search_nodes", {})
    await client.aclose()


async def test_odd_fields_in_an_answer_do_not_crash_the_client():
    """n8n is not the plugin: a field of the wrong type is an N8nError or is
    skipped, never an AttributeError the model cannot read."""
    fake = FakeN8n()
    fake.next_init_status.append((200, {}, '{"jsonrpc": "2.0", "id": 1, "result": "odd"}'))
    client = make(fake)
    fake.next_status += [(200, {}, '{"jsonrpc": "2.0", "id": 2, "error": "boom"}'),
                         (200, {}, '{"jsonrpc": "2.0", "id": 3, "result": "text"}'),
                         (200, {}, '{"jsonrpc": "2.0", "id": 4, "result": {"content": ["x", {"type": "text"}]}}')]
    with pytest.raises(N8nError, match="boom"):
        await client.mcp_call("search_nodes", {})
    assert client.server_info == {}
    with pytest.raises(N8nError, match="not JSON"):
        await client.mcp_call("search_nodes", {})
    assert (await client.mcp_call("search_nodes", {})).text == ""
    await client.aclose()


async def test_a_json_rpc_error_is_an_n8n_error():
    client = make(FakeN8n())
    with pytest.raises(N8nError, match="rpc_error: Tool rpc_error not found"):
        await client.mcp_call("rpc_error", {})
    await client.aclose()


async def test_an_unreachable_n8n_points_at_healthz():
    def down(request):
        raise httpx.ConnectError("refused")

    client = N8nClient("http://n8n.test", API_KEY, MCP_KEY, transport=httpx.MockTransport(down))
    with pytest.raises(N8nError, match="healthz"):
        await client.api_get("/workflows")
    await client.aclose()


# ── reading a tool's answer, per tool ─────────────────────────────────────

def result(payload=None, text="", is_error=False):
    return McpResult(payload=payload if payload is not None else {}, text=text, is_error=is_error)


def test_iserror_is_an_error():
    assert mcp_error("search_nodes", result({"error": "bad"}, is_error=True)) == "bad"


def test_a_failed_test_run_is_a_result_not_an_error():
    """M-MCP-6: status error WITH an execution id is the test's outcome."""
    assert mcp_error("test_workflow", result({"executionId": "7", "status": "error", "error": "boom"})) is None


def test_a_test_that_did_not_start_is_an_error():
    assert mcp_error("test_workflow", result({"success": False, "error": "not available in MCP"}))


def test_an_update_error_without_isError_is_an_error():
    """M-MCP-31: {"error": "Invalid operations ..."} arrives without isError."""
    assert "Invalid operations" in mcp_error("update_workflow", result({"error": "Invalid operations: x"}))
    assert mcp_error("update_workflow", result({"workflowId": "w1", "error": "partial"})) is None


def test_node_types_with_an_errors_section_is_not_an_error():
    """M-MCP-44: # Errors can stand behind valid definitions, without isError."""
    text = "# TypeScript Type Definitions\n...\n# Errors\n- Version '99' not found for node 'x'"
    assert mcp_error("get_node_types", result(text, text=text)) is None


def test_unparseable_code_is_a_verdict_not_a_broken_tool():
    """Measured 21.09.: code n8n cannot parse comes back with isError AND
    {"valid": false, "errors": [...]} -- the builder must see the finding."""
    assert mcp_error("validate_workflow", result({"valid": False, "errors": ["Unrecognized node type"]},
                                                 is_error=True)) is None


def test_to_result_prefers_structured_content_then_json_text():
    raw = {"content": [{"type": "text", "text": '{"a": 1}'}]}
    assert to_result(raw).payload == {"a": 1}
    raw = {"content": [{"type": "text", "text": "plain"}], "structuredContent": {"b": 2}}
    assert to_result(raw).payload == {"b": 2}
    assert to_result({"content": [{"type": "text", "text": "plain"}]}).payload == "plain"



async def test_a_webhook_call_carries_no_key_and_sends_get_as_query():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"message": "Workflow was started"})

    client = N8nClient("http://n8n.test", API_KEY, MCP_KEY, transport=httpx.MockTransport(handler))
    await client.webhook("POST", "orders", {"n": 1}, timeout=5)
    await client.webhook("GET", "/orders", {"n": 1}, timeout=5)
    await client.aclose()
    post, get = seen
    assert post.url.path == "/webhook/orders" and json.loads(post.content) == {"n": 1}
    assert get.url.path == "/webhook/orders" and get.url.params["n"] == "1" and not get.content
    for request in seen:
        assert API_KEY not in str(request.headers) and MCP_KEY not in str(request.headers)


@pytest.mark.parametrize("error,sent", [(httpx.ConnectError, False), (httpx.ConnectTimeout, False),
                                        (httpx.ReadTimeout, True), (httpx.RemoteProtocolError, True)])
async def test_a_webhook_sent_without_an_answer_is_told_apart_from_one_never_sent(error, sent):
    def handler(request):
        raise error("boom", request=request)

    client = N8nClient("http://n8n.test", API_KEY, MCP_KEY, transport=httpx.MockTransport(handler))
    with pytest.raises(N8nError) as raised:
        await client.webhook("POST", "orders", {}, timeout=5)
    await client.aclose()
    assert isinstance(raised.value, N8nNoAnswer) is sent


@pytest.mark.parametrize("tool", ["publish_workflow", "unpublish_workflow"])
@pytest.mark.parametrize("payload,expected", [
    ({"success": True, "workflowId": "w", "activeVersionId": "v"}, None),
    ({"success": False, "error": "There is a conflict with one of the webhooks."}, "conflict"),
    ({"workflowId": "w"}, "did not confirm"),
])
def test_publish_answers_count_only_with_success(tool, payload, expected):
    error = mcp_error(tool, McpResult(payload, json.dumps(payload), False))
    assert error is None if expected is None else expected in error



async def test_a_server_error_or_no_connection_is_worth_waiting_for_a_refused_key_is_not():
    fake = FakeN8n()
    client = N8nClient("http://n8n.test", API_KEY, MCP_KEY, transport=httpx.MockTransport(fake))
    with pytest.raises(N8nUnavailable):
        await client.api_get("/broken")
    with pytest.raises(N8nError) as refused:
        await client.api_get("/denied")
    assert not isinstance(refused.value, N8nUnavailable)
    await client.aclose()

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    down = N8nClient("http://n8n.test", API_KEY, MCP_KEY, transport=httpx.MockTransport(refuse))
    with pytest.raises(N8nUnavailable):
        await down.api_get("/workflows")
    await down.aclose()
