from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import os
import sys
import threading
from pathlib import Path

import yaml
from typing import (Any, Dict, List, Literal, Optional, Tuple, Union,
                    get_args, get_origin)

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None

from .config.settings import get_mcp_config_by_name, load_settings
from .config.models import AgentSystemConfig
from .llm.models import ChatMessage
from .plugins import discover_all_plugins
from .mcp.base import MCPRegistry
from .mcp.status import status_bus
from .mcp.integration import MCPIntegration, initialize_mcp, shutdown_mcp
from .llm.batch.initialization import init_batch_system, shutdown_batch_system
from .utils.logging import setup_logging
from .servers.agent.server import Agent

# Import services
from .services import MCPService, ToolService
from .services.session_manager import SessionPermissionError
from .cli_utils.common import (
    supports_color as _supports_color,
    colorize as _colorize,
    set_color_mode,
    format_output_with_hooks,
    render_with_rich
)
from .cli_utils.commands.hooks import handle_hooks_command


def _literal_strings(annotation: Any) -> frozenset:
    """String values a field's type accepts verbatim (its ``Literal`` members).

    Walks nested generics so ``Optional[Literal["none", ...]]`` is covered.
    """
    found: set = set()
    todo = [annotation]
    while todo:
        ann = todo.pop()
        if get_origin(ann) is Literal:
            found.update(a for a in get_args(ann) if isinstance(a, str))
        else:
            todo.extend(get_args(ann))
    return frozenset(found)


def _coerce_cli_value(value: str, keep: frozenset = frozenset()) -> Any:
    """Auto-type a CLI KEY=VALUE value: int/float/bool/none, sonst String.

    ``keep`` holds spellings the target field accepts as a literal STRING;
    those win over the generic coercion. Without it ``thinking_level=none``
    became Python ``None`` — which drops the field instead of setting it, so
    the request went out with no ``reasoning`` at all and the provider default
    applied (DeepSeek: high). Asking for no thinking silently bought the most.
    """
    v = value.strip()
    low = v.lower()
    if low in keep:
        return low
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def parse_llm_params_args(raw_items: Optional[List[str]]) -> Optional[Dict[str, Any]]:
    """Parse ``--llm-params KEY=VALUE ...`` into a flat llm_params dict.

    Werte werden auto-getypt (``max_tokens=1000`` → int, ``stream=false`` →
    bool, ``thinking_level=max`` → str) — die LLM-Params-Validierung
    (LLMModelConfig-Re-Validierung in ``resolve_llm_config_for_agent``)
    braucht echte Typen, keine Strings. Leeres Ergebnis → ``None``.

    Zwei harte Fehler (``ValueError``) statt stiller Drift (Review-Befunde):
    - Eintrag ohne ``=``: das ist fast immer der vom greedy ``nargs='+'``
      verschluckte TASK-String — still überspringen hieße, der Agent läuft
      lautlos mit dem Default-Task.
    - Unbekannter Key (kein ``LLMModelConfig``-Feld): ``resolve_llm_params``
      würde ein Dict aus lauter Fremd-Keys als profil-gekeyte Form deuten
      und den Override LAUTLOS zu ``None`` mergen — ein Tippfehler
      (``temperatur=``) verschwände wirkungslos, während die CLI ihn als
      angewandt anzeigt.
    """
    if not raw_items:
        return None
    from .config.models import LLMModelConfig
    fields = LLMModelConfig.model_fields
    params: Dict[str, Any] = {}
    for item in raw_items:
        if "=" not in item:
            raise ValueError(
                f"invalid --llm-params entry (expected KEY=VALUE): {item!r}. "
                f"Steht --llm-params VOR dem Task? Task zuerst angeben oder "
                f"--llm-params ans Ende stellen."
            )
        key, _, value = item.partition("=")
        key = key.strip()
        if key:
            field = fields.get(key)
            keep = _literal_strings(field.annotation) if field else frozenset()
            params[key] = _coerce_cli_value(value, keep)
    if params:
        valid_keys = set(fields.keys())
        unknown = sorted(set(params) - valid_keys)
        if unknown:
            raise ValueError(
                f"unknown --llm-params key(s): {', '.join(unknown)}. "
                f"Valid keys: {', '.join(sorted(valid_keys))}"
            )
    return params or None


def _get_plugins_config(config: AgentSystemConfig):
    """Get plugins configuration."""
    return config.plugins


def _build_entry_agent(entry_name: str, config: AgentSystemConfig, registry: MCPRegistry,
                       session_service) -> Agent:
    """Build the entry agent when bootstrap did not register it, from its
    MERGED server config, and register it.

    This used to read the raw ``plugins.servers[name]`` entry -- without
    default_config and the ``type:`` inheritance chain that bootstrap applies
    to every other agent. Measured 2026-09-01 on the real config: 133 of 203
    agents carry a raw ``max_steps`` of 20 where the merged value is 100 or
    30, so an agent built here ran a quietly downgraded configuration.

    Exits with a listing of the available agents when the name has no
    agent config at all (unchanged behaviour). That gate keeps reading the
    RAW entry: plugins.default_config carries an agent_config, so every
    merged config has one and a merged gate would wave through any tool
    server name (measured on the real config: 96 of 219 servers have no raw
    agent_config, all of them tool servers, no agent among them).
    """
    logger.info("Creating new Agent instance '%s'", entry_name)
    raw_config = config.plugins.servers.get(entry_name) if config.plugins else None
    if not raw_config or not getattr(raw_config, 'agent_config', None):
        logger.error(f"Cannot create agent '{entry_name}': no agent_config found in MCP config")
        print(f"Error: Agent '{entry_name}' not found and cannot be created (no agent_config in plugins.yaml).", file=sys.stderr)
        print("\nAvailable agents:", file=sys.stderr)
        for name in registry.list():
            server = registry.get(name)
            if isinstance(server, Agent):
                print(f"  - {name}", file=sys.stderr)
        sys.exit(1)

    mcp_config = get_mcp_config_by_name(entry_name, config)
    agent = Agent(entry_name, config, mcp_config, registry, session_service=session_service)
    registry.register(entry_name, agent)
    return agent


logger = logging.getLogger(__name__)


async def _maybe_await_get_client(mcp_integration: MCPIntegration, name: str):
    """Call client_manager.get_client(name) and await if it returns a coroutine.

    Some implementations expose `get_client` as a coroutine (tests may use
    AsyncMock), while others provide a synchronous method. This helper
    abstracts that difference so callers can `await _maybe_await_get_client(..)`.
    """
    try:
        provider = getattr(mcp_integration, "external_provider", None)
        pool = getattr(provider, "pool", None) if provider else None
        if pool is None:
            return None
        res = pool.get(name)
    except Exception as e:
        logger.debug(f"Failed to get client {name}: {e}")
        return None
    import inspect
    if inspect.isawaitable(res):
        try:
            return await res
        except Exception as e:
            logger.debug(f"Failed to await client {name}: {e}")
            return None
    return res


async def _mcp_list_servers(mcp_service: MCPService, args: Any) -> None:
    """List configured external MCP servers using MCPService."""
    try:
        servers = await mcp_service.list_servers()

        if args.out_format == "json":
            print(json.dumps(servers, indent=2, ensure_ascii=False))
        else:
            # Table format
            if not servers:
                print("No external MCP servers configured.")
                return

            rows = []
            for server in servers:
                status = server.get("status", "unknown")

                if _supports_color():
                    if status == "connected":
                        status = _colorize("Connected", "32")  # green
                    elif status == "disconnected":
                        status = _colorize("Disconnected", "31")  # red
                    else:
                        status = _colorize("Disabled", "90")  # gray

                rows.append((
                    server["name"],
                    server.get("address", ""),
                    status,
                    server.get("description", "")
                ))

            headers = ["NAME", "ADDRESS", "STATUS", "DESCRIPTION"]
            if tabulate:
                print(tabulate(rows, headers=headers, tablefmt="github"))
            else:
                # Simple fallback
                if rows:
                    name_w = max(len(str(r[0])) for r in rows)
                    addr_w = max(len(str(r[1])) for r in rows)
                    status_w = max(len(str(r[2])) for r in rows)
                    desc_w = max(len(str(r[3])) for r in rows)
                else:
                    name_w = addr_w = status_w = desc_w = 10
                hdr = f"{'NAME'.ljust(name_w)}  {'ADDRESS'.ljust(addr_w)}  {'STATUS'.ljust(status_w)}  {'DESCRIPTION'.ljust(desc_w)}"
                print(hdr)
                print("-" * len(hdr))
                for n, a, s, d in rows:
                    print(f"{str(n).ljust(name_w)}  {str(a).ljust(addr_w)}  {str(s).ljust(status_w)}  {str(d).ljust(desc_w)}")

    except Exception as e:
        logger.exception("Failed to list MCP servers: %s", e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_connect_server(mcp_service: MCPService, server_name: str, args: Any) -> None:
    """Connect to an external MCP server using MCPService."""
    try:
        result = await mcp_service.connect_server(server_name)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to connect to server: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_disconnect_server(mcp_service: MCPService, server_name: str, args: Any) -> None:
    """Disconnect from an external MCP server using MCPService."""
    try:
        result = await mcp_service.disconnect_server(server_name)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to disconnect from server: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_status_servers(mcp_service: MCPService, server_name: str | None, args: Any) -> None:
    """Show status of external MCP servers using MCPService."""
    try:
        if server_name:
            status_info = await mcp_service.get_server_status(server_name)
            print(json.dumps(status_info, indent=2, ensure_ascii=False))
        else:
            # Status for all servers - just list them
            await _mcp_list_servers(mcp_service, args)
    except Exception as e:
        logger.exception("Failed to get server status: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_test_server(mcp_service: MCPService, server_name: str, args: Any) -> None:
    """Test connectivity and basic functionality of an external MCP server using MCPService."""
    try:
        result = await mcp_service.test_server(server_name)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to test server: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_tool_management(tool_service: ToolService, server_name: str, args: Any) -> None:
    """Manage tools for a specific MCP server (list, allow, block) using ToolService."""
    # Tool action is in the 'key' argument
    tool_action = getattr(args, 'key', None)
    if not tool_action:
        print(json.dumps({"error": "Tool action required: list, allow, or block"}, ensure_ascii=False))
        return

    if tool_action == "list":
        await _list_server_tools_via_service(tool_service, server_name, args)
    elif tool_action == "allow":
        tool_name = getattr(args, 'value', None)
        if not tool_name:
            print(json.dumps({"error": "Tool name required for allow action"}, ensure_ascii=False))
            return
        await _allow_server_tool(tool_service, server_name, tool_name)
    elif tool_action == "block":
        tool_name = getattr(args, 'value', None)
        if not tool_name:
            print(json.dumps({"error": "Tool name required for block action"}, ensure_ascii=False))
            return
        await _block_server_tool(tool_service, server_name, tool_name)
    else:
        print(json.dumps({"error": f"Unknown tool action: {tool_action}. Use list, allow, or block"}, ensure_ascii=False))


async def _list_server_tools_via_service(tool_service: ToolService, server_name: str, args: Any) -> None:
    """List all available tools for a server using ToolService."""
    try:
        result = await tool_service.list_tools(server_name, include_filtering=True)

        # Handle output format
        if getattr(args, 'out_format', 'json') == "table":
            # Table format output
            print(f"\nServer: {server_name}")
            print("=" * (len(server_name) + 8))

            available_tools = result.get("tools", [])
            if not available_tools:
                print("No tools available")
            else:
                filtering = result.get("filtering", {})
                blocked = set(filtering.get("blocked_tools") or [])
                allowed = set(filtering.get("allowed_tools") or [])

                print(f"\nAvailable Tools ({len(available_tools)}):")
                print("-" * 30)
                for tool in available_tools:
                    status = ""
                    if tool in blocked:
                        status = " [BLOCKED]"
                    elif allowed and tool not in allowed:
                        status = " [NOT ALLOWED]"
                    print(f"  {tool}{status}")

                effective = result.get("effective_tools", available_tools)
                print(f"\nEffective Tools ({len(effective)}):")
                print("-" * 30)
                for tool in effective:
                    print(f"  {tool}")

                if blocked or allowed:
                    print("\nFiltering Configuration:")
                    print("-" * 30)
                    if allowed:
                        print(f"  Allowed: {', '.join(sorted(allowed))}")
                    if blocked:
                        print(f"  Blocked: {', '.join(sorted(blocked))}")
        else:
            # JSON format output
            print(json.dumps(result, indent=2, ensure_ascii=False))

    except Exception as e:
        logger.exception("Failed to list tools: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _allow_server_tool(tool_service: ToolService, server_name: str, tool_name: str) -> None:
    """Allow a tool for a server using ToolService."""
    try:
        result = await tool_service.allow_tool(server_name, tool_name)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to allow tool: server=%s, tool=%s, error=%s", server_name, tool_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _block_server_tool(tool_service: ToolService, server_name: str, tool_name: str) -> None:
    """Block a tool for a server using ToolService."""
    try:
        result = await tool_service.block_tool(server_name, tool_name)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to block tool: server=%s, tool=%s, error=%s", server_name, tool_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


#: The CLI runs a SEQUENCE of coroutines with plain synchronous code between
#: them: bootstrap, batch system, session handling, the agent run, then the
#: shutdowns. ``asyncio.run`` gives each of those its own loop and CLOSES it on
#: return -- which silently kills whatever a previous step left running.
#:
#: That is not theoretical. An external MCP server connects during bootstrap
#: and keeps a task alive for the session; ``ServerConnection.connected`` is
#: ``self._task is not None and not self._task.done()``. With a loop per step
#: that task is already done when the agent asks for tools, so the pool reports
#: no connected server, the catalogue comes back empty, and the agent silently
#: gets zero external tools -- while ``agent-cli mcp test`` works, because it
#: opens and uses a single loop of its own. Measured 2026-09-01:
#: "External MCP servers: 2 connected, 0 failed" followed seconds later by
#: ``connected=[]``.
#:
#: One loop for the whole process fixes it without restructuring anything: the
#: call sites keep their order and the synchronous code between them stays put.
_cli_loop: Optional[asyncio.AbstractEventLoop] = None
#: The thread the shared loop belongs to. A loop may only be driven from the
#: thread that created it, and ``asyncio.run`` gave every thread its own by
#: construction -- a property this helper would otherwise silently drop.
_cli_loop_thread: Optional[int] = None


def get_cli_loop() -> asyncio.AbstractEventLoop:
    """The CLI's shared loop, created on first use.

    For the rare caller that needs the loop OBJECT rather than to run one
    coroutine -- the chat REPL drives it directly with ``run_until_complete``
    per turn. Handing chat its own loop instead would strand the MCP
    connections from bootstrap on a loop that never runs again: ``connected``
    stays True (the task is not done, its loop is merely parked), every
    call runs into the submit timeout, and the tools fail slowly instead of
    working. Borrowers must NOT close it; ``close_cli_loop`` owns teardown.
    """
    global _cli_loop, _cli_loop_thread
    if _cli_loop is None or _cli_loop.is_closed():
        _cli_loop_thread = threading.get_ident()
        _cli_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_cli_loop)
        # Every exit path -- early return of a subcommand, exception, sys.exit
        # -- must still tear the loop down, so register once, here, instead of
        # hoping a finally block covers them all.
        atexit.register(close_cli_loop)
    return _cli_loop


def run_async(coro: Any) -> Any:
    """Run one coroutine on the CLI's single, persistent event loop.

    Drop-in for ``asyncio.run`` at this layer, with the one difference that
    matters: the loop stays open afterwards, so anything the coroutine started
    is still alive for the next call.

    Off the owning thread it falls back to ``asyncio.run``. Sharing the loop
    there would be a cross-thread use of an event loop -- the sequencing this
    exists for is a property of the CLI's single main thread, not of the
    process.
    """
    if _cli_loop is not None and _cli_loop_thread != threading.get_ident():
        return asyncio.run(coro)
    return get_cli_loop().run_until_complete(coro)


def stored_session_settings(session_manager: Any, session_user: str,
                            session_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The agent and LLM profile a session was started with, or (None, None).

    Every session record carries both. Reading them back is what lets a bare
    ``--session <id>`` continue a conversation the way it was begun; without
    it the CLI fell back to the config defaults, so the same conversation
    silently went on with a different agent on a different model.

    A session that cannot be read is not an error here -- ``--session`` also
    NAMES a new session. The load further down reports a real problem with
    the message that belongs to it.
    """
    if not session_id or session_manager is None:
        return None, None
    try:
        data = run_async(session_manager.load_session(session_user, session_id))
    except Exception:
        logger.debug("No stored settings for session %s", session_id, exc_info=True)
        return None, None
    if not isinstance(data, dict):
        return None, None
    return (data.get("agent_name") or None), (data.get("llm_profile") or None)


def usable_session_defaults(stored_agent: Optional[str], stored_llm: Optional[str],
                            config: Any) -> tuple[Optional[str], Optional[str]]:
    """Drop stored values the current configuration no longer knows.

    A session record is a memory, not an instruction. Sessions outlive the
    config that made them -- agents get renamed or removed, profiles get
    retired (measured on this repo: 707 of 2914 cli_user sessions name an
    agent that no longer exists) -- and the gates further down were written
    for names a PERSON typed, so they abort. Turning "continue this
    conversation" into a hard exit over a name the user never mentioned would
    be a worse bug than the one this whole feature fixes.

    An explicitly typed --agent/--llm is untouched by this: it still has to
    exist, and still aborts loudly when it does not.
    """
    agent = stored_agent
    if agent:
        raw = config.plugins.servers.get(agent) if getattr(config, "plugins", None) else None
        # The RAW entry, the same gate _build_entry_agent uses: the merged
        # config gives every tool server an agent_config.
        if not raw or not getattr(raw, "agent_config", None):
            logger.info("Session names agent '%s', which this config does not "
                        "define -- falling back to the default", agent)
            agent = None

    profile = stored_llm
    if profile:
        llm_system = getattr(config, "llm_system", None)
        profiles = getattr(llm_system, "profiles", None) or {}
        if profile not in profiles:
            logger.info("Session names LLM profile '%s', which llm.yaml no "
                        "longer defines -- falling back to the agent's own",
                        profile)
            profile = None

    return agent, profile


def choose_agent_name(override: Optional[str], stored: Optional[str],
                      default: str) -> str:
    """Which agent runs: what was asked for, what the session used, the default.

    The stored name only outranks the CONFIG DEFAULT, never an explicit
    --agent. Without it, continuing a session with a bare --session handed the
    conversation to whatever agent the config happens to name today.
    """
    return override or stored or default


def choose_llm_profile(override: Optional[str], stored_llm: Optional[str],
                       stored_agent: Optional[str], entry_name: str,
                       agent_default: Optional[str]) -> Optional[str]:
    """Which LLM profile to force, or None to leave the agent on its own.

    Three conditions before a stored profile is used, and each one has a
    reason. An explicit --llm wins. The agent must be the one the profile was
    stored for -- a profile picked for another agent has no business being
    forced onto this one. And a profile that IS the agent's default is not an
    override at all; returning it would build a second client for nothing.
    """
    if override:
        return override
    if not stored_llm or entry_name != stored_agent:
        return None
    if stored_llm == agent_default:
        return None
    return stored_llm


def close_cli_loop() -> None:
    """Tear down the CLI loop: cancel leftovers, close async generators, close.

    This is what ``asyncio.run`` did after every single step. Doing it ONCE at
    process exit is the whole point -- doing it in between was the bug.
    """
    global _cli_loop, _cli_loop_thread
    loop, _cli_loop = _cli_loop, None
    _cli_loop_thread = None
    if loop is None or loop.is_closed():
        return
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        # Subprocess transports (the stdio MCP servers!) are torn down by the
        # executor thread pool; without waiting for it the interpreter can
        # outrun those threads and their __del__ lands on a closed loop.
        # Chat's own teardown learned this the hard way -- same reason here.
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception as exc:  # pragma: no cover - best effort at exit
        logging.getLogger(__name__).debug("CLI loop teardown: %s", exc)
    finally:
        loop.close()
        # asyncio.run leaves the thread's loop slot EMPTY afterwards (measured:
        # get_event_loop -> RuntimeError "no current event loop"). Leaving our
        # closed loop in the slot instead would hand later get_event_loop()
        # callers a dead loop and "Event loop is closed" errors.
        asyncio.set_event_loop(None)


def main() -> None:
    global logger
    # Windows-Konsolen/Pipes laufen oft mit cp1252 — Unicode in Ausgaben
    # (Box-Zeichen der Plugin-Tabelle, Emojis in Beschreibungen) crashte dann
    # mit UnicodeEncodeError.
    # - Terminal (tty): Encoding beibehalten, nicht darstellbare Zeichen
    #   ersetzen (Anzeige degradiert sichtbar statt zu crashen).
    # - Pipe/Datei (non-tty): UTF-8 erzwingen — Maschinen-Konsum (z.B.
    #   `agent-cli mcp status | jq`) bekommt byte-treue Daten statt stiller
    #   '?'-Korruption. Gleiche Konvention wie utils/logging.py.
    # - stdin: nur errors="replace" (kein Encoding-Wechsel) — verhindert
    #   UnicodeDecodeError bei Paste/Pipe-Input in Chat-Modi.
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                if _stream.isatty():
                    _stream.reconfigure(errors="replace")
                else:
                    _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass  # exotische Streams (Tests, Pipes) — Verhalten wie bisher
    if sys.stdin is not None and hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(errors="replace")
        except Exception:
            pass

    # Backward-compatible: allow calling `agent-cli <task>` without an explicit subcommand.
    # If the first non-option arg isn't a known subcommand, inject an implicit 'run' subcommand.
    # Use a two-stage parse: first extract global options from anywhere using parse_known_args,
    # then parse the remaining args (subcommand + subargs). This avoids confusing option values
    # with subcommands when we need to insert an implicit 'run'.
    prelim = argparse.ArgumentParser(add_help=False)
    prelim.add_argument("--config", dest="config", default=str(Path("config/config.yaml")))
    prelim.add_argument("-v", "--verbose", dest="verbose", action="store_true")
    # color can be set to auto/always/never/ansi/html/text.
    # Default 'auto', not 'always': 'always' emitted escape sequences into
    # redirected output and into consoles that render them literally.
    prelim.add_argument("--color", dest="color", choices=["auto", "always", "never", "ansi", "html", "text"], default="auto")
    prelim.add_argument("--no-color", dest="no_color", action="store_true")
    prelim.add_argument("--show-mcp", dest="show_mcp", action="store_true")
    prelim.add_argument("--no-status", dest="no_status", action="store_true")
    prelim.add_argument("--raw", dest="raw", action="store_true")
    orig_args = sys.argv[1:]
    ns, rest = prelim.parse_known_args(orig_args)

    # decide color mode early so helpers behave predictably
    if getattr(ns, "no_color", False):
        set_color_mode("never")
    else:
        set_color_mode(getattr(ns, "color", "auto"))

    # Initialize colorama on interactive TTYs so ANSI renders on Windows
    try:
        mode = "never" if getattr(ns, "no_color", False) else getattr(ns, "color", "auto")
        if mode != "never" and sys.stdout.isatty():
            import colorama
            colorama.init()
    except Exception:
        pass

    # If the first token of the remaining args isn't a known subcommand, insert implicit 'run'
    known = ("plugins", "mcp", "hooks", "run", "chat", "users", "reload", "-h", "--help")
    if rest:
        if not rest[0].startswith("-") and rest[0] not in known:
            rest.insert(0, "run")
    else:
        # no remaining tokens: nothing to parse further
        rest = []

    # Reconstruct final argv for full parsing: prepend any global options we care about
    final_args = []
    if getattr(ns, "config", None):
        final_args.extend(["--config", ns.config])
    if getattr(ns, "verbose", False):
        final_args.append("--verbose")
    if getattr(ns, "no_color", False):
        final_args.append("--no-color")
    elif getattr(ns, "color", "auto") != "auto":
        final_args.extend(["--color", ns.color])
    # Forward streaming/raw flags parsed in the preliminary stage so the
    # final parser receives the same intent (these flags may have been
    # placed anywhere on the command line by the user).
    if getattr(ns, "show_mcp", False):
        final_args.append("--show-mcp")
    if getattr(ns, "no_status", False):
        final_args.append("--no-status")
    if getattr(ns, "raw", False):
        final_args.append("--raw")
    # append the remaining tokens (subcommand + subargs)
    argv = [sys.argv[0]] + final_args + rest

    parser = argparse.ArgumentParser(description="Agent System CLI")
    parser.add_argument("--config", dest="config", default=str(Path("config/config.yaml")), help="Path to config")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print progress messages")
    parser.add_argument("--color", dest="color", choices=["auto", "always", "never", "ansi", "html", "text"], default="auto",
                        help="Output format: auto=ANSI where it renders, always/ansi=force ANSI, html=HTML, never/text=plain text")
    parser.add_argument("--no-color", dest="no_color", action="store_true", help="Disable color output (alias for --color never)")
    parser.add_argument("--show-mcp", dest="show_mcp", action="store_true", help="Show MCP call/result details (for debugging)")
    parser.add_argument("--no-status", dest="no_status", action="store_true", help="Hide status messages during execution")
    parser.add_argument("--raw", dest="raw", action="store_true", help="Output raw JSON result instead of pretty printing")
    subparsers = parser.add_subparsers(dest="subcommand")

    def _add_agent_session_args(p: argparse.ArgumentParser) -> None:
        """Arguments shared verbatim between `run` and `chat`."""
        p.add_argument("--agent", dest="agent_override", help="Override the default agent (use agent name from config)")
        p.add_argument("--llm", dest="llm_profile_override", help="Override the LLM profile (use profile name from llm.yaml)")
        p.add_argument("--llm-params", dest="llm_params", nargs="+", metavar="KEY=VALUE",
                       help="Override LLM parameters for this run (e.g. --llm-params thinking_level=max max_tokens=16384). "
                            "Values are auto-typed (int/float/bool/none); applies to the --llm profile or the agent's default profile.")
        p.add_argument("--max-steps", dest="max_steps", type=int, metavar="N",
                       help="Step budget for this run, overriding the agent's max_steps. "
                            "Only for this process -- nothing is written to the YAML.")
        p.add_argument("--session", dest="session_id", help="Continue an existing session by ID")
        p.add_argument("--session-user", dest="session_user", default="cli_user", help="User ID for session storage (default: cli_user)")
        p.add_argument("--session-title", dest="session_title", help="Title for the new session (auto-generated from task if not provided)")
        p.add_argument("--list-sessions", dest="list_sessions", action="store_true", help="List all sessions for the current user")
        p.add_argument("--vars", "--template-vars", dest="template_vars", nargs="+", metavar="KEY=VALUE",
                       help="Template variables for prompt rendering (e.g. --vars lang=German user_name=Alice)")

    # run subcommand (default behavior)
    run_parser = subparsers.add_parser("run", help="Run an agent task (default)")
    run_parser.add_argument("task", nargs="?", default="What can you do?", help="Task to run")
    run_parser.add_argument("--images", "--attach", dest="images", nargs="+", metavar="PATH", help="Path(s) to image file(s) to attach to the task")
    run_parser.add_argument("--audio", dest="audio", nargs="+", metavar="PATH", help="Path(s) to audio file(s) to attach to the task (mp3, wav, ogg, etc.)")
    run_parser.add_argument("--text", "--files", dest="text_files", nargs="+", metavar="PATH", help="Path(s) to text file(s) to attach to the task (txt, md, py, json, etc.)")
    _add_agent_session_args(run_parser)

    # chat subcommand: interactive REPL that keeps the session across turns
    chat_parser = subparsers.add_parser("chat", help="Interactive chat with an agent (stays in the session)")
    chat_parser.add_argument("task", nargs="?", default=None, help="Optional first message to send immediately")
    _add_agent_session_args(chat_parser)



    # plugins subcommand
    plugins_parser = subparsers.add_parser("plugins", help="Manage plugins")
    plugins_parser.add_argument("action", choices=["list", "info", "enable", "disable", "search", "status"], nargs="?", default="list", help="Action to perform on plugins")
    plugins_parser.add_argument("name", nargs="?", help="Plugin name for the 'info', 'enable', 'disable' actions or search term for 'search'")
    plugins_parser.add_argument("--yes", dest="yes", action="store_true", help="Assume yes for confirmations")
    plugins_parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Don't persist changes; show preview")
    plugins_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format for plugin listing")
    plugins_parser.add_argument("--show-metadata", dest="show_metadata", action="store_true", help="Also display plugin._plugin_metadata in listing (JSON output only)")
    plugins_parser.add_argument("--raw", dest="raw", action="store_true", help="Show raw factory information for 'info' action")

    # reload subcommand: deliberately tell the RUNNING server to re-read the
    # on-disk config and refresh live plugin instances (no restart). agent-cli
    # itself is in-process (fresh config each run), so this targets the server
    # via its admin endpoint.
    reload_parser = subparsers.add_parser(
        "reload", help="Reload the running server's config (no restart)")
    reload_parser.add_argument("--url", dest="reload_url", default=None,
                               help="Server base URL (default: http://127.0.0.1:8000 or AGENT_SERVER_URL)")
    reload_parser.add_argument("--api-key", dest="reload_api_key", default=None,
                               help="Admin API key (default: AGENT_ADMIN_API_KEY / AGENT_API_KEY env)")
    reload_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table",
                               help="Output format")
    reload_parser.add_argument("--timeout", dest="timeout", type=float, default=None,
                               help="Request timeout seconds (default: network.cli_request_timeout or 30)")

    # mcp subcommand for external server management (use subparsers so each
    # action can provide its own help output). We keep argument names that
    # the existing handler expects (`server`, `key`, `value`) for
    # backwards-compatibility with the rest of the code.
    mcp_parser = subparsers.add_parser("mcp", help="Manage external MCP servers")
    # Global options for mcp
    mcp_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format for server listing")
    mcp_parser.add_argument("--timeout", dest="timeout", type=int, default=30, help="Timeout in seconds for connection operations")
    mcp_parser.add_argument("--no-probe", dest="no_probe", action="store_true", help="When listing features, don't probe the live server for reported capabilities; only show configured values")

    # Per-action subparsers
    # Provide a description so `mcp --help` shows a helpful line expected by tests.
    mcp_subparsers = mcp_parser.add_subparsers(dest="action", description="Action to perform on external MCP servers")

    # Helper to add mcp-level options to individual action subparsers so
    # users may place them after the action (e.g. `mcp list --format json`).
    def _add_mcp_common_opts(p):
        try:
            p.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format for server listing")
        except Exception as e:
            logger.warning(f"Failed to add --format argument to parser: {e}", exc_info=True)
        try:
            p.add_argument("--timeout", dest="timeout", type=int, default=30, help="Timeout in seconds for connection operations")
        except Exception as e:
            logger.warning(f"Failed to add --timeout argument to parser: {e}", exc_info=True)
        try:
            p.add_argument("--no-probe", dest="no_probe", action="store_true", help="When listing features, don't probe the live server for reported capabilities; only show configured values")
        except Exception as e:
            logger.warning(f"Failed to add --no-probe argument to parser: {e}", exc_info=True)

    # list
    list_p = mcp_subparsers.add_parser("list", help="List configured external MCP servers")
    _add_mcp_common_opts(list_p)

    # connect / disconnect
    connect_p = mcp_subparsers.add_parser("connect", help="Connect to an external MCP server")
    _add_mcp_common_opts(connect_p)
    connect_p.add_argument("server", nargs="?", help="Server name to connect")
    disconnect_p = mcp_subparsers.add_parser("disconnect", help="Disconnect from an external MCP server")
    _add_mcp_common_opts(disconnect_p)
    disconnect_p.add_argument("server", nargs="?", help="Server name to disconnect")

    # status
    status_p = mcp_subparsers.add_parser("status", help="Show status for a server or all servers")
    _add_mcp_common_opts(status_p)
    status_p.add_argument("server", nargs="?", help="Optional server name to show status for")

    # test
    test_p = mcp_subparsers.add_parser("test", help="Test connectivity and basic functionality of an external MCP server")
    _add_mcp_common_opts(test_p)
    test_p.add_argument("server", nargs="?", help="Server name for test action")

    # enable / disable (persisted to config)
    enable_p = mcp_subparsers.add_parser("enable", help="Enable a configured external MCP server")
    _add_mcp_common_opts(enable_p)
    enable_p.add_argument("server", nargs="?", help="Server name to enable")
    disable_p = mcp_subparsers.add_parser("disable", help="Disable a configured external MCP server")
    _add_mcp_common_opts(disable_p)
    disable_p.add_argument("server", nargs="?", help="Server name to disable")

    # feature (keeps key/value semantics)
    feature_p = mcp_subparsers.add_parser("feature", help="Manage MCP feature flags")
    _add_mcp_common_opts(feature_p)
    feature_p.add_argument("server", nargs="?", help="Server name for feature actions")
    feature_p.add_argument("key", nargs="?", help="Feature subcommand or feature name (for feature set)")
    feature_p.add_argument("value", nargs="?", help="Feature value (on|off) for feature set)")

    # tool subcommand: provide natural help for tool usage
    tool_p = mcp_subparsers.add_parser("tool", help="Manage individual tools on an MCP server")
    _add_mcp_common_opts(tool_p)
    tool_p.add_argument("server", nargs="?", help="Server name for tool actions")
    tool_p.add_argument("key", nargs="?", choices=["list", "allow", "block"], help="Tool action: list, allow, or block")
    tool_p.add_argument("value", nargs="?", help="Tool name for allow/block actions")
    # enable/disable always persist; no interactive prompt or dry-run

    # hooks subcommand for hook introspection
    hooks_parser = subparsers.add_parser("hooks", help="Hook introspection and debugging")
    hooks_parser.add_argument("action", choices=["list", "inspect", "stats", "clear-stats"], nargs="?", default="list", help="Action to perform")
    hooks_parser.add_argument("name", nargs="?", help="Hook name for 'inspect' action")
    from agent_system.hooks import HookType as _HookType
    hooks_parser.add_argument("--type", dest="hook_type", choices=[t.value for t in _HookType],
                              help="Filter by hook type (e.g., pre_llm_call, post_llm_call)")
    hooks_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format")

    # users subcommand for user management
    users_parser = subparsers.add_parser(
        "users",
        help="Manage users (admin)",
        description="User management commands. Examples:\n"
                    "  agent-cli users list\n"
                    "  agent-cli users info admin\n"
                    "  agent-cli users update admin --activate\n"
                    "  agent-cli users create newuser user@example.com --password secret",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    users_parser.add_argument("action", choices=["list", "create", "delete", "update", "info", "generate-api-key", "revoke-api-key"], nargs="?", default="list", help="Action to perform (default: list)")
    users_parser.add_argument("username", nargs="?", help="Target username to operate on")
    users_parser.add_argument("email", nargs="?", help="Email address (required for 'create' action)")
    users_parser.add_argument("--password", "-p", dest="password", help="User password (for create/update)")
    users_parser.add_argument("--name", "-n", dest="full_name", help="User's full display name (e.g., 'John Doe')")
    users_parser.add_argument("--role", "-r", dest="role", choices=["user", "admin", "guest"], help="User role (for create/update)")
    users_parser.add_argument("--admin", dest="admin", action="store_true", help="Make user an admin (shortcut for --role admin)")
    users_parser.add_argument("--inactive", dest="inactive", action="store_true", help="Create user as inactive (for 'create' action)")
    users_parser.add_argument("--activate", dest="activate", action="store_true", help="Activate user (for 'update' action)")
    users_parser.add_argument("--deactivate", dest="deactivate", action="store_true", help="Deactivate user (for 'update' action)")
    users_parser.add_argument("--force", "-f", dest="force", action="store_true", help="Skip confirmation prompts")
    users_parser.add_argument("--limit", dest="limit", type=int, default=100, help="Maximum number of users to show (for 'list' action)")
    users_parser.add_argument("--skip", dest="skip", type=int, default=0, help="Number of users to skip (for 'list' action)")

    args = parser.parse_args(argv[1:])

    # If no subcommand was provided, show help and exit instead of proceeding
    if not getattr(args, "subcommand", None):
        parser.print_help()
        return

    # Checked HERE, where arguments are checked -- not where the override is
    # applied. That sits behind the bootstrap, and answering a typo with an
    # error message fifteen seconds later is the wrong place to learn it.
    if getattr(args, "max_steps", None) is not None and args.max_steps < 1:
        parser.error("--max-steps must be at least 1")

    def vprint(msg: str) -> None:
        if args.verbose:
            print(msg, flush=True)

    vprint("[cli] verbose mode on")
    vprint(f"[cli] loading config: {args.config}")
    config = load_settings(args.config)
    # If user requested plugin listing, handle and exit early (no heavy bootstrap)
    if args.subcommand == "reload":
        # NB: kein lokales `import os` hier — das würde `os` zu einer lokalen
        # Variable von main() machen und den Modul-Import fuer ALLE nested
        # Funktionen shadowen (NameError bei jedem anderen Subcommand).
        try:
            import httpx
            from .llm.tls import httpx_verify
        except ImportError:
            print(json.dumps({"error": "httpx library not installed",
                              "message": "Install with: pip install httpx"}, indent=2))
            return

        base_url = (args.reload_url or os.environ.get("AGENT_SERVER_URL")
                    or "http://127.0.0.1:8000").rstrip("/")
        api_key = (args.reload_api_key or os.environ.get("AGENT_ADMIN_API_KEY")
                   or os.environ.get("AGENT_API_KEY"))
        timeout = args.timeout if args.timeout is not None else (
            config.network.cli_request_timeout if config and config.network else 30.0)
        url = f"{base_url}/admin/reload-config"
        headers = {"X-API-Key": api_key} if api_key else {}

        async def _do_reload():
            async with httpx.AsyncClient(timeout=timeout, verify=httpx_verify()) as client:
                return await client.post(url, headers=headers)

        try:
            resp = run_async(_do_reload())
        except httpx.ConnectError:
            print(json.dumps({"error": "cannot connect to server", "url": url,
                              "hint": "is the server running? set --url / AGENT_SERVER_URL"}, indent=2))
            return
        except Exception as e:
            print(json.dumps({"error": str(e), "url": url}, indent=2))
            return

        if resp.status_code in (401, 403):
            print(json.dumps({"error": f"auth failed (HTTP {resp.status_code})",
                              "hint": "pass --api-key or set AGENT_ADMIN_API_KEY to an admin user's API key"}, indent=2))
            return
        if resp.status_code != 200:
            print(json.dumps({"error": f"server returned HTTP {resp.status_code}",
                              "body": resp.text[:500]}, indent=2))
            return

        data = resp.json()
        report = data.get("report", {})
        if args.out_format == "json":
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return

        refreshed = report.get("refreshed", [])
        print("\nConfig reload:")
        if not refreshed:
            print("  No live server changed (already up to date, or the change needs a restart).")
        for item in refreshed:
            changes = item.get("changes", {})
            print(f"  [ok] {item.get('server')}: {', '.join(sorted(changes.keys()))}")
            for field, ch in changes.items():
                print(f"       {field}: {ch.get('old')!r} -> {ch.get('new')!r}")
        unsupported = report.get("unsupported", [])
        if unsupported:
            print(f"  ({len(unsupported)} server(s) without hot-reload support — a new/changed "
                  f"definition there needs a restart)")
        for err in report.get("errors", []):
            print(f"  [ERR] {err.get('server')}: {err.get('error')}")
        return

    if args.subcommand == "plugins":
        # Use the configured plugin_dirs from the loaded settings. The
        # `load_settings()` call resolves relative paths against the
        # config file directory, so we can trust these paths as provided by
        # the user. If no plugin dirs are configured, pass None to
        # `discover_all_plugins()` to discover only entrypoint plugins.
        plugins_cfg = _get_plugins_config(config)
        dirs = [Path(p) for p in (plugins_cfg.plugin_dirs or []) if p] if plugins_cfg else []
        plugins = discover_all_plugins(dirs if dirs else None)

        def to_list():
            """Build a list of plugins with their instances grouped by type."""
            out = []
            # Build a mapping of plugin_type -> list of instances
            type_to_instances = {}

            if plugins_cfg:
                for instance_name, mcp_config in plugins_cfg.servers.items():
                    plugin_type = mcp_config.type
                    if plugin_type not in type_to_instances:
                        type_to_instances[plugin_type] = []
                    type_to_instances[plugin_type].append({
                        "instance_name": instance_name,
                        "enabled": mcp_config.enabled,
                        "description": mcp_config.description or "",
                    })

            # Now build the output list with plugin types and their instances
            for plugin_type, factory in plugins.items():
                meta = getattr(factory, "_plugin_metadata", None) or {}
                instances = type_to_instances.get(plugin_type, [])

                # Check if any instance of this type is enabled
                any_enabled = any(inst["enabled"] for inst in instances)

                plugin_entry = {
                    "name": plugin_type,
                    "description": meta.get("description"),
                    "version": meta.get("version"),
                    "enabled": any_enabled,
                    "instances": instances if len(instances) > 1 else [],  # Only show instances if multiple exist
                }
                out.append(plugin_entry)

            return out

        # info action: print metadata for a specific plugin
        if getattr(args, "action", None) == "info":
            target = getattr(args, "name", None)
            if not target:
                print(json.dumps({"error": "missing plugin name"}, ensure_ascii=False))
                return
            factory = plugins.get(target)
            if not factory:
                print(json.dumps({"error": "plugin not found", "name": target}, ensure_ascii=False))
                return
            meta = getattr(factory, "_plugin_metadata", None) or {}
            # raw output: include factory repr and module path
            # Accept either the parsed flag or fallback to detecting '--raw'
            # in sys.argv to be resilient to argument ordering and parser quirks.
            raw_flag = getattr(args, "raw", False) or ("--raw" in sys.argv)
            if raw_flag:
                # Ensure we always include these keys so downstream callers/tests
                # can rely on stable JSON shape. Use safe fallbacks if repr()
                # or attribute access fails.
                try:
                    fr = repr(factory)
                except Exception:
                    fr = None
                fm = getattr(factory, "__module__", None)
                factory_info = {
                    "factory_repr": fr,
                    "factory_module": fm,
                }
                out = {"name": target, "metadata": meta, **factory_info}
                plugins_cfg = _get_plugins_config(config)
                enabled_servers = [k for k, v in plugins_cfg.servers.items() if v.enabled] if plugins_cfg else []
                out["enabled"] = target in enabled_servers
                if args.out_format == "table":
                    # Print header and key/value lines
                    print(f"NAME: {target}")
                    for k, v in out.items():
                        if k == "name":
                            continue
                        print(f"{k.upper()}: {v}")
                    return
                print(json.dumps(out, indent=2, ensure_ascii=False))
                return

            # human table optionally
            if args.out_format == "table":
                # Print the plugin name header followed by metadata key: value lines
                print(f"NAME: {target}")
                # Always print DESCRIPTION and VERSION lines (may be blank) to keep output stable
                print(f"DESCRIPTION: {meta.get('description', '')}")
                print(f"VERSION: {meta.get('version', '')}")
                # show enabled status for this plugin
                plugins_cfg = _get_plugins_config(config)
                enabled_servers = [k for k, v in plugins_cfg.servers.items() if v.enabled] if plugins_cfg else []
                enabled_flag = target in enabled_servers
                enabled_text = "YES" if enabled_flag else "NO"
                display_enabled = enabled_text
                if _supports_color():
                    if enabled_flag:
                        display_enabled = _colorize(enabled_text, "32")
                    else:
                        display_enabled = _colorize(enabled_text, "31")
                print(f"ENABLED: {display_enabled}")
                return
            print(json.dumps({"name": target, "metadata": meta}, indent=2, ensure_ascii=False))
            return

        # REMOVED: enable/disable actions (Task #9265 - Epic 0044)
        # Config mutation removed - users should edit config files directly
        if getattr(args, "action", None) in ("enable", "disable"):
            print(json.dumps({
                "error": "enable/disable commands removed",
                "message": "Please edit config files directly (config/plugins.yaml)",
                "info": "Set 'enabled: true/false' for the specific plugin server in plugins.yaml"
            }, ensure_ascii=False))
            return

        # search action: filter plugins by name or description
        if getattr(args, "action", None) == "search":
            term = (getattr(args, "name", None) or "").lower()
            listing = to_list()
            filtered = [p for p in listing if term in (p["name"] or "").lower() or term in (p.get("description") or "").lower()]
            print(json.dumps(filtered, indent=2, ensure_ascii=False))
            return

        # status action: show discovered plugins and whether they're enabled in config
        if getattr(args, "action", None) == "status":
            # use the properly loaded config (with includes processed) instead of reading file directly
            listing = to_list()
            print(json.dumps(listing, indent=2, ensure_ascii=False))
            return

        # list action: either json or simple table
        listing = to_list()
        # If user requested metadata in the listing and JSON format, attach it
        if getattr(args, "show_metadata", False) and args.out_format == "json":
            for item in listing:
                factory = plugins.get(item.get("name"))
                item["metadata"] = getattr(factory, "_plugin_metadata", None) or {}

        # Metadata should have been attached by discover_all_plugins() when
        # filesystem plugin dirs were provided. If any metadata is still
        # missing, leave it blank rather than attempting to read repository
        # paths — callers should configure plugin_dirs in `mcp.yaml` if
        # they expect filesystem plugin metadata to be used.
        if args.out_format == "table":
            # nice table layout using tabulate if available
            rows: List[Tuple[str, str, str, str]] = []
            for p in listing:
                enabled_flag = bool(p.get("enabled"))
                enabled_text = "YES" if enabled_flag else "NO"
                display_enabled = enabled_text
                if _supports_color():
                    if enabled_flag:
                        display_enabled = _colorize(enabled_text, "32")
                    else:
                        display_enabled = _colorize(enabled_text, "31")

                # Add the main plugin type row with truncated description
                desc = p.get("description") or ""
                if len(desc) > 80:
                    desc = desc[:77] + "..."
                rows.append((p.get("name") or "", display_enabled, desc, p.get("version") or ""))

                # Add instance rows if multiple instances exist
                instances = p.get("instances", [])
                if instances:
                    for inst in instances:
                        inst_enabled = inst.get("enabled", False)
                        inst_enabled_text = "YES" if inst_enabled else "NO"
                        inst_display_enabled = inst_enabled_text
                        if _supports_color():
                            if inst_enabled:
                                inst_display_enabled = _colorize(inst_enabled_text, "32")
                            else:
                                inst_display_enabled = _colorize(inst_enabled_text, "31")

                        # Indent instance name with tree characters
                        inst_name = f"  ├─ {inst.get('instance_name', '')}"
                        inst_desc = inst.get("description", "")
                        if len(inst_desc) > 80:
                            inst_desc = inst_desc[:77] + "..."
                        rows.append((inst_name, inst_display_enabled, inst_desc, ""))

            headers = ["NAME", "ENABLED", "DESCRIPTION", "VERSION"]
            if tabulate:
                print(tabulate(rows, headers=headers, tablefmt="github"))
            else:
                # simple fallback
                if rows:
                    name_w = max(len(r[0]) for r in rows)
                    enabled_w = max(len(r[1]) for r in rows)
                    desc_w = max(len(r[2]) for r in rows)
                else:
                    name_w = enabled_w = desc_w = 10
                hdr = f"{'NAME'.ljust(name_w)}  {'ENABLED'.ljust(enabled_w)}  {'DESCRIPTION'.ljust(desc_w)}  VERSION"
                print(hdr)
                print("-" * len(hdr))
                for n, e, d, v in rows:
                    print(f"{n.ljust(name_w)}  {e.ljust(enabled_w)}  {d.ljust(desc_w)}  {v}")
            return

        print(json.dumps(listing, indent=2, ensure_ascii=False))
        return

    # Handle hooks introspection subcommand
    if args.subcommand == "hooks":
        handle_hooks_command(args)
        return

    # Handle MCP external server management subcommand
    if args.subcommand == "mcp":
        # Use config loaded via settings.py - no direct YAML access
        # All config mutations removed - users should edit config files directly

        async def handle_mcp_command():
            # Use direct AgentConfig approach for consistency with main CLI bootstrapping
            mcp_integration = MCPIntegration(config=config)
            # Ensure MCPIntegration sets up external clients and plugins
            try:
                try:
                    await mcp_integration.initialize(config)
                except Exception as e:
                    # Non-fatal: continue without live clients if initialization fails
                    logger.warning(f"MCP integration initialization failed, continuing without live clients: {e}", exc_info=True)

                # Initialize services for clean separation of concerns
                mcp_service = MCPService(mcp_integration, config)
                tool_service = ToolService(mcp_integration, config)

                action = getattr(args, "action", "list")
                server_name = getattr(args, "server", None)
                if action == "list":
                    result = await _mcp_list_servers(mcp_service, args)
                    return
                elif action in ("enable", "disable"):
                    # Config mutation removed - edit config files directly
                    print(json.dumps({
                        "error": "enable/disable commands removed",
                        "message": "Please edit config files directly (config/mcp_servers.yaml)",
                        "info": "Set 'enabled: true/false' for the specific server in mcp_servers.yaml"
                    }, ensure_ascii=False))
                    return
                elif action == "feature":
                    # Config mutation removed - edit config files directly
                    sub = getattr(args, "key", None)
                    if not server_name:
                        print(json.dumps({"error": "server name required for feature action"}, ensure_ascii=False))
                        return

                    # LIST features: query server capabilities via client if connected
                    if sub == "list" or sub is None:
                        # Try client if available to get actual capabilities
                        # honor --no-probe: skip live query if requested
                        if getattr(args, "no_probe", False):
                            client = None
                        else:
                            client = await _maybe_await_get_client(mcp_integration, server_name)
                        capabilities = None
                        if client:
                            try:
                                # A live connection has already completed the
                                # handshake -- its capabilities come from that
                                # initialize response, so there is nothing to
                                # re-initialize here.
                                capabilities = (
                                    getattr(client, 'capabilities', None)
                                    or getattr(client, 'server_capabilities', None)
                                )
                            except Exception as e:
                                logger.debug(f"Failed to get capabilities for {server_name}: {e}")
                                capabilities = None

                        # Get configured features from config object (read-only)
                        # Get MCP servers config
                        mcp_servers_cfg = config.external_servers
                        conf_features = {}
                        if mcp_servers_cfg and hasattr(mcp_servers_cfg, 'remote_servers'):
                            server_cfg = mcp_servers_cfg.remote_servers.get(server_name, {})
                            conf_features = server_cfg.get('features', {}) if isinstance(server_cfg, dict) else {}

                        out = {
                            'server': server_name,
                            'configured_features': conf_features,
                            'reported_capabilities': capabilities
                        }
                        print(json.dumps(out, indent=2, ensure_ascii=False))
                        result = None
                        return

                    # SET feature: Config mutation removed
                    print(json.dumps({
                        "error": "feature set command removed",
                        "message": "Please edit config files directly (config/mcp_servers.yaml)",
                        "info": "Update 'features' section for the specific server in mcp_servers.yaml"
                    }, ensure_ascii=False))
                    return
                elif action == "connect":
                    if not server_name:
                        print(json.dumps({"error": "server name required for connect action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_connect_server(mcp_service, server_name, args)
                    return
                elif action == "disconnect":
                    if not server_name:
                        print(json.dumps({"error": "server name required for disconnect action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_disconnect_server(mcp_service, server_name, args)
                    return
                elif action == "status":
                    result = await _mcp_status_servers(mcp_service, server_name, args)
                    return
                elif action == "test":
                    if not server_name:
                        print(json.dumps({"error": "server name required for test action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_test_server(mcp_service, server_name, args)
                    return
                elif action == "tool":
                    if not server_name:
                        print(json.dumps({"error": "server name required for tool action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_tool_management(tool_service, server_name, args)
                    return
            finally:
                # Ensure we always attempt to shutdown the integration so any
                # created aiohttp client sessions are closed and we don't leak
                # resources when the CLI command exits.
                try:
                    logging.getLogger(__name__).debug("MCPIntegration: calling shutdown()")
                    await mcp_integration.shutdown()
                    logging.getLogger(__name__).debug("MCPIntegration: shutdown() completed")
                except Exception as e:
                    logging.getLogger(__name__).debug(f"Error shutting down MCPIntegration: {e}")
            # Return the captured result (if any) after shutdown completes.
            # Use locals().get to avoid UnboundLocalError when `result` was
            # never assigned due to early returns inside the try/finally.
            return locals().get('result', None)

        # Run the async MCP handler
        try:
            run_async(handle_mcp_command())
        except Exception as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return

    # Handle users management subcommand
    if args.subcommand == "users":
        # Import typer-based user CLI using importlib to avoid package/module name conflict
        # (cli.py and cli/ directory both exist)
        import importlib.util

        # Dynamically import users module from cli_utils/ directory
        users_module_path = Path(__file__).parent / "cli_utils" / "users.py"
        spec = importlib.util.spec_from_file_location("agent_system.agent_cli.users", users_module_path)
        if spec and spec.loader:
            users_module = importlib.util.module_from_spec(spec)
            sys.modules["agent_system.agent_cli.users"] = users_module
            spec.loader.exec_module(users_module)
            users_app = users_module.app
        else:
            print("ERROR: Could not load users module", file=sys.stderr)
            return

        # Build arguments for typer command
        typer_args = [args.action] if args.action else []

        # Add positional arguments (username, email for create command)
        if args.username:
            typer_args.append(args.username)
        if hasattr(args, 'email') and args.email and args.action == 'create':
            typer_args.append(args.email)

        # Add optional arguments
        if hasattr(args, 'password') and args.password:
            typer_args.extend(['--password', args.password])
        if hasattr(args, 'full_name') and args.full_name:
            typer_args.extend(['--name', args.full_name])
        if hasattr(args, 'role') and args.role:
            typer_args.extend(['--role', args.role])
        if hasattr(args, 'admin') and args.admin:
            typer_args.append('--admin')
        if hasattr(args, 'inactive') and args.inactive:
            typer_args.append('--inactive')
        if hasattr(args, 'activate') and args.activate:
            typer_args.append('--activate')
        if hasattr(args, 'deactivate') and args.deactivate:
            typer_args.append('--deactivate')
        if hasattr(args, 'force') and args.force:
            typer_args.append('--force')
        # limit and skip are only for 'list' action
        if args.action == 'list':
            if hasattr(args, 'limit') and args.limit:
                typer_args.extend(['--limit', str(args.limit)])
            if hasattr(args, 'skip') and args.skip:
                typer_args.extend(['--skip', str(args.skip)])

        # Execute typer command
        try:
            users_app(typer_args, standalone_mode=False)
        except SystemExit:
            # Typer raises SystemExit, catch it to prevent full CLI exit
            pass
        return

    # Setup logging from config; file handler is created here. Console level is adjusted below.
    # Use a role-specific logfile so concurrent processes (cli vs api) don't
    # clobber the same file. If the configured file is `logs/agent.log` this
    # will create `logs/agent-cli.log` for the CLI.
    def _role_logfile(base: str, role: str) -> str:
        try:
            p = Path(base)
            stem = p.stem or "agent"
            # preserve all suffixes (e.g. .log)
            suffix = "".join(p.suffixes) or ".log"
            return str(p.with_name(f"{stem}-{role}{suffix}"))
        except Exception as e:
            # fallback to a simple role-specific name in logs/
            logger.debug(f"Failed to construct role-specific logfile from {base}: {e}")
            return str(Path("logs") / f"agent-{role}.log")

    # Determine logfile: prefer explicit per-role setting if provided in config.
    log_path = config.logging.file_cli or _role_logfile(config.logging.file or "logs/agent.log", "cli")
    log_file = setup_logging(
        config.logging.enabled, 
        config.logging.level, 
        log_path,
        rotation_enabled=config.logging.rotation_enabled,
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count
    )
    logger = logging.getLogger(__name__)
    # If verbose not set, reduce console output to WARNING to avoid noisy logs on stdout
    if not args.verbose:
        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            # FileHandler is a subclass of StreamHandler — exclude it
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)
    if log_file:
        logger.info("Logging initialized, file=%s", log_file)
    # Apply SSL bypass if configured
    if not config.network.ssl_verify:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ.setdefault("SSL_CERT_FILE", "")
        os.environ.setdefault("CURL_CA_BUNDLE", "")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
    registry = MCPRegistry()
    vprint("[cli] bootstrapping servers...")
    logger.info("Bootstrapping servers")

    # Store config for lazy batch queue manager initialization
    # This allows LLMFactory to create the manager when first needed
    from .llm.factory import set_batch_config
    set_batch_config(config)

    # Use InitializationService for consistent bootstrap + injection
    from .services.initialization_service import InitializationService
    init_service = InitializationService(config)
    registry, session_service = init_service.initialize_for_cli()

    # Keep references to session_manager for CLI use
    session_manager = init_service.session_manager

    vprint(f"[cli] servers registered: {', '.join(registry.list())}")
    logger.info("Servers registered: %s", ", ".join(registry.list()))

    # Initialize global MCP integration to enable tool sharing across agents
    vprint("[cli] initializing MCP integration...")
    logger.info("Initializing MCP integration")
    try:
        run_async(initialize_mcp(config))
        vprint("[cli] MCP integration initialized")
        logger.info("MCP integration initialized successfully")
    except Exception as e:
        logger.warning("Failed to initialize MCP integration: %s", e)
        vprint(f"[cli] Warning: MCP integration failed: {e}")

    # Initialize batch queue manager if any LLM models have batch enabled
    vprint("[cli] initializing batch queue manager...")
    try:
        run_async(init_batch_system(config))
        vprint("[cli] batch queue manager initialized")
        logger.info("Batch queue manager initialized successfully")
    except Exception as e:
        logger.warning("Failed to initialize batch queue manager: %s", e)
        vprint(f"[cli] Warning: batch queue manager failed: {e}")

    # Note: SessionManager, SessionService, and dependency injection
    # are now handled by InitializationService.initialize_for_cli() above

    # What this session was started with, when one is being continued. An
    # explicit --agent/--llm still wins; the stored values only replace the
    # config defaults, which are the wrong answer for a session that was
    # begun with something else.
    stored_agent, stored_llm = usable_session_defaults(
        *stored_session_settings(
            session_manager, getattr(args, "session_user", "cli_user"),
            getattr(args, "session_id", None)),
        config)

    # Determine CLI agent name from config (can be overridden with --agent)
    entry_name = choose_agent_name(
        getattr(args, "agent_override", None), stored_agent, config.default_agent)
    if stored_agent and entry_name == stored_agent:
        vprint(f"[cli] continuing session with its own agent: {entry_name}")

    # Get or create the agent
    from .servers.agent.server import Agent as _Agent

    agent = None
    if entry_name in registry.list():
        existing = registry.get(entry_name)
        if isinstance(existing, _Agent):
            agent = existing
            agent.registry = registry  # type: ignore[attr-defined]
            # Update session_service for existing agent
            agent._session_service = session_service  # type: ignore[attr-defined]
        else:
            # Entry exists but is not an Agent (probably a plugin/tool)
            logger.error(f"'{entry_name}' is registered as {type(existing).__name__}, not an Agent")
            print(f"Error: '{entry_name}' is not an agent. It's a {type(existing).__name__}.", file=sys.stderr)
            print("\nAvailable agents:", file=sys.stderr)
            for name in registry.list():
                server = registry.get(name)
                if isinstance(server, _Agent):
                    print(f"  - {name}", file=sys.stderr)
            sys.exit(1)

    if agent is None:
        agent = _build_entry_agent(entry_name, config, registry, session_service)
        vprint(f"[cli] created agent: {entry_name}")

    # --max-steps: the budget for THIS process, not a config change.
    #
    # Copied onto the instance rather than threaded through the run: both
    # readers take it from agent_config (server.py's run loop and the final
    # answer), and the copy keeps the loaded config untouched. Safe here in a
    # way it would not be in the API, where one agent instance serves every
    # request -- which is why /run has no such override.
    max_steps_override = getattr(args, "max_steps", None)
    if max_steps_override is not None:
        agent.agent_config = agent.agent_config.model_copy(
            update={"max_steps": max_steps_override})
        vprint(f"[cli] max_steps override: {max_steps_override}")

    # Process multimodal attachments (images, audio, text files)
    task_input: Union[str, ChatMessage] = args.task
    has_images = getattr(args, "images", None)
    has_audio = getattr(args, "audio", None)
    has_text_files = getattr(args, "text_files", None)
    
    if has_images or has_audio or has_text_files:
        attachment_counts = []
        if has_images:
            attachment_counts.append(f"{len(has_images)} image(s)")
        if has_audio:
            attachment_counts.append(f"{len(has_audio)} audio(s)")
        if has_text_files:
            attachment_counts.append(f"{len(has_text_files)} text file(s)")
        vprint(f"[cli] processing attachments: {', '.join(attachment_counts)}")

        # Same check the HTTP API does. Without it the picture went to whatever
        # model the chain picked, and the complaint came back from the provider.
        from .llm.capabilities import ensure_model_supports
        model_name = getattr(getattr(agent, "llm", None), "model", None)
        problem = ensure_model_supports(
            model_name, images=len(has_images or []), audio=len(has_audio or []))
        if problem:
            print(f"Error: {problem}", file=sys.stderr)
            sys.exit(1)

        try:
            from .utils.multimodal_processor import (
                create_multimodal_message_extended,
                ImageProcessingError,
                AudioProcessingError,
                TextFileProcessingError
            )

            # Convert string paths to lists of Path objects
            image_paths = [Path(p) for p in has_images] if has_images else None
            audio_paths = [Path(p) for p in has_audio] if has_audio else None
            text_file_paths = [Path(p) for p in has_text_files] if has_text_files else None

            # Create multimodal message with all attachment types
            task_input = create_multimodal_message_extended(
                text=args.task,
                image_paths=image_paths,
                audio_paths=audio_paths,
                text_file_paths=text_file_paths,
            )

            vprint("[cli] created multimodal message")

        except ImageProcessingError as e:
            print(f"Error processing image: {e}", file=sys.stderr)
            return
        except AudioProcessingError as e:
            print(f"Error processing audio: {e}", file=sys.stderr)
            return
        except TextFileProcessingError as e:
            print(f"Error processing text file: {e}", file=sys.stderr)
            return
        except ImportError as e:
            print(f"Error: Multimodal processing requires Pillow: {e}", file=sys.stderr)
            print("Install with: pip install Pillow", file=sys.stderr)
            return
        except Exception as e:
            print(f"Error processing attachments: {e}", file=sys.stderr)
            logger.exception("Unexpected error in multimodal processing")
            return

    vprint(f"[cli] running task: {args.task}")
    logger.info("Running task: %s", args.task)

    # Extract LLM profile override early (needed in session operations)
    llm_profile_override = getattr(args, "llm_profile_override", None)

    # Initialize session management
    session_id = getattr(args, "session_id", None)
    session_user = getattr(args, "session_user", "cli_user")
    list_sessions = getattr(args, "list_sessions", False)

    # Generate or use provided session ID
    from .utils.id import short_id
    actual_session_id = session_id or short_id()

    # SessionManager and SessionService already initialized earlier (before Agent creation)
    # to enable passing session_service to Agent constructor

    # Parsed --vars, hoisted so chat mode can re-apply them on /new
    parsed_cli_vars: dict[str, str] = {}

    # Helper async function for session operations
    async def handle_session_operations():
        nonlocal actual_session_id
        was_new_session = False  # Track if we're creating a new session

        # Handle --list-sessions flag
        if list_sessions:
            vprint(f"[cli] listing sessions for user: {session_user}")
            try:
                sessions = await session_manager.list_sessions(session_user)

                if not sessions:
                    print(f"No sessions found for user '{session_user}'")
                    return False, was_new_session  # Signal to exit

                print(f"\nSessions for user '{session_user}':")
                print("-" * 80)
                for sess in sessions:
                    sess_id = sess.get("session_id", "unknown")
                    title = sess.get("title", "Untitled")
                    agent_name = sess.get("agent_name", "unknown")
                    llm_profile = sess.get("llm_profile", "unknown")
                    created = sess.get("created_at", "unknown")
                    msg_count = sess.get("message_count", len(sess.get("messages", [])))  # Use message_count from metadata

                    print(f"ID: {sess_id}")
                    print(f"  Title: {title}")
                    print(f"  Agent: {agent_name}, LLM: {llm_profile}")
                    print(f"  Messages: {msg_count}, Created: {created}")
                    print()
                return False, was_new_session  # Signal to exit
            except Exception as e:
                logger.error(f"Failed to list sessions: {e}", exc_info=True)
                print(f"Error listing sessions: {e}", file=sys.stderr)
                return False, was_new_session  # Signal to exit

        # Load existing session if --session provided
        session_exists = False
        if session_id:
            vprint(f"[cli] loading session: {session_id}")
            try:
                session_exists, msg_count = await session_service.load_and_restore_session(
                    agent, session_user, session_id
                )
                if session_exists:
                    vprint(f"[cli] loaded session with {msg_count} messages")
                    logger.info(f"Loaded session {session_id} with {msg_count} messages")
                    was_new_session = False
                else:
                    # Session ID provided but doesn't exist - create it
                    logger.info(f"Session '{session_id}' not found, creating new session with this ID")
                    # stderr: stdout traegt das Task-Ergebnis (Redirects sauber halten)
                    print(f"Creating new session '{session_id}'", file=sys.stderr)
                    was_new_session = True  # Will be saved at end
                    # Initialize empty session in agent ONLY when it doesn't exist.
                    # MUST stay inside the else: when the session WAS restored,
                    # load_and_restore_session already populated the tracker -
                    # clearing it here wipes the restored history and the
                    # subsequent save permanently destroys it on disk.
                    if hasattr(agent, '_session_tracker'):
                        agent._session_tracker.set_session_messages(actual_session_id, [])
            except SessionPermissionError as e:
                # User trying to access session they don't own
                logger.error(f"Permission denied for session {session_id}: {e}")
                print(f"Error: {e}", file=sys.stderr)
                print("This session belongs to a different user. Use a different session ID.", file=sys.stderr)
                return False, was_new_session  # Signal to exit
            except Exception as e:
                logger.error(f"Failed to load session {session_id}: {e}", exc_info=True)
                print(f"Error loading session: {e}", file=sys.stderr)
                return False, was_new_session  # Signal to exit
        else:
            # No session ID provided - create new one with auto-generated ID
            logger.debug(f"Creating new session: {actual_session_id}")
            was_new_session = True
            if hasattr(agent, '_session_tracker'):
                agent._session_tracker.set_session_messages(actual_session_id, [])

        # CRITICAL: Initialize session template_vars from agent_config for NEW sessions
        # This ensures initial values (like workflow_phase: "planning") are available
        # without requiring explicit set_context calls
        if was_new_session and hasattr(agent, '_session_tracker') and hasattr(agent, 'agent_config'):
            if agent.agent_config and agent.agent_config.template_vars:
                initial_vars = agent.agent_config.template_vars.copy()
                agent._session_tracker.set_session_template_vars(actual_session_id, initial_vars)
                logger.debug(f"[cli] Initialized session template_vars from agent_config: {list(initial_vars.keys())}")

        # Merge CLI --vars overrides into session template_vars
        cli_vars_raw = getattr(args, 'template_vars', None)
        if cli_vars_raw and hasattr(agent, '_session_tracker'):
            cli_vars: dict[str, str] = {}
            for item in cli_vars_raw:
                if '=' not in item:
                    print(f"Warning: ignoring invalid --vars entry (expected KEY=VALUE): {item}", file=sys.stderr)
                    continue
                key, _, value = item.partition('=')
                cli_vars[key.strip()] = value.strip()
            if cli_vars:
                agent._session_tracker.set_session_template_vars(actual_session_id, cli_vars)
                logger.debug(f"[cli] Applied CLI template_vars overrides: {list(cli_vars.keys())}")
                # Chat's /new creates further sessions and has to re-apply these.
                parsed_cli_vars.update(cli_vars)

        return True, was_new_session  # Continue with task execution

    # Run session operations
    should_continue, was_new_session = run_async(handle_session_operations())
    if not should_continue:
        return

    async def _stream_and_run_with_status(
        agent: Agent,
        task: Union[str, ChatMessage],
        session_id: str,  # Add session_id parameter
        show_mcp: bool = False,
        show_status: bool = True,
        llm_override=None,
        llm_profile_info: Optional[str] = None
    ) -> dict:
        """Stream and run agent with status display.

        Args:
            agent: The agent to run
            task: Either a string task or ChatMessage with multimodal content
            show_mcp: Whether to show MCP call details
            show_status: Whether to show status events
            llm_override: Optional LLM client to override agent's default
            llm_profile_info: Optional profile info string for logging
        """
        # Extract task text for logging
        if isinstance(task, ChatMessage):
            if isinstance(task.content, str):
                task_text = task.content
            elif isinstance(task.content, list):
                # Content items are Pydantic models, use attribute access
                text_parts = [getattr(item, "text", "") for item in task.content if hasattr(item, "type") and getattr(item, "type") == "text"]
                task_text = " ".join(text_parts) if text_parts else "[multimodal input]"
            else:
                task_text = "[multimodal input]"
        else:
            task_text = task

        final_result: Dict[str, Any] = {"task": task_text, "calls": []}

        # Subscribe to status events if enabled
        status_queue = None
        if show_status:
            status_queue = await status_bus.subscribe()

        # Optionally auto-subscribe to external SSE status stream
        sse_task = None
        sse_url = os.environ.get("AGENT_STATUS_SSE_STREAM_URL")

        async def _status_subscriber():
            """Subscribe to local status events and display them"""
            if not status_queue:
                return
            try:
                while True:
                    event = await status_queue.get()
                    # Display status event in a clean format using new StatusEvent format
                    phase = event.phase.value if hasattr(event.phase, 'value') else str(event.phase)
                    phase_disp = phase
                    if _supports_color():
                        phase_color_map = {
                            "start": "36",      # cyan
                            "progress": "34",   # blue
                            "end": "32",        # green
                            "error": "31",      # red
                        }
                        c = phase_color_map.get(phase, "34")
                        phase_disp = _colorize(phase, c)

                    server_col = event.server
                    txt = event.message
                    status_line = f"[{phase_disp}] {server_col}: {txt}"

                    # Error phase should be red
                    if phase == "error" and _supports_color():
                        status_line = _colorize(status_line, "31")
                    print(status_line)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug(f"Status subscriber error: {e}")
                return

        async def _sse_subscriber(url: str):
            try:
                try:
                    import aiohttp
                except Exception:
                    return
                timeout = aiohttp.ClientTimeout(total=None)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(url) as resp:
                        if resp.status != 200:
                            return
                        async for line in resp.content:
                            try:
                                text = line.decode("utf-8").strip()
                            except Exception:
                                continue
                            if not text:
                                continue
                            if text.startswith("data:"):
                                payload = text[len("data:"):].strip()
                                try:
                                    obj = json.loads(payload)
                                except Exception as e:
                                    logger.debug(f"Failed to parse SSE payload: {e}")
                                    obj = {"raw": payload}
                                # Print SSE messages in short form
                                if _supports_color():
                                    print(_colorize(f"[SSE] {obj.get('server','?')}: {obj.get('message','')}", "34"))
                                else:
                                    print(f"[SSE] {obj.get('server','?')}: {obj.get('message','')}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug(f"SSE subscriber error: {e}")
                return

        # Start status subscriber task if enabled
        status_task = None
        if show_status and status_queue:
            status_task = asyncio.create_task(_status_subscriber())

        if sse_url:
            try:
                sse_task = asyncio.create_task(_sse_subscriber(sse_url))
            except Exception as e:
                logger.warning(f"Failed to create SSE subscriber task: {e}", exc_info=True)
                sse_task = None
        # Track whether thinking/reasoning tokens were actually streamed this step.
        # The terminating newline on thinking_complete must only print when content
        # was streamed - non-streaming LLMs emit thinking_complete with no thinking_delta,
        # which would otherwise produce a stray blank line per LLM call.
        thinking_streamed = False

        def _close_thinking_block() -> None:
            """Reset the colour and end the streamed line of a thinking block."""
            if _supports_color():
                print("\x1b[0m", end="")
            print()

        try:
            async for ev in agent.run_events(task, session_id=actual_session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info):
                t = ev.get("type")
                if t == "mcp_call" and show_mcp:
                    srv = ev.get("server")
                    action = ev.get("action")
                    params = ev.get("params") or {}
                    # Human readable print
                    header = f"MCP CALL -> server={srv} action={action}"
                    if _supports_color():
                        header = _colorize(header, "36")
                    print(header)
                    print(json.dumps(params, indent=2, ensure_ascii=False))
                elif t == "mcp_result":
                    srv = ev.get("server")
                    action = ev.get("action")
                    res = ev.get("result")
                    # Append to final_result calls for JSON output
                    final_result.setdefault("calls", []).append({"server": srv, "action": action, "result": res})
                    if show_mcp:
                        header = f"MCP RESULT <- server={srv} action={action}"
                        if _supports_color():
                            header = _colorize(header, "32")
                        print(header)
                        try:
                            print(json.dumps(res, indent=2, ensure_ascii=False))
                        except Exception as e:
                            logger.debug(f"Failed to JSON dump MCP result: {e}")
                            print(str(res))
                elif t == "thinking_delta":
                    # Show thinking/reasoning content as it streams (like WebUI).
                    # Gated auf show_status: bei --no-status traegt stdout NUR das
                    # Ergebnis (`>out.json` bleibt sauber, gleiche Klasse wie
                    # die Session-saved-Zeile).
                    delta = ev.get("delta", "")
                    if delta and show_status:
                        # Open the grey block once, not around every token: one
                        # escape pair per delta buried the text in ESC[90m/ESC[0m.
                        if not thinking_streamed:
                            thinking_streamed = True
                            if _supports_color():
                                print("\x1b[90m", end="", flush=True)  # Dark gray
                        # Print without newline for streaming effect
                        print(delta, end="", flush=True)
                elif t == "thinking_complete":
                    # Thinking finished - terminate the streamed line, but only if
                    # thinking content was actually printed this step
                    if thinking_streamed:
                        _close_thinking_block()
                    thinking_streamed = False
                elif t == "thinking":
                    # New step starting - close any block left open by a step that
                    # ended without thinking_complete, or the grey leaks onward.
                    if thinking_streamed:
                        _close_thinking_block()
                    thinking_streamed = False
                    # Optionally show LLM progress when verbose (backward compatibility)
                    if args.verbose:
                        step = ev.get("step")
                        print(f"[LLM] thinking (step {step})")
                elif t == "final":
                    # Store final summary in result AND print it immediately for streaming
                    summary = ev.get("summary")
                    if summary:
                        final_result["summary"] = summary
                        # Print summary immediately during streaming (don't wait for end)
                        print("", flush=True)  # Newline before summary
                        try:
                            # Use the formatting function for consistent ANSI output
                            formatted_summary, content_format = await format_output_with_hooks(
                                output=summary,
                                agent_instance=agent,
                                session_id=actual_session_id,
                                request_id="cli_display",
                                output_format='ansi'  # Request ANSI format for terminal display
                            )
                            if content_format == 'ansi':
                                render_with_rich(formatted_summary)
                            else:
                                line = f"{formatted_summary}"
                                print(line, flush=True)
                        except Exception as e:
                            # Fallback to plain text
                            logger.debug(f"Failed to format summary: {e}")
                            print(f"{summary}", flush=True)
                elif t == "error":
                    err = f"ERROR: {ev.get('message')}"
                    if _supports_color():
                        err = _colorize(err, "31")
                    print(err)
                elif t == "cancelled":
                    # Agent was cancelled (Ctrl-C or timeout)
                    msg = "\n✋ Cancelled by user"
                    if _supports_color():
                        msg = _colorize(msg, "33")  # yellow
                    print(msg)
                    final_result["cancelled"] = True
                elif t == "done":
                    # run_events may emit a final aggregated result
                    fr = ev.get("result")
                    if isinstance(fr, dict):
                        final_result = fr
                # keep looping until 'end'

            return final_result
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Close an open thinking block first, or the exit message is grey
            if thinking_streamed:
                _close_thinking_block()
                thinking_streamed = False
            # User pressed Ctrl-C: provide clean exit message
            msg = "\n✋ Cancelled by user"
            if _supports_color():
                msg = _colorize(msg, "33")  # yellow
            print(msg)
            return {"task": task, "cancelled": True, "summary": final_result.get("summary", "")}
        except Exception as e:
            # Fallback: surface exception as result
            return {"task": task, "errors": [str(e)]}
        finally:
            # Safety net: a block left open by an exception would bleed grey
            # into the shell prompt after we exit.
            if thinking_streamed:
                _close_thinking_block()
                thinking_streamed = False
            # Cleanup background tasks
            # Drain any queued status events deterministically before cancelling
            # the background status subscriber. This avoids a race where the
            # final PHASE_END is published but the subscriber is cancelled
            # before it can process the queued event.
            if status_queue:
                try:
                    while not status_queue.empty():
                        try:
                            event = status_queue.get_nowait()
                        except Exception as e:
                            logger.debug(f"Failed to get status event from queue: {e}")
                            break
                        # Reuse the same display logic as _status_subscriber —
                        # including the enum normalization: the raw StatusPhase
                        # enum printed as "[StatusPhase.END]" and never matched
                        # the color map.
                        phase = getattr(event, "phase", "progress")
                        phase = phase.value if hasattr(phase, "value") else str(phase)
                        phase_disp = phase
                        if _supports_color():
                            phase_color_map = {
                                "start": "36",
                                "progress": "34",
                                "end": "32",
                                "error": "31",
                            }
                            c = phase_color_map.get(phase, "34")
                            phase_disp = _colorize(phase, c)

                        server_col = event.server
                        txt = event.message
                        status_line = f"[{phase_disp}] {server_col}: {txt}"

                        if event.level == "error" and _supports_color():
                            status_line = _colorize(status_line, "31")
                        elif event.level == "warning" and _supports_color():
                            status_line = _colorize(status_line, "33")
                        print(status_line)
                except Exception as e:
                    # If anything goes wrong while draining, continue to cancel tasks
                    logger.debug(f"Exception while draining status queue: {e}")
            if status_task and not status_task.done():
                try:
                    status_task.cancel()
                except Exception as e:
                    logger.debug(f"Failed to cancel status task: {e}")
                    pass
            if sse_task and not sse_task.done():
                try:
                    sse_task.cancel()
                except Exception:
                    pass

    # Execute with new status-aware streaming
    show_mcp = getattr(args, "show_mcp", False)
    show_status = not getattr(args, "no_status", False)

    # Create LLM override if --llm and/or --llm-params was specified
    llm_override = None
    llm_profile_info = None
    # Continue on the model the session was started with (see
    # choose_llm_profile for what that does and does not outrank).
    requested_profile = getattr(args, "llm_profile_override", None)
    llm_profile_override = choose_llm_profile(
        requested_profile, stored_llm, stored_agent, entry_name,
        agent.agent_config.default_llm_profile)
    if llm_profile_override and not requested_profile:
        vprint(f"[cli] continuing session with its own LLM profile: "
               f"{llm_profile_override}")
    try:
        llm_params_override = parse_llm_params_args(getattr(args, "llm_params", None))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return

    if llm_profile_override or llm_params_override:
        # --llm-params ohne --llm: auf das Default-Profil des Agenten anwenden.
        effective_profile = (
            llm_profile_override or agent.agent_config.default_llm_profile
        )
        if config.llm_system and config.llm_system.profiles:
            if effective_profile not in config.llm_system.profiles:
                available_profiles = sorted(config.llm_system.profiles.keys())
                error_msg = f"ERROR: LLM profile '{effective_profile}' not found in configuration."
                if available_profiles:
                    error_msg += "\n\nAvailable profiles:\n  " + "\n  ".join(available_profiles)
                print(error_msg, file=sys.stderr)
                return

            try:
                # Use factory function that properly handles batch mode
                from .llm.factory import create_llm_from_profile, resolve_llm_config_for_agent
                from .config.models import AgentConfig

                llm_override = create_llm_from_profile(
                    config=config,
                    llm_profile=effective_profile,
                    llm_params=llm_params_override,
                )

                # Get profile info for logging
                temp_agent_config = AgentConfig(llm_profile=effective_profile)
                resolved = resolve_llm_config_for_agent(config, temp_agent_config)
                model = resolved.spec.model
                provider = resolved.spec.provider
                llm_profile_info = f"{effective_profile}:{provider}/{model}"
                if llm_params_override:
                    _params_str = ",".join(
                        f"{k}={v}" for k, v in llm_params_override.items()
                    )
                    llm_profile_info += f" +params({_params_str})"

                logger.info(f"Using LLM override: {llm_profile_info}")
                vprint(f"[cli] Using LLM profile: {llm_profile_info}")
            except Exception as e:
                logger.error(f"Failed to create LLM override: {e}", exc_info=True)
                print(f"ERROR: Failed to apply LLM profile '{effective_profile}': {str(e)}", file=sys.stderr)
                return

    # Set session metadata for tool execution context (enables _user_id, _agent injection)
    if hasattr(agent, '_session_tracker'):
        # Determine effective LLM profile (override or agent default)
        effective_llm_profile = llm_profile_override or agent.agent_config.default_llm_profile

        agent._session_tracker.set_session_metadata(actual_session_id, {
            "user_id": session_user,
            "agent_name": entry_name,
            "llm_profile": effective_llm_profile
        })

    # Chat mode: hand over to the REPL instead of the one-shot execution.
    # Everything above (bootstrap, agent, session ops, LLM override) is shared.
    # Inside the same try/finally as the one-shot path so MCP and the batch
    # system get shut down the same way -- returning early leaked stdio child
    # processes and aiohttp sessions until interpreter exit.
    is_chat = args.subcommand == "chat"

    try:
        if is_chat:
            from .cli_utils.chat import run_chat_loop
            run_chat_loop(
                # The SHARED loop, not a private one: bootstrap connected the
                # external MCP servers on it, and their tasks only make
                # progress while this very loop runs the turns.
                loop=get_cli_loop(),
                agent=agent,
                entry_name=entry_name,
                session_service=session_service,
                session_manager=session_manager,
                session_user=session_user,
                session_id=actual_session_id,
                was_new_session=was_new_session,
                llm_profile=llm_profile_override or agent.agent_config.default_llm_profile,
                llm_override=llm_override,
                llm_profile_info=llm_profile_info,
                show_status=show_status,
                initial_task=getattr(args, "task", None),
                template_vars=parsed_cli_vars,
            )
            result = {}
        elif getattr(args, "raw", False):
            # Raw mode: use run_events with result collection
            from .servers.agent.result_utils import collect_final_result

            result = run_async(collect_final_result(agent, task_input, session_id=actual_session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info))
        else:
            result = run_async(_stream_and_run_with_status(agent, task_input, actual_session_id, show_mcp=show_mcp, show_status=show_status, llm_override=llm_override, llm_profile_info=llm_profile_info))

        # Chat saved its own sessions per turn and prints its own output.
        if is_chat:
            return

        # Check if request was cancelled
        if result.get("cancelled", False):
            vprint("[cli] cancelled by user")
            logger.info("Request cancelled by user")
        else:
            vprint("[cli] done")
            logger.info("Task completed")

        # Save session after successful task execution (skip if cancelled)
        async def save_session_after_task():
            try:
                # Use the actual agent name that was requested (entry_name from args)
                # instead of agent.agent_name which may not exist or be "default"
                agent_name_used = entry_name  # The agent name determined from args.agent_override or config.default_agent
                # The profile actually USED, not the raw flag: session_service
                # overwrites the record unconditionally, so reading the flag
                # here wrote the agent's default over the session's own choice
                # on every bare --session resume -- the choice then survived
                # exactly one continuation.
                llm_profile_used = llm_profile_override or agent.agent_config.default_llm_profile

                # Save the session
                success = await session_service.save_session(
                    agent=agent,
                    user_id=session_user,
                    session_id=actual_session_id,
                    agent_name=agent_name_used,
                    llm_profile=llm_profile_used,
                    was_new_session=was_new_session,
                    title=getattr(args, "session_title", None)
                )

                if success:
                    if session_id:
                        vprint(f"[cli] updated session: {session_id}")
                        logger.info(f"Updated session {session_id}")
                    else:
                        vprint(f"[cli] created new session: {actual_session_id}")
                        logger.info(f"Created new session {actual_session_id}")
                        # stderr: stdout traegt das Ergebnis (`>out.json` darf
                        # keine Meta-Zeilen einsammeln); im Terminal weiter sichtbar.
                        print(f"\nSession saved: {actual_session_id}", file=sys.stderr)
                else:
                    logger.warning("Session save returned False")
            except Exception as e:
                logger.error(f"Failed to save session: {e}", exc_info=True)
                print(f"Warning: Failed to save session: {e}", file=sys.stderr)

        # Only save session if not cancelled
        if not result.get("cancelled", False):
            run_async(save_session_after_task())

    finally:
        # Shutdown batch queue manager first
        try:
            run_async(shutdown_batch_system())
            vprint("[cli] batch queue manager shut down")
            logger.info("Batch queue manager shut down successfully")
        except Exception as e:
            logger.warning("Failed to shutdown batch queue manager: %s", e)
        
        # Ensure MCP integration is properly shut down to close aiohttp sessions
        try:
            run_async(shutdown_mcp())
            vprint("[cli] MCP integration shut down")
            logger.info("MCP integration shut down successfully")
        except Exception as e:
            logger.warning("Failed to shutdown MCP integration: %s", e)

    # Human-readable final output
    def _pretty_print_result(res: dict, show_mcp: bool = False, agent_instance=None, session_id_val: str = "unknown", skip_summary: bool = False) -> None:
        # Calls (print first so summary appears at the end, only when show_mcp is True)
        calls = res.get("calls", []) or []
        if calls and show_mcp:
            print("")
            print("Tool calls:")
            for c in calls:
                srv = c.get("server")
                action = c.get("action")
                header = f"- {srv} :: {action}"
                if _supports_color():
                    header = _colorize(header, "36")
                print(header)
                result_obj = c.get("result")
                # Render result as YAML for human readability when possible
                try:
                    yaml_text = yaml.safe_dump(result_obj, allow_unicode=True, sort_keys=False)
                    for line in yaml_text.rstrip().splitlines():
                        print(f"    {line}")
                except Exception as e:
                    # Fallback to JSON-ish string
                    logger.debug(f"Failed to YAML dump result: {e}")
                    try:
                        j = json.dumps(result_obj, ensure_ascii=False)
                        print(f"    {j}")
                    except Exception as e2:
                        logger.debug(f"Failed to JSON dump result: {e2}")
                        print(f"    {str(result_obj)}")

        # Summary (print after calls so it is the final user-visible result)
        # Skip if skip_summary=True (already printed during streaming)
        summary = None
        if not skip_summary:
            summary = res.get("summary")

        if summary:
            # Format summary using FORMAT_OUTPUT hooks if available (ANSI for terminal)
            formatted_summary = summary
            content_format = 'text'

            try:
                # Use central ANSI formatting function (respects --color flag)
                import asyncio

                # Get or create event loop
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                formatted_summary, content_format = loop.run_until_complete(
                    format_output_with_hooks(
                        output=summary,
                        agent_instance=agent_instance,
                        session_id=session_id_val,
                        request_id="cli_display"
                    )
                )
                logger.info(f"Formatted summary: format={content_format}, length={len(formatted_summary)}")
                vprint(f"[cli] Formatted summary: format={content_format}, length={len(formatted_summary)}")

            except Exception as e:
                logger.warning(f"Failed to format summary with ANSI: {e}", exc_info=True)
                vprint(f"[cli] ERROR formatting summary: {e}")

            print("")
            if content_format == 'ansi':
                render_with_rich(formatted_summary)
            else:
                line = f"Summary: {formatted_summary}"
                if _supports_color():
                    line = _colorize(line, "33")
                print(line)

        # Errors
        errors = res.get("errors") or []
        if errors:
            print("")
            print(_colorize("Errors:", "31") if _supports_color() else "Errors:")
            for e in errors:
                print(f"  - {e}")

        # If verbose, print raw JSON for debugging
        if getattr(args, "verbose", False):
            print("")
            print(_colorize("Raw result JSON:", "35") if _supports_color() else "Raw result JSON:")
            print(json.dumps(res, indent=2, ensure_ascii=False))

    # If raw requested, print JSON and exit. Ensure output is flushed so
    # test harnesses and non-interactive environments capture it.
    if getattr(args, "raw", False):
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    else:
        # Skip summary in pretty print since it was already printed during streaming
        _pretty_print_result(result, show_mcp=show_mcp, agent_instance=agent, session_id_val=actual_session_id, skip_summary=True)
        try:
            sys.stdout.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
