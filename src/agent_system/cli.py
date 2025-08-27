from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import yaml

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None

from .config.settings import load_settings
from .mcp.plugins import discover_all_plugins
from .mcp.base import MCPRegistry
from .utils.logging import setup_logging
from .servers.bootstrap import bootstrap_servers
from .servers.agent.server import Agent

# Global color mode: tests may monkeypatch this variable
color_mode: str = "auto"


def _supports_color() -> bool:
    """Return whether ANSI color sequences should be used.

    Honors the global `color_mode` which tests may set to 'auto',
    'always' or 'never'. In 'auto' mode this checks stdout.isatty().
    """
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
        # Interpret configured plugin_dirs: if a path is relative, resolve it
        # against the repository root so `plugins` in `config/agent.yaml`
        # refers to the repo-level `plugins/` directory (common UX).
        repo_root = Path(__file__).resolve().parents[2]
        dirs = []
        for p in (config.mcp.plugin_dirs or []):
            pp = Path(p)
            if not pp.is_absolute():
                pp = repo_root.joinpath(pp)
            dirs.append(pp)
        # If configured dirs do not exist, prefer the repository `plugins/` dir
        # so the CLI shows repo example plugins in common setups/tests.
        if not any(p.exists() for p in dirs):
            repo_plugins = repo_root.joinpath("plugins")
            if repo_plugins.exists():
                dirs = [repo_plugins]
        plugins = discover_all_plugins(dirs)

        def to_list():
            out = []
            for name, factory in plugins.items():
                meta = getattr(factory, "_plugin_metadata", None) or {}
                # metadata loading is handled centrally in discover_all_plugins();
                # keep local code minimal.
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
                # Always print DESCRIPTION and VERSION lines (may be blank) to keep output stable
                print(f"DESCRIPTION: {meta.get('description', '')}")
                print(f"VERSION: {meta.get('version', '')}")
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
            # Read master manifest to discover included files and prefer writing
            # to the included file that contains an `mcp` mapping (e.g. `mcp.yaml`).
            managed_path = cfg_path
            try:
                master_raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                master_raw = {}

            includes = master_raw.get("includes") or master_raw.get("files") or []
            if isinstance(includes, str):
                includes = [includes]

            # Attempt to find an included file that contains 'mcp' mapping
            found = None
            for inc in includes:
                inc_path = Path(inc)
                if not inc_path.is_absolute():
                    inc_path = cfg_path.parent.joinpath(inc_path)
                if inc_path.exists():
                    try:
                        inc_data = yaml.safe_load(inc_path.read_text(encoding="utf-8")) or {}
                        if isinstance(inc_data, dict) and "mcp" in inc_data:
                            found = inc_path
                            break
                    except Exception:
                        continue

            # If a specific mcp-managed file was found in includes, use it.
            if found:
                managed_path = found
            else:
                # Fallback: if master declares mcp.managed_file, respect it; otherwise
                # default to writing a `.managed` sibling next to master (legacy behavior)
                mcp_master = master_raw.get("mcp", {}) or {}
                mf = mcp_master.get("managed_file")
                if mf:
                    mp = Path(mf)
                    if not mp.is_absolute():
                        managed_path = cfg_path.parent.joinpath(mp)
                    else:
                        managed_path = mp
                else:
                    managed_path = cfg_path.with_name(cfg_path.stem + ".managed" + cfg_path.suffix)

            # load managed data (this is what we'll update)
            managed_data = {}
            if managed_path.exists():
                try:
                    managed_data = yaml.safe_load(managed_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    managed_data = {}
            mcp = managed_data.get("mcp", {}) or {}
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
            # persist into the managed_data (not the master data) so we don't overwrite
            # user-edited master config. We'll write managed_data to managed_path.
            managed_data["mcp"] = mcp
            # atomic write only: write temp file in same dir and atomically replace target.
            try:
                with tempfile.NamedTemporaryFile("w", delete=False, dir=str(managed_path.parent), encoding="utf-8") as tf:
                    tf.write(json.dumps(managed_data, ensure_ascii=False, indent=2))
                    tmp_name = tf.name
                os.replace(tmp_name, str(managed_path))
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
        # Fill missing metadata from repo plugins/<name>/plugin.yaml when possible
        repo_root = Path(__file__).resolve().parents[2]
        for p in listing:
            if (p.get("description") is None or p.get("version") is None) and p.get("name"):
                meta_path = repo_root.joinpath("plugins", p["name"], "plugin.yaml")
                if meta_path.exists():
                    try:
                        import yaml as _yaml
                        loaded = _yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                        if p.get("description") is None:
                            p["description"] = loaded.get("description")
                        if p.get("version") is None:
                            p["version"] = loaded.get("version")
                    except Exception:
                        pass
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
