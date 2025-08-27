from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
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


def main() -> None:
    # Backward-compatible: allow calling `agent-cli <task>` without an explicit subcommand.
    # If the first non-option arg isn't a known subcommand, inject an implicit 'run' subcommand.
    argv = list(sys.argv)
    # Normalize argv so global options (--config, -v/--verbose) are accepted
    # even when provided after the subcommand (tests often do this).
    globals_opts = []
    i = 1
    while i < len(argv):
        if argv[i] == "--config" and i + 1 < len(argv):
            globals_opts.extend([argv[i], argv[i + 1]])
            del argv[i:i + 2]
            continue
        if argv[i] in ("-v", "--verbose"):
            globals_opts.append(argv[i])
            del argv[i]
            continue
        i += 1

    if globals_opts:
        argv = [argv[0]] + globals_opts + argv[1:]

    if len(argv) > 1 and argv[1] not in ("plugins", "run", "-h", "--help", "--config", "-v", "--verbose"):
        # insert the 'run' subcommand so argparse handles both styles
        argv.insert(1, "run")

    parser = argparse.ArgumentParser(description="Agent System CLI")
    parser.add_argument("--config", dest="config", default=str(Path("config/agent.yaml")), help="Path to config")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print progress messages")
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
                out.append({
                    "name": name,
                    "description": meta.get("description"),
                    "version": meta.get("version"),
                    "enabled": name in enabled_set,
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
                print(f"ENABLED: {enabled_flag}")
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

            # confirm unless --yes. If stdin is not a TTY (non-interactive/test), skip prompt.
            if not args.yes and sys.stdin.isatty():
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
            try:
                cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")
            except Exception as e:
                print(json.dumps({"error": "failed to write config", "reason": str(e)}, ensure_ascii=False))
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
            rows = [(p.get("name") or "", "YES" if p.get("enabled") else "NO", p.get("description") or "", p.get("version") or "") for p in listing]
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
        import os
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
