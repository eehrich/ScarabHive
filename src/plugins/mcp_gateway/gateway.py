"""The gateway's two MCP methods: tools/list and tools/call, for the grant the request's token names.

Every call goes through the runner agent's ``dispatch_tool_call``: its allowlist is the ceiling, its
pre_tool_call/post_tool_call hooks run (``hook_source="mcp_gateway"``), the tool gets the grant's user, session and
request id like any call of an agent. What a grant may see is what it may call -- one function decides both.
"""
from __future__ import annotations

import asyncio
import copy
import fnmatch
import json
import logging
import time
from typing import Any, Optional

from mcp import types

from . import grants
from .grants import Grant

logger = logging.getLogger(__name__)

SOURCE = "mcp_gateway"


class Gateway:
    def __init__(self, name: str, runner_agent: str, max_result_chars: int):
        self.name = name
        self.runner_agent = runner_agent
        self.max_result_chars = max_result_chars

    def runner(self) -> Any:
        from agent_system import app_state

        registry = app_state.app_registry
        try:
            return registry.get(self.runner_agent) if registry is not None else None
        except KeyError:
            return None

    async def visible(self, grant: Grant, runner: Any) -> dict[str, tuple[str, dict[str, Any]]]:
        """name -> (description, parameters as the tool declares them) of every tool the grant may call: the
        runner's own list (allowlist, blocklist, every tool interface), narrowed to the grant's patterns. A tool
        that does not take every pinned parameter is left out -- it could not be held to the run's values."""
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        for entry in await runner.build_llm_tool_schemas():
            function = (entry.get("function") or {}) if isinstance(entry, dict) else {}
            name = function.get("name")
            if not isinstance(name, str) or not any(fnmatch.fnmatchcase(name, p) for p in grant.tools):
                continue
            if runner._resolve_flat_tool_name(name)[0] is None:
                continue  # an external MCP tool: programmatic dispatch cannot run it
            parameters = function.get("parameters") or {"type": "object", "properties": {}}
            if any(key not in (parameters.get("properties") or {}) for key in grant.pinned):
                continue
            found[name] = (str(function.get("description") or ""), parameters)
        return found

    async def list_tools(self, grant: Grant) -> types.ListToolsResult:
        runner = self.runner()
        tools = []
        for name, (description, parameters) in sorted((await self.visible(grant, runner)).items() if runner else []):
            shown = copy.deepcopy(parameters)  # the pinned ones are the run's, not the model's to choose
            for key in grant.pinned:
                (shown.get("properties") or {}).pop(key, None)
            if grant.pinned and isinstance(shown.get("required"), list):
                shown["required"] = [key for key in shown["required"] if key not in grant.pinned]
            tools.append(types.Tool(name=name, description=description, input_schema=shown))
        return types.ListToolsResult(tools=tools)

    async def call_tool(self, grant: Grant, name: str, arguments: Optional[dict[str, Any]]) -> types.CallToolResult:
        started, failed = time.monotonic(), True
        try:
            text, failed = await self._call(grant, name, dict(arguments or {}))
        finally:  # a call that raises is a call too
            logger.info("mcp_gateway %s: grant %s %s %s in %d ms", self.name, grant.key, name,
                        "error" if failed else "ok", (time.monotonic() - started) * 1000)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=failed)

    async def _call(self, grant: Grant, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """(the answer's text, whether it is an error)."""
        from agent_system.servers.agent.components.tool_execution import ToolDispatchError

        runner = self.runner()
        if runner is None:
            return f"The gateway's runner agent {self.runner_agent!r} is not available.", True
        visible = await self.visible(grant, runner)
        if name not in visible:
            return f"Unknown tool {name!r}: call one from tools/list.", True
        parameters = visible[name][1]
        for key, value in grant.pinned.items():
            if key in arguments and arguments[key] != value:
                return (f"{key} is fixed to {value!r} for this run; this call asked for {arguments[key]!r}. "
                        f"Leave {key} out."), True
            arguments.pop(key, None)
        injected = dict(grant.pinned)
        complete = {**arguments, **injected}
        problem = grants.refusal(grant, name, complete) or _schema_problem(name, parameters, complete)
        if problem:
            return problem, True
        from agent_system.core.request_context import current_run_user
        from agent_system.tools.status import current_request_id

        request_id = grant.next_request_id()
        grant.loop = asyncio.get_running_loop()
        # Whose call this is, for what the tool does without an agent at hand (a status without an id, an LLM call
        # reported through the hooks): a run of an agent sets both, and the task copies them at its creation.
        reset = (current_request_id.set(request_id), current_run_user.set(grant.user_id))
        try:
            call = asyncio.ensure_future(runner.dispatch_tool_call(
                name, arguments, session_id=grant.session_id, user_id=grant.user_id, request_id=request_id,
                hook_source=SOURCE, cancellation_token=grant.cancel, injected_params=injected or None))
        finally:
            current_request_id.reset(reset[0])
            current_run_user.reset(reset[1])
        # The tool does not watch the grant's cancellation token (dispatch hands it to the hooks only): a revoked
        # grant, or one whose time runs out while the call runs, stops the call here, as a client that goes away
        # does.
        revoked = asyncio.ensure_future(grant.cancel.wait_for_cancellation())
        try:
            await asyncio.wait({call, revoked}, timeout=max(0.0, grant.expires_at - time.monotonic()),
                               return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            call.cancel()
            raise
        finally:
            revoked.cancel()
        if not call.done():
            call.cancel()
            await asyncio.wait({call})
            return f"{name}: the run's token was revoked or expired; the call was stopped.", True
        try:
            result = call.result()
        except ToolDispatchError as error:
            return str(error), True
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > self.max_result_chars:
            return (f"{name}: the result has {len(text)} characters, more than the {self.max_result_chars} this "
                    "gateway hands on. Ask for less (fewer ids, a line range, a narrower query)."), True
        failed = isinstance(result, dict) and (result.get("status") == "error"
                                               or (result.get("error") and result.get("status") is None))
        return text, bool(failed)


def _schema_problem(name: str, parameters: dict[str, Any], arguments: dict[str, Any]) -> Optional[str]:
    """What is wrong with ``arguments`` for the tool's parameters, or None. The framework does not check tool
    arguments; a CLI's model should read the mistake here, not three hops down in the tool."""
    from jsonschema import Draft202012Validator
    from referencing import Registry

    properties = parameters.get("properties") or {}
    unknown = [key for key in arguments if properties and key not in properties]
    if unknown:
        return f"{name}: unknown parameter(s) {unknown}. Valid: {sorted(properties)}"
    try:
        # An empty registry: a $ref the schema does not hold itself is never fetched.
        error = next(iter(Draft202012Validator(parameters, registry=Registry()).iter_errors(arguments)), None)
    except Exception as exc:  # noqa: BLE001 -- a schema the validator cannot read is the tool's to answer for
        logger.debug("mcp_gateway: schema of %s not checked: %s", name, exc)
        return None
    if error is None:
        return None
    where = "/".join(str(part) for part in error.absolute_path) or "the arguments"
    return f"{name}: {where}: {error.message}"
