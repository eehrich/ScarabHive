"""Starting and ending the processes of an eval: agent runs, setups, checks.

Output goes to a file, never a pipe: a pipe keeps ``subprocess.run`` waiting past its timeout for as long as any
grandchild holds it -- an agent that left a sleeper behind would hang the runner. Each process gets a group of
its own, so a timeout ends what it started too, and no console window of its own (on Windows a console program
started from a process without a console opens one per start).
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Union

GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt"
         else {"start_new_session": True})


def kill_tree(proc: subprocess.Popen) -> None:
    """End *proc* and its process group. A process that left the group on purpose (a terminal command in a
    session or job of its own) is beyond this."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_bounded(args: Union[str, List[str]], *, cwd: Path, log: Path, timeout: float,
                env: Optional[Dict[str, str]] = None, shell: bool = False,
                errors: Optional[Path] = None) -> Optional[int]:
    """Run *args* with its output in *log* (its error output too, unless *errors* names a file of its own); its
    exit code, or None when it was killed at *timeout*."""
    with (open(log, "w", encoding="utf-8", errors="replace") as out,
          open(errors or os.devnull, "w", encoding="utf-8", errors="replace") as err):
        proc = subprocess.Popen(args, cwd=cwd, env=env, shell=shell, stdout=out,
                                stderr=err if errors else subprocess.STDOUT, stdin=subprocess.DEVNULL, **GROUP)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            return None


def tail(log: Path, chars: int = 2000) -> str:
    try:
        return log.read_text(encoding="utf-8", errors="replace")[-chars:]
    except OSError:
        return ""
