"""agent-cli: read the command line, load the config, hand over to the command.

The entry point only. How the line is read is cli_utils/cli_parser.py (three
stages, docs/_arch_cli_architecture.md section 2); each command lives in
cli_utils/commands/ -- ``run``/``chat`` in run.py, ``plugins``, ``mcp``,
``hooks`` and ``reload`` in their own modules -- and ``users`` is the Typer app
in cli_utils/users.py. What stays here is what every command shares: the
project directory, the console streams, the colour mode, the checks that need
no config, and the config itself.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

from .config.settings import load_settings
from .paths import enter_project, user_path
from .config.models import AgentSystemConfig
from .cli_utils.common import set_color_mode
from .cli_utils.agent_runner import run_as_local_operator
from .cli_utils.cli_parser import (
    main_parser,
    main_parser_args,
    parse_llm_params_args,
    preliminary_parser,
)
from .cli_utils.commands.hooks import run_hooks_command
from .cli_utils.commands.mcp import run_mcp_command
from .cli_utils.commands.plugins import run_plugins_command
from .cli_utils.commands.reload import run_reload_command
from .cli_utils.commands.run import run_agent_command


def _exit_on_unknown_profile(config: AgentSystemConfig, profile: str) -> None:
    """Exit 1 with the available profiles when `profile` is not configured --
    before the bootstrap, which is slow; the message is the factory's own."""
    from .llm.factory import UnknownLLMProfile

    profiles = config.llm_system.profiles if config.llm_system else {}
    if profile not in profiles:
        print(f"ERROR: {UnknownLLMProfile(profile, profiles)}", file=sys.stderr)
        sys.exit(1)


def _run_users_cli(users_args: List[str], config_path: Optional[str]) -> None:
    """Hand `agent-cli users ...` to the typer app, tokens as typed."""
    from .cli_utils import users as users_cli
    users_cli.CONFIG_PATH = config_path
    # `list` is the default, also when only its options are given
    # (`users --limit 5`); -h/--help stay with the group.
    if not users_args or (users_args[0].startswith("-") and users_args[0] not in ("-h", "--help")):
        users_args = ["list", *users_args]
    users_cli.app(users_args, prog_name="agent-cli users")


def _tolerate_unencodable_text() -> None:
    """Console streams that replace what they cannot encode instead of crashing."""
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


def _apply_colour_flags(no_color: bool, color: str) -> None:
    """--color/--no-color, decided early so helpers behave predictably."""
    if no_color:
        set_color_mode("never")
    else:
        set_color_mode(color)

    # Initialize colorama on interactive TTYs so ANSI renders on Windows
    try:
        mode = "never" if no_color else color
        if mode != "never" and sys.stdout.isatty():
            import colorama
            colorama.init()
    except Exception:
        pass


def main() -> None:
    """The agent-cli entry point, run as the local operator
    (cli_utils.agent_runner.run_as_local_operator)."""
    run_as_local_operator(_main)


def _main() -> None:
    # Run from the repository, whatever directory this was started in: config,
    # prompts, databases and logs are declared as repository-relative strings
    # all over the plugin configs. What the person meant by "." is kept by
    # paths.launch_dir() for the arguments THEY typed.
    enter_project()
    _tolerate_unencodable_text()

    prelim = preliminary_parser()
    orig_args = sys.argv[1:]

    # users: typer owns its arguments, help and exit codes. The argparse copy
    # of them had drifted -- `update --admin` and `create --force` reached
    # typer as options it does not have (a traceback), the EMAIL of
    # `update USER EMAIL` was dropped without a word, and every failure
    # exited 0.
    #
    # Split off BEFORE the preliminary parser sees the line: it reads option
    # VALUES as well -- `-p -vS3cret` arrived as `-p -S3cret`, `-p --no` died
    # as an ambiguous option, `-p --conf x` became the config path. Only what
    # precedes `users` is global; after it, typer refuses global options out
    # loud.
    if "users" in orig_args:
        at = orig_args.index("users")
        ns_before, stray = prelim.parse_known_args(orig_args[:at])
        if not stray:  # nothing but global options before it: it is the subcommand
            # Resolved here as well: this branch RETURNS, so the --config the
            # run path resolves further down never reaches it, and the same
            # flag would have named two different files.
            return _run_users_cli(
                orig_args[at + 1:],
                str(user_path(ns_before.config)) if ns_before.config else None)

    ns, rest = prelim.parse_known_args(orig_args)
    _apply_colour_flags(getattr(ns, "no_color", False), getattr(ns, "color", "auto"))

    parser, mcp_parser = main_parser()
    args = parser.parse_args(main_parser_args(ns, rest))

    # If no subcommand was provided, show help and exit instead of proceeding
    if not getattr(args, "subcommand", None):
        parser.print_help()
        return

    # Checked HERE, where arguments are checked -- not where the override is
    # applied. That sits behind the bootstrap, and answering a typo with an
    # error message fifteen seconds later is the wrong place to learn it.
    if getattr(args, "max_steps", None) is not None and args.max_steps < 1:
        parser.error("--max-steps must be at least 1")
    # Same place for --llm-params: parsing them needs no config. Behind the
    # bootstrap a typo was answered late -- and with exit code 0.
    try:
        llm_params_override = parse_llm_params_args(getattr(args, "llm_params", None))
    except ValueError as e:
        parser.error(str(e))

    def vprint(msg: str) -> None:
        if args.verbose:
            print(msg, flush=True)

    vprint("[cli] verbose mode on")
    # A --config is typed where the person stands, not where the process runs.
    cfg_arg = str(user_path(args.config)) if args.config else None
    vprint(f"[cli] loading config: {cfg_arg or os.environ.get('AGENT_CONFIG_PATH') or 'config/config.yaml'}")
    config = load_settings(cfg_arg)
    # A mistyped --llm needs only the config to be recognised -- behind the
    # bootstrap it cost the whole plugin start before the error. Not for the
    # commands that only read or move the session store: they need no profile
    # and must not be hidden by a broken one.
    store_only = (
        getattr(args, "list_sessions", None) is not None
        or getattr(args, "list_archived", None) is not None
        or getattr(args, "archive_sessions", None) is not None
        or bool(getattr(args, "restore_session", None))
    )
    if (getattr(args, "llm_profile_override", None)
            and not store_only
            and config.llm_system and config.llm_system.profiles):
        _exit_on_unknown_profile(config, args.llm_profile_override)

    # The commands that inspect or post first: no agent, no heavy bootstrap.
    if args.subcommand == "reload":
        run_reload_command(args, config)
    elif args.subcommand == "plugins":
        run_plugins_command(args, config)
    elif args.subcommand == "hooks":
        run_hooks_command(args, config)
    elif args.subcommand == "mcp":
        if args.action:
            run_mcp_command(args, config)
        else:
            mcp_parser.print_help()
    else:
        run_agent_command(args, config, llm_params_override, vprint)


if __name__ == "__main__":
    main()
