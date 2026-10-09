"""The agents and LLM profiles a client can pick, and what an agent reaches.

GET /agents (the picker: public agents the caller may run, with their details),
GET /llm/profiles, an agent's tools and allowlist (``/agents/{name}/...``), the
admin's diagnostics of both (``/agents/debug/{name}/...``) and GET /admin/config.
What answers a question about the configuration reads the live one
(``AppContext.live_config``); the role gate judges by the start config.

Registration order matters within this router: FastAPI matches in order, and
``/agents/{name}/...`` comes before ``/agents/debug/{name}/...`` as it always did.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from agent_system import app_state
from agent_system.api.app_context import AppContext, app_context, registry_of
# Module level: inside list_agents it would sit in a try/except that
# skips the server -- an import cycle would then empty the UI dropdown
# in silence instead of failing loud at start.
from agent_system.runtime import ServerView

logger = logging.getLogger(__name__)

router = APIRouter()


def _agent_or_error(agent_name: str, *, loud: bool = False) -> tuple[Any, Optional[dict]]:
    """The registered Agent named *agent_name*, or what the allowlist and debug routes answer
    instead: ``(None, {"error": ...})`` -- a 200, not a 404. ``loud``: the system prompt route
    logs a failed lookup as a warning, with its traceback."""
    try:
        srv = app_state.app_registry.get(agent_name)  # type: ignore[attr-defined]
    except Exception as e:
        if loud:
            logger.warning(f"Failed to get agent {agent_name}: {e}", exc_info=True)
        else:
            logger.debug(f"Failed to get agent {agent_name}: {e}")
        return None, {"error": "agent not found", "agent": agent_name}
    from agent_system.servers.agent.server import Agent as _Agent
    if not isinstance(srv, _Agent):
        return None, {"error": "not an agent", "agent": agent_name}
    return srv, None


@router.get("/admin/config")
def get_config(ctx: AppContext = Depends(app_context)):
    """Return the full system configuration (admin only).

    Live, not the start state: the endpoint whose whole job is to show the
    configuration must not report the state before the reload that just
    succeeded.
    """
    return ctx.live_config().model_dump()


def _public_agents() -> tuple[list[str], dict[str, str]]:
    """The registered agents GET /agents lists, and the role gate of those that carry one.

    Plain code on purpose: the endpoint runs it in the thread pool, as it ran the
    whole endpoint while that was a plain ``def`` -- the walk and the details read
    the config of every server, and on the event loop that stalls every stream.
    """
    agents = []
    #: name -> min_role of every listed agent that carries a gate
    gated: dict[str, str] = {}
    try:
        for name in app_state.app_registry.list():  # type: ignore[attr-defined]
            try:
                # describe() first: it answers "is this an agent, is it
                # public" from the instance when there is one and from the
                # declaration otherwise -- no build to read two flags.
                #
                # isinstance, not "is not None": a Mock registry answers
                # describe() with a truthy Mock whose attributes are all
                # truthy, which would list every server as a public agent.
                view = app_state.app_registry.describe(name)  # type: ignore[attr-defined]
                if isinstance(view, ServerView):
                    if view.is_agent and view.tool_public:
                        agents.append(name)
                        if view.min_role is not None:
                            gated[name] = view.min_role
                    elif view.is_agent:
                        logger.debug(f"Skipping agent '{name}' in UI list (_tool_public=False)")
                    continue

                # Unbound registry, or a declaration that cannot answer for
                # its instance: the instance is the only source.
                srv = app_state.app_registry.get(name)  # type: ignore[attr-defined]
                from agent_system.servers.agent.server import Agent as _Agent
                if isinstance(srv, _Agent):
                    # Filter by _tool_public flag (visibility control)
                    # Default to True if attribute doesn't exist (backward compatibility with plugin agents)
                    if hasattr(srv, '_tool_public'):
                        if not srv._tool_public:
                            logger.debug(f"Skipping agent '{name}' in UI list (_tool_public=False)")
                            continue
                    # else: No _tool_public attribute → show in UI (backward compat)
                    agents.append(name)
                    if srv.min_role is not None:
                        gated[name] = srv.min_role
            except Exception as e:
                logger.debug(f"Failed to check agent {name}: {e}")
                continue
    except Exception as e:
        logger.debug(f"Failed to list agents: {e}")
    return agents, gated


@router.get("/agents")
async def list_agents(request: Request, response: Response, ctx: AppContext = Depends(app_context)):
    """List registered agent-like servers that are publicly visible (UI dropdown).

    Returns agents with _tool_public=True OR agents without _tool_public attribute (backward compat).
    Agents with visibility='tool' or 'private' (_tool_public=False) are excluded.

    Only agents the caller may run are listed (their role gate, metadata.min_role),
    and ``default`` names the entry agent only if the caller may run it: an
    entry the caller can pick is one /run refuses with a 403.
    """
    # Without this the browser may serve the list from its HTTP cache on a
    # normal reload — newly registered agents then only appear after a
    # force reload (observed: agent missing from the dropdown until Ctrl+F5).
    response.headers["Cache-Control"] = "no-store"
    from starlette.concurrency import run_in_threadpool

    agents, gated = await run_in_threadpool(_public_agents)
    # The agent /run actually uses without agent_name is this object --
    # not config.default_agent, which a reload can move without moving
    # the entry agent with it.
    default: Optional[str] = ctx.agent.name
    entry_gate = getattr(ctx.agent, "min_role", None)
    if (gated or entry_gate is not None) and ctx.config.auth.enabled:
        # Asked only when a gate is in play: an installation without gates
        # answers exactly as before, without resolving anybody.
        from agent_system.auth.agent_access import may_run_agent
        caller = await ctx.enforce_endpoint_security(request)
        agents = [name for name in agents
                  if may_run_agent(gated.get(name), caller, ctx.config.auth)]
        if not may_run_agent(entry_gate, caller, ctx.config.auth):
            default = None
    details = await run_in_threadpool(_agent_details, ctx, sorted(agents))
    return {"agents": sorted(agents), "details": details, "default": default}


def _hostname(base_url: Optional[str]) -> Optional[str]:
    """The host a model's requests go to; a malformed URL ("http://[fe80::1") names none rather than ending the list."""
    try:
        return urlparse(base_url).hostname if base_url else None
    except ValueError:
        return None


def _agent_details(ctx: AppContext, names: list[str]) -> list[dict[str, Any]]:
    """What the agent picker filters and groups by: description, category and tags from the merged config."""
    from agent_system.config.settings import get_tool_server_config
    live = ctx.live_config()
    details = []
    for name in names:
        try:
            cfg = get_tool_server_config(name, live)
        except Exception as e:
            logger.debug(f"No config details for agent {name}: {e}")
            cfg = None
        meta = cfg.metadata if cfg else None
        agent_config = getattr(cfg, "agent_config", None) if cfg else None
        details.append({
            "name": name,
            # the agent's own model: what the chat runs on until a person picks another
            "llm_profile": agent_config.default_llm_profile if agent_config else None,
            "description": cfg.description if cfg else None,
            "category": meta.category if meta else None,
            "tags": (meta.tags or []) if meta else [],
        })
    return details


@router.get("/llm/profiles")
def list_llm_profiles(response: Response, ctx: AppContext = Depends(app_context)):
    """List available LLM profiles with their descriptions."""
    response.headers["Cache-Control"] = "no-store"  # same staleness class as /agents
    profiles = []
    default_profile = None
    # Live config: /run accepts a profile a reload added, so the list the
    # UI picks from must know it too.
    live = ctx.live_config()
    try:
        if live.llm_system and live.llm_system.profiles:
            for profile_name, profile_config in live.llm_system.profiles.items():
                model = live.llm_system.models.get(profile_config.model_ref)
                profiles.append({
                    "name": profile_name,
                    "model_ref": profile_config.model_ref,
                    # the picker groups by route (host, else provider) or model and finds a profile by either
                    "provider": model.provider if model else None,
                    "model": model.model if model else None,
                    "host": _hostname(model.base_url) if model else None,
                    "description": profile_config.description or profile_name,
                    "max_steps": profile_config.max_steps
                })
            default_profile = live.llm_system.default_profile
    except Exception as e:
        logger.debug(f"Failed to list LLM profiles: {e}")
    from agent_system.llm.factory import THINKING_LEVELS
    return {
        "profiles": sorted(profiles, key=lambda p: p["name"].lower()),
        "default": default_profile or "normal",
        # what a chat's llm_params.thinking_level takes (CHAT_LLM_PARAMS)
        "thinking_levels": list(THINKING_LEVELS),
    }


@router.get("/agents/{agent_name}/allowed-tools")
async def get_agent_allowed_tools(request: Request, agent_name: str, ctx: AppContext = Depends(app_context)):
    """Return the effective allowed tools list for an agent after pattern filtering.

    An agent the caller may not run answers as one that does not exist: what
    a gated agent can reach is part of what the gate keeps from them.
    """
    srv, error = _agent_or_error(agent_name)
    if error is not None:
        return error
    if await ctx.gate_refuses_caller(request, srv):
        return {"error": "agent not found", "agent": agent_name}
    try:
        available, allowed_patterns, blocked_patterns = await srv.list_usable_tools()
        patterns = srv.agent_config.tools.allowed if srv.agent_config.tools else None
        return {
            "agent": agent_name,
            "patterns": patterns or [],
            "blocked_patterns": blocked_patterns or [],
            "available": sorted(available),
            "effective": sorted(available)
        }
    except Exception as e:
        return {"agent": agent_name, "error": str(e)}


@router.get("/agents/{agent_name}/tools")
async def get_agent_tools(request: Request, agent_name: str, ctx: AppContext = Depends(app_context)):
    """The tools an agent REALLY has: name, description, and its server.

    The same ground truth the terminal chat's /tools prints -- the filtered
    schema the model is given, not what the model says it has. The
    neighbouring /allowed-tools answers a different question: its
    "available" list holds SERVER names, so the browser had no way to the
    tools themselves and /tools said "only in the terminal".

    User role, like its neighbour, not admin like the debug twin: any
    agent_name the caller may RUN is allowed here, because /run allows it
    too -- reading the tool names of such an agent discloses nothing that
    running it would not. An agent behind a role gate the caller does not
    pass (metadata.min_role) answers as one that does not exist, as in /run.

    No filter parameter on purpose: the terminal filters the list it
    already holds, and a server that returns only the matches also
    returns a ``total`` that can no longer tell "this agent has no tools"
    from "nothing matched" -- the caller would have to guess which
    sentence to show.
    """
    from agent_system.chat_commands import group_tools_by_server
    from agent_system.servers.agent.server import Agent as _Agent

    registry = registry_of(request)
    try:
        srv = registry.get(agent_name) if registry is not None else None
    except Exception as e:
        logger.debug("Failed to get agent %s: %s", agent_name, e)
        srv = None
    if srv is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_name}' not found")
    if not isinstance(srv, _Agent):
        raise HTTPException(status_code=400,
                            detail=f"'{agent_name}' is a tool server, not an agent")
    if await ctx.gate_refuses_caller(request, srv):
        raise HTTPException(status_code=404, detail=f"agent '{agent_name}' not found")

    try:
        tools = await srv._list_usable_tools_with_details({})
    except Exception as e:
        # Not total: 0 -- the browser reads that as an empty allowlist.
        logger.error("Listing the tools of %s failed: %s", agent_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not list tools: {e}")
    try:
        servers = list(registry.list())
    except Exception:
        logger.debug("Could not read registry server names", exc_info=True)
        servers = []
    return {
        "agent": agent_name,
        "total": len(tools),
        "groups": [{"server": server, "tools": grouped}
                   for server, grouped in group_tools_by_server(tools, servers)],
    }


@router.get("/agents/debug/{agent_name}/allowed-tools")
async def get_agent_allowed_tools_debug(agent_name: str):
    """Return detailed pattern match diagnostics for an agent's allowed tools.

    Provides:
    - Phase 1: Server-level filtering (which servers pass the allow patterns)
    - Phase 2: Tool-level filtering (actual tools after expansion and blocked filtering)

    This shows the complete two-phase filtering process.

    Note: This endpoint is admin-only (requires admin role).
    """
    srv, error = _agent_or_error(agent_name)
    if error is not None:
        return error
    try:
        patterns = srv.agent_config.tools.allowed if srv.agent_config.tools else None
        available, allowed_patterns, blocked_patterns = await srv.list_usable_tools()

        # Phase 1: Server-level diagnostics (which servers matched which patterns)
        # Uses the SAME shared matcher as discovery/details listing.
        from agent_system.servers.agent.tool_schema_builder import server_matches_patterns
        server_diagnostics = []
        if patterns:
            for tool in available:
                matched_by = []
                for pat in patterns:
                    if server_matches_patterns(tool, [pat]):
                        matched_by.append(pat)
                server_diagnostics.append({"server": tool, "matched_patterns": matched_by})
        else:
            server_diagnostics = [{"server": t, "matched_patterns": ["<implicit:all>"]} for t in available]

        # Phase 2: Get actual expanded and filtered tools via ToolSchemaBuilder
        final_tools = []
        tool_details = []
        try:
            from agent_system.servers.agent.tool_schema_builder import ToolSchemaBuilder

            # Create tool schema builder (same as used during chat)
            tool_builder = ToolSchemaBuilder(
                agent_name=agent_name,
                tool_integration_manager=srv._tool_integration_manager,
                server_getter_func=srv._get_server_from_any_registry
            )

            # Build schemas with filtering applied
            tools_schema, tool_name_mapping, usable_tools, display_tools = await tool_builder.build_schemas(
                available_tools=available,
                allowed_patterns=allowed_patterns,
                blocked_patterns=blocked_patterns
            )

            # Extract final tool names from schemas
            for schema in tools_schema:
                if schema.get("type") == "function" and "function" in schema:
                    func = schema["function"]
                    tool_name = func.get("name", "")
                    server_name = tool_name_mapping.get(tool_name, "")
                    final_tools.append(tool_name)
                    tool_details.append({
                        "tool": tool_name,
                        "server": server_name,
                        "full_path": f"{server_name}/{tool_name}" if server_name else tool_name
                    })
        except Exception as e:
            logger.warning(f"Failed to build tool schemas for debug: {e}")
            tool_details = [{"error": str(e)}]

        return {
            "agent": agent_name,
            "allowed_patterns": patterns or [],
            "blocked_patterns": blocked_patterns or [],
            "phase1_server_filtering": {
                "description": "Servers that passed allowed pattern matching",
                "servers": server_diagnostics
            },
            "phase2_tool_filtering": {
                "description": "Final tools after expansion and blocked pattern filtering",
                "total_tools": len(final_tools),
                "tools": tool_details
            }
        }
    except Exception as e:
        return {"agent": agent_name, "error": str(e)}


@router.get("/agents/debug/{agent_name}/system-prompt")
async def get_agent_system_prompt(agent_name: str):
    """Return the currently rendered system & tools prompt for the agent.

    Renders on demand using the same logic as execution, including:
      - allowed tool filtering
      - max_steps (minus one for planning budget inside prompt)
      - datetime context (if enabled)

    Note: This endpoint is admin-only (requires admin role).
    """
    srv, error = _agent_or_error(agent_name, loud=True)
    if error is not None:
        return error
    try:
        return await srv.get_current_system_prompt()
    except Exception as e:  # pragma: no cover - defensive
        return {"error": str(e), "agent": agent_name}
