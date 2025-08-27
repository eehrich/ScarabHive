from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import os
import tempfile
import yaml

from .config.settings import load_settings
from .mcp.base import MCPRegistry
from .servers.agent.server import Agent  # Use Agent from servers
from .servers.bootstrap import bootstrap_servers
from .mcp.plugins import discover_all_plugins
from .utils.logging import setup_logging
import logging
import sys
from pathlib import Path
try:
    from tabulate import tabulate  # type: ignore
except Exception:
    tabulate = None


# color_mode: 'auto'|'always'|'never' - can be set from CLI --color
color_mode = "auto"


def _supports_color() -> bool:
    """Return True if color output should be used based on mode/env/tty."""
    if os.environ.get("NO_COLOR"):
        return False
    if color_mode == "never":
        return False
    if color_mode == "always":
        return True
    # auto
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _colorize(text: str, color_code: str) -> str:
    """Wrap text in ANSI color codes when supported."""
    if not _supports_color():
        return text
    return f"\x1b[{color_code}m{text}\x1b[0m"



def main() -> None:
    # Backward-compatible: allow calling `agent-cli <task>` without an explicit subcommand.
    # If the first non-option arg isn't a known subcommand, inject an implicit 'run' subcommand.
    # Use a two-stage parse: first extract global options from anywhere using parse_known_args,
    # then parse the remaining args (subcommand + subargs). This avoids confusing option values
    # with subcommands when we need to insert an implicit 'run'.
    prelim = argparse.ArgumentParser(add_help=False)
    prelim.add_argument("--config", dest="config", default=str(Path("config/agent.yaml")))
    prelim.add_argument("-v", "--verbose", dest="verbose", action="store_true")
    prelim.add_argument("--color", dest="color", choices=["auto", "always", "never"], default="auto")
    prelim.add_argument("--no-color", dest="no_color", action="store_true")
    orig_args = sys.argv[1:]
    ns, rest = prelim.parse_known_args(orig_args)

    # decide color mode early so helpers behave predictably
    global color_mode
    if getattr(ns, "no_color", False):
        color_mode = "never"
    else:
        color_mode = getattr(ns, "color", "auto")

    # If the first token of the remaining args isn't a known subcommand, insert implicit 'run'
    known = ("plugins", "run", "-h", "--help")
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
    # append the remaining tokens (subcommand + subargs)
    argv = [sys.argv[0]] + final_args + rest

    parser = argparse.ArgumentParser(description="Agent System CLI")
    parser.add_argument("--config", dest="config", default=str(Path("config/agent.yaml")), help="Path to config")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print progress messages")
    parser.add_argument("--color", dest="color", choices=["auto", "always", "never"], default="auto", help="Colorize output (auto|always|never)")
    parser.add_argument("--no-color", dest="no_color", action="store_true", help="Disable color output (alias for --color never)")
    subparsers = parser.add_subparsers(dest="subcommand")

    # run subcommand (default behavior)
    run_parser = subparsers.add_parser("run", help="Run an agent task (default)")
    run_parser.add_argument("task", nargs="?", default="What can you do?", help="Task to run")

    # plugins subcommand
    plugins_parser = subparsers.add_parser("plugins", help="Manage plugins")
    plugins_parser.add_argument("action", choices=["list", "info", "enable", "disable", "search", "status"], nargs="?", default="list", help="Action to perform on plugins")
    plugins_parser.add_argument("name", nargs="?", help="Plugin name for the 'info', 'enable', 'disable' actions or search term for 'search'")
    plugins_parser.add_argument("--yes", dest="yes", action="store_true", help="Assume yes for confirmations")
    plugins_parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Don't persist changes; show preview")
    plugins_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format for plugin listing")
    plugins_parser.add_argument("--raw", dest="raw", action="store_true", help="Show raw factory information for 'info' action")

    args = parser.parse_args(argv[1:])

    # If no subcommand was provided, show help and exit instead of proceeding
    if not getattr(args, "subcommand", None):
        parser.print_help()
        return

    def vprint(msg: str) -> None:
        if args.verbose:
            print(msg, flush=True)

    vprint(f"[cli] verbose mode on")
    vprint(f"[cli] loading config: {args.config}")
    config = load_settings(args.config)

    # If user requested plugin listing, handle and exit early (no heavy bootstrap)
    if args.subcommand == "plugins":
        dirs = [Path(p) for p in (config.mcp.plugin_dirs or [])]
        plugins = discover_all_plugins(dirs)

        def to_list():
            out = []
            for name, factory in plugins.items():
                meta = getattr(factory, "_plugin_metadata", None) or {}
                # include whether this plugin is enabled in the current config
                enabled_set = set((config.mcp.enabled_servers or []) or [])
                enabled_flag = name in enabled_set
                out.append({
                    "name": name,
                    "description": meta.get("description"),
                    "version": meta.get("version"),
                    "enabled": enabled_flag,
                })
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
            if getattr(args, "raw", False):
                factory_info = {
                    "factory_repr": repr(factory),
                    "factory_module": getattr(factory, "__module__", None),
                }
                out = {"name": target, "metadata": meta, **factory_info}
                out["enabled"] = target in set((config.mcp.enabled_servers or []) or [])
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
                for k, v in meta.items():
                    print(f"{k.upper()}: {v}")
                # show enabled status for this plugin
                enabled_flag = target in set((config.mcp.enabled_servers or []) or [])
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

        # enable/disable actions: persist to the YAML config's mcp.enabled_servers
        if getattr(args, "action", None) in ("enable", "disable"):
            target = getattr(args, "name", None)
            if not target:
                print(json.dumps({"error": "missing plugin name"}, ensure_ascii=False))
                return
            cfg_path = Path(args.config)
            data = {}
            if cfg_path.exists():
                try:
                    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    data = {}
            mcp = data.get("mcp", {}) or {}
            enabled = set(mcp.get("enabled_servers", []) or [])

            intended = "enable" if args.action == "enable" else "disable"
            # preview/dry-run
            if args.dry_run:
                preview = sorted(enabled | {target}) if intended == "enable" else sorted(enabled - {target})
                print(json.dumps({"dry_run": True, "action": intended, "preview_enabled": preview}, ensure_ascii=False))
                return

            # confirm unless --yes. If stdin or stdout are not a TTY (non-interactive/test), skip prompt.
            if not args.yes and (sys.stdin.isatty() and sys.stdout.isatty()):
                resp = input(f"Are you sure you want to {intended} plugin '{target}'? [y/N]: ")
                if resp.strip().lower() not in ("y", "yes"):
                    print(json.dumps({"result": "cancelled"}, ensure_ascii=False))
                    return

            if args.action == "enable":
                enabled.add(target)
            else:
                enabled.discard(target)
            mcp["enabled_servers"] = sorted(enabled)
            data["mcp"] = mcp
            # atomic write with backup to avoid corrupting config on failure
            try:
                backup_suffix = (config.mcp.backup_suffix if getattr(config, "mcp", None) else ".bak")
                backup_rotate = (config.mcp.backup_rotate if getattr(config, "mcp", None) else 1)
                backup_path = cfg_path.with_suffix(cfg_path.suffix + backup_suffix)
                # create tmp file in same directory to ensure atomic os.replace works
                with tempfile.NamedTemporaryFile("w", delete=False, dir=str(cfg_path.parent), encoding="utf-8") as tf:
                    tf.write(yaml.safe_dump(data))
                    tmp_name = tf.name
                # make backup if original exists
                if cfg_path.exists():
                    try:
                        # rotate existing backups if requested
                        if backup_rotate and backup_rotate > 1:
                            # rotate up: .bak.N <- .bak.(N-1), ..., .bak.1 <- current
                            for i in range(backup_rotate - 1, 0, -1):
                                older = cfg_path.with_suffix(cfg_path.suffix + f"{backup_suffix}.{i}")
                                newer = cfg_path.with_suffix(cfg_path.suffix + f"{backup_suffix}.{i+1}")
                                if older.exists():
                                    try:
                                        os.replace(str(older), str(newer))
                                    except Exception:
                                        pass
                            # move current to .bak.1
                            first_rot = cfg_path.with_suffix(cfg_path.suffix + f"{backup_suffix}.1")
                            os.replace(str(cfg_path), str(first_rot))
                        else:
                            # backup_rotate == 1 or no rotation requested: move to simple backup (e.g. .bak)
                            os.replace(str(cfg_path), str(backup_path))
                    except Exception:
                        # fallback: copy contents
                        backup_path.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
                # atomically move temp to target
                os.replace(tmp_name, str(cfg_path))
            except Exception as e:
                print(json.dumps({"error": "failed to write config", "reason": str(e)}, ensure_ascii=False))
                # cleanup temp file if present
                try:
                    if 'tmp_name' in locals() and os.path.exists(tmp_name):
                        os.remove(tmp_name)
                except Exception:
                    pass
                return
            print(json.dumps({"result": "ok", "enabled": mcp["enabled_servers"]}, ensure_ascii=False))
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
            # load config file to read enabled list
            cfg_path = Path(args.config)
            data = {}
            if cfg_path.exists():
                try:
                    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    data = {}
            enabled = set((data.get("mcp") or {}).get("enabled_servers", []) or [])
            listing = to_list()
            for p in listing:
                p["enabled"] = p["name"] in enabled
            print(json.dumps(listing, indent=2, ensure_ascii=False))
            return

        # list action: either json or simple table
        listing = to_list()
        if args.out_format == "table":
            # nice table layout using tabulate if available
            rows = []
            for p in listing:
                enabled_flag = bool(p.get("enabled"))
                enabled_text = "YES" if enabled_flag else "NO"
                display_enabled = enabled_text
                if _supports_color():
                    if enabled_flag:
                        display_enabled = _colorize(enabled_text, "32")
                    else:
                        display_enabled = _colorize(enabled_text, "31")
                rows.append((p.get("name") or "", display_enabled, p.get("description") or "", p.get("version") or ""))
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
    # Setup logging from config; file handler is created here. Console level is adjusted below.
    log_file = setup_logging(config.logging.enabled, config.logging.level, config.logging.file)
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
    bootstrap_servers(config, registry)
    vprint(f"[cli] servers registered: {', '.join(registry.list())}")
    logger.info("Servers registered: %s", ", ".join(registry.list()))
    agent = Agent("cli_agent", config, registry=registry)

    vprint(f"[cli] running task: {args.task}")
    logger.info("Running task: %s", args.task)
    result = asyncio.run(agent.run(args.task))
    vprint("[cli] done")
    logger.info("Task completed")
    # Always print the JSON result to stdout for consumption
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
