"""n8n tool server: a policy proxy over n8n's own instance MCP.

n8n ships everything a workflow builder needs in its instance MCP -- node
search, exact parameter definitions, validation, create/update from SDK code,
test runs with pin data (docs/n8n_facts.md M-MCP-H4). This server rebuilds
none of it. It forwards a curated set and lays our policy on top:

* a managed tag: acting tools touch only workflows ScarabHive created,
* blocked node types (shell, files, legacy code) checked on the stored workflow,
* a pin plan: a test pins everything that could act outside the execution --
  the server pins nothing by itself (M-MCP-30),
* the gaps n8n's validators measurably miss (validate.py),
* n8n data wrapped as untrusted, every result capped, one status line per call.

Publishing, triggering and archiving are not offered here (design §3.4).
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import TYPE_CHECKING, Any, Optional

from agent_system.tools.schema_based import SchemaBasedToolServer

from .client import N8nClient, N8nError, N8nNotFound, mcp_error
from .validate import (DEFAULT_BLOCKED_NODE_TYPES, DEFAULT_REVIEW_NODE_TYPES, blocking_save_settings, check_workflow, classify_node_validation,
                       classify_workflow_validation, code_precheck, format_version, normalize_type,
                       normalized, node_type_errors, pin_plan)

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

CAP_SEARCH = 13_000
CAP_NODE_TYPES = 20_000
CAP_SMALL = 8_000
CAP_SDK_SECTION = 16_000
CAP_WORKFLOW = 40_000
CAP_EXECUTION = 12_000
SAMPLE_CHARS = 1_000
SDK_SECTIONS = ("patterns", "patterns_detailed", "expressions", "functions", "rules",
                "import", "guidelines", "design")
BEST_PRACTICE_TECHNIQUES = (
    "list", "scheduling", "chatbot", "form_input", "scraping_and_research", "monitoring",
    "enrichment", "triage", "content_generation", "document_processing", "data_extraction",
    "data_analysis", "data_transformation", "data_persistence", "notification",
    "knowledge_base", "human_in_the_loop", "web_app")


class _NoStatus:
    """Stand-in when a handler is called without the framework's status scope."""

    async def progress(self, message, meta=None):
        pass

    async def end(self, message, meta=None):
        pass

    async def error(self, message, meta=None):
        pass


def _cap(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


def _untrusted(content: Any) -> dict:
    """Content that came out of n8n -- names, notes, execution data. It is data
    for the model, never instructions (design §8.5)."""
    return {"untrusted": True, "content": content}


def _short(value: Any, limit: int = 40) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _bounded(value: Any, default: int, low: int, high: int) -> Optional[int]:
    """An integer argument clamped to [low, high]; None when it is no number --
    the framework does not check arguments against the schema."""
    try:
        return max(low, min(int(value if value not in (None, "") else default), high))
    except (TypeError, ValueError, OverflowError):
        return None


class N8nServer(SchemaBasedToolServer):
    """The n8n plugin's tools. Offered only when n8n is configured."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)
        # From the environment, which config/secrets.env fills at load: a
        # ${VAR} in the YAML would warn on every start of every installation
        # that has no n8n at all.
        self.base_url = str(getattr(server_config, "base_url", "")
                            or os.environ.get("N8N_BASE_URL", "")).rstrip("/")
        self.api_key = str(getattr(server_config, "api_key", "") or os.environ.get("N8N_API_KEY", ""))
        self.mcp_key = str(getattr(server_config, "mcp_key", "") or os.environ.get("N8N_MCP_KEY", ""))
        self.managed_tag = str(getattr(server_config, "managed_tag", "") or "scarabhive")
        self.allowed_hosts = frozenset(getattr(server_config, "allowed_hosts", None) or ())
        self.live_node_types = normalized(getattr(server_config, "live_node_types", None) or ())
        self.blocked = normalized(getattr(server_config, "blocked_node_types", None)
                                  or DEFAULT_BLOCKED_NODE_TYPES)
        self.review = normalized(getattr(server_config, "review_node_types", None)
                                 or DEFAULT_REVIEW_NODE_TYPES)
        self.timeout = float(getattr(server_config, "timeout", 60) or 60)
        self._client: Optional[N8nClient] = None

        missing = [var for var, value in (("N8N_BASE_URL", self.base_url), ("N8N_MCP_KEY", self.mcp_key),
                                          ("N8N_API_KEY", self.api_key)) if not value]
        if len(missing) == 3:
            logger.info("n8n not configured -- %s offers no tools", name)
        elif missing:
            logger.warning("n8n partly configured -- %s lacks %s (docs/deploy/README.md)",
                           name, ", ".join(missing))
        if self.base_url.startswith("http://") and not any(
                h in self.base_url for h in ("://localhost", "://127.0.0.1", "://[::1]")):
            logger.warning("n8n at %s is plain HTTP: both keys travel unencrypted", self.base_url)

    def get_template_vars(self) -> dict[str, Any]:
        """Knowledge tools need the MCP; every acting tool also needs the public
        API, because the managed-tag check reads through it (design §4)."""
        template_vars = super().get_template_vars()
        template_vars["mcp_configured"] = bool(self.base_url and self.mcp_key)
        template_vars["api_configured"] = bool(self.base_url and self.mcp_key and self.api_key)
        return template_vars

    def _n8n(self) -> N8nClient:
        if self._client is None:
            self._client = N8nClient(self.base_url, self.api_key, self.mcp_key, timeout=self.timeout)
        return self._client

    async def stop_plugin(self) -> None:
        """The shutdown hook the framework calls: closes the HTTP client and
        with it the MCP session."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _editor_url(self, workflow_id: str) -> str:
        return f"{self.base_url}/workflow/{workflow_id}"

    async def _fail(self, status, message: str) -> dict:
        await status.error(message[:140])
        return {"status": "error", "error": message}

    async def _written_but_unchecked(self, status, workflow_id: str, done: str, exc: N8nError) -> dict:
        """The write happened, the check after it did not. Said plainly, so the
        model does not create the workflow or apply the operations twice."""
        answer = await self._fail(status, f"workflow {workflow_id} was {done}, but the check afterwards "
                                          f"failed ({exc}); do not repeat it -- test_workflow checks it anew")
        return {**answer, "workflow_id": workflow_id, "editor_url": self._editor_url(workflow_id)}

    # ── shared building blocks ────────────────────────────────────────────

    async def _mcp(self, tool: str, arguments: dict, *, timeout: Optional[float] = None):
        """A forwarded MCP call, read per tool. Raises N8nError on any failure."""
        result = await self._n8n().mcp_call(tool, arguments, timeout=timeout)
        error = mcp_error(tool, result)
        if error:
            raise N8nError(f"n8n {tool}: {error}")
        return result

    async def _require_managed(self, workflow_id: str) -> dict:
        """The one lock every acting tool goes through (design §3, §8.6).

        Reads over the public API -- no MCP budget -- and hands the workflow on,
        so the checks after it need no second GET."""
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise N8nError("workflow_id is required")
        try:
            workflow = await self._n8n().api_get(f"/workflows/{workflow_id}")
        except N8nNotFound:
            raise N8nError(f"workflow {workflow_id} not found") from None
        tags = {t.get("name") for t in workflow.get("tags") or [] if isinstance(t, dict)}
        if self.managed_tag not in tags:
            raise N8nError(f"workflow {workflow_id} is not managed by ScarabHive "
                           f"(tag {self.managed_tag!r} missing); it stays untouched")
        if workflow.get("isArchived"):
            raise N8nError(f"workflow {workflow_id} is archived -- restore it in the n8n editor first")
        if not (workflow.get("settings") or {}).get("availableInMCP"):
            raise N8nError(f"workflow {workflow_id} is not exposed to the n8n MCP; "
                           f"exposing it is a decision for a human in the editor")
        return workflow

    async def _credential_types(self) -> dict[str, str]:
        result = await self._mcp("list_credentials", {"limit": 200})
        return {str(c["id"]): str(c.get("type", "")) for c in _credential_list(result.payload)}

    async def _type_errors(self, workflow: dict) -> dict:
        """(type, version) pairs n8n does not know, via ONE get_node_types call.

        typeVersion 99 passes both n8n validators and the publish (M-MCP-H8,
        M-MCP-H9, F-VAL3); get_node_types names it (M-MCP-32, M-MCP-44)."""
        pairs = sorted({(n.get("type", ""), format_version(n.get("typeVersion", 1)))
                        for n in workflow.get("nodes") or [] if n.get("type")})
        if not pairs:
            return {}
        result = await self._mcp("get_node_types",
                                 {"nodeIds": [{"nodeId": t, "version": v} for t, v in pairs]})
        return node_type_errors(result.text, pairs)

    async def _check_stored(self, workflow: dict) -> list[dict]:
        """Policy and gap checks on what n8n really stored -- the binding check;
        the code pre-check before creating is only an early hint."""
        return check_workflow(workflow, blocked=self.blocked, review=self.review,
                              credentials=await self._credential_types(),
                              type_errors=await self._type_errors(workflow))

    # ── §3.1 node knowledge ───────────────────────────────────────────────

    async def search_nodes(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        queries = params.get("queries")
        if isinstance(queries, str):
            queries = [queries]
        if not isinstance(queries, list) or not queries or not all(isinstance(q, str) and q.strip() for q in queries):
            return await self._fail(status, "queries: give one or two search terms")
        if len(queries) > 2:
            return await self._fail(status, "queries: at most 2 per call -- the answer grows with each")
        arguments = {"queries": queries}
        if params.get("usage") in ("workflow", "agentTool"):
            arguments["usage"] = params["usage"]
        try:
            result = await self._mcp("search_nodes", arguments)
        except N8nError as exc:
            return await self._fail(status, str(exc))
        text, truncated = _cap(result.text, CAP_SEARCH)
        await status.end(f"{len(queries)} term(s) {_short(', '.join(queries))}: {len(result.text)} chars")
        # Node descriptions: a community node's author wrote them, not n8n.
        return {"status": "success", "data": _untrusted(text), "truncated": truncated}

    async def get_node_types(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        nodes = params.get("nodes")
        if not isinstance(nodes, list) or not nodes or len(nodes) > 3:
            return await self._fail(status, "nodes: one to three {node_id, version?, resource?, operation?}")
        node_ids, pairs = [], []
        for node in nodes:
            if not isinstance(node, dict) or not node.get("node_id"):
                return await self._fail(status, "every entry in nodes needs node_id")
            entry = {"nodeId": str(node["node_id"])}
            for key in ("version", "resource", "operation", "mode"):
                if node.get(key) not in (None, ""):
                    entry[key] = format_version(node[key]) if key == "version" else str(node[key])
            node_ids.append(entry)
            if "version" in entry:
                pairs.append((entry["nodeId"], entry["version"]))
        try:
            result = await self._mcp("get_node_types", {"nodeIds": node_ids})
        except N8nError as exc:
            return await self._fail(status, str(exc))
        errors = sorted(set(node_type_errors(result.text, pairs).values()))
        if "# Errors" in result.text and not errors:
            section = result.text[result.text.find("# Errors"):]
            errors = [line.strip(" -*") for line in section.splitlines()[1:6] if line.strip(" -*")]
        text, truncated = _cap(result.text, CAP_NODE_TYPES)
        await status.end(f"{len(node_ids)} type(s), {len(result.text)} chars, {len(errors)} error(s)")
        return {"status": "success", "data": _untrusted(text), "errors": errors, "truncated": truncated}

    async def explore_node_resources(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        required = ("node_type", "version", "method_name", "method_type", "credential_type", "credential_id")
        missing = [k for k in required if params.get(k) in (None, "")]
        if missing:
            return await self._fail(status, f"missing: {', '.join(missing)}")
        if params["method_type"] not in ("listSearch", "loadOptions"):
            return await self._fail(status, "method_type: listSearch or loadOptions")
        try:
            version = float(params["version"])
        except (TypeError, ValueError, OverflowError):
            version = math.nan
        if not math.isfinite(version):
            return await self._fail(status, "version: the node's typeVersion, a number such as 2.1")
        arguments = {"nodeType": params["node_type"], "version": version,
                     "methodName": params["method_name"], "methodType": params["method_type"],
                     "credentialType": params["credential_type"], "credentialId": str(params["credential_id"])}
        for key, mcp_key in (("filter", "filter"), ("pagination_token", "paginationToken"),
                             ("current_node_parameters", "currentNodeParameters")):
            if params.get(key) not in (None, ""):
                arguments[mcp_key] = params[key]
        try:
            result = await self._mcp("explore_node_resources", arguments)
        except N8nError as exc:
            return await self._fail(status, str(exc))
        text, truncated = _cap(result.text, CAP_SMALL)
        await status.end(f"{_short(params['method_name'])} on {_short(params['node_type'])}: {len(result.text)} chars")
        return {"status": "success", "data": _untrusted(text), "truncated": truncated}

    async def get_best_practices(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        technique = params.get("technique")
        if technique not in BEST_PRACTICE_TECHNIQUES:
            return await self._fail(status, f"technique: one of {', '.join(BEST_PRACTICE_TECHNIQUES)}")
        try:
            result = await self._mcp("get_workflow_best_practices", {"technique": technique})
        except N8nError as exc:
            return await self._fail(status, str(exc))
        text, truncated = _cap(result.text, CAP_SMALL)
        await status.end(f"{technique}: {len(result.text)} chars of guidance")
        return {"status": "success", "result": text, "truncated": truncated}

    async def get_sdk_reference(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        section = params.get("section")
        if section not in SDK_SECTIONS:
            return await self._fail(status, f"section: one of {', '.join(SDK_SECTIONS)} "
                                            f"(the whole reference is ~50k chars, so one at a time)")
        try:
            result = await self._mcp("get_workflow_sdk_reference", {"section": section})
        except N8nError as exc:
            return await self._fail(status, str(exc))
        text, truncated = _cap(result.text, CAP_SDK_SECTION)
        await status.end(f"section {section}: {len(result.text)} chars")
        return {"status": "success", "result": text, "truncated": truncated}

    async def list_credentials(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        arguments: dict = {"limit": 200}
        if params.get("type"):
            arguments["type"] = str(params["type"])
        try:
            result = await self._mcp("list_credentials", arguments)
        except N8nError as exc:
            return await self._fail(status, str(exc))
        creds = [{"id": str(c["id"]), "name": str(c.get("name", "")), "type": str(c.get("type", ""))}
                 for c in _credential_list(result.payload)][:100]
        await status.end(f"{len(creds)} credential(s)" + (f" of type {_short(params['type'])}" if params.get("type") else ""))
        return {"status": "success", "count": len(creds), "data": _untrusted(creds)}

    # ── §3.2 workflows ────────────────────────────────────────────────────

    async def validate_node_config(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        nodes = params.get("nodes")
        if not isinstance(nodes, list) or not nodes or len(nodes) > 50:
            return await self._fail(status, "nodes: 1 to 50 node configs {type, typeVersion, parameters}")
        policy = [f for n in nodes if isinstance(n, dict)
                  for f in check_workflow({"nodes": [n]}, blocked=self.blocked, review=self.review)
                  if f["code"] in ("BLOCKED_NODE", "REVIEW_NODE")]
        try:
            result = await self._mcp("validate_node_config", {"nodes": nodes})
        except N8nError as exc:
            return await self._fail(status, str(exc))
        payload = result.payload if isinstance(result.payload, dict) else {}
        errors = classify_node_validation(payload) + [f for f in policy if f["level"] == "error"]
        warnings = [f for f in policy if f["level"] == "warning"]
        await status.end(f"{len(nodes)} node(s): {len(errors)} error(s), {len(warnings)} warning(s)")
        return {"status": "success", "ok": not errors, "errors": errors, "warnings": warnings}

    async def validate_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            return await self._fail(status, "code: the workflow as n8n Workflow SDK code")
        try:
            errors, warnings = await self._validate_code(code)
        except N8nError as exc:
            return await self._fail(status, str(exc))
        await status.end(f"{len(errors)} error(s), {len(warnings)} warning(s)")
        return {"status": "success", "ok": not errors, "errors": errors, "warnings": warnings}

    async def _validate_code(self, code: str) -> tuple[list, list]:
        """Code pre-check plus n8n's validator, read correctly: valid:true with
        warnings that describe real errors is NOT ok (M-MCP-8)."""
        errors = code_precheck(code, self.blocked)
        result = await self._mcp("validate_workflow", {"code": code})
        n8n_errors, warnings = classify_workflow_validation(
            result.payload if isinstance(result.payload, dict) else {"errors": [result.text]})
        return errors + n8n_errors, warnings

    async def create_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        code, name = params.get("code"), params.get("name")
        if not isinstance(code, str) or not code.strip():
            return await self._fail(status, "code: the workflow as n8n Workflow SDK code")
        if not isinstance(name, str) or not name.strip():
            return await self._fail(status, "name: the workflow's name")
        try:
            errors, warnings = await self._validate_code(code)
            if errors:
                await status.error(f"not created: {len(errors)} validation error(s) in {_short(name)}")
                return {"status": "error", "error": "validation failed; nothing was created",
                        "errors": errors, "warnings": warnings}
            arguments = {"code": code, "name": name}
            if params.get("description"):
                arguments["description"] = str(params["description"])
            created = (await self._mcp("create_workflow_from_code", arguments)).payload
            workflow_id = str(created.get("workflowId", "")) if isinstance(created, dict) else ""
            if not workflow_id:
                return await self._fail(status, "n8n created no workflow id")
            await self._tag(workflow_id)
        except N8nError as exc:
            return await self._fail(status, str(exc))
        try:
            stored = await self._n8n().api_get(f"/workflows/{workflow_id}")
            findings = await self._check_stored(stored)
        except N8nError as exc:
            return await self._written_but_unchecked(status, workflow_id, "created and tagged", exc)
        blocking = [f for f in findings if f["level"] == "error"]
        await status.end(f"created {workflow_id} {_short(name)}: {len(blocking)} blocking finding(s)")
        return {"status": "success", "workflow_id": workflow_id, "editor_url": self._editor_url(workflow_id),
                "ok": not blocking, "findings": findings, "warnings": warnings,
                "auto_assigned_credentials": _untrusted(created.get("autoAssignedCredentials") or [])}

    async def _tag(self, workflow_id: str) -> None:
        """Mark a new workflow as ours. Without the tag it stays a harmless,
        unpublished draft the plugin refuses to touch (design §3.2 step 4)."""
        operation = [{"type": "addTags", "names": [self.managed_tag]}]
        last: Optional[Exception] = None
        for _ in range(2):
            try:
                await self._mcp("update_workflow", {"workflowId": workflow_id, "operations": operation})
                return
            except N8nError as exc:
                last = exc
        raise N8nError(f"workflow {workflow_id} was created but not tagged {self.managed_tag!r} "
                       f"({last}); the plugin will not touch it") from None

    async def update_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        workflow_id = params.get("workflow_id")
        operations = params.get("operations")
        if not isinstance(operations, list) or not operations or len(operations) > 100:
            return await self._fail(status, "operations: 1 to 100 update operations")
        try:
            workflow = await self._require_managed(workflow_id)
            expected = params.get("expected_version_id")
            if expected and expected != workflow.get("versionId"):
                return await self._fail(status, f"workflow {workflow_id} changed since you read it "
                                                f"(version {workflow.get('versionId')}); read it again")
            refusal = await self._refuse_operations(operations)
            if refusal:
                return await self._fail(status, refusal)
            result = (await self._mcp("update_workflow",
                                      {"workflowId": workflow_id, "operations": operations})).payload
        except N8nError as exc:
            return await self._fail(status, str(exc))
        try:
            stored = await self._n8n().api_get(f"/workflows/{workflow_id}")
            findings = await self._check_stored(stored)
        except N8nError as exc:
            return await self._written_but_unchecked(status, workflow_id, "updated (operations applied)", exc)
        n8n_warnings = result.get("validationWarnings") or [] if isinstance(result, dict) else []
        blocking = [f for f in findings if f["level"] == "error"]
        await status.end(f"{workflow_id}: {len(operations)} op(s) applied, {len(blocking)} blocking finding(s)")
        return {"status": "success", "workflow_id": workflow_id, "version_id": stored.get("versionId"),
                "ok": not blocking, "findings": findings, "n8n_warnings": _untrusted(n8n_warnings)}

    async def _refuse_operations(self, operations: list) -> Optional[str]:
        """The operations the builder may not send (design §3.2): removing our
        tag, adding a blocked type, a credential n8n does not know (it stores
        ghost ids unchecked, M-MCP-11), the setting that leaves a test without a
        stored execution (M-MCP-41)."""
        credentials: Optional[dict] = None
        for index, op in enumerate(operations):
            if not isinstance(op, dict) or not op.get("type"):
                return f"operation {index}: needs a type"
            kind = op["type"]
            if kind == "removeTags" and self.managed_tag in (op.get("names") or []):
                return f"operation {index}: the tag {self.managed_tag!r} marks the workflow as ours; it stays"
            if kind == "addNode":
                node_type = str((op.get("node") or {}).get("type", ""))
                if normalize_type(node_type) in self.blocked:
                    return f"operation {index}: node type {node_type} is blocked by policy"
            if kind == "setWorkflowSettings" and (op.get("settings") or {}).get("saveManualExecutions") is False:
                return (f"operation {index}: saveManualExecutions false would leave test runs without a "
                        f"stored execution, and nothing could prove the workflow works")
            if kind == "setNodeCredential":
                if credentials is None:
                    credentials = await self._credential_types()
                cred_id, key = str(op.get("credentialId", "")), str(op.get("credentialKey", ""))
                if cred_id not in credentials:
                    return f"operation {index}: credential {cred_id!r} does not exist (list_credentials)"
                if credentials[cred_id] != key:
                    return f"operation {index}: credential {cred_id!r} is a {credentials[cred_id]}, not a {key}"
        return None

    async def get_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        workflow_id = params.get("workflow_id")
        detail = params.get("detail") or "full"
        if not isinstance(workflow_id, str) or not workflow_id:
            return await self._fail(status, "workflow_id is required")
        if detail not in ("execution", "full"):
            return await self._fail(status, "detail: full (nodes, connections) or execution (metadata only)")
        try:
            result = await self._mcp("get_workflow_details", {"workflowId": workflow_id, "detailLevel": detail})
        except N8nError as exc:
            return await self._fail(status, str(exc))
        text, truncated = _cap(result.text, CAP_WORKFLOW)
        if truncated:
            return await self._fail(status, f"workflow {workflow_id} is over {CAP_WORKFLOW} chars in full; "
                                            f"detail execution gives its metadata, the editor "
                                            f"({self._editor_url(workflow_id)}) the rest")
        await status.end(f"{workflow_id} ({detail}): {len(result.text)} chars")
        return {"status": "success", "workflow_id": workflow_id, "editor_url": self._editor_url(workflow_id),
                "data": _untrusted(text)}

    async def list_workflows(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        limit = _bounded(params.get("limit"), 20, 1, 50)
        if limit is None:
            return await self._fail(status, "limit: a number from 1 to 50")
        try:
            page = await self._n8n().api_get("/workflows", {
                "tags": self.managed_tag, "name": params.get("name"), "limit": limit,
                "cursor": params.get("cursor"), "excludePinnedData": "true"})
        except N8nError as exc:
            return await self._fail(status, str(exc))
        workflows = [{"id": w.get("id"), "name": w.get("name"), "active": w.get("active"),
                      "is_archived": w.get("isArchived"), "updated_at": w.get("updatedAt"),
                      "editor_url": self._editor_url(str(w.get("id")))} for w in page.get("data") or []]
        await status.end(f"{len(workflows)} managed workflow(s)" + (" (more)" if page.get("nextCursor") else ""))
        return {"status": "success", "count": len(workflows), "next_cursor": page.get("nextCursor"),
                "data": _untrusted(workflows)}

    # ── §3.3 test and executions ──────────────────────────────────────────

    async def test_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        workflow_id = params.get("workflow_id")
        timeout_s = _bounded(params.get("timeout_s"), 60, 5, 300)
        mocks = params.get("mocks") or {}
        live_nodes = params.get("live_nodes") or []
        if timeout_s is None:
            return await self._fail(status, "timeout_s: seconds, a number from 5 to 300")
        if not isinstance(mocks, dict) or not isinstance(live_nodes, list) \
                or not all(isinstance(n, str) for n in live_nodes):
            return await self._fail(status, "mocks is {node: [items]}, live_nodes is a list of node names")
        try:
            workflow = await self._require_managed(workflow_id)
            findings = await self._check_stored(workflow)
            blocking = [f for f in findings if f["level"] == "error"]
            if blocking:
                await status.error(f"{workflow_id}: not tested, {len(blocking)} blocking finding(s)")
                return {"status": "error", "error": "the stored workflow has blocking findings; fix them first",
                        "findings": findings}
            settings = blocking_save_settings(workflow)
            if settings:
                return await self._fail(status, f"{workflow_id}: not tested, a setting prevents a stored "
                                                f"execution: {', '.join(settings)} (set it to true with setWorkflowSettings)")
            plan = pin_plan(workflow, trigger_node=params.get("trigger_node"),
                            trigger_input=params.get("trigger_input"), mocks=mocks, live_nodes=live_nodes,
                            allowed_hosts=self.allowed_hosts, live_node_types=self.live_node_types)
            if plan.errors:
                return await self._fail(status, "; ".join(plan.errors))
            arguments = {"workflowId": workflow_id, "pinData": plan.pin_data, "timeout": timeout_s}
            if plan.trigger:
                # The trigger the plan fed trigger_input to, named: n8n must not pick another.
                arguments["triggerNodeName"] = plan.trigger
            answer = (await self._mcp("test_workflow", arguments, timeout=timeout_s + 30)).payload
            execution_id = str(answer.get("executionId", ""))
            try:
                execution = await self._n8n().api_get(f"/executions/{execution_id}", {"includeData": "true"})
            except N8nNotFound:
                execution = None
        except N8nError as exc:
            return await self._fail(status, str(exc))
        run_status = str(answer.get("status", "unknown"))
        if execution is None:
            await status.error(f"{workflow_id}: execution {execution_id} was not stored -- not proven")
            return {"status": "error", "error": f"execution {execution_id} was not stored, so the test "
                                                f"proves nothing", "execution_id": execution_id}
        nodes = _summarize_execution(execution, plan.runs)
        tested = run_status == "success"
        await status.end(f"{workflow_id}: execution {execution_id} {run_status}, "
                         f"{sum(n['run'] == 'live' for n in nodes)} live / "
                         f"{sum(n['run'] == 'pinned' for n in nodes)} pinned")
        return {"status": "success", "tested": tested, "execution_id": execution_id,
                "execution_status": run_status,
                "error": _untrusted(answer.get("error")) if answer.get("error") else None,
                "nodes": _untrusted(nodes), "warnings": plan.warnings,
                "editor_url": self._editor_url(str(workflow_id))}

    async def get_execution(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        workflow_id, execution_id = params.get("workflow_id"), params.get("execution_id")
        if not workflow_id or not execution_id:
            return await self._fail(status, "workflow_id and execution_id are required")
        arguments: dict = {"workflowId": str(workflow_id), "executionId": str(execution_id),
                           "includeData": bool(params.get("include_data")), "truncateData": 2000}
        if params.get("nodes"):
            arguments["nodeNames"] = [str(n) for n in params["nodes"]][:10]
        try:
            result = await self._mcp("get_workflow_execution", arguments)
        except N8nError as exc:
            return await self._fail(status, str(exc))
        text, truncated = _cap(result.text, CAP_EXECUTION)
        await status.end(f"execution {execution_id}: {len(result.text)} chars"
                         + (" with data" if arguments["includeData"] else ""))
        return {"status": "success", "execution_id": str(execution_id), "data": _untrusted(text),
                "truncated": truncated}

    async def list_executions(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        limit = _bounded(params.get("limit"), 10, 1, 20)
        if limit is None:
            return await self._fail(status, "limit: a number from 1 to 20")
        try:
            page = await self._n8n().api_get("/executions", {
                "workflowId": params.get("workflow_id"), "status": params.get("status"), "limit": limit})
        except N8nError as exc:
            return await self._fail(status, str(exc))
        executions = [{"id": e.get("id"), "workflow_id": e.get("workflowId"), "status": e.get("status"),
                       "mode": e.get("mode"), "started_at": e.get("startedAt"), "stopped_at": e.get("stoppedAt")}
                      for e in page.get("data") or []]
        await status.end(f"{len(executions)} execution(s)"
                         + (f" of {_short(params['workflow_id'])}" if params.get("workflow_id") else ""))
        return {"status": "success", "count": len(executions), "executions": executions}


def _credential_list(payload: Any) -> list[dict]:
    """list_credentials answers {"data": [...], "count": n} (measured)."""
    items = payload.get("data") if isinstance(payload, dict) else None
    return [c for c in items if isinstance(c, dict) and c.get("id")] if isinstance(items, list) else []


def _summarize_execution(execution: dict, runs: dict) -> list[dict]:
    """Per node: pinned by us / ran live / not reached, from the stored runData.
    A subnode counts as live when it ran inside its root, else as not reached."""
    run_data = ((execution.get("data") or {}).get("resultData") or {}).get("runData") or {}
    summary = []
    for name, plan in runs.items():
        entry: dict = {"name": name, "run": plan["run"], "reason": plan["reason"]}
        runs_of_node = run_data.get(name) or []
        if not runs_of_node:
            entry["run"] = "not_reached"
            summary.append(entry)
            continue
        if plan["run"] == "sub":
            entry["run"], entry["reason"] = "live", "ran as a subnode of its root"
        first = runs_of_node[0] or {}
        entry["node_status"] = first.get("executionStatus")
        # One list per output: an IF's false branch is output 1, not nothing.
        outputs = (first.get("data") or {}).get("main") or []
        entry["items_out"] = sum(len(o or []) for o in outputs)
        if len(outputs) > 1:
            entry["items_per_output"] = [len(o or []) for o in outputs]
        items = next((o for o in outputs if o), [])
        if items:
            entry["sample"] = json.dumps(items[0].get("json", items[0]), ensure_ascii=False,
                                         default=str)[:SAMPLE_CHARS]
        if first.get("error"):
            entry["error"] = str((first["error"] or {}).get("message", first["error"]))[:500]
        summary.append(entry)
    return summary
