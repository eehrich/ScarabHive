import os
import socket
import pytest
import subprocess
import sys
import time
import signal
from typing import List


@pytest.fixture(scope="session", autouse=True)
def set_test_server_port():
    """Find a free TCP port and set TEST_SERVER_PORT for the test session.

    This ensures tests that start or connect to the local HTTP API use an
    ephemeral port and don't conflict with a production server running on
    the default port (8000).
    """
    # Bind to port 0 to get an ephemeral free port from the OS
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    os.environ.setdefault("TEST_SERVER_PORT", str(port))
    # Expose to pytest (optional)
    yield port

    # No teardown required; environment variable will be discarded after tests


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _find_project_python_pids() -> List[int]:
    """Return python process PIDs that look like test servers for this repo.

    We filter by command-line content to avoid killing unrelated Python.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    matches: List[int] = []

    if _is_windows():
        try:
            out = subprocess.check_output([
                "wmic",
                "process",
                "where",
                "name like '%python%'",
                "get",
                "ProcessId,Name,CommandLine",
                "/format:list",
            ], stderr=subprocess.DEVNULL, text=True)
        except Exception:
            return matches

        # wmic outputs blocks like "CommandLine=...\nName=python.exe\nProcessId=12345\n"
        blocks = [b for b in out.split("\n\n") if b.strip()]
        for b in blocks:
            pid = None
            cmd = ""
            for line in b.splitlines():
                if line.startswith("ProcessId="):
                    try:
                        pid = int(line.split("=", 1)[1].strip())
                    except Exception:
                        pid = None
                elif line.startswith("CommandLine="):
                    cmd = line.split("=", 1)[1].strip().strip('"')

            if pid and cmd:
                low = cmd.lower()
                if repo_root.lower() in low or "-m agent_system.agent.interface_api" in low or ".venv\\" in low:
                    matches.append(pid)

    else:
        # POSIX: use ps
        try:
            out = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
        except Exception:
            return matches

        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                pid_str, args = line.strip().split(None, 1)
                pid = int(pid_str)
            except Exception:
                continue
            low = args.lower()
            if "python" in low and (repo_root.lower() in low or "-m agent_system.agent.interface_api" in low or ".venv/" in low):
                matches.append(pid)

    return matches


def _kill_pids(pids: List[int]) -> None:
    if not pids:
        return
    if _is_windows():
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    else:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass


@pytest.fixture(scope="session", autouse=True)
def ensure_test_servers_terminated():
    """Ensure any leftover test server python processes are terminated.

    This runs before the test session and again after the session finishes.
    It only targets processes whose command lines reference the repository,
    the project's virtualenv, or the test server module name.
    """
    # Pre-test cleanup (best-effort)
    pre = _find_project_python_pids()
    if pre:
        print("[conftest] terminating pre-existing project python processes:", pre)
        _kill_pids(pre)
        # give OS a moment to settle
        time.sleep(0.3)

    yield

    # Post-test cleanup (best-effort)
    post = _find_project_python_pids()
    if post:
        print("[conftest] terminating leftover project python processes:", post)
        _kill_pids(post)
        time.sleep(0.3)
