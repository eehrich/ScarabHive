"""Tool bindings of a run: parameters its tool calls are held to.

A run may carry ``{pattern: {parameter: value}}`` (``Agent.run_events(tool_bindings=...)``, a grant of the MCP
gateway): every call of a tool a pattern matches gets those values, and may ask for no other. Patterns are the
allowlist's (``tool_matches_patterns``: ``server/tool``, ``server/*``, a wildcard on the full path or the bare
name), and the bare tool name besides. Bindings only narrow -- the allowlist stays the ceiling -- and a run without them is not touched.

One function holds a call to them, whichever way the call comes: the model's own tool loop
(``ToolInvoker.execute_tool``, after the pre_tool_call hooks), ``dispatch_tool_call`` (scripts, hooks) and the
gateway, which dispatches through it. An external MCP tool a pattern matches does not run: nothing can be put into
its call.

What they do not reach: the runs a bound run starts -- an agent called as a tool, a sub-agent the model spawns --
start with bindings of their own (none, unless their caller passes some: AgentCaller's ``tool_bindings``). A run
that must stay inside its bindings gets no tool that starts another agent; its allowlist decides that. The model
sees a bound parameter in the tool's schema as before (schemas are shared by every session of an agent, for the
prompt cache); a call that asks for another value is refused with the value to leave out.

Every later run must be handed them again; nothing reads them from the session. The sub-agent manager keeps a
bound instance's in its metadata for its continues, and a sub-agent's session is never woken (SessionPresence.notify
leaves its input to the run that spawned it). A bound run in a session that can be woken would need them read from
its record -- a woken run gets none.
"""
from __future__ import annotations

from typing import Any, Collection, Optional

from ..tool_schema_builder import tool_matches_patterns
from .tool_call_contract import FRAMEWORK_REQUEST_ID_KEYS

Bindings = dict[str, dict[str, Any]]


def form_problem(bindings: Any) -> Optional[str]:
    """What is wrong with *bindings* as a run's bindings, or None. Values are JSON scalars; a key the framework
    owns (``_session_id``, request_id ...) cannot be bound -- the run's own values stand there."""
    if not isinstance(bindings, dict):
        return f"tool_bindings must be a mapping of tool pattern to parameters, not {type(bindings).__name__}"
    for pattern, values in bindings.items():
        if not isinstance(pattern, str) or not pattern:
            return f"tool_bindings: {pattern!r} is no tool pattern"
        if not isinstance(values, dict) or not values:
            return f"tool_bindings[{pattern!r}] must map parameters to values"
        for key, value in values.items():
            if not isinstance(key, str) or not key or key.startswith("_") or key in FRAMEWORK_REQUEST_ID_KEYS:
                return f"tool_bindings[{pattern!r}]: {key!r} cannot be bound"
            if not isinstance(value, (str, int, float, bool)):
                return f"tool_bindings[{pattern!r}][{key!r}] must be a string, number or boolean"
    return None


def values_for(bindings: Optional[Bindings], tool_name: str, server_name: Optional[str]) -> tuple[dict, Optional[str]]:
    """The values *tool_name* is held to (every matching pattern's, merged), and the conflict, if two patterns
    hold one parameter to different values."""
    values: dict[str, Any] = {}
    for pattern, bound in (bindings or {}).items():
        # The bare tool name too, which the allowlist reads as a server's: a gateway's `tools` name tools that way,
        # and a binding written alike would otherwise hold nothing. Matching more only binds more.
        if pattern != tool_name and not tool_matches_patterns(tool_name, server_name or "", [pattern]):
            continue
        for key, value in bound.items():
            if key in values and values[key] != value:
                return values, (f"{tool_name} is not callable in this run: its tool bindings hold {key} to both "
                                f"{values[key]!r} and {value!r}")
            values[key] = value
    return values, None


def hold(bindings: Optional[Bindings], tool_name: str, server_name: Optional[str], params: dict[str, Any],
         declared: Optional[Collection[str]]) -> tuple[dict[str, Any], Optional[str]]:
    """*params* with the bound values in, or the refusal the caller hands back instead of running the call.

    *declared* are the parameters the tool takes (None: unknown, an external tool). A bound tool that does not
    take a bound parameter is refused: it would ignore the value, and the binding would hold nothing."""
    values, problem = values_for(bindings, tool_name, server_name)
    if problem or not values:
        return params, problem
    missing = sorted(key for key in values if declared is None or key not in declared)
    if missing:
        return params, (f"{tool_name} is not callable in this run: it does not take {', '.join(missing)}, which "
                        "this run's tool bindings fix")
    for key, value in values.items():
        if key in params and params[key] != value:
            return params, (f"{key} is fixed to {value!r} for this run; this call asked for {params[key]!r}. "
                            f"Leave {key} out.")
    return {**params, **values}, None


def without_bound(parameters: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """A tool's parameter schema without the bound ones: they are the run's, not the caller's to choose."""
    shown = dict(parameters)
    properties = {k: v for k, v in (parameters.get("properties") or {}).items() if k not in values}
    if "properties" in parameters:
        shown["properties"] = properties
    if isinstance(parameters.get("required"), list):
        shown["required"] = [k for k in parameters["required"] if k not in values]
    return shown
