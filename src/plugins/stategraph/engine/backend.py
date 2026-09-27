"""What activities reach outside the process through: agents, tools, decision models (§5.8).

``ScarabHiveBackend`` is the production backend. An agent activity runs the
registered agent itself (``run_events``) on an instance session of its own, a
sub-session of the run's; a continue runs it again on that session. No SAM is
involved: the machine file names its agents, so it is their allowlist, and the
engine is the only retry layer. Each instance session holds the call's
template vars. Tools go through the runner agent's ``dispatch_tool_call``,
whose allowlist is the boundary of what a machine may call; configured
``inject_params`` are applied there, so secrets never live in machine files.
Decisions go through ``create_decisions_from_profile``. ``NoBackend`` refuses
everything; mock-only and test runs use it.

``make_config_check`` answers the validator's SG007 questions from the
resolved configuration, with the matchers the runtime itself uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol

import jsonschema

from plugins.stategraph.kinds import ActivityError

if TYPE_CHECKING:
    from .activity import ActivityRun

logger = logging.getLogger(__name__)

#: After a timeout or terminate cancelled its token, how long an agent run gets to stop before the
#: activity ends without it. None: until the CancellationManager force-cancels what a cancelled
#: request left running (its cleanup timeout plus one monitor round), and a second more.
AGENT_STOP_GRACE: Optional[float] = None


def _stop_grace() -> float:
    if AGENT_STOP_GRACE is not None:
        return AGENT_STOP_GRACE
    try:
        from agent_system.core.cancellation import get_cancellation_manager

        manager = get_cancellation_manager()
        return float(manager.default_cleanup_timeout) + float(manager.monitor_interval) + 1.0
    except Exception:
        return 12.0


class Backend(Protocol):
    async def agent_create(self, act: "ActivityRun", *, agent: str, task: str, advanced: bool,
                           vars: dict[str, Any]) -> tuple[str, Optional[str]]:
        """Run a new instance of an agent: ``(answer text, instance id)``."""

    async def agent_continue(self, act: "ActivityRun", *, agent: str, instance_id: str, message: str,
                             advanced: bool, vars: dict[str, Any]) -> str:
        """Follow up an instance of ``agent``: its answer text."""

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


def _run_message(role: str, content: str) -> dict[str, Any]:
    """A message of a run's session: written by the plugin, not by a person or a model (``injected_by``)."""
    from datetime import datetime, timezone

    return {"role": role, "content": content, "injected_by": "stategraph",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


def inject(tool: str, args: dict[str, Any], inject_params: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """tool_script's rule: for every fnmatch pattern that matches the tool, its params win over the machine's."""
    merged = dict(args)
    for pattern, extra in inject_params.items():
        if isinstance(extra, dict) and fnmatch.fnmatchcase(tool, pattern):
            merged.update(extra)
    return merged


class ScarabHiveBackend:
    def __init__(self, *, runner: Any, system_config: Any, session_id: str, user_id: Optional[str],
                 token: Any = None, inject_params: Optional[dict[str, dict[str, Any]]] = None,
                 config_check: Optional[Any] = None, nesting: Optional[dict[str, Any]] = None):
        self.runner = runner
        self.system_config = system_config
        self.session_id = session_id
        self.user_id = user_id
        self.token = token
        self.nesting = nesting  # the run's caller in a sub-agent tree: {depth, depth_budget} of its session
        self.inject_params = dict(inject_params or {})
        self.config_check = config_check  # the validator's SG007 check, applied again at run time
        self._busy: set[str] = set()  # instances with a run in flight: one conversation, one run at a time

    # ------------------------------------------------------------ agents
    def _agent(self, name: str) -> Any:
        """The registered agent ``name`` -- the instance the app runs, not a copy (§3.9)."""
        if self.config_check is not None:  # the runner, a machine facade, an agent that controls machines
            problem = self.config_check("agent", name, {})
            if problem:
                raise ActivityError("config", problem)
        registry = getattr(self.runner, "registry", None)
        try:
            agent = registry.get(name) if registry is not None else None
        except Exception:  # the registry raises for a name it does not know
            agent = None
        if not callable(getattr(agent, "run_events", None)) or getattr(agent, "_session_tracker", None) is None:
            raise ActivityError("config", f"agent {name!r} is not configured, not enabled, or not an agent")
        return agent

    def _sessions(self, agent: Any) -> Any:
        service = getattr(agent, "_session_service", None) or getattr(self.runner, "_session_service", None)
        if getattr(service, "session_manager", None) is None:
            raise ActivityError("config", "no session service: an agent instance keeps its conversation in a session")
        return service

    def _token_for(self, act: "ActivityRun") -> Any:
        """The run's token -- none for a finally or close activity: it runs on after a terminate (§3.10)."""
        return None if act.finalizer else self.token

    async def _instance_of_this_run(self, service: Any, user: str, instance_id: str, agent: str) -> None:
        """A continue reaches only an instance this run created, of the agent the activity names."""
        try:
            data = await service.session_manager.load_session(user, instance_id)
        except Exception:  # unknown, another user's, or not a session id at all
            data = None
        parent = ((data or {}).get("parent_session") or {}).get("session_id")
        if data is None or parent != self.session_id:
            raise ActivityError("config", f"continue: {instance_id!r} is not an agent instance of this run")
        if data.get("agent_name") != agent:
            raise ActivityError("config", f"continue: instance {instance_id!r} belongs to agent "
                                          f"{data.get('agent_name')!r}, not {agent!r}")

    async def _run_agent(self, act: "ActivityRun", agent: Any, service: Any, *, instance_id: str, message: str,
                         advanced: bool, variables: dict[str, Any], new: bool) -> str:
        """One run of ``agent`` on its instance session: the run's final answer.

        The instance session holds exactly this call's effective vars -- replaced, never accumulated
        (§3.9); the agent's own template_vars stay under them. A continue loads the instance's
        conversation from its stored session, so it holds across a restart of the process.
        """
        from agent_system.core.request_context import register_request_user, release_request_user_tree

        if instance_id in self._busy:
            raise ActivityError("config", f"instance {instance_id!r} is already running in this run: "
                                          "one conversation takes one call at a time")
        token = self._token_for(act)
        if token is not None and token.is_cancelled:
            raise asyncio.CancelledError("run cancelled")
        request_id = act.request_id()
        act.meta["request_id"] = request_id
        user = self.user_id or "anonymous"
        profile = getattr(agent.agent_config, "default_llm_profile", None) or "normal"
        _protect(act, request_id)
        self._busy.add(instance_id)
        register_request_user(request_id, user)
        work: Optional[asyncio.Future[str]] = None
        try:
            try:
                # the stored vars too: a save merges into them, and a sub-agent the instance starts
                # through its own SAM inherits what is stored
                await service.session_manager.replace_session_context_vars(user, instance_id, dict(variables))
                await service.open_for_run(agent, user, instance_id, profile)
            except Exception as exc:
                raise ActivityError("agent_failed", f"{agent.name}: its session could not be opened: {exc}") from exc
            tracker = agent._session_tracker
            tracker.clear_session_template_vars(instance_id)
            if variables:
                tracker.set_session_template_vars(instance_id, dict(variables))
            work = asyncio.ensure_future(self._answer(agent, instance_id, message, request_id, advanced))
            text = await self._guarded(act, agent.name, work)
            if not await service.save_session(agent, user, instance_id, agent.name, profile, was_new_session=new):
                logger.warning("stategraph: instance %s of %s was not saved; a continue after a restart "
                               "would miss its conversation", instance_id, agent.name)
            return text
        finally:
            def release(*_: Any) -> None:
                self._busy.discard(instance_id)
                release_request_user_tree(request_id)  # with the ids its tool calls registered
                _unprotect(act, request_id)

            if work is not None and not work.done():  # it outlived its grace: the instance stays busy until it ends
                work.add_done_callback(release)
            else:
                release()

    @staticmethod
    async def _answer(agent: Any, instance_id: str, message: str, request_id: str, advanced: bool) -> str:
        """Consume the run's events to its end; the final summary is the answer, an error or cancel a failure."""
        from contextlib import aclosing

        text = ""
        async with aclosing(agent.run_events(task=message, request_id=request_id, session_id=instance_id,
                                             use_advanced_model=advanced)) as events:
            async for event in events:
                kind = event.get("type")
                if kind == "final":
                    text = event.get("summary") or ""  # read on to "end": the run stores its messages there
                elif kind == "end":
                    break
                elif kind == "error":
                    raise RuntimeError(event.get("message") or "the agent run failed")
                elif kind == "cancelled":
                    raise RuntimeError(f"cancelled: {event.get('reason') or event.get('message') or 'from outside'}")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("the agent ended without an answer")
        return text

    async def _guarded(self, act: "ActivityRun", agent: str, work: "asyncio.Future[Any]") -> Any:
        """Await the agent run, a task of its own, so that a timeout or terminate reaches the run's token
        first. A cancel that reaches the run itself can land in one of its tool calls, which turns it into a
        "cancelled" tool result -- and the agent runs on. The token is what it stops at: cancelled while the
        run still holds it, then the task, and the run gets ``_stop_grace()`` to wind down -- also when a
        second cancel comes meanwhile (a terminate after a timeout)."""
        work.add_done_callback(lambda done: done.cancelled() or done.exception())  # retrieved, whoever waits
        try:
            await asyncio.wait({work})  # never cancels work, and logs nothing when it fails later (shield does)
            return work.result()
        except asyncio.CancelledError:
            request_id = act.meta.get("request_id")
            if request_id:
                try:
                    from agent_system.core.cancellation import get_cancellation_manager

                    get_cancellation_manager().cancel_request(request_id)
                except Exception:
                    logger.debug("cancel_request(%s) failed", request_id, exc_info=True)
            work.cancel()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _stop_grace()
            while not work.done() and loop.time() < deadline:
                try:
                    await asyncio.wait({work}, timeout=deadline - loop.time())
                except asyncio.CancelledError:
                    continue  # it is stopping already; the first cancel is the one that ends the activity
            if not work.done():
                logger.warning("stategraph: agent run %s of %s did not stop within %.0fs", request_id, agent,
                               _stop_grace())
            raise
        except ActivityError:
            raise
        except Exception as exc:
            raise ActivityError("agent_failed", f"{agent}: {exc}") from exc

    async def agent_create(self, act: "ActivityRun", *, agent: str, task: str, advanced: bool,
                           vars: dict[str, Any]) -> tuple[str, Optional[str]]:
        target = self._agent(agent)
        service = self._sessions(target)
        budget = (self.nesting or {}).get("depth_budget")
        if budget is not None and budget < 1:  # the SAM above the run granted no level below its caller
            raise ActivityError("config", f"{agent}: the caller of this run has no sub-agent level left below it "
                                          f"(depth_budget {budget}); the SAM above it limits max_nesting_depth")
        try:
            created = await service.session_manager.create_session(
                user_id=self.user_id or "anonymous", title=f"{agent} ({act.path})", agent_name=target.name,
                llm_profile=getattr(target.agent_config, "default_llm_profile", None) or "normal",
                parent_session_id=self.session_id)  # a sub-session: not in the user's session list
            if self.nesting:  # one level below the run's caller, where its SAM would have put it: a SAM the
                created["depth"] = int(self.nesting.get("depth") or 1) + 1  # instance calls counts on from there
                if budget is not None:
                    created["depth_budget"] = budget - 1
                await service.session_manager.save_session(created)
        except Exception as exc:
            raise ActivityError("agent_failed", f"{agent}: its session could not be created: {exc}") from exc
        instance = created["session_id"]
        text = await self._run_agent(act, target, service, instance_id=instance, message=task, advanced=advanced,
                                     variables=vars, new=True)
        return text, instance

    # ------------------------------------------------------------ the run's own session
    def _run_sessions(self) -> Any:
        """The session service of the runner -- the host of runs -- or None: then the run keeps no session."""
        service = getattr(self.runner, "_session_service", None)
        return service if getattr(service, "session_manager", None) is not None else None

    async def run_began(self, *, machine: str, title: str, params: dict[str, Any]) -> None:
        """The run's own session, ``sg_<run id>``: its agents' instance sessions are its sub-sessions, so the
        session list shows the run with them -- under the caller's session when an agent started it (a machine
        facade, a tool call), at the top of its user's list otherwise. A resume finds it there already. The view
        of a run: a run never fails over it."""
        service = self._run_sessions()
        if service is None:
            return
        manager, user = service.session_manager, self.user_id or "anonymous"
        try:
            await manager.load_session(user, self.session_id)
            return  # a resume: begun before
        except Exception:
            pass
        try:
            created = await manager.create_session(
                user_id=user, title=f"{title or machine} · {self.session_id.removeprefix('sg_')}",
                agent_name=getattr(self.runner, "name", "stategraph_runner"),
                llm_profile=getattr(getattr(self.runner, "agent_config", None), "default_llm_profile", None) or "normal",
                session_id=self.session_id, parent_session_id=(self.nesting or {}).get("session"))
            # a level below its caller, or the top: the chat opens a session with a depth whose agent it cannot
            # pick -- the runner is private -- read-only (static/js/shell/sessions.js), so nobody types a message
            # that would run the runner on the session the run's tool activities use
            created["depth"] = int((self.nesting or {}).get("depth") or 0) + 1
            shown = json.dumps(params, ensure_ascii=False, indent=2) if params else "(none)"
            created["messages"].append(_run_message("user", f"Run of the state machine `{machine}`, params:\n\n"
                                                            f"```json\n{shown}\n```"))
            await manager.save_session(created)
        except Exception:
            logger.warning("stategraph: the session of run %s could not be created", self.session_id, exc_info=True)

    async def run_ended(self, *, status: str, final_state: Optional[str], output: Any, error: Any) -> None:
        """How the run ended, as the answer in its session: status, final state, output or error."""
        service = self._run_sessions()
        if service is None:
            return
        manager, user = service.session_manager, self.user_id or "anonymous"
        lines = [f"The run ended **{status}**" + (f" in `{final_state}`" if final_state else "") + "."]
        if error:
            lines.append(f"\n**{error.get('type', 'error')}**: {error.get('message', '')}" if isinstance(error, dict)
                         else f"\n{error}")
        if output is not None:
            shown = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, indent=2)
            lines.append(f"\nOutput:\n\n```json\n{shown}\n```" if not isinstance(output, str) else f"\n{shown}")
        try:
            lock = service.save_lock(self.session_id) if hasattr(service, "save_lock") else None
            async with lock if lock is not None else contextlib.nullcontext():
                data = await manager.load_session(user, self.session_id)
                data["messages"].append(_run_message("assistant", "\n".join(lines)))
                await manager.save_session(data)
        except Exception:
            logger.warning("stategraph: the end of run %s was not written to its session", self.session_id,
                           exc_info=True)

    async def agent_continue(self, act: "ActivityRun", *, agent: str, instance_id: str, message: str,
                             advanced: bool, vars: dict[str, Any]) -> str:
        target = self._agent(agent)
        service = self._sessions(target)
        await self._instance_of_this_run(service, self.user_id or "anonymous", instance_id, target.name)
        return await self._run_agent(act, target, service, instance_id=instance_id, message=message,
                                     advanced=advanced, variables=vars, new=False)

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

        if self.config_check is not None:  # a computed tool name never passed the validator's check
            problem = self.config_check("tool", tool, {})
            if problem:
                raise ActivityError("tool_denied", problem)
        request_id = act.request_id()
        act.meta["request_id"] = request_id
        _protect(act, request_id)
        try:
            result = await self.runner.dispatch_tool_call(tool, inject(tool, args, self.inject_params),
                                                          session_id=self.session_id, user_id=self.user_id,
                                                          request_id=request_id)
        except ToolDispatchError as exc:
            raise ActivityError("tool_denied", str(exc)) from exc
        finally:
            _unprotect(act, request_id)
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

@functools.cache
def _control_tools() -> tuple[str, ...]:
    """stategraph's tools that save, run or control machines: all of schema.yaml but the read-only ones."""
    import re

    from plugins.stategraph.server import READ_ONLY_TOOLS

    text = (Path(__file__).resolve().parents[1] / "schema.yaml").read_text(encoding="utf-8")
    return tuple(tool for tool in re.findall(r'name: "\{\{ name \}\}_(\w+)"', text) if tool not in READ_ONLY_TOOLS)


def _protect(act: "ActivityRun", request_id: str) -> None:
    """A finally or close activity's request runs to its end (§3.10): no cancel of the requests around it cuts it --
    a terminate of its run, nor a cancel of the caller's request tree above the run; only the platform's forced
    cancel after the cleanup timeout does."""
    if act.finalizer:
        from agent_system.core.cancellation import get_cancellation_manager

        get_cancellation_manager().protect(request_id)


def _unprotect(act: "ActivityRun", request_id: str) -> None:
    if act.finalizer:
        from agent_system.core.cancellation import get_cancellation_manager

        get_cancellation_manager().unprotect(request_id)


def make_config_check(system_config: Any, *, runner: str, own_instance: str,
                      is_agent: Optional[Callable[[str], Optional[bool]]] = None):
    """SG007: can the configuration run what a machine names? Same matchers as the runtime.

    ``is_agent(name)`` answers from the running registry whether a server is an agent (None: it does not
    know); without it the configuration alone is checked, and a tool server named as an agent fails at run
    time instead.
    """
    from agent_system.config.settings import _resolve_server_inheritance, get_tool_server_config
    from agent_system.servers.agent.components.server_resolution import resolve_longest_prefix
    from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns

    servers = getattr(getattr(system_config, "plugins", None), "servers", None) or {}
    @functools.cache
    def final_type(name: str) -> str:
        try:
            return _resolve_server_inheritance(name, system_config)[0]
        except Exception:
            return str(getattr(servers.get(name), "type", "") or "")

    stategraphs = {own_instance} | {name for name in servers if final_type(name) == "stategraph"}

    sams = sorted(name for name in servers if final_type(name) == "sub_agent_manager")

    def may_call(config: Any, tool: str, owner: str) -> bool:
        tools = getattr(getattr(config, "agent_config", None), "tools", None)
        allowed = list(getattr(tools, "allowed", None) or [])
        blocked = list(getattr(tools, "blocked", None) or [])
        return tool_matches_patterns(tool, owner, allowed) and not tool_matches_patterns(tool, owner, blocked)

    def own_reason(name: str) -> Optional[str]:
        """Why agent ``name`` itself would save, start or control machines; None when it would not."""
        if name == runner:
            return f"{name!r} is the runner: it hosts the run's tool activities and is no agent to call"
        if final_type(name) == "stategraph_machine":
            return (f"{name!r} runs a machine as an agent: a machine may not start machines -- import that "
                    "machine and use it as a submachine (machine:)")
        config = server(name)
        tool = next((f"{instance}_{tool}" for instance in sorted(stategraphs) for tool in _control_tools()
                     if config is not None and may_call(config, f"{instance}_{tool}", instance)), None)
        return f"agent {name!r} may call {tool}: a machine may not save, run or control machines" if tool else None

    @functools.cache
    def reaching() -> dict[str, str]:
        """Every configured agent that reaches machines, and why: itself, or an agent it can start through a
        SAM it may call -- the whole graph at once, walked back from the ones that reach them themselves."""
        from plugins.sub_agent_manager.server import agent_allowed

        names = sorted(name for name in servers if server(name) is not None)
        why = {name: reason for name in names if (reason := own_reason(name))}
        starters: dict[str, list[tuple[str, str]]] = {}  # spawned -> [(the agent that can start it, through)]
        for sam in (s for s in sams if server(s) is not None):
            allowed = list(getattr(server(sam), "allowed_agents", None) or ["*"])  # the SAM's own default
            blocked = list(getattr(server(sam), "blocked_agents", None) or [])
            callers = [name for name in names if may_call(server(name), f"{sam}_manage_sub_agent", sam)]
            for spawned in (n for n in names if callers and agent_allowed(n, allowed, blocked)):
                starters.setdefault(spawned, []).extend((caller, sam) for caller in callers if caller != spawned)
        queue = sorted(why)
        while queue:
            spawned = queue.pop(0)
            for caller, sam in starters.get(spawned, []):
                if caller not in why:
                    why[caller] = f"agent {caller!r} can start {spawned!r} through {sam} -- {why[spawned]}"
                    queue.append(caller)
        return why

    @functools.cache
    def server(name: str) -> Any:
        try:
            config = get_tool_server_config(name, system_config)
        except Exception:
            return None
        return config if config is not None and getattr(config, "enabled", False) else None

    def runner_refuses(tool: str, prefix: str) -> Optional[str]:
        """Why the runner may not call ``tool`` (None: it may): tool activities go through its allowlist."""
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
        if what == "vars_from":
            return None if server(name) is not None else f"agent {name!r} is not configured or not enabled"
        if what == "agent":  # the backend runs the registered agent itself; the machine file names it
            config = server(name)
            if config is None:
                return f"agent {name!r} is not configured or not enabled"
            if is_agent is not None and is_agent(name) is False:
                return f"{name!r} is not an agent"
            return reaching().get(name)
        if what == "tool":
            owner, prefix = resolve_longest_prefix(lambda p: p if p in servers else None, name)
            if owner is None:
                return f"tool {name!r}: no configured server owns it (tool names carry the server prefix)"
            if prefix == own_instance or server(prefix) is not None and final_type(prefix) == "stategraph":
                return f"tool {name!r} belongs to stategraph itself: a machine may not save, run or control machines"
            if server(prefix) is not None and final_type(prefix) == "sub_agent_manager":
                return (f"tool {name!r} belongs to the SAM {prefix}: a machine starts agents with an agent activity, "
                        "which the run journals and cancels with itself")
            refused = runner_refuses(name, prefix)
            if refused is None:
                return None
            return refused if server(runner) is None else f"tool {refused} (the runner is the boundary)"
        if what == "profile":
            llm = getattr(system_config, "llm_system", None)
            profiles = getattr(llm, "decision_profiles", None) or {}
            if name not in profiles:
                return f"decision profile {name!r} is not configured (known: {', '.join(profiles) or 'none'})"
            return None
        return None

    return check
