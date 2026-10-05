"""Our policy and the checks n8n measurably leaves out -- pure functions.

Every check here exists because n8n's own validators MISS the case (measured,
docs/n8n_facts.md) or because our policy demands it. Where n8n catches a case
itself -- unknown node type, invalid option values, missing required
parameters -- there is no check here, and an upgrade that closes one of the
gaps below makes the matching check redundant (design §5.3).

All type comparisons go through ``normalize_type``: many nodes exist as an
agent-tool variant (``gitTool`` for ``git``), and a list that names ``git``
must catch both (M-MCP-42).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

DEFAULT_BLOCKED_NODE_TYPES = (
    "n8n-nodes-base.executeCommand", "n8n-nodes-base.ssh",
    "n8n-nodes-base.localFileTrigger", "n8n-nodes-base.readWriteFile",
    "n8n-nodes-base.readBinaryFile", "n8n-nodes-base.readBinaryFiles",
    "n8n-nodes-base.writeBinaryFile", "n8n-nodes-base.function",
    "n8n-nodes-base.functionItem", "n8n-nodes-base.git", "n8n-nodes-base.n8n",
    "@n8n/n8n-nodes-langchain.code", "@n8n/n8n-nodes-langchain.toolHttpRequest",
)
DEFAULT_REVIEW_NODE_TYPES = (
    "n8n-nodes-base.code", "@n8n/n8n-nodes-langchain.toolCode",
    "n8n-nodes-base.httpRequest", "n8n-nodes-base.graphql", "n8n-nodes-base.rssFeedRead",
    "n8n-nodes-base.ftp", "n8n-nodes-base.executeWorkflow",
    "@n8n/n8n-nodes-langchain.toolWorkflow", "@n8n/n8n-nodes-langchain.mcpRegistryClientTool",
    "@n8n/n8n-nodes-langchain.mcpClient",
)
# Nodes without any effect outside the execution. Extended only with a reason.
LOCAL_NODE_TYPES = frozenset({
    "n8n-nodes-base.set", "n8n-nodes-base.if", "n8n-nodes-base.switch",
    "n8n-nodes-base.filter", "n8n-nodes-base.noOp", "n8n-nodes-base.respondToWebhook",
    "n8n-nodes-base.splitOut", "n8n-nodes-base.aggregate", "n8n-nodes-base.limit",
    "n8n-nodes-base.dateTime", "n8n-nodes-base.sort", "n8n-nodes-base.merge",
})
HTTP_URL_PARAMETER = {
    "n8n-nodes-base.httpRequest": "url",
    "n8n-nodes-base.graphql": "endpoint",
    "n8n-nodes-base.rssFeedRead": "url",
}
# Code reaches the network through this.helpers.httpRequest (M-MCP-38), so a
# host list cannot bound it: it never runs live in a test.
CODE_TYPES = frozenset({"n8n-nodes-base.code", "@n8n/n8n-nodes-langchain.toolCode"})
SUB_WORKFLOW_TYPES = frozenset({"n8n-nodes-base.executeWorkflow", "@n8n/n8n-nodes-langchain.toolWorkflow"})
# n8n's validate_workflow says valid:true while these warnings describe real
# errors (M-MCP-8). Extended only with a measured case.
N8N_WARNING_ERRORS = frozenset({"MISSING_REQUIRED_INPUT", "INVALID_PARAMETER", "SET_INVALID_ASSIGNMENT"})
# Triggers whose name does not end in "Trigger" (n8n's group 'trigger', work/nodes.json).
_TRIGGER_TYPES = frozenset({"n8n-nodes-base.webhook", "n8n-nodes-base.cron", "n8n-nodes-base.interval",
                            "n8n-nodes-base.emailReadImap"})

# Characters on which Python's urlparse and n8n's WHATWG parser read a
# different host: "http://evil\@allowed/" is allowed to one, evil to the other.
# Refused anywhere in the URL; the rest only in the host part (fuzzed against
# Node's new URL and url.parse: no disagreement left).
_UNSAFE_URL = re.compile(r"[\\\x00-\x1f\x7f]")
_UNSAFE_NETLOC = re.compile(r"[\s@%]")
_VERSION_NOT_FOUND = re.compile(r"Version '([^']+)' not found for node '([^']+)'")
_CODE_TYPE_LITERAL = re.compile(r"""\btype\s*:\s*['"]([^'"]+)['"]""")
_CODE_INLINE_SOURCE = re.compile(r"""\bsource\s*:\s*['"](parameter|localFile|url)['"]""")


def url_host(url: str) -> Optional[str]:
    """The host of a plain http(s) URL, or None where parsers could disagree:
    a backslash or control character anywhere, or user info, whitespace, a
    percent sign or a non-ASCII character in the host part."""
    if _UNSAFE_URL.search(url):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    netloc = parsed.netloc
    if parsed.scheme not in ("http", "https") or not netloc.isascii() or _UNSAFE_NETLOC.search(netloc):
        return None
    return normalize_host(parsed.hostname or "") or None


def normalize_host(host: str) -> str:
    """How allowed_hosts entries and URL hosts are compared: lower case, no
    trailing dot, no IPv6 brackets. The port is not part of it."""
    return str(host).strip().lower().strip("[]").rstrip(".")


def normalize_type(node_type: str) -> str:
    """``n8n-nodes-base.gitTool`` -> ``n8n-nodes-base.git``; everything else unchanged."""
    return node_type[:-4] if node_type.endswith("Tool") and len(node_type) > 4 else node_type


def normalized(types: Iterable[str]) -> frozenset:
    return frozenset(normalize_type(t) for t in types)


def is_trigger_type(node_type: str) -> bool:
    return node_type.endswith("Trigger") or node_type in _TRIGGER_TYPES


# A webhook path becomes part of the production URL. A dot segment, "?", "#",
# "%" or a backslash would send a call to another n8n URL (review, Phase 1b).
_WEBHOOK_PATH = re.compile(r"[A-Za-z0-9_~:.-]+(/[A-Za-z0-9_~:.-]+)*")


def webhook_path_problem(path: str) -> str:
    """Why a webhook path is unsafe to call, or ""."""
    trimmed = path.strip("/")
    if not _WEBHOOK_PATH.fullmatch(trimmed) or any(s in (".", "..") for s in trimmed.split("/")):
        return (f"webhook path {path[:60]!r}: only letters, digits and - _ . ~ : between single slashes, "
                f"and no . or .. segment")
    return ""


def finding(code: str, level: str, message: str, node: Optional[str] = None, fix_hint: str = "") -> dict:
    return {"code": code, "level": level, "node": node, "message": message, "fix_hint": fix_hint}


def format_version(version: Any) -> str:
    """How get_node_types wants a version: ``2.1`` -> "2.1", ``1.0`` -> "1" (M-MCP-32)."""
    if isinstance(version, float) and version.is_integer():
        return str(int(version))
    return str(version)


# ── code pre-check (a hint before anything is created; not the guard) ────

def code_precheck(code: str, blocked: frozenset) -> list[dict]:
    """Type literals in SDK code against the block list. A HINT only: code can
    assemble a type string, so the binding check runs on the stored workflow."""
    found = []
    for node_type in sorted(set(_CODE_TYPE_LITERAL.findall(code))):
        if normalize_type(node_type) in blocked:
            found.append(finding("BLOCKED_NODE", "error", f"node type {node_type} is blocked by policy",
                                 fix_hint="use a dedicated node without shell, file or code access"))
    for source in sorted(set(_CODE_INLINE_SOURCE.findall(code))):
        found.append(finding("EXECUTE_WORKFLOW_SOURCE", "error",
                             f"sub-workflow source '{source}' is not allowed",
                             fix_hint="use source 'database' with the id of a stored workflow"))
    return found


# ── n8n's own validators, read correctly ──────────────────────────────────

def classify_workflow_validation(payload: dict) -> tuple[list[dict], list[dict]]:
    """validate_workflow -> (errors, warnings). ``valid`` alone never decides."""
    errors = [finding("N8N_ERROR", "error", str(e)) for e in payload.get("errors") or []]
    warnings = []
    for w in payload.get("warnings") or []:
        code = w.get("code", "N8N_WARNING")
        # Decided once: the list a finding lands in and its level must agree.
        level = "error" if code in N8N_WARNING_ERRORS else "warning"
        item = finding(code, level, str(w.get("message", "")), node=w.get("nodeName"))
        (errors if level == "error" else warnings).append(item)
    return errors, warnings


def classify_node_validation(payload: dict) -> list[dict]:
    """validate_node_config -> errors, one per failing parameter."""
    errors = []
    for result in payload.get("results") or []:
        for e in result.get("errors") or []:
            errors.append(finding("N8N_NODE_CONFIG", "error", str(e.get("message", "")),
                                  node=result.get("name") or result.get("type"),
                                  fix_hint=str(e.get("path", ""))))
    return errors


def node_type_errors(text: str, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """``# Errors`` lines of a get_node_types answer, attributed to (type, version).

    The section can stand anywhere in an otherwise valid answer (M-MCP-44),
    so the whole text is searched, not just its start."""
    pairs = set(pairs)
    start = text.find("# Errors")
    if start < 0:
        return {}
    section = text[start + len("# Errors"):]
    end = section.find("\n# ")
    section = section if end < 0 else section[:end]
    errors: dict[tuple[str, str], str] = {}
    for line in section.splitlines():
        line = line.strip(" -*\t")
        if not line:
            continue
        match = _VERSION_NOT_FOUND.search(line)
        if match:
            key = (match.group(2), match.group(1))
            if key in pairs:
                errors[key] = line
            continue
        for node_type, version in pairs:
            if node_type in line:
                errors.setdefault((node_type, version), line)
    return errors


# ── the stored workflow ───────────────────────────────────────────────────

def _nodes(workflow: dict) -> list[dict]:
    return [n for n in workflow.get("nodes") or [] if isinstance(n, dict)]


def _successors(workflow: dict, name: str) -> list[str]:
    out = []
    for branch in (workflow.get("connections") or {}).get(name, {}).get("main") or []:
        for conn in branch or []:
            if isinstance(conn, dict) and conn.get("node"):
                out.append(conn["node"])
    return out


def _reaches(workflow: dict, start: str, node_type: str) -> bool:
    types = {n.get("name"): n.get("type") for n in _nodes(workflow)}
    seen, todo = {start}, [start]
    while todo:
        for nxt in _successors(workflow, todo.pop()):
            if nxt in seen:
                continue
            if types.get(nxt) == node_type:
                return True
            seen.add(nxt)
            todo.append(nxt)
    return False


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _static_workflow_id(parameters: dict) -> Optional[str]:
    """The id a sub-workflow node calls, or None when it is an expression."""
    ref = parameters.get("workflowId")
    if isinstance(ref, dict):
        ref = ref.get("value")
    if isinstance(ref, (int, float)):
        ref = str(ref)
    if not isinstance(ref, str) or not ref or ref.startswith("="):
        return None
    return ref


def callee_ids(nodes: Any, settings: Any) -> list[str]:
    """The workflows a run of these nodes can start by a fixed id: enabled
    sub-workflow nodes reading from the database, and the error workflow."""
    ids = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict) or node.get("disabled") \
                or normalize_type(node.get("type", "")) not in SUB_WORKFLOW_TYPES:
            continue
        params = node.get("parameters") if isinstance(node.get("parameters"), dict) else {}
        callee = _static_workflow_id(params) if params.get("source", "database") == "database" else None
        if callee:
            ids.append(callee)
    error_workflow = settings.get("errorWorkflow") if isinstance(settings, dict) else None
    if isinstance(error_workflow, str) and error_workflow:
        ids.append(error_workflow)
    return ids


def check_workflow(workflow: dict, *, blocked: frozenset, review: frozenset,
                   credentials: Optional[dict[str, str]] = None,
                   type_errors: Optional[dict[tuple[str, str], str]] = None) -> list[dict]:
    """Policy and gap checks on a stored workflow (public GET shape).

    ``credentials`` maps credential id -> type (from list_credentials); None
    skips the credential checks. ``type_errors`` comes from node_type_errors.
    """
    found: list[dict] = []
    type_errors = type_errors or {}
    for node in _nodes(workflow):
        name, node_type = node.get("name"), node.get("type", "")
        base = normalize_type(node_type)
        params = node.get("parameters") or {}
        if base in blocked:
            found.append(finding("BLOCKED_NODE", "error", f"{node_type} is blocked by policy", name,
                                 "use a dedicated node without shell, file or code access"))
        elif base in review:
            found.append(finding("REVIEW_NODE", "warning", f"{node_type} needs a human look before publishing", name))
        error = type_errors.get((node_type, format_version(node.get("typeVersion"))))
        if error:
            found.append(finding("UNKNOWN_TYPE_VERSION", "error", error, name,
                                 "use a version get_node_types knows"))
        if base in SUB_WORKFLOW_TYPES:
            source = params.get("source", "database")
            if source != "database" or _static_workflow_id(params) is None:
                found.append(finding("EXECUTE_WORKFLOW_SOURCE", "error",
                                     "a sub-workflow must come from the database with a fixed id "
                                     f"(source={source!r})", name,
                                     "store the sub-workflow and reference its id"))
        if node_type == "n8n-nodes-base.webhook":
            path = str(params.get("path") or "")
            if not path.strip():
                found.append(finding("WEBHOOK_PATH_EMPTY", "error", "webhook path is empty", name,
                                     "set a path; an empty one answers 404"))
            elif webhook_path_problem(path):
                found.append(finding("WEBHOOK_PATH_UNSAFE", "error", webhook_path_problem(path), name,
                                     "use a plain path such as orders or orders/new"))
            if params.get("responseMode") == "responseNode" and \
                    not _reaches(workflow, name, "n8n-nodes-base.respondToWebhook"):
                found.append(finding("RESPOND_NODE_MISSING", "error",
                                     "responseMode responseNode without a Respond to Webhook node after it",
                                     name, "add a Respond to Webhook node, or use responseMode lastNode"))
        for value in _strings(params):
            if value.startswith("=") and value.count("{{") != value.count("}}"):
                found.append(finding("EXPRESSION_UNBALANCED", "error",
                                     f"unbalanced expression {value[:80]!r}", name,
                                     "close every {{ with }}; an open one evaluates to null silently"))
                break
        if credentials is not None:
            for cred_type, ref in (node.get("credentials") or {}).items():
                cred_id = str((ref or {}).get("id") or "")
                if cred_id not in credentials:
                    found.append(finding("CREDENTIAL_UNKNOWN_ID", "error",
                                         f"credential {cred_id!r} for {cred_type} does not exist", name,
                                         "use an id from list_credentials, or leave it for the user"))
                elif credentials[cred_id] != cred_type:
                    found.append(finding("CREDENTIAL_TYPE_MISMATCH", "error",
                                         f"credential {cred_id!r} is a {credentials[cred_id]}, not a {cred_type}",
                                         name, "use a credential of the type the node expects"))
    return found


def blocking_save_settings(workflow: dict) -> list[str]:
    """Settings that leave a test run without a stored execution (M-MCP-41).

    Only saveManualExecutions: a test run is a manual one. saveData*Execution
    'none' still stores it (executions 137, 139)."""
    settings = workflow.get("settings") or {}
    return ["saveManualExecutions"] if settings.get("saveManualExecutions") is False else []


# ── the pin plan: what a test run may execute for real ────────────────────

@dataclass
class PinPlan:
    pin_data: dict = field(default_factory=dict)
    runs: dict = field(default_factory=dict)      # node name -> {"run": pinned|live|sub, "reason": ...}
    trigger: Optional[str] = None                 # the trigger the run starts from
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _items(value: Any) -> list[dict]:
    items = value if isinstance(value, list) else [value]
    wrapped = []
    for item in items:
        if isinstance(item, dict) and "json" in item:
            wrapped.append(item)
        elif isinstance(item, dict):
            wrapped.append({"json": item})
        else:
            wrapped.append({"json": {"value": item}})
    return wrapped


def _is_local(node: dict) -> bool:
    base = normalize_type(node.get("type", ""))
    params = node.get("parameters") or {}
    if base not in LOCAL_NODE_TYPES:
        return False
    if base == "n8n-nodes-base.sort" and params.get("type") == "code":
        return False    # runs its own JavaScript live (M-MCP-43)
    if base == "n8n-nodes-base.merge" and params.get("mode") == "combineBySql":
        return False
    return True


def live_check(node: dict, *, allowed_hosts: frozenset, live_node_types: frozenset) -> tuple[bool, str]:
    """May a node the builder named in live_nodes really run? (design §3.3)"""
    base = normalize_type(node.get("type", ""))
    params = node.get("parameters") or {}
    if base in CODE_TYPES:
        return False, "code never runs live in a test: it has network access"
    if base in SUB_WORKFLOW_TYPES:
        # The sub-workflow's own nodes run outside this test's pin plan, so no
        # check on the caller can bound them (executions 135/136). A pinned
        # caller does not start it (execution 138).
        return False, "a sub-workflow never runs live in a test: its nodes would run unpinned"
    if base in HTTP_URL_PARAMETER:
        url = params.get(HTTP_URL_PARAMETER[base])
        if not isinstance(url, str) or not url or url.startswith("="):
            return False, "its URL is an expression, so its host cannot be checked"
        host = url_host(url)
        if host is None:
            return False, ("its URL is not a plain http(s) URL (backslash, control character, user info "
                           "or an unusual host), so its host cannot be checked")
        if host not in allowed_hosts:
            return False, f"host {host} is not in allowed_hosts"
        options = params.get("options") or {}
        if not isinstance(options, dict) or options.get("proxy") or options.get("pagination") \
                or options.get("sendCredentialsOnCrossOriginRedirect"):
            return False, "a proxy, pagination or a credential sent on redirect can reach another host"
        if node.get("credentials") and base not in live_node_types:
            # The credential travels with the request, and an allowed host is
            # not the service it belongs to (design §8.3).
            return False, "it carries a credential; with one it runs live only if its type is in live_node_types"
        return True, f"host {host} is in allowed_hosts"
    if base in live_node_types:
        return True, "type allowed live by the operator (live_node_types)"
    return False, "type not in live_node_types, the operator's list"


def pin_plan(workflow: dict, *, trigger_node: Optional[str] = None, trigger_input: Any = None,
             mocks: Optional[dict] = None, live_nodes: Iterable[str] = (),
             allowed_hosts: Iterable[str] = (), live_node_types: Iterable[str] = ()) -> PinPlan:
    """Pin everything that could act outside the execution, unless it is local
    or passes the live check. The server pins nothing by itself (M-MCP-30):
    this plan is the only line between a test and the outside world."""
    plan = PinPlan()
    mocks = mocks or {}
    live_nodes = set(live_nodes)
    hosts, live_types = frozenset(normalize_host(h) for h in allowed_hosts), normalized(live_node_types)
    nodes = {n["name"]: n for n in _nodes(workflow) if n.get("name") and not n.get("disabled")}

    for name in sorted(set(mocks) | live_nodes):
        if name not in nodes:
            plan.warnings.append(f"{name!r} is not a node of this workflow")

    # ai_* connections hang subnodes (model, tools, memory) under a root node.
    subnodes: dict[str, set] = {}
    main_targets: set = set()
    for source, outputs in (workflow.get("connections") or {}).items():
        for kind, branches in (outputs or {}).items():
            for branch in branches or []:
                for conn in branch or []:
                    if not (isinstance(conn, dict) and conn.get("node")):
                        continue
                    if kind.startswith("ai_"):
                        subnodes.setdefault(conn["node"], set()).add(source)
                    else:
                        main_targets.add(conn["node"])
    # A node on the main path runs there, whatever else it is wired to.
    sub_only = (set().union(*subnodes.values()) if subnodes else set()) - main_targets

    def subtree(root: str) -> list[str]:
        """Every enabled node under a root, the tools of a tool included."""
        seen, todo = set(), [root]
        while todo:
            for sub in subnodes.get(todo.pop(), ()):
                if sub in nodes and sub not in seen:
                    seen.add(sub)
                    todo.append(sub)
        return sorted(seen - {root})

    triggers = [n for n in nodes if is_trigger_type(nodes[n].get("type", ""))]
    if trigger_node and trigger_node not in triggers:
        plan.errors.append(f"trigger_node {trigger_node!r} is not a trigger of this workflow")
    chosen = trigger_node or (triggers[0] if triggers else None)
    plan.trigger = chosen if chosen in triggers else None
    for name in sorted(set(mocks) & sub_only - set(triggers)):
        plan.warnings.append(f"mock for {name!r} is ignored: a subnode runs only inside its root, pin the root")

    def may_run(node_name: str) -> tuple[bool, str]:
        node = nodes[node_name]
        if _is_local(node):
            return True, "local node, no effect outside the execution"
        if node_name in live_nodes:
            return live_check(node, allowed_hosts=hosts, live_node_types=live_types)
        return False, "pinned by default; name it in live_nodes to run it for real"

    for name, node in nodes.items():
        if name in triggers:
            value = trigger_input if name == chosen and trigger_input is not None else mocks.get(name)
            if value is None:
                value = [{}]
                plan.warnings.append(f"trigger {name!r} runs with an empty item; pass trigger_input")
            plan.pin_data[name] = _items(value)
            plan.runs[name] = {"run": "pinned", "reason": "triggers are always pinned"}
            continue
        if name in sub_only:
            plan.runs[name] = {"run": "sub", "reason": "subnode: runs only when its root runs"}
            continue
        # A root runs live only if it may itself: a stray ai_* edge must not
        # make any node live, and a root can act on its own (vector store insert).
        ok, reason = may_run(name)
        if ok and name in subnodes:
            blockers = [s for s in subtree(name) if not may_run(s)[0]]
            ok, reason = (True, "it and every subnode may run live") if not blockers else \
                (False, f"subnode {blockers[0]!r} may not run live: {may_run(blockers[0])[1]}")
        if ok:
            plan.runs[name] = {"run": "live", "reason": reason}
            continue
        if name not in mocks:
            plan.warnings.append(f"{name!r} is pinned with an empty item; pass mocks[{name!r}]")
        plan.pin_data[name] = _items(mocks.get(name, [{}]))
        plan.runs[name] = {"run": "pinned", "reason": reason}
    return plan
