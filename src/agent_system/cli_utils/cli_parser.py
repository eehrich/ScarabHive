"""agent-cli's command line: the preliminary parser, the main parser, --llm-params.

The line is read in three stages (docs/_arch_cli_architecture.md, section 2):
the preliminary parser picks the global options up from anywhere, `users` is
handed to its Typer app with the tokens as typed (agent_cli does that, it is a
dispatch), and the main parser reads the rest. The two parsers are built here,
next to each other, because their pitfall is a pair: a flag defined in both a
parent and a subparser takes the subparser's default. Building them in one
place keeps each flag where it is defined once.

``parse_llm_params_args`` is here as well: it is checked where arguments are
checked, before any config is loaded.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict, List, Literal, Optional, get_args, get_origin

from .session_listing import DEFAULT_LIMIT

#: What --color takes, in agent-cli's parsers and in agent-run's.
COLOR_CHOICES = ("auto", "always", "never", "ansi", "html", "text")

#: The words the main parser knows as its first token; anything else gets an
#: implicit `run` in front of it.
_SUBCOMMANDS = ("plugins", "mcp", "hooks", "run", "chat", "users", "reload", "-h", "--help")


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
    """Auto-type a CLI KEY=VALUE value: int/float/bool/none, otherwise string.

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

    Values are auto-typed (``max_tokens=1000`` → int, ``stream=false`` →
    bool, ``thinking_level=max`` → str) — the LLM-params validation
    (LLMModelConfig re-validation in ``resolve_llm_config_for_agent``)
    needs real types, not strings. Empty result → ``None``.

    Two hard errors (``ValueError``) instead of silent drift (review findings):
    - Entry without ``=``: this is almost always the TASK string swallowed
      by the greedy ``nargs='+'`` — skipping it silently would make the agent
      run quietly with the default task.
    - Unknown key (not an ``LLMModelConfig`` field): ``resolve_llm_params``
      would read a dict made up entirely of foreign keys as the profile-keyed
      form and merge the override SILENTLY to ``None`` — a typo
      (``temperatur=``) would vanish without effect while the CLI displays it
      as applied.
    """
    if not raw_items:
        return None
    from ..config.models import LLMModelConfig
    fields = LLMModelConfig.model_fields
    params: Dict[str, Any] = {}
    for item in raw_items:
        if "=" not in item:
            raise ValueError(
                f"invalid --llm-params entry (expected KEY=VALUE): {item!r}. "
                f"Is --llm-params placed BEFORE the task? Give the task first or "
                f"put --llm-params at the end."
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


def preliminary_parser() -> argparse.ArgumentParser:
    """Stage one: the global options, wherever on the line they stand.

    Backward-compatible: allow calling `agent-cli <task>` without an explicit subcommand.
    If the first non-option arg isn't a known subcommand, inject an implicit 'run' subcommand.
    Use a two-stage parse: first extract global options from anywhere using parse_known_args,
    then parse the remaining args (subcommand + subargs). This avoids confusing option values
    with subcommands when we need to insert an implicit 'run'.
    """
    prelim = argparse.ArgumentParser(add_help=False)
    # No default path here: load_settings(None) reads AGENT_CONFIG_PATH before
    # falling back to config/config.yaml. A default of that path shadowed the
    # environment variable for every subcommand.
    prelim.add_argument("--config", dest="config", default=None)
    prelim.add_argument("-v", "--verbose", dest="verbose", action="store_true")
    # color can be set to auto/always/never/ansi/html/text.
    # Default 'auto', not 'always': 'always' emitted escape sequences into
    # redirected output and into consoles that render them literally.
    prelim.add_argument("--color", dest="color", choices=COLOR_CHOICES, default="auto")
    prelim.add_argument("--no-color", dest="no_color", action="store_true")
    prelim.add_argument("--show-tools", "--show-mcp", dest="show_tools", action="store_true")
    prelim.add_argument("--no-status", dest="no_status", action="store_true")
    prelim.add_argument("--raw", dest="raw", action="store_true")
    return prelim


def main_parser_args(ns: argparse.Namespace, rest: List[str]) -> List[str]:
    """The tokens for the main parser: the global options the preliminary
    parser found, then the subcommand -- an implicit `run` when the first
    remaining word is none -- and its arguments."""
    # If the first token of the remaining args isn't a known subcommand, insert implicit 'run'
    if rest:
        if not rest[0].startswith("-") and rest[0] not in _SUBCOMMANDS:
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
    if getattr(ns, "show_tools", False):
        final_args.append("--show-tools")
    if getattr(ns, "no_status", False):
        final_args.append("--no-status")
    if getattr(ns, "raw", False):
        final_args.append("--raw")
    # append the remaining tokens (subcommand + subargs)
    return final_args + rest


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
    p.add_argument("--force", dest="force", action="store_true",
                   help="Run the session even though another process holds it "
                        "(for a lock left behind by a process that hangs)")
    # Set by the wake command (core/session_presence.wake_command): nobody
    # typed this run, so it steps aside quietly where a person gets an error.
    p.add_argument("--woken", dest="woken", action="store_true", help=argparse.SUPPRESS)
    # nargs="?" without type=int on purpose: argparse fills an optional's
    # slot from the next token BEFORE converting it, so `--list-sessions
    # "write me a poem"` would die on int() instead of listing -- which is
    # what the store_true version did. parse_limit sorts the count from the
    # task text afterwards.
    p.add_argument("--list-sessions", dest="list_sessions", nargs="?",
                   const="", default=None, metavar="COUNT|all",
                   help=f"List this user's sessions, one line each (default {DEFAULT_LIMIT}, "
                        "0 = no limit). Only agents the chat offers; 'all' adds the runs "
                        "pipelines started. Sub-agent sessions are not listed.")
    # Same nargs="?" reasoning as --list-sessions above: argparse fills an
    # optional's slot from the next token before converting it.
    p.add_argument("--list-archived", dest="list_archived", nargs="?",
                   const="", default=None, metavar="COUNT",
                   help=f"List this user's archived conversations (default {DEFAULT_LIMIT}, 0 = all).")
    p.add_argument("--restore-session", dest="restore_session", metavar="ID",
                   help="Restore an archived conversation and its sub-agent sessions.")
    p.add_argument("--archive-sessions", dest="archive_sessions", nargs="?",
                   const="", default=None, metavar="DAYS",
                   help="Archive this user's conversations that are older than DAYS "
                        "(default: config session_archive.retention_days). "
                        "Add --dry-run to see what it would take.")
    p.add_argument("--dry-run", dest="archive_dry_run", action="store_true",
                   help="With --archive-sessions: report, change nothing.")
    p.add_argument("--vars", "--template-vars", dest="template_vars", nargs="+", metavar="KEY=VALUE",
                   help="Template variables for prompt rendering (e.g. --vars lang=German user_name=Alice)")


def _add_attachment_args(p: argparse.ArgumentParser) -> None:
    """One flag for every kind of file, like `/attach` in the chat.

    The kind is read from the file, not from which flag was typed -- see
    cli_utils.attachments. The three old flags still work for anyone's
    shell history but are out of the help; they land in the same list and
    are sorted the same way, so `--images notes.txt` now sends a text file
    instead of base64-encoding it as a picture.
    """
    p.add_argument("--attach", dest="attachments", nargs="+", metavar="PATH",
                   action="extend", default=None,
                   help="File(s) to attach to the task -- images, audio or "
                        "text; the kind is detected per file")
    p.add_argument("--images", "--audio", "--text", "--files",
                   dest="attachments", nargs="+", metavar="PATH",
                   action="extend", default=None, help=argparse.SUPPRESS)


def _add_mcp_format(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format")


def main_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """Stage three: the parser with a subparser per command.

    Returns the parser and the `mcp` subparser, whose help a bare `mcp` prints.
    """
    parser = argparse.ArgumentParser(description="Agent System CLI")
    parser.add_argument("--config", dest="config", default=None, help="Path to config (default: AGENT_CONFIG_PATH, else config/config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print progress messages")
    parser.add_argument("--color", dest="color", choices=COLOR_CHOICES, default="auto",
                        help="Output format: auto=ANSI where it renders, always/ansi=force ANSI, html=HTML, never/text=plain text")
    parser.add_argument("--no-color", dest="no_color", action="store_true", help="Disable color output (alias for --color never)")
    parser.add_argument("--show-tools", "--show-mcp", dest="show_tools", action="store_true",
                        help="Show tool call/result details (for debugging)")
    parser.add_argument("--no-status", dest="no_status", action="store_true", help="Hide status messages during execution")
    parser.add_argument("--raw", dest="raw", action="store_true", help="Output raw JSON result instead of pretty printing (plugins info: add factory details)")
    subparsers = parser.add_subparsers(dest="subcommand")

    # run subcommand (default behavior)
    run_parser = subparsers.add_parser("run", help="Run an agent task (default)")
    run_parser.add_argument("task", nargs="?", default="What can you do?", help="Task to run")
    _add_attachment_args(run_parser)
    _add_agent_session_args(run_parser)

    # chat subcommand: interactive REPL that keeps the session across turns
    chat_parser = subparsers.add_parser("chat", help="Interactive chat with an agent (stays in the session)")
    chat_parser.add_argument("task", nargs="?", default=None, help="Optional first message to send immediately")
    _add_attachment_args(chat_parser)
    _add_agent_session_args(chat_parser)

    # plugins subcommand -- read-only. Switching a plugin on is an edit in
    # plugins.yaml (enabled, plus the agent's tool allowlist), not a command.
    # No --raw here: a subparser's default overwrites the global flag, so a
    # second definition made `plugins info --raw` read False.
    plugins_parser = subparsers.add_parser("plugins", help="Inspect discovered plugins")
    plugins_parser.add_argument("action", choices=["list", "info", "search"], nargs="?", default="list", help="Action to perform on plugins")
    plugins_parser.add_argument("name", nargs="?", help="Plugin name for 'info', search term for 'search'")
    plugins_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format for plugin listing")
    plugins_parser.add_argument("--show-metadata", dest="show_metadata", action="store_true", help="Also display plugin._plugin_metadata in listing (JSON output only)")

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

    # mcp subcommand: inspect external MCP servers. Read-only, and nothing
    # that holds state -- a connection opened here dies with this process, so
    # connect/disconnect could not mean anything. Configuration is an edit in
    # mcp_servers.yaml. --format sits on the actions only: a subparser's
    # default overwrites the parent's, so an mcp-level --format was ignored.
    mcp_parser = subparsers.add_parser("mcp", help="Inspect external MCP servers")
    mcp_subparsers = mcp_parser.add_subparsers(dest="action", description="Action to perform on external MCP servers")

    list_p = mcp_subparsers.add_parser("list", help="List configured external MCP servers")
    _add_mcp_format(list_p)

    status_p = mcp_subparsers.add_parser("status", help="Show status for a server or all servers")
    _add_mcp_format(status_p)
    status_p.add_argument("server", nargs="?", help="Optional server name to show status for")

    test_p = mcp_subparsers.add_parser("test", help="Test connectivity and basic functionality of an external MCP server")
    test_p.add_argument("server", help="Server name to test")

    tools_p = mcp_subparsers.add_parser("tools", help="List a server's tools and which of them are blocked")
    _add_mcp_format(tools_p)
    tools_p.add_argument("server", help="Server name")

    # hooks subcommand for hook introspection. No stats: they live in the
    # memory of the process that ran the hooks, which this one never is.
    hooks_parser = subparsers.add_parser("hooks", help="Hook introspection")
    hooks_parser.add_argument("action", choices=["list", "inspect"], nargs="?", default="list", help="Action to perform")
    hooks_parser.add_argument("name", nargs="?", help="Hook name for 'inspect' action")
    from agent_system.hooks import HookType as _HookType
    hooks_parser.add_argument("--type", dest="hook_type", choices=[t.value for t in _HookType],
                              help="Filter by hook type (e.g., pre_llm_call, post_llm_call)")
    hooks_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format")

    # users subcommand for user management
    # Listed for `agent-cli --help` only: `users` is handed to its typer app
    # before any parser runs (agent_cli._run_users_cli).
    subparsers.add_parser("users", help="Manage users (admin) -- see `agent-cli users --help`")

    return parser, mcp_parser
