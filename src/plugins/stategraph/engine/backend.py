"""What activities reach outside the process through: agents, tools, decision models (§5.8).

``ScarabHiveBackend`` is the production backend. Agents go through one
``AgentCaller`` per call on a SAM (``retries=0``: the engine is the only retry
layer). Tools go through the runner agent's ``dispatch_tool_call``, whose
allowlist is the boundary of what a machine may call; configured
``inject_params`` are applied there, so secrets never live in machine files.
Decisions go through ``create_decisions_from_profile``. Agent template vars
are set on the run's session right before a SAM call, where sub-agents inherit
them. ``NoBackend`` refuses everything; mock-only and test runs use it.

``make_config_check`` answers the validator's SG007 questions from the
resolved configuration, with the matchers the runtime itself uses.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from typing import TYPE_CHECKING, Any, Optional, Protocol

import jsonschema

from plugins.stategraph.kinds import ActivityError

if TYPE_CHECKING:
    from .activity import ActivityRun

logger = logging.getLogger(__name__)


class Backend(Protocol):
    async def agent_create(self, act: "ActivityRun", *, agent: str, task: str, sam: Optional[str],
                           advanced: bool, vars: dict[str, Any]) -> tuple[str, Optional[str]]:
        """Spawn a sub-agent: ``(answer text, instance id)``."""

    async def agent_continue(self, act: "ActivityRun", *, instance_id: str, message: str, sam: Optional[str],
                             advanced: bool, vars: dict[str, Any]) -> str:
        """Follow up an instance: its answer text."""

    async def call_tool(self, act: "ActivityRun", *, tool: str, args: dict[str, Any]) -> Any:
        """The tool's result; an error-shaped result raises ``ActivityError``."""

    async def decide(self, act: "ActivityRun", *, questions: dict[str, dict[str, Any]], input: Any,
                     profile: Optional[str]) -> dict[str, dict[str, Any]]:
        """``{name: {value, confidence, probabilities}}``."""


class NoBackend:
    """Refuses every external call: runs without a backend must mock agents, tools and decisions."""

    reason = "this run has no backend (mock the activity or start the run through the stategraph plugin)"

    async def agent_create(self, act: "ActivityRun", **_: Any) -> tuple[str, Optional[str]]:
        raise ActivityError("no_backend", f"{act.path}: {self.reason}")

    async def agent_continue(self, act: "ActivityRun", **_: Any) -> str:
        raise ActivityError("no_backend", f"{act.path}: {self.reason}")

    async def call_tool(self, act: "ActivityRun", **_: Any) -> Any:
        raise ActivityError("no_backend", f"{act.path}: {self.reason}")

    async def decide(self, act: "ActivityRun", **_: Any) -> dict[str, dict[str, Any]]:
        raise ActivityError("no_backend", f"{act.path}: {self.reason}")

    def agent_template_vars(self, agent: str) -> dict[str, Any]:
        return {}


def validate_answer(value: Any, schema: dict[str, Any]) -> Optional[str]:
    """None if ``value`` satisfies ``schema``, else a short reason."""
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        return f"{where}: {exc.message}"
    except jsonschema.SchemaError as exc:
        return f"the activity's schema is not a valid JSON schema: {exc.message}"
    return None


def inject(tool: str, args: dict[str, Any], inject_params: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """tool_script's rule: for every fnmatch pattern that matches the tool, its params win over the machine's."""
    merged = dict(args)
    for pattern, extra in inject_params.items():
        if isinstance(extra, dict) and fnmatch.fnmatchcase(tool, pattern):
            merged.update(extra)
    return merged


class ScarabHiveBackend:
    def __init__(self, *, runner: Any, system_config: Any, session_id: str, user_id: Optional[str],
                 token: Any = None, default_sam: Optional[str] = None,
                 inject_params: Optional[dict[str, dict[str, Any]]] = None,
                 tool_check: Optional[Any] = None):
        self.runner = runner
        self.system_config = system_config
        self.session_id = session_id
        self.user_id = user_id
        self.token = token
        self.default_sam = default_sam
        self.inject_params = dict(inject_params or {})
        self.tool_check = tool_check  # the validator's SG007 tool check, applied again at run time

    # ------------------------------------------------------------ agents
    def _caller(self, act: "ActivityRun", sam: Optional[str]) -> Any:
        from agent_system.core.agent_caller import AgentCaller

        sam = sam or self.default_sam
        if not sam:
            raise ActivityError("config", "no SAM for agent activities: set sam: in the machine or default_sam")
        request_id = act.request_id()
        act.meta["request_id"] = request_id
        return AgentCaller(self.runner, sam_instance=sam, session_id=self.session_id, user_id=self.user_id,
                           request_id=request_id, cancellation_token=self._token_for(act), retries=0)

    def _token_for(self, act: "ActivityRun") -> Any:
        """The run's token -- none for a finally or close activity: it runs on after a terminate (§3.10)."""
        return None if act.finalizer else self.token

    def _set_vars(self, variables: dict[str, Any]) -> None:
        """The run's session holds exactly this call's effective vars -- replaced, never accumulated (§3.9)."""
        tracker = getattr(self.runner, "_session_tracker", None)
        if tracker is None:
            if variables:
                raise ActivityError("config", "the runner agent has no session tracker: vars cannot reach sub-agents")
            return
        tracker.clear_session_template_vars(self.session_id)
        if variables:
            tracker.set_session_template_vars(self.session_id, dict(variables))

    async def _guarded(self, act: "ActivityRun", agent: str, call: Any) -> Any:
        try:
            return await call
        except asyncio.CancelledError:
            request_id = act.meta.get("request_id")
            if request_id:  # a timeout or terminate: stop the sub-run too, not only our await
                try:
                    from agent_system.core.cancellation import get_cancellation_manager

                    get_cancellation_manager().cancel_request(request_id)
                except Exception:
                    logger.debug("cancel_request(%s) failed", request_id, exc_info=True)
            raise
        except ActivityError:
            raise
        except Exception as exc:
            raise ActivityError("agent_failed", f"{agent}: {exc}") from exc

    async def agent_create(self, act: "ActivityRun", *, agent: str, task: str, sam: Optional[str],
                           advanced: bool, vars: dict[str, Any]) -> tuple[str, Optional[str]]:
        caller = self._caller(act, sam)
        self._set_vars(vars)
        text = await self._guarded(act, agent, caller.call_text(agent, task, use_advanced_model=advanced))
        return text, caller.last_instance_id  # one caller per call: this is our create

    async def agent_continue(self, act: "ActivityRun", *, instance_id: str, message: str, sam: Optional[str],
                             advanced: bool, vars: dict[str, Any]) -> str:
        caller = self._caller(act, sam)
        self._set_vars(vars)
        return await self._guarded(act, instance_id,
                                   caller.follow_up_text(instance_id, message, use_advanced_model=advanced))

    def agent_template_vars(self, agent: str) -> dict[str, Any]:
        from agent_system.config.settings import get_tool_server_config

        config = get_tool_server_config(agent, self.system_config)
        if config is None:
            raise ActivityError("config", f"vars_from: agent {agent!r} is not configured")
        agent_config = getattr(config, "agent_config", None)
        return dict(getattr(agent_config, "template_vars", None) or {})

    # ------------------------------------------------------------ tools
    async def call_tool(self, act: "ActivityRun", *, tool: str, args: dict[str, Any]) -> Any:
        from agent_system.servers.agent.components.tool_execution import ToolDispatchError
        from agent_system.tools.base import _error_result_message

        if self.tool_check is not None:  # a computed tool name never passed the validator's check
            problem = self.tool_check("tool", tool, {})
            if problem:
                raise ActivityError("tool_denied", problem)
        request_id = act.request_id()
        act.meta["request_id"] = request_id
        try:
            result = await self.runner.dispatch_tool_call(tool, inject(tool, args, self.inject_params),
                                                          session_id=self.session_id, user_id=self.user_id,
                                                          request_id=request_id)
        except ToolDispatchError as exc:
            raise ActivityError("tool_denied", str(exc)) from exc
        result = _redact(result, tool, self.inject_params)  # before anything reads it: out, error text, error_if
        if isinstance(result, dict) and result.get("cancelled") is True and act.run.cancelled and not act.finalizer:
            raise asyncio.CancelledError()
        message = _error_result_message(result)
        if message is not None:
            raise ActivityError("tool_failed", f"{tool}: {message}", data=result)
        return result

    # ------------------------------------------------------------ decisions
    async def decide(self, act: "ActivityRun", *, questions: dict[str, dict[str, Any]], input: Any,
                     profile: Optional[str]) -> dict[str, dict[str, Any]]:
        from agent_system.llm.decisions import create_decisions_from_profile

        try:
            client = create_decisions_from_profile(self.system_config, profile)
            result = await client.decide(input, questions, cancellation_token=self._token_for(act),
                                         session_id=self.session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ActivityError("decision_failed", f"{type(exc).__name__}: {exc}") from exc
        act.meta.update({"model": result.model, "cost": result.cost})
        return {name: {"value": answer.value, "confidence": answer.confidence,
                       "probabilities": answer.probabilities}
                for name, answer in result.answers.items()}


def _redact(result: Any, tool: str, inject_params: dict[str, dict[str, Any]]) -> Any:
    """Injected values never reach the journal, even when a tool echoes its arguments."""
    secrets = {str(v) for pattern, extra in inject_params.items() if fnmatch.fnmatchcase(tool, pattern)
               for v in (extra or {}).values()}
    if not secrets:
        return result

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[redacted]")
        return value

    return scrub(result)


# ------------------------------------------------------------------ config checks

def make_config_check(system_config: Any, *, runner: str, default_sam: Optional[str], own_instance: str):
    """SG007: can the configuration run what a machine names? Same matchers as the runtime."""
    from agent_system.config.settings import get_tool_server_config
    from agent_system.servers.agent.components.server_resolution import resolve_longest_prefix
    from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns

    servers = getattr(getattr(system_config, "plugins", None), "servers", None) or {}

    def server(name: str) -> Any:
        try:
            config = get_tool_server_config(name, system_config)
        except Exception:
            return None
        return config if config is not None and getattr(config, "enabled", False) else None

    def runner_refuses(tool: str, prefix: str) -> Optional[str]:
        """Why the runner may not call ``tool`` (None: it may). Tool activities and the SAM calls of agent
        activities both go through the runner's allowlist."""
        host = server(runner)
        if host is None:
            return f"runner agent {runner!r} is not configured or not enabled"
        tools = getattr(getattr(host, "agent_config", None), "tools", None)
        allowed = list(getattr(tools, "allowed", None) or [])
        blocked = list(getattr(tools, "blocked", None) or [])
        if not tool_matches_patterns(tool, prefix, allowed) or tool_matches_patterns(tool, prefix, blocked):
            return f"{tool!r} is not in {runner}'s tool allowlist"
        return None

    def check(what: str, name: str, extra: dict[str, Any]) -> Optional[str]:
        if what in ("agent", "sam"):
            sam_name = name if what == "sam" else (extra.get("sam") or default_sam)
            if not sam_name:
                return "no SAM: set sam: in the machine or default_sam in the plugin config"
            sam = server(sam_name)
            if sam is None:
                return f"SAM {sam_name!r} is not configured or not enabled"
            if getattr(sam, "type", "") != "sub_agent_manager":
                return f"SAM {sam_name!r} is not a sub_agent_manager (it is {getattr(sam, 'type', '?')!r})"
            refused = runner_refuses(f"{sam_name}_manage_sub_agent", sam_name)
            if refused is not None:  # AgentCaller spawns through the SAM's tool, as the runner
                return refused if server(runner) is None else (
                    f"agent activities spawn through {sam_name}, but {refused}: add '{sam_name}/*' to it")
            if what == "agent":
                from plugins.sub_agent_manager.server import agent_allowed

                if server(name) is None:
                    return f"agent {name!r} is not configured or not enabled"
                allowed = list(getattr(sam, "allowed_agents", None) or ["*"])
                blocked = list(getattr(sam, "blocked_agents", None) or [])
                if not agent_allowed(name, allowed, blocked):
                    return f"agent {name!r} is not in {sam_name}.allowed_agents (the SAM refuses to spawn it)"
            return None
        if what == "tool":
            owner, prefix = resolve_longest_prefix(lambda p: p if p in servers else None, name)
            if owner is None:
                return f"tool {name!r}: no configured server owns it (tool names carry the server prefix)"
            if prefix == own_instance or server(prefix) is not None and getattr(server(prefix), "type", "") == "stategraph":
                return f"tool {name!r} belongs to stategraph itself: a machine may not save, run or control machines"
            if getattr(server(prefix), "type", "") == "sub_agent_manager":  # the runner may call it, for agent steps
                return (f"tool {name!r} belongs to the SAM {prefix}: a machine starts agents with an agent activity, "
                        "which the run journals and cancels with itself")
            refused = runner_refuses(name, prefix)
            if refused is None:
                return None
            return refused if server(runner) is None else f"tool {refused} (the runner is the boundary)"
        if what == "agent_exists":
            return None if server(name) is not None else f"agent {name!r} is not configured or not enabled"
        if what == "profile":
            llm = getattr(system_config, "llm_system", None)
            profiles = getattr(llm, "decision_profiles", None) or {}
            if name not in profiles:
                return f"decision profile {name!r} is not configured (known: {', '.join(profiles) or 'none'})"
            return None
        return None

    return check
