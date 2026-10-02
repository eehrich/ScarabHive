"""Which runner agent runs a machine: the one whose ``runs_machines_in`` holds the machine's file, else the
stategraph instance's ``runner_agent`` (docs/stategraph_design.md §8.3).

The runner is the boundary of a machine's tool activities: they go through its ``dispatch_tool_call``, so its tool
allowlist is everything they may call, and its ``inject_params`` are added to their arguments. A plugin that ships
machines ships their runner next to them, in its own ``agents/*.yaml``::

    tickets_machine_runner:
      type: stategraph_runner            # inherits the runner; its list grows with + entries
      enabled: true
      runs_machines_in: [src/plugins/tickets/machines]
      inject_params: {"ticket_store_*": {api_key: ...}}
      agent_config: {tools: {allowed: ["+ticket_store/*"]}}

The folder decides, never the machine file: a machine anyone may write (data/stategraph/machines) cannot pick the
runner with the most tools. ``runs_machines_in`` is read from the entry itself, not inherited -- a runner derived
from this one claims no folder by accident. Folders are relative to the project root, globs allowed; the deepest
folder that holds a file wins. A whole run, its submachines included, runs with its root machine's runner -- the one
its row names from its start on (a resume, a snapshot fork keep it).
"""
from __future__ import annotations

import glob
import logging
import os
from pathlib import Path
from typing import Any, Optional

RUNS_KEY = "runs_machines_in"
logger = logging.getLogger(__name__)


def _project_root() -> Path:
    from agent_system.paths import PROJECT_ROOT

    return PROJECT_ROOT


def runner_folders(system_config: Any) -> dict[str, list[str]]:
    """``{runner: [real folder paths]}`` from every enabled server entry that declares ``runs_machines_in``."""
    servers = getattr(getattr(system_config, "plugins", None), "servers", None) or {}
    found: dict[str, list[str]] = {}
    for name, entry in servers.items():
        declared = getattr(entry, RUNS_KEY, None)
        if not declared or not getattr(entry, "enabled", False):
            continue
        if not isinstance(declared, (str, list, tuple)):  # a typo must not stop every machine of every folder
            logger.warning("stategraph: %s: runs_machines_in must be a folder or a list of folders, not %r -- ignored",
                           name, declared)
            continue
        folders: list[str] = []
        for raw in [declared] if isinstance(declared, str) else list(declared):
            path = Path(str(raw))
            pattern = str(path if path.is_absolute() else _project_root() / path)
            matches = sorted(glob.glob(pattern)) if any(ch in str(raw) for ch in "*?[") else [pattern]
            folders.extend(os.path.realpath(match) for match in matches)
        found[str(name)] = folders
    return found


def runner_of(system_config: Any, path: Optional[str], default: str) -> tuple[str, Optional[str]]:
    """The runner of the machine file at ``path``, and why none can be told when two runners claim its folder (then
    the default comes back with that problem). No path, or a folder nobody claims: the default."""
    if not path:
        return default, None
    real = os.path.realpath(str(path))
    claims: dict[str, set[str]] = {}
    for runner, folders in runner_folders(system_config).items():
        for folder in folders:
            if real.startswith(folder + os.sep):
                claims.setdefault(folder, set()).add(runner)
    if not claims:
        return default, None
    deepest = max(claims, key=len)
    owners = sorted(claims[deepest])
    if len(owners) > 1:
        return default, (f"runners {', '.join(owners)} both claim {deepest} in runs_machines_in: keep it in one -- "
                         "until then no machine there runs")
    return owners[0], None


def runner_inject_params(system_config: Any, name: str) -> dict[str, dict[str, Any]]:
    """The runner's own ``inject_params`` (pattern on the flat tool name -> params), inherited like its other keys."""
    from agent_system.config.settings import get_tool_server_config

    try:
        config = get_tool_server_config(name, system_config)
    except Exception:
        return {}
    raw = getattr(config, "inject_params", None) or {}
    return {str(pattern): dict(params) for pattern, params in raw.items() if isinstance(params, dict)}


def merged_inject_params(instance: dict[str, dict[str, Any]],
                         runner: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The instance's params, the runner's over them: param by param where both name a pattern, and every pattern of
    the runner's last -- ``inject`` applies them in order, so a param the runner sets wins whichever pattern of the
    instance's also sets it."""
    merged = {pattern: dict(params) for pattern, params in instance.items()}
    for pattern, params in runner.items():
        merged[pattern] = {**merged.pop(pattern, {}), **params}
    return merged


def runner_names(system_config: Any, default: str) -> set[str]:
    """Every runner: the default and each one that claims a folder."""
    return {default, *runner_folders(system_config)}
