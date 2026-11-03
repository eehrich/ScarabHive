"""run_bounded: a timeout ends the whole process tree, not the direct child only."""
import subprocess
import sys
import time

import psutil
import pytest

from agent_system.utils.proctree import run_bounded

CHILD_THAT_STARTS_A_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(60)\n")


def test_a_timeout_kills_the_grandchild_too(tmp_path):
    out = tmp_path / "pid"
    script = CHILD_THAT_STARTS_A_GRANDCHILD.replace(
        "print(g.pid, flush=True)", f"open({str(out)!r}, 'w').write(str(g.pid))")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, "-c", script], timeout=2)
    assert time.monotonic() - started < 30
    grandchild = int(out.read_text())
    assert not psutil.pid_exists(grandchild) or \
        psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE


def test_output_and_input():
    done = run_bounded([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                       timeout=30, input="abc")
    assert done.returncode == 0 and done.stdout.strip() == "ABC"
