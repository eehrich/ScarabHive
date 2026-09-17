"""Slash commands that plugins bring along — collection and execution.

A plugin declares them in its ``schema.yaml``, next to its tools::

    commands:
      - name: compact
        description: "compact this conversation now"
        tool: "{{ name }}_compact"
        argument: query          # optional: rest of the line -> this parameter
        argument_hint: "<text>"  # optional: shown in /help
        params: {operation: list}  # optional: fixed for every call

Every command RUNS ONE OF THE PLUGIN'S OWN TOOLS. That is the whole design
decision: a plugin gets a shorter way to something the agent can already do,
not a second entry point beside its tools that skips authorization, runtime
params (``_session_id``, ``_agent``) and the status channel. Execution goes
through ``Agent.dispatch_tool_call``, the same in-process path tool_script
uses, so a command is exactly as powerful as the agent's allowlist permits —
and a command whose tool the agent may NOT call is never listed in the first
place (``Agent.tool_dispatch_denial``, the same predicate dispatch raises on).

What this is deliberately NOT: prompt expansion. Claude Code's plugin commands
and MCP prompts expand into a message for the model; here skills already do
that (``/writer analysiere X``), and a second mechanism for the same thing
would only split the namespace further. A plugin command does something and
prints the result — no LLM turn, no tokens.

The catalogue types live in ``agent_system.chat_commands`` with the built-ins;
this module is the part that needs a running agent, which keeps that one
import-light and usable from the web surface.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from agent_system.chat_commands import (
    PluginCommand,
    is_builtin_command,
    is_typeable_command_name,
)

logger = logging.getLogger(__name__)

#: Parameter names a command must not bind its argument to, compared with
#: underscores removed so ``requestId`` and ``request_id`` are the same name.
#:
#: Everything with a LEADING underscore, and ``request_id``/``requestId`` (the
#: status channel's routing key, ``ToolServer.call_with_status``), is dropped by
#: ``dispatch_tool_call`` before the real runtime params are injected -- a
#: command bound to one would silently lose its argument. The other two get
#: past the strip, because they are read AFTER it: ``session_id`` is read as an
#: alias of ``_session_id`` by the agent-as-tool path (``Agent.call``), and
#: ``agent_name`` selects the target agent in lessons_learned. Deliberately
#: NOT here: ``status`` and ``user_id`` -- enumerated the consumers, both are
#: ordinary tool parameters (todo, lessons_learned, writer_audio), and
#: forbidding them would block a perfectly good ``/todo open``.
_RESERVED_ARGUMENTS = frozenset({"requestid", "sessionid", "agentname"})


def _is_reserved_argument(argument: str) -> bool:
    return (argument.startswith("_")
            or argument.replace("_", "").lower() in _RESERVED_ARGUMENTS)


def collect_plugin_commands(agent: Any) -> list[PluginCommand]:
    """Every plugin command *agent* can actually run, sorted by name.

    Reads the rendered schema of each registered server, so ``{{ name }}`` in a
    command's tool has already become the configured instance name — a plugin
    mounted twice under different names yields two distinct commands.

    A broken or command-less plugin is skipped, never fatal: the chat REPL must
    come up even when one plugin's schema does not load.
    """
    registry = getattr(agent, "registry", None)
    if registry is None:
        return []
    try:
        server_names = list(registry.list())
    except Exception:  # noqa: BLE001 - a broken registry must not kill the REPL
        logger.debug("Could not list registry servers", exc_info=True)
        return []

    commands: list[PluginCommand] = []
    for server_name in server_names:
        for raw in _declared_commands(registry, server_name):
            command = _build_command(server_name, raw)
            if command is None:
                continue
            denial = _denial(agent, command.tool)
            if denial is not None:
                logger.debug("Hiding /%s: %s", command.qualified, denial)
                continue
            commands.append(command)
    return sorted(commands, key=lambda c: (c.name, c.plugin))


def _declared_commands(registry: Any, server_name: str) -> list[dict]:
    """The raw ``commands:`` entries of one server, or an empty list."""
    try:
        server = registry.get(server_name)
        getter = getattr(server, "get_schema_data", None)
        if getter is None:
            return []
        declared = (getter() or {}).get("commands")
    except Exception:  # noqa: BLE001 - one unloadable schema, not all of them
        logger.debug("Could not read schema of '%s'", server_name, exc_info=True)
        return []
    if declared is None:
        return []
    if not isinstance(declared, list):
        logger.warning("Plugin '%s': 'commands' must be a list, got %s",
                       server_name, type(declared).__name__)
        return []
    return [entry for entry in declared if isinstance(entry, dict)]


def _build_command(server_name: str, raw: dict) -> Optional[PluginCommand]:
    """One schema entry as a PluginCommand, or None if it is unusable.

    A malformed entry is named in the log rather than dropped quietly — the
    author would otherwise be looking for a command that simply never appears.
    """
    name = str(raw.get("name") or "").strip()
    tool = str(raw.get("tool") or "").strip()
    if not name or not tool:
        logger.warning("Plugin '%s': command entry needs 'name' and 'tool', got %r",
                       server_name, raw)
        return None
    if not is_typeable_command_name(name):
        logger.warning("Plugin '%s': command name %r cannot be typed as a "
                       "slash command and would only sit in the help",
                       server_name, name)
        return None
    argument = raw.get("argument")
    if argument and _is_reserved_argument(str(argument).strip()):
        logger.warning("Plugin '%s': command %r may not bind its argument to "
                       "the reserved parameter %r", server_name, name, argument)
        return None
    params = _fixed_params(server_name, name, raw.get("params"),
                           str(argument).strip() if argument else None)
    if params is None:
        return None
    return PluginCommand(
        plugin=server_name,
        name=name,
        summary=str(raw.get("description") or "").strip(),
        tool=tool,
        argument=str(argument).strip() if argument else None,
        argument_hint=str(raw.get("argument_hint") or "").strip(),
        params=params,
    )


def _fixed_params(server_name: str, command: str, raw: Any,
                  argument: Optional[str]) -> Optional[dict[str, Any]]:
    """The parameters a command fixes for every call, or None if unusable.

    They are what makes a UNIFIED tool reachable: sub_agent_manager offers one
    tool with an ``operation`` of nine values, and without a fixed one a
    command could only ever bind the rest of the line -- there is nowhere to
    say WHICH operation. None means the whole command is dropped: a command
    that silently ran with half its parameters would call the wrong operation.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("Plugin '%s': command %r: 'params' must be a mapping, got %s",
                       server_name, command, type(raw).__name__)
        return None
    params: dict[str, Any] = {}
    for key, value in raw.items():
        key = str(key).strip()
        if not key:
            logger.warning("Plugin '%s': command %r has a parameter without a name",
                           server_name, command)
            return None
        if _is_reserved_argument(key):
            logger.warning("Plugin '%s': command %r may not set the reserved "
                           "parameter %r", server_name, command, key)
            return None
        if argument and key == argument:
            # One of the two would win silently, and which one is an
            # implementation detail nobody should have to know.
            logger.warning("Plugin '%s': command %r fixes %r and also binds its "
                           "argument to it", server_name, command, key)
            return None
        params[key] = value
    return params


def _denial(agent: Any, tool: str) -> Optional[str]:
    """Why the agent may not run *tool*, or None.

    Resolves the OWNING server the same way ``dispatch_tool_call`` does and
    then asks the agent's own predicate. Using the declaring plugin's name
    instead would be a second definition of "which server owns this tool", and
    the two disagree as soon as a command names a tool that is not its own:
    the listing would promise a command that can only answer with a refusal.
    Resolving here also names a tool NO server provides — otherwise invisible,
    because the unknown tool is dropped by authorization without a word. (A
    typo that still hits the plugin's own prefix is caught at test time by
    tests/pluginsystem/test_plugin_command_declarations.py.)

    An agent that cannot answer (a test double, a non-Agent registry entry) is
    not silently treated as permissive: it gets no plugin commands at all,
    because nothing here can establish that it may run them.
    """
    resolve = getattr(agent, "_resolve_flat_tool_name", None)
    predicate = getattr(agent, "tool_dispatch_denial", None)
    if resolve is None or predicate is None:
        return "agent cannot authorize tool dispatch"
    try:
        server, server_name = resolve(tool)
        if server is None:
            logger.warning("No server provides tool '%s' named by a plugin "
                           "command — check the 'tool:' in its schema.yaml", tool)
            return f"no server provides {tool!r}"
        return predicate(tool, server_name)
    except Exception as e:  # noqa: BLE001 - deny on doubt, never list on doubt
        logger.debug("Authorization check for %s failed", tool, exc_info=True)
        return f"authorization check failed: {e}"


def format_command_result(result: Any) -> str:
    """A tool result as one readable block for the terminal.

    Plugin tools answer with a status dict, which is written for the model. A
    person gets the plugin's own ``message`` when there is one, otherwise the
    fields as they are — no per-command formatting rules, because those would
    have to be kept in step with every plugin that ever adds a command.
    """
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result)
    status = result.get("status")
    if status == "error":
        return f"error: {result.get('error') or result.get('message') or 'failed'}"
    message = result.get("message")
    if isinstance(message, str) and message:
        return message
    # "status: success" on every single command is noise; everything else the
    # plugin chose to report is kept.
    fields = {k: v for k, v in result.items()
              if not k.startswith("_") and not (k == "status" and v == "success")}
    if not fields:
        return "done"
    lines: list[str] = []
    for key, value in fields.items():
        if isinstance(value, list) and not value:
            # "instances: []" is a repr of nothing, in answer to a question
            # somebody asked in words.
            lines.append(f"{key}: (none)")
            continue
        rows = _record_rows(value)
        if rows:
            lines.append(f"{key} ({len(value)}):")
            lines.extend(rows)
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


#: Rows a command prints before it starts summarising. Twenty sub-agents or
#: twenty tool servers is already more than anyone reads at a prompt.
_MAX_ROWS = 20


def _record_rows(value: Any) -> list[str]:
    """A list of records as one line each, or [] when it is not one.

    Plugin tools answer for the MODEL, and a list of dicts printed with str()
    is one long Python repr -- `/subagents` and `/mcp` both return exactly
    that. Generic on purpose: a per-command formatting rule would have to be
    kept in step with every plugin that ever adds a command.
    """
    from agent_system.chat_actions import one_line

    if not isinstance(value, list) or not value:
        return []
    if not all(isinstance(row, dict) for row in value):
        return []
    lines = []
    for row in value[:_MAX_ROWS]:
        fields = [f"{key}={one_line(item, 40)}" for key, item in row.items()
                  if item not in (None, "", [], {}) and not str(key).startswith("_")]
        lines.append("  " + "  ".join(fields) if fields else "  (empty)")
    if len(value) > _MAX_ROWS:
        lines.append(f"  ... {len(value) - _MAX_ROWS} more")
    return lines


async def run_plugin_command(
    agent: Any,
    command: PluginCommand,
    payload: str = "",
    *,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """Run *command* and return what to print.

    Never raises: a plugin command is a convenience at the prompt, and a REPL
    that dies because one plugin threw would take the whole session with it.
    The failure is reported on the line where it happened, not swallowed.
    """
    payload = payload.strip()
    if payload and not command.argument:
        return (f"/{command.name} takes no arguments "
                f"(got {payload!r}).")
    # The fixed ones first, the typed one on top -- they cannot collide,
    # _fixed_params drops a command that declares both for one parameter.
    params: dict[str, Any] = dict(command.params)
    if command.argument and payload:
        params[command.argument] = payload
    try:
        result = await agent.dispatch_tool_call(
            command.tool, params, session_id=session_id, user_id=user_id)
        # Inside the try as well: rendering is where a value the plugin chose
        # meets json.dumps, and a REPL that dies because one plugin put a
        # datetime in a record would take the whole session with it.
        return format_command_result(result)
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.warning("Plugin command /%s failed: %s", command.qualified, e,
                       exc_info=True)
        return f"/{command.name} failed: {e}"


def spellings(commands: Sequence[PluginCommand]) -> list[str]:
    """How each command must be typed, without the slash, in the same order.

    Bare while its name is unique AND not taken by a built-in, ``plugin:name``
    otherwise -- the same rule the resolution actually follows. Help and typo
    suggestions both read it from here, so neither can offer a spelling that
    does not reach the command: a plugin naming a command ``history`` would
    otherwise be advertised as ``/history``, which lands on the built-in.
    """
    names = [c.name.lower() for c in commands]
    return [c.name if names.count(c.name.lower()) == 1
            and not is_builtin_command(c.name)
            else c.qualified
            for c in commands]


def help_lines(commands: Sequence[PluginCommand]) -> list[str]:
    """Help block for the plugin commands, or an empty list if there are none."""
    if not commands:
        return []
    displays = [
        f"/{spelling}" + (f" {c.argument_hint}" if c.argument_hint else "")
        for spelling, c in zip(spellings(commands), commands)
    ]
    width = max(len(d) for d in displays)
    lines = ["", "Plugin commands:"]
    lines += [f"  {display:<{width}}   {command.summary}".rstrip()
              for display, command in zip(displays, commands)]
    return lines
