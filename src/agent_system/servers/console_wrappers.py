from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable


def _ensure_repo_root_on_path() -> None:
    """Ensure the repository root (the folder containing `src/`) is on sys.path.

    This allows runtime imports of the top-level `plugins/` package which lives
    at the repo root rather than under `src/`.
    """
    here = Path(__file__).resolve()
    # repo_root = .../src/agent_system/servers/.. -> parents[3] is repo root
    repo_root = here.parents[3]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


def _run_plugin_module(module_path: str, callable_name: str) -> None:
    _ensure_repo_root_on_path()
    try:
        mod = __import__(module_path, fromlist=[callable_name])
    except Exception as e:
        raise RuntimeError(f"Failed to import plugin module {module_path}: {e}") from e

    func = getattr(mod, callable_name, None)
    if not callable(func):
        raise RuntimeError(f"Plugin module {module_path} has no callable {callable_name}")
    # Call without args; the plugin __main__ typically reads sys.argv
    return func()


def mcp_weather() -> None:
    """Console entry for mcp-weather: runs plugins.weather.main()"""
    return _run_plugin_module("plugins.weather.__main__", "main")


def mcp_duckduckgo_search() -> None:
    """Console entry for mcp-duckduckgo-search: runs plugins.duckduckgo_search.cli_main()"""
    return _run_plugin_module("plugins.duckduckgo_search.__main__", "cli_main")
