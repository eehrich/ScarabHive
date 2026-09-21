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

Publishing, archiving and triggering go through the same lock; publishing
takes only the version a successful test run proved (design §3.4).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlencode

from agent_system.core.session_presence import presence_for, wake_blocked, wake_depth, wake_session
from agent_system.plugins.cache import PluginCache
from agent_system.tools.schema_based import SchemaBasedToolServer

from .client import N8nClient, N8nError, N8nNoAnswer, N8nNotFound, mcp_error
from .validate import (DEFAULT_BLOCKED_NODE_TYPES, DEFAULT_REVIEW_NODE_TYPES, blocking_save_settings, check_workflow, classify_node_validation,
                       classify_workflow_validation, code_precheck, format_version, normalize_type,
                       normalized, node_type_errors, pin_plan, webhook_path_problem)
from .watch import END_STATES, wait_for_end

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

CAP_SEARCH = 13_000
CAP_NODE_TYPES = 20_000
CAP_SMALL = 8_000
CAP_SDK_SECTION = 16_000
CAP_WORKFLOW = 40_000
CAP_EXECUTION = 12_000
CAP_WEBHOOK_ANSWER = 8_000
SAMPLE_CHARS = 1_000
MAX_PAYLOAD_CHARS = 64_000
# A GET payload travels in the request line, and n8n's HTTP server refuses long
# ones before any workflow runs (Node's header limit is 16 KiB).
MAX_QUERY_CHARS = 8_000
# How long a watch's outcome and read mark are kept: n8n prunes runs after 14 days (F-EXE4).
WATCH_RECORD_TTL_S = 14 * 24 * 3600
# Where watch outcomes and read marks live; None is PluginCache's data/cache. Tests set a tmp dir.
CACHE_ROOT: Optional[Path] = None
# The parts of a watch's file names. They reach get_execution from the model,
# so anything else never becomes a path.
_WORKFLOW_ID = re.compile(r"[A-Za-z0-9]{1,40}")
_EXECUTION_ID = re.compile(r"[0-9]{1,18}")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
# The test of the current version is looked for among this many newest
# successful runs; only the test runs among them are read one by one.
TESTED_SCAN = 250
TESTED_LOOKBACK = 20
# Pause between looks for the execution a webhook call started.
LOOKUP_PAUSE_S = 1.0
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
        # Where browsers and webhook callers reach n8n, when that is not where
        # ScarabHive does (docs/deploy/README.md): the links and webhook URLs
        # handed to the user are built from it.
        self.public_url = str(getattr(server_config, "public_url", "") or os.environ.get("N8N_PUBLIC_URL", "")
                              or self.base_url).rstrip("/")
        self.managed_tag = str(getattr(server_config, "managed_tag", "") or "scarabhive")
        self.allowed_hosts = frozenset(getattr(server_config, "allowed_hosts", None) or ())
        self.live_node_types = normalized(getattr(server_config, "live_node_types", None) or ())
        self.blocked = normalized(getattr(server_config, "blocked_node_types", None)
                                  or DEFAULT_BLOCKED_NODE_TYPES)
        self.review = normalized(getattr(server_config, "review_node_types", None)
                                 or DEFAULT_REVIEW_NODE_TYPES)
        self.timeout = float(getattr(server_config, "timeout", 60) or 60)
        # Publishing, unpublishing, archiving a published workflow (design E4).
        # Only a real true counts: the framework does not check plugin config.
        self.allow_publish = getattr(server_config, "allow_publish", False) is True
        self.watch_max_hours = _bounded(getattr(server_config, "watch_max_hours", None), 24, 1, 168) or 24
        self._client: Optional[N8nClient] = None
        self._watches: set[asyncio.Task] = set()
        # execution id -> {"status", "read"}: the watches of this process.
        self._watched: dict[str, dict] = {}
        self._outcomes: Optional[PluginCache] = None

        missing = [var for var, value in (("N8N_BASE_URL", self.base_url), ("N8N_MCP_KEY", self.mcp_key),
                                          ("N8N_API_KEY", self.api_key)) if not value]
        if len(missing) == 3:
            wanting = _agents_wanting(system_config, name)
            if wanting:
                logger.warning("n8n not configured -- %s offers no tools, yet %s allow them: set N8N_BASE_URL, "
                               "N8N_MCP_KEY and N8N_API_KEY (docs/deploy/README.md)", name, ", ".join(wanting))
            else:
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
        """The shutdown hook the framework calls: ends the watches, then closes
        the HTTP client and with it the MCP session."""
        watches = list(self._watches)
        for task in watches:
            task.cancel()
        await asyncio.gather(*watches, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _editor_url(self, workflow_id: str) -> str:
        return f"{self.public_url}/workflow/{workflow_id}"

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
        execution_id = str(execution_id)
        key = self._watch_key(workflow_id, execution_id, params.get("_session_id") or "")
        record = await self._watch_record(key)
        if record is not None:
            # This session's watch rang for it: this turn took the news, even if the read below fails.
            self._mark_read(key)
        try:
            result = await self._mcp("get_workflow_execution", arguments)
        except N8nError as exc:
            answer = await self._fail(status, str(exc))
            if record is not None:
                answer["watch"] = record    # a run that is gone can only be told through this
            return answer
        execution = result.payload.get("execution") if isinstance(result.payload, dict) else None
        if isinstance(execution, dict) and execution.get("status") in END_STATES and self._watched_somewhere(key):
            self._mark_read(key)            # the end is read: a wake for it would bring nothing new
        text, truncated = _cap(result.text, CAP_EXECUTION)
        await status.end(f"execution {_short(execution_id)}: {len(result.text)} chars"
                         + (" with data" if arguments["includeData"] else ""))
        answer = {"status": "success", "execution_id": execution_id, "data": _untrusted(text),
                  "truncated": truncated}
        if record is not None:
            answer["watch"] = record
        return answer

    async def list_executions(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        limit = _bounded(params.get("limit"), 10, 1, 20)
        if limit is None:
            return await self._fail(status, "limit: a number from 1 to 20")
        try:
            found = await self._executions(params.get("workflow_id"), limit, params.get("status"))
        except N8nError as exc:
            return await self._fail(status, str(exc))
        executions = [{"id": e.get("id"), "workflow_id": e.get("workflowId"), "status": e.get("status"),
                       "mode": e.get("mode"), "started_at": e.get("startedAt"), "stopped_at": e.get("stoppedAt")}
                      for e in found]
        await status.end(f"{len(executions)} execution(s)"
                         + (f" of {_short(params['workflow_id'])}" if params.get("workflow_id") else ""))
        return {"status": "success", "count": len(executions), "executions": executions}

    # ── §3.4 publish, archive, trigger ────────────────────────────────────

    async def publish_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        workflow_id = params.get("workflow_id")
        if not self.allow_publish:
            return await self._fail(status, "publishing is switched off by the operator (allow_publish)")
        try:
            workflow = await self._require_managed(workflow_id)
            findings = await self._check_stored(workflow)
            blocking = [f for f in findings if f["level"] == "error"]
            if blocking:
                await status.error(f"{workflow_id}: not published, {len(blocking)} blocking finding(s)")
                return {"status": "error", "error": "the workflow has blocking findings; fix and test it first",
                        "findings": findings}
            version = str(workflow.get("versionId") or "")
            proof = await self._tested_in(workflow_id, version)
            if proof is None:
                return await self._fail(status, f"{workflow_id}: its current version has no successful test "
                                                f"run among the newest {TESTED_SCAN} successful runs -- run "
                                                f"n8n_test_workflow on it first")
            # By version id: an edit after these checks must not go live untested.
            answer = (await self._mcp("publish_workflow", {"workflowId": workflow_id, "versionId": version})).payload
        except N8nError as exc:
            return await self._fail(status, str(exc))
        await status.end(f"published {workflow_id} {_short(workflow.get('name'))}, proven by execution {proof}")
        return {"status": "success", "workflow_id": workflow_id, "active_version_id": answer.get("activeVersionId"),
                "proven_by_execution": proof, "webhooks": _untrusted(self._production_urls(workflow)),
                "editor_url": self._editor_url(workflow_id)}

    async def _tested_in(self, workflow_id: str, version: str) -> Optional[str]:
        """The id of a successful TEST run of exactly this version, or None.

        Only a test proves the draft: a production run ran the published
        version, and n8n stores a test as mode manual (M-MCP-70). The list
        leaves workflowVersionId empty; an execution read on its own carries it
        (M-MCP-65), so the newest test runs are read one by one."""
        if not version:
            return None
        # ponytail: one page; a workflow with more production runs since its test
        # than TESTED_SCAN is refused -- page on with nextCursor if that happens.
        page = await self._n8n().api_get("/executions", {"workflowId": workflow_id, "status": "success",
                                                         "limit": TESTED_SCAN})
        for item in [i for i in _items(page) if i.get("mode") == "manual"][:TESTED_LOOKBACK]:
            try:
                execution = await self._n8n().api_get(f"/executions/{item.get('id')}")
            except N8nNotFound:
                continue
            if isinstance(execution, dict) and execution.get("workflowVersionId") == version:
                return str(item.get("id"))
        return None

    def _production_urls(self, workflow: dict) -> list[dict]:
        urls = []
        for hook in _webhooks(workflow.get("nodes")):
            entry: dict = {"node": hook["node"], "methods": hook["methods"]}
            if ":" in hook["path"]:
                # n8n serves route parameters under another URL form (unmeasured): no guess.
                entry["note"] = "the path has route parameters; take the URL from the n8n editor"
            else:
                entry["url"] = f"{self.public_url}/webhook/{hook['path']}"
            urls.append(entry)
        return urls

    async def unpublish_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        workflow_id = params.get("workflow_id")
        if not self.allow_publish:
            return await self._fail(status, "taking workflows offline is switched off with publishing (allow_publish)")
        try:
            workflow = await self._require_managed(workflow_id)
            await self._mcp("unpublish_workflow", {"workflowId": workflow_id})
        except N8nError as exc:
            return await self._fail(status, str(exc))
        was_published = bool(workflow.get("activeVersionId"))
        await status.end(f"unpublished {workflow_id} {_short(workflow.get('name'))}"
                         + ("" if was_published else " (was not published)"))
        return {"status": "success", "workflow_id": workflow_id, "was_published": was_published}

    async def archive_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        workflow_id = params.get("workflow_id")
        if not self.allow_publish:
            return await self._fail(status, "archiving is switched off with publishing (allow_publish)")
        try:
            workflow = await self._require_managed(workflow_id)
            await self._mcp("archive_workflow", {"workflowId": workflow_id})
        except N8nError as exc:
            return await self._fail(status, str(exc))
        await status.end(f"archived {workflow_id} {_short(workflow.get('name'))}")
        return {"status": "success", "workflow_id": workflow_id, "archived": True,
                "note": "an archived workflow is restored in the n8n editor"}

    async def trigger_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        workflow_id = params.get("workflow_id")
        payload = params.get("payload") if params.get("payload") is not None else {}
        wait = params.get("wait") or "wake"
        timeout_s = _bounded(params.get("timeout_s"), 60, 5, 300)
        if timeout_s is None:
            return await self._fail(status, "timeout_s: seconds, a number from 5 to 300")
        if wait not in ("wake", "none"):
            return await self._fail(status, "wait: wake (be woken when the run ends) or none")
        if not isinstance(payload, dict):
            return await self._fail(status, "payload: a JSON object")
        try:
            size = len(json.dumps(payload, allow_nan=False))
        except (TypeError, ValueError):
            return await self._fail(status, "payload: plain JSON values only")
        if size > MAX_PAYLOAD_CHARS:
            return await self._fail(status, f"payload: at most {MAX_PAYLOAD_CHARS} characters as JSON")
        try:
            workflow = await self._require_managed(workflow_id)
            hook, method = _trigger_target(workflow, params.get("webhook_node"), params.get("method"))
            if method in ("GET", "HEAD"):
                if not all(isinstance(v, (str, int, float, bool)) for v in payload.values()):
                    return await self._fail(status, f"a {method} webhook takes the payload as query parameters: "
                                                    f"flat values only")
                if len(urlencode(payload)) > MAX_QUERY_CHARS:
                    return await self._fail(status, f"a {method} webhook takes at most {MAX_QUERY_CHARS} characters "
                                                    f"of query parameters; more needs a POST webhook")
            before = max((_as_int(e.get("id")) for e in await self._executions_now(workflow_id)), default=0)
            response, note = None, ""
            try:
                response = await self._n8n().webhook(method, hook["path"], payload, timeout=timeout_s)
            except N8nNoAnswer as exc:
                note = f"{exc}; the run may still be going"
            refused = _not_run(response) if response is not None else ""
        except N8nError as exc:
            return await self._fail(status, str(exc))
        # From here on the webhook was called: no answer may read as "nothing
        # happened", or the model calls it a second time. Even n8n's refusal is
        # believed only when no run of this call turns up: a workflow can answer
        # the same codes and text.
        result: dict[str, Any] = {
            "status": "success", "workflow_id": workflow_id, "method": method,
            "http_status": response.status_code if response is not None else None,
            "response": _untrusted(_cap(response.text, CAP_WEBHOOK_ANSWER)[0]) if response is not None else None,
            "execution_id": None, "editor_url": self._editor_url(workflow_id)}
        looked = False
        try:
            execution_id, correlation, candidates = await self._find_execution(workflow_id, before, response)
            state = ""
            if execution_id:
                execution = await self._n8n().api_get(f"/executions/{execution_id}")
                state = str(execution.get("status") or "") if isinstance(execution, dict) else ""
            looked = True
        except N8nError as exc:
            execution_id, correlation, candidates, state = None, None, [], ""
            note = f"{note}; " if note else ""
            note += f"its execution could not be looked up ({exc})"
        if refused and looked and not execution_id and not candidates:
            return await self._fail(status, f"{method} /webhook/{hook['path']}: {refused}")
        result.update(execution_id=execution_id, correlation=correlation, execution_status=state or None)
        if candidates:
            result["candidates"] = candidates
            note = (f"{note}; " if note else "") + "several runs started at the same time -- pick yours by its data"
        if not execution_id and not candidates and not note:
            note = "no run of it was found yet"
        settings = workflow.get("settings") or {}
        if "none" in (settings.get("saveDataSuccessExecution"), settings.get("saveDataErrorExecution")):
            note = (f"{note}; " if note else "") + "the workflow does not store some runs (saveData... none)"
        if note:
            result["note"] = note + ("" if execution_id else
                                     " -- do not call the webhook again, look with n8n_list_executions")
        armed = False
        if wait == "wake" and state not in END_STATES:
            session_id, user_id = params.get("_session_id") or "", params.get("_user_id") or ""
            if not execution_id:
                blocked = "no execution was found to watch"
            elif wake_depth():
                # A woken run is a one-shot process: its end takes the watch with it.
                blocked = "this run was itself woken and ends with its turn"
            else:
                blocked = wake_blocked(self.system_config, session_id, user_id)
            if not blocked and self._watch_key(workflow_id, execution_id, session_id) is None:
                blocked = "this session cannot be watched"
            if not blocked and await self._is_sub_agent(session_id, user_id):
                blocked = "a sub-agent's session is never woken"
            if blocked:
                if execution_id:
                    # Every refusal says what to do: most read as settings, not as a next step.
                    blocked += (f"; you are not woken -- give execution id {execution_id} to whoever asked and "
                                f"read it later with n8n_get_execution, do not poll it")
                result.update(wake=False, wake_note=blocked)
            else:
                self._watch(workflow_id, execution_id, session_id, user_id)
                armed = True
                result.update(wake=True, wake_note=f"you are woken when the run ends: give the user execution id "
                                                   f"{execution_id} and end your turn, then read it with "
                                                   f"n8n_get_execution. A one-shot agent-cli run is never woken.")
        await status.end(f"{workflow_id}: {method} webhook {result['http_status'] or 'no answer'}, execution "
                         f"{execution_id or '?'} {state or 'not found'}" + (", wake armed" if armed else ""))
        return result

    async def _executions(self, workflow_id: Any, limit: int, state: Any = None) -> list[dict]:
        """Executions newest first. Without a state the plain list leaves out
        what still runs (M-MCP-67), so running and waiting ones are asked for too."""
        seen: dict[str, dict] = {}
        for query in ([{"status": state}] if state else [{"status": "running"}, {"status": "waiting"}, {}]):
            page = await self._n8n().api_get("/executions", {"workflowId": workflow_id, "limit": limit, **query})
            for item in _items(page):
                seen[str(item.get("id"))] = item
        return sorted(seen.values(), key=lambda e: _as_int(e.get("id")), reverse=True)[:limit]

    async def _executions_now(self, workflow_id: str) -> list[dict]:
        return await self._executions(workflow_id, 20)

    async def _find_execution(self, workflow_id: str, before: int, response) -> tuple:
        """(id, correlation, candidates) of the run a webhook call started:
        exact when the workflow answered with its executionId (design §5.5),
        else the one new non-manual run since the call."""
        try:
            body = response.json() if response is not None else None
        except (ValueError, RecursionError):
            body = None
        # The answer is the workflow's own output, which may carry anything:
        # only a plain id of a run this call can have started is believed.
        claimed = str(body.get("executionId") or "") if isinstance(body, dict) else ""
        if claimed.isascii() and claimed.isdigit() and len(claimed) <= 18 and int(claimed) > before:
            try:
                execution = await self._n8n().api_get(f"/executions/{claimed}")
            except N8nError:
                execution = None            # a bogus claim never stops the lookup below
            if isinstance(execution, dict) and str(execution.get("workflowId")) == str(workflow_id) \
                    and execution.get("mode") == "webhook":
                return claimed, "exact", []
        for attempt in range(3):
            # A production webhook call starts a run in mode webhook (M-MCP-67);
            # schedule, sub-workflow and test runs are someone else's.
            fresh = sorted(str(e.get("id")) for e in await self._executions_now(workflow_id)
                           if _as_int(e.get("id")) > before and e.get("mode") == "webhook")
            if len(fresh) == 1:
                return fresh[0], "heuristic", []
            if fresh:
                return None, None, fresh
            if attempt < 2:
                await asyncio.sleep(LOOKUP_PAUSE_S)
        return None, None, []

    async def _is_sub_agent(self, session_id: str, user_id: str) -> bool:
        """wake_blocked leaves this out (core/session_presence.py): a sub-agent's
        session is never woken, the run that spawned it hands its result over.
        Asked off the loop, as it parses the session file."""
        presence = presence_for(self.system_config)
        if presence is None:
            return False
        try:
            state = await asyncio.to_thread(presence.get, session_id, user_id)
        except Exception as exc:  # noqa: BLE001 - after the webhook call nothing may raise
            logger.warning("n8n: could not tell whether session %s is a sub-agent's: %s", session_id, exc)
            return False
        return bool(state and state.get("sub_agent"))

    def _watch(self, workflow_id: str, execution_id: str, session_id: str, user_id: str) -> None:
        key = self._watch_key(workflow_id, execution_id, session_id)
        entry: dict = {"status": None, "read": False}
        self._watched[key] = entry
        _touch(self._mark_path("watch", key))
        task = asyncio.create_task(self._run_watch(key, execution_id, entry, session_id, user_id))
        self._watches.add(task)
        task.add_done_callback(self._watches.discard)

    async def _run_watch(self, key: str, execution_id: str, entry: dict, session_id: str, user_id: str) -> None:
        """Waits for the end, records it, then rings the session unless the end
        was read already. n8n holds the run; the woken run -- a new process --
        reads it by id and gets the record with it (design §6.2)."""
        try:
            outcome = await wait_for_end(lambda: self._n8n().api_get(f"/executions/{execution_id}"),
                                         max_s=self.watch_max_hours * 3600)
            entry.update(outcome)
            record = {"status": outcome["status"]}
            if outcome.get("note"):
                record["note"] = outcome["note"] + ("; the watch has ended, no further wake comes"
                                                    if outcome["status"] in ("unknown", "not_found") else "")
            logger.info("n8n execution %s ended for the watch: %s", execution_id, record)
            store = self._store()
            if store is not None:
                await store.set(f"exec:{key}", record, ttl=WATCH_RECORD_TTL_S)
            if self._was_read(key, entry):
                return
            await wake_session(self.system_config, session_id, user_id,
                               what=f"n8n execution {execution_id} ({outcome['status']})",
                               still_needed=lambda: not self._was_read(key, entry))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken watch costs its wake, never the process
            logger.exception("n8n watch of execution %s failed", execution_id)
        finally:
            self._watched.pop(key, None)
            _remove(self._mark_path("watch", key))
            await self._sweep()

    def _watch_key(self, workflow_id: Any, execution_id: Any, session_id: Any) -> Optional[str]:
        """One session's watch of one run on this n8n instance: the name of its
        record and marks, so an old mark of another instance, workflow or session
        silences nothing. None when a part could make an unsafe file name."""
        workflow_id, execution_id, session_id = str(workflow_id), str(execution_id), str(session_id)
        if not (_WORKFLOW_ID.fullmatch(workflow_id) and _EXECUTION_ID.fullmatch(execution_id)
                and _SESSION_ID.fullmatch(session_id)):
            return None
        instance = hashlib.sha256(self.base_url.encode()).hexdigest()[:8]
        return f"{instance}-{workflow_id}-{execution_id}-{session_id}"

    def _store(self) -> Optional[PluginCache]:
        """Watch records and marks, visible to every process: a woken run is a new
        one (core/session_presence.py). Best effort: without a writable cache a
        watch still rings, it only cannot see a read in another process."""
        if self._outcomes is None:
            try:
                self._outcomes = PluginCache(self.name, cache_dir=CACHE_ROOT)
            except OSError as exc:
                logger.warning("n8n: no watch records, the cache is not writable: %s", exc)
                return None
        return self._outcomes

    def _mark_path(self, kind: str, key: Optional[str]) -> Optional[Path]:
        store = self._store() if key else None
        return store.cache_dir / kind / key if store is not None else None

    async def _watch_record(self, key: Optional[str]) -> Optional[dict]:
        store = self._store() if key else None
        record = await store.get(f"exec:{key}") if store is not None else None
        return record if isinstance(record, dict) else None

    def _watched_somewhere(self, key: Optional[str]) -> bool:
        """A watch of this key runs here or in another process. Reads of runs
        nobody watches leave no mark."""
        mark = self._mark_path("watch", key)
        return key in self._watched or (mark is not None and mark.exists())

    def _mark_read(self, key: Optional[str]) -> None:
        entry = self._watched.get(key)
        if entry is not None:
            entry["read"] = True
        _touch(self._mark_path("read", key))

    def _was_read(self, key: str, entry: dict) -> bool:
        """Synchronous, as wake_session's still_needed must be: read here, or in
        another process (the woken run)."""
        mark = self._mark_path("read", key)
        return bool(entry["read"] or (mark is not None and mark.exists()))

    async def _sweep(self) -> None:
        """Marks and records past their TTL. PluginCache drops a record only when
        its key is read again, and nobody reads an old watch's key."""
        store = self._store()
        if store is None:
            return
        await store.cleanup_expired()
        cutoff = time.time() - WATCH_RECORD_TTL_S
        for kind in ("read", "watch"):
            try:
                folder = store.cache_dir / kind
                for mark in folder.glob("*") if folder.is_dir() else []:
                    if mark.stat().st_mtime < cutoff:
                        mark.unlink()
            except OSError as exc:
                logger.debug("n8n: %s marks not swept: %s", kind, exc)


def _touch(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as exc:
        logger.warning("n8n: could not write %s: %s", path, exc)


def _remove(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("n8n: could not remove %s: %s", path, exc)


def _items(page: Any) -> list[dict]:
    items = page.get("data") if isinstance(page, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _agents_wanting(system_config: Any, name: str) -> list[str]:
    """The enabled agents whose allowlist names this instance's tools."""
    servers = getattr(getattr(system_config, "plugins", None), "servers", None) or {}
    wanting = []
    for server, cfg in servers.items():
        allowed = getattr(getattr(getattr(cfg, "agent_config", None), "tools", None), "allowed", None) or []
        if getattr(cfg, "enabled", False) and any(str(p).lstrip("+").split("/")[0] == name for p in allowed):
            wanting.append(server)
    return sorted(wanting)


def _not_run(response) -> str:
    """Why n8n seems to have answered before any workflow ran, or "": its own
    "not registered" 404 (M-MCP-67) and the too-large codes. A workflow can
    answer these too, so the caller believes it only when no run turns up."""
    if response.status_code == 404 and "is not registered" in response.text:
        return "n8n does not know this webhook -- is the workflow still published?"
    if response.status_code in (413, 414, 431):
        return f"n8n refused the request as too large (HTTP {response.status_code}); nothing ran"
    return ""


def _as_int(value: Any) -> int:
    try:
        return int(str(value))
    except ValueError:
        return -1


def _webhooks(nodes: Any) -> list[dict]:
    """The enabled Webhook triggers among nodes: name, path, methods, authentication.
    httpMethod defaults to GET, with multipleMethods to GET and POST (F-NOD12)."""
    found = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict) or node.get("disabled") or node.get("type") != "n8n-nodes-base.webhook":
            continue
        parameters = node.get("parameters") if isinstance(node.get("parameters"), dict) else {}
        methods = parameters.get("httpMethod")
        if parameters.get("multipleMethods"):
            methods = methods if isinstance(methods, list) and methods else ["GET", "POST"]
        else:
            methods = [methods if isinstance(methods, str) and methods else "GET"]
        found.append({"node": node.get("name"), "path": str(parameters.get("path") or "").strip("/"),
                      "methods": [str(m).upper() for m in methods],
                      "auth": str(parameters.get("authentication") or "none")})
    return found


def _trigger_target(workflow: dict, node_name: Any, method: Any) -> tuple[dict, str]:
    """The published Webhook trigger to call, and the method; N8nError says why none fits."""
    active = workflow.get("activeVersion")
    if not workflow.get("activeVersionId") or not isinstance(active, dict):
        raise N8nError(f"workflow {workflow.get('id')} is not published; publishing it is the user's decision "
                       f"(n8n_publish_workflow)")
    hooks = [h for h in _webhooks(active.get("nodes")) if not node_name or h["node"] == node_name]
    if not hooks:
        raise N8nError("the published version has no enabled Webhook trigger"
                       + (f" named {_short(node_name)}" if node_name else "")
                       + "; only webhook workflows are started from here, a schedule runs by itself")
    if len(hooks) > 1:
        raise N8nError("the published version has several Webhook triggers; name one in webhook_node")
    hook = hooks[0]
    if hook["auth"] != "none":
        raise N8nError(f"the webhook requires {_short(hook['auth'])}; ScarabHive holds no credential for it")
    problem = webhook_path_problem(hook["path"]) if hook["path"] else "the webhook path is empty"
    if problem:
        raise N8nError(problem)
    if ":" in hook["path"]:
        raise N8nError("the webhook path has route parameters (:name); call it from the n8n editor")
    wanted = str(method).upper() if method else ""
    if wanted and wanted not in hook["methods"]:
        raise N8nError(f"the webhook takes {', '.join(hook['methods'])}, not {_short(wanted)}")
    if not wanted and len(hook["methods"]) > 1:
        raise N8nError(f"the webhook takes {', '.join(hook['methods'])}; pick one in method")
    return hook, wanted or hook["methods"][0]


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
