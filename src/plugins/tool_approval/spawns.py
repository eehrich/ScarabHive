"""Calls that start work this run's approvals may not reach.

A sub-run whose agent has this hook on inherits the run's rules (policy.py).
One whose agent has it off does not: its calls pass no approval at all, and a
deny rule of this run would be one spawn away from being void. So such a spawn
is decided on its own (hooks.py): blocked in ``auto``, asked with a warning in
``ask``, blocked where nobody can be asked.

What counts as a spawn -- by the server's TYPE, since an instance may be named
anything (``coder_sam`` is a sub_agent_manager):

* ``sub_agent_manager`` ``manage_sub_agent`` with operation ``create`` (the
  agent named by ``agent_type``) or ``continue`` (the agent of the sub-session
  ``instance_id``); the operation inferred as the manager infers it when the
  model left it out.
* an agent called as a tool: the server itself is an Agent -- any of its tools
  but a schema agent's ``list_available_tools``, which is routed to a method
  that only lists (a plain Agent runs itself on any name, and a script's call
  reaches names no model was offered).
* ``coding_cli`` ``run_task``: Claude Code, a process of its own that passes
  no hook of ours -- never guarded.
* ``stategraph`` ``run_machine``, ``send_event``, and ``control_run`` with an
  action that makes a run execute activities (terminate included: a machine's
  ``finally`` activities run first): a machine's ``tool:`` activities pass no
  hook (the framework calls them), so a machine is never guarded, whatever its
  agents are. For the same reason an agent of type ``stategraph_machine`` is
  never guarded, as a sub-agent or as a tool.

The target agent's hooks are read from the agent the process built from the
configuration: the registry instance and its merged ``agent_config`` -- the
config it runs with, never the raw YAML. A target the registry does not have is
left to the tool, which refuses it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from agent_system.hooks.registry import get_hook_registry
from agent_system.servers.agent.components.hook_integration import hook_runs_for

logger = logging.getLogger(__name__)

#: stategraph control_run actions that make a machine run activities. terminate
#: too: a machine's ``finally`` activities run before it ends, live or interrupted.
_STATEGRAPH_RUNS_ON = frozenset({"continue", "step", "run_to", "resume", "fork", "terminate"})
#: Tools of an agent server that do not run the agent.
_AGENT_READS_ONLY = frozenset({"list_available_tools"})
#: Agent types whose runs pass no hook of ours, whatever their config says: a
#: stategraph machine as an agent runs its ``tool:`` activities unhooked.
_NEVER_GUARDED_AGENT_TYPES = frozenset({"stategraph_machine"})


@dataclass(frozen=True)
class Spawn:
    """A call that starts work outside this run's own loop."""

    target: str      # what the person and the model read: "agent 'x'", "Claude Code (coding_cli)"
    guarded: bool    # the work passes this approval hook (and inherits the run's rules)


def _registry_instance(agent: Any, server: str) -> Any:
    registry = getattr(agent, "registry", None)
    if registry is None:
        return None
    try:
        return registry.get(server)
    except KeyError:
        return None


def _server_type(agent: Any, server: str, instance: Any) -> Optional[str]:
    """The plugin type of ``server``: from the config it was built with, else
    from the configuration system."""
    server_config = getattr(instance, "server_config", None)
    declared = getattr(server_config, "type", None)
    if isinstance(declared, str) and declared:
        return declared
    system_config = getattr(agent, "system_config", None)
    if system_config is None:
        return None
    try:
        from agent_system.config.settings import get_tool_server_config
        resolved = get_tool_server_config(server, system_config)
    except Exception:
        logger.debug("tool_approval: no config for server %s", server, exc_info=True)
        return None
    return getattr(resolved, "type", None)


def _is_agent(instance: Any) -> bool:
    from agent_system.servers.agent.server import Agent
    return isinstance(instance, Agent)


def _reads_only(instance: Any, tool: str) -> bool:
    """Whether calling ``tool`` on the agent ``instance`` only reads: a schema
    agent routes the name to its method of that name (``list_available_tools``
    lists tools). A plain Agent -- a machine agent is one -- runs itself on ANY
    name it is called with (``Agent.call``), and a script's call reaches names
    no model was offered."""
    if tool not in _AGENT_READS_ONLY:
        return False
    from agent_system.tools.schema_mixin import SchemaBasedToolMixin
    return isinstance(instance, SchemaBasedToolMixin) and callable(getattr(instance, tool, None))


def agent_is_guarded(instance: Any, hook_name: str) -> bool:
    """Whether this approval hook runs for the agent ``instance``: its
    effective ``agent_config.hooks`` against the hook's registered default --
    and its runs are the model's loop at all (not a stategraph machine's)."""
    kind = getattr(getattr(instance, "server_config", None), "type", None)
    if kind in _NEVER_GUARDED_AGENT_TYPES:
        return False
    info = get_hook_registry().get_hook_info(hook_name)
    default = bool(info.get("enabled", False)) if info else False
    hooks = getattr(getattr(instance, "agent_config", None), "hooks", None)
    return hook_runs_for(hooks, hook_name, default)


def infer_sub_agent_operation(arguments: Mapping[str, Any]) -> Optional[str]:
    """The operation of a manage_sub_agent call, as the manager reads it
    (SubAgentManagerServer.manage_sub_agent / _infer_operation)."""
    operation = arguments.get("operation")
    if operation:
        return str(operation)
    has_type = bool(arguments.get("agent_type"))
    has_id = bool(arguments.get("instance_id"))
    has_prompt = bool(arguments.get("message") or arguments.get("task"))
    if has_type and not has_id:
        return "create"
    if has_id and has_prompt and not has_type:
        return "continue"
    return None


async def _continued_agent(context: Any, instance_id: Any) -> Optional[str]:
    """The agent of the caller's sub-session ``instance_id``, as the manager
    finds it (_callers_sub_session). "" when the manager would refuse the call
    (no such sub-session of this session); None when it cannot be told."""
    service = getattr(getattr(context, "agent", None), "_session_service", None)
    sessions = getattr(service, "session_manager", None)
    user_id = getattr(context, "user_id", None)
    if sessions is None or not user_id or not isinstance(instance_id, str) or not instance_id:
        return None
    try:
        sessions._validate_session_id(instance_id)
    except ValueError:
        return ""          # no session is called that: the manager refuses
    except Exception:
        return None
    try:
        data = await sessions.load_session(user_id, instance_id)
    except Exception as exc:
        if isinstance(exc, FileNotFoundError) or type(exc).__name__ == "SessionNotFoundError":
            return ""      # none of this user's: the manager refuses
        logger.warning("tool_approval: sub-session %s could not be read (%s)", instance_id, exc)
        return None
    if not isinstance(data, Mapping):
        return None
    if (data.get("parent_session") or {}).get("session_id") != getattr(context, "session_id", None):
        return ""          # another session's: the manager refuses
    return str(data.get("agent_name") or "") or None


def is_script(context: Any, server: str) -> bool:
    """Whether ``server`` runs scripts (tool_script): a script is allowed call by
    call, never for a session -- the next one's code would go unread."""
    agent = getattr(context, "agent", None)
    if not server or "." in server or agent is None:
        return False
    return _server_type(agent, server, _registry_instance(agent, server)) == "tool_script"


async def spawn_of(context: Any, server: str, name: str, arguments: Mapping[str, Any],
                   hook_name: str) -> Optional[Spawn]:
    """The spawn a call makes, or None when it starts nothing of the kind (or
    the tool will refuse it, as for an agent the registry does not have)."""
    agent = getattr(context, "agent", None)
    if not server or "." in server or agent is None:
        return None          # an external MCP tool starts none of ours
    instance = _registry_instance(agent, server)
    tool = name[len(server) + 1:] if name.startswith(f"{server}_") else name
    if instance is not None and _is_agent(instance):
        if agent_is_guarded(instance, hook_name):
            return Spawn(target=f"agent '{server}'", guarded=True)
        return None if _reads_only(instance, tool) else Spawn(target=f"agent '{server}'", guarded=False)
    kind = _server_type(agent, server, instance)
    if kind == "sub_agent_manager" and tool == "manage_sub_agent":
        operation = infer_sub_agent_operation(arguments)
        if operation == "create":
            target_name = arguments.get("agent_type")
            target = _registry_instance(agent, target_name) if isinstance(target_name, str) else None
            if target is None:
                return None  # the manager refuses an agent its registry does not have
            return Spawn(target=f"agent '{target_name}'", guarded=agent_is_guarded(target, hook_name))
        if operation == "continue":
            agent_name = await _continued_agent(context, arguments.get("instance_id"))
            if agent_name == "":
                return None
            if agent_name is None:
                return Spawn(target=f"the sub-agent '{arguments.get('instance_id')}'", guarded=False)
            target = _registry_instance(agent, agent_name)
            if target is None:
                return None  # the manager refuses: "Agent type ... not found"
            return Spawn(target=f"agent '{agent_name}'", guarded=agent_is_guarded(target, hook_name))
        return None
    if kind == "coding_cli" and tool == "run_task":
        return Spawn(target="Claude Code (coding_cli)", guarded=False)
    if kind == "stategraph" and (tool in ("run_machine", "send_event") or (
            tool == "control_run" and arguments.get("action") in _STATEGRAPH_RUNS_ON)):
        machine = arguments.get("machine_id") or arguments.get("run_id") or "?"
        return Spawn(target=f"the state machine '{machine}'", guarded=False)
    return None
