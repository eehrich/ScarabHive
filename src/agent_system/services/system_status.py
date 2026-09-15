"""What the System panel reports: is this process healthy, and if not, why.

``/health`` answers "ok" whenever the process answers at all -- a liveness
probe, and it stays that. This module gives the status a meaning: a list of
checks, each ``ok``/``warn``/``error`` with a sentence, and the overall status
is the worst of them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LEVELS = ("ok", "warn", "error")

#: When this process started and which commit it runs; set by record_start().
_started: dict[str, Any] = {"at": None, "commit": None}


def git_commit(cwd: Path = _REPO_ROOT) -> Optional[dict]:
    """The checked-out commit: hash, committer date, subject. None without git."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H%x00%cI%x00%s"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    commit, date, subject = (result.stdout.strip().split("\x00") + ["", ""])[:3]
    return {"hash": commit, "date": date, "subject": subject}


def record_start() -> None:
    """Remember the start time and the commit the process was started from."""
    _started.update(at=time.time(), commit=git_commit())


def _check(name: str, level: str, detail: str) -> dict:
    return {"name": name, "level": level, "detail": detail}


def build_checks(*, problems: list[str], blocked: list[dict],
                 commit_at_start: Optional[dict], commit_on_disk: Optional[dict],
                 config_findings: Optional[list[str]] = None) -> list[dict]:
    """The checks behind the overall status, from already collected facts."""
    checks = []
    if problems:
        checks.append(_check("servers", "error",
                             f"{len(problems)} configured server(s) did not start: " + "; ".join(problems)))
    else:
        checks.append(_check("servers", "ok", "every enabled server started"))

    if config_findings:
        checks.append(_check("config", "error",
                             f"{len(config_findings)} agent config error(s): " + "; ".join(config_findings)))

    if blocked:
        names = ", ".join(f"{b['model']} ({round(b['seconds_left'])}s)" for b in blocked)
        checks.append(_check("llm", "warn", f"paused after rate limits: {names}"))
    else:
        checks.append(_check("llm", "ok", "no model is paused"))

    if commit_at_start and commit_on_disk and commit_at_start["hash"] != commit_on_disk["hash"]:
        checks.append(_check(
            "deploy", "warn",
            f"code on disk is {commit_on_disk['hash'][:8]} ({commit_on_disk['subject']}), "
            f"this process runs {commit_at_start['hash'][:8]} -- a restart deploys it"))
    elif commit_at_start:
        checks.append(_check("deploy", "ok", "running the checked-out commit"))
    else:
        checks.append(_check("deploy", "ok", "commit unknown (no git)"))
    return checks


def overall(checks: list[dict]) -> str:
    return max((c["level"] for c in checks), key=_LEVELS.index, default="ok")


def _memory_mb() -> Optional[float]:
    try:
        import psutil
    except ImportError:
        return None
    return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)


def _servers(runtime: Any) -> dict:
    from ..servers.agent.server import Agent

    if runtime is None:
        return {"declared": 0, "running": 0, "agents": 0, "problems": [], "config_findings": []}
    names = runtime.registry.list()
    return {
        "declared": len(runtime.declarations()),
        "running": len(names),
        "agents": sum(isinstance(runtime.registry.get(n), Agent) for n in names),
        "problems": list(runtime.problems),
        "config_findings": list(runtime.config_findings),
    }


def _hooks() -> dict:
    from ..hooks import get_hook_registry

    registry = get_hook_registry()
    names = [n for group in registry.list_hooks().values() for n in group]
    enabled = sum(bool((registry.get_hook_info(n) or {}).get("enabled")) for n in names)
    return {"registered": len(names), "enabled": enabled}


async def collect_system_status() -> dict:
    from ..llm.model_health import model_health
    from ..runtime import Runtime

    if _started["at"] is None:
        record_start()
    commit_on_disk = await asyncio.to_thread(git_commit)
    runtime = Runtime.last_started
    servers = _servers(runtime)
    blocked = model_health.blocked()
    checks = build_checks(problems=servers["problems"], blocked=blocked,
                          commit_at_start=_started["commit"], commit_on_disk=commit_on_disk,
                          config_findings=servers["config_findings"])
    return {
        "status": overall(checks),
        "checks": checks,
        "build": {
            "commit": _started["commit"],
            "commit_on_disk": commit_on_disk,
            "started_at": _started["at"],
            "uptime_seconds": round(time.time() - _started["at"], 1),
            "python": platform.python_version(),
        },
        "process": {
            "pid": os.getpid(),
            "memory_mb": _memory_mb(),
            "threads": threading.active_count(),
            "async_tasks": len(asyncio.all_tasks()),
        },
        "servers": servers,
        "hooks": _hooks(),
        "llm_blocked": blocked,
    }
