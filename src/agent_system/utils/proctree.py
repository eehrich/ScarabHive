"""A subprocess and everything it started, ended together.

``subprocess.run(timeout=...)`` kills the direct child only and then waits for
its pipes: a filter, hook, credential helper or ``git-remote-https`` the child
started keeps them open -- the timeout bounds nothing on Windows, and on POSIX
the grandchildren keep running. ``run_bounded`` kills the whole tree instead.
It was written once in coding_cli and copied nowhere; forge and coding_cli's
workdir set-up used ``subprocess.run``.
"""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

import psutil

from agent_system.core.session_presence import alive

__all__ = ["kill_tree", "process_start", "run_bounded"]


def process_start(pid: int) -> float:
    """The start time of ``pid`` (0.0 if it is gone): with the pid, it names one process, not a reused pid."""
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return 0.0


def kill_tree(pid: Optional[int], started: Optional[float], wait_s: float = 10) -> None:
    """``pid`` and whatever it started, and back only once they are gone.

    On Windows the kill is asynchronous, and a caller finalizing right after
    it found the process still alive. ``started`` (``process_start``) keeps a
    reused pid from being taken for it.
    """
    if not pid or not alive(pid, started):
        return
    try:
        root = psutil.Process(pid)
        procs = [*root.children(recursive=True), root]
    except psutil.Error:
        return
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=wait_s)


def run_bounded(cmd: Sequence[str], *, timeout: float, cwd: Union[str, Path, None] = None,
                env: Optional[Mapping[str, str]] = None, input: Optional[str] = None,
                ) -> subprocess.CompletedProcess:
    """``subprocess.run(cmd, capture_output=True, text=True)`` whose timeout ends the whole tree.

    Text is UTF-8 with replacement. stdin is ``input`` when given, else closed
    (DEVNULL: nothing waits for a terminal). Raises ``subprocess.TimeoutExpired``
    after the tree is gone, and ``OSError`` when the program cannot start.
    """
    proc = subprocess.Popen(
        list(cmd), cwd=cwd, env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    try:
        stdout, stderr = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid, process_start(proc.pid))
        with contextlib.suppress(subprocess.TimeoutExpired):
            # A grandchild that left the tree before the kill may still hold a pipe.
            proc.communicate(timeout=5)
        raise subprocess.TimeoutExpired(list(cmd), timeout) from None
    return subprocess.CompletedProcess(list(cmd), proc.returncode, stdout, stderr)
