"""The API stops cleanly on SIGINT while a client holds a connection open.

Starts the real app (`python -m agent_system.app`) as a subprocess on a free
port. It used to start it on the configured port (8000) and talk to
127.0.0.1:8000 -- with the developer's API running there, the test talked to
THAT server. Every request here goes to the child's own port, and the test
checks that the connection it holds is one the child accepted.

The held connection is a request whose headers never finish, not an SSE
stream: every SSE endpoint of the API runs an agent, and a test must not
start real LLM runs.

The app loads the real config/*.yaml whatever its working directory, but
opens the data and log paths in it (data/users.db, data/writer/books.db,
data/stategraph/runs.db -- whose startup sweep UPDATEs runs --, logs/api.log
at DEBUG, ...) relative to that directory. The child therefore runs in a
temporary one; only the few lines logged before the configured logging takes
over still reach the repo's logs/api.log. Every key the config expands
(${..._API_KEY}) is set empty, so the configured LLM clients get none --
config/secrets.env still fills in the names the config does not expand.
"""

import http.client
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTUP_TIMEOUT = 60.0  # measured: ~3 s; bounded well inside pytest.ini's 120 s
SHUTDOWN_TIMEOUT = 30.0
STOPPED_MARKER = "API Server STOPPED"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _health_status(port: int) -> int | None:
    """Status of GET /health on the port, None while nothing answers there."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        return response.status
    except OSError:
        return None
    finally:
        connection.close()


def _tail(path: Path, lines: int = 40) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _wait_until_serving(server: subprocess.Popen, port: int, output: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if server.poll() is not None:
            pytest.fail(f"the API exited with {server.returncode} before it served "
                        f"port {port}:\n{_tail(output)}")
        if _health_status(port) == 200:
            return
        time.sleep(0.5)
    pytest.fail(f"the API did not answer /health on port {port} within "
                f"{STARTUP_TIMEOUT:.0f} s:\n{_tail(output)}")


def _accepted_by(server: subprocess.Popen, client: socket.socket) -> bool:
    """Whether the child holds the other end of this client's connection."""
    client_address = client.getsockname()[:2]
    return any(connection.raddr and tuple(connection.raddr)[:2] == client_address
               for connection in psutil.Process(server.pid).net_connections(kind="tcp"))


def _config_key_names() -> set[str]:
    """The ${VAR} names config/*.yaml expands -- the provider keys among them."""
    names = set()
    for path in (REPO_ROOT / "config").rglob("*.yaml"):
        names.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)",
                                path.read_text(encoding="utf-8", errors="replace")))
    return {name for name in names if name.endswith(("_KEY", "_TOKEN", "_SECRET"))}


def _server_env(port: int, workdir: Path) -> dict[str, str]:
    env = dict(os.environ)  # the root conftest's test-session marker included
    # Set but empty: config/secrets.env fills only names the environment lacks.
    env.update({name: "" for name in _config_key_names()})
    # PYTEST_CURRENT_TEST under a cwd containing "test" turns a plugin that fails
    # to build into a crash (runtime._in_test_cwd); the child is the app, not a test.
    env.pop("PYTEST_CURRENT_TEST", None)
    env.update(PORT=str(port), HOST="127.0.0.1",  # honoured by agent_system.app.run()
               AGENT_SESSION_STORAGE_PATH=str(workdir / "sessions"),
               # coding_cli's runs live under the project, not the cwd: its
               # sweep would ring the wakes armed for the developer's API.
               CODING_CLI_DATA_ROOT=str(workdir / "coding_cli"))
    return env


def run_shutdown_check(scratch: Path) -> None:
    port = _free_port()
    workdir = scratch / "cwd"
    workdir.mkdir()
    output = scratch / "api_output.log"
    assert _config_key_names(), "no key names found in config/ -- nothing would be blanked"
    with open(output, "wb") as sink:
        server = subprocess.Popen([sys.executable, "-m", "agent_system.app"],
                                  cwd=workdir, env=_server_env(port, workdir),
                                  stdout=sink, stderr=subprocess.STDOUT)
    client = None
    try:
        _wait_until_serving(server, port, output)

        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # no blank line: in flight
        deadline = time.monotonic() + 5
        while not _accepted_by(server, client):
            assert time.monotonic() < deadline, \
                f"the held connection is not one the API on port {port} accepted"
            time.sleep(0.1)

        server.send_signal(signal.SIGINT)
        try:
            returncode = server.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            returncode = None
        if returncode is None:
            pytest.fail(f"the API did not stop within {SHUTDOWN_TIMEOUT:.0f} s of SIGINT "
                        f"with a client connected:\n{_tail(output)}")

        # uvicorn shuts down, re-raises the captured SIGINT and uvicorn.run()
        # swallows the KeyboardInterrupt: a clean stop exits 0. The marker is
        # logged once the lifespan shutdown (SSE streams, batch manager, tool
        # servers) has run through without error.
        assert returncode == 0, f"the API ended with {returncode}:\n{_tail(output)}"
        assert STOPPED_MARKER in output.read_text(encoding="utf-8", errors="replace"), \
            f"the lifespan shutdown did not finish:\n{_tail(output)}"
    finally:
        if client is not None:
            client.close()
        if server.poll() is None:
            server.kill()
            server.wait(timeout=10)


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="POSIX only: SIGINT cannot be sent to a child process on Windows")
def test_the_api_stops_cleanly_on_sigint_with_a_client_connected(tmp_path):
    run_shutdown_check(tmp_path)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        run_shutdown_check(Path(scratch))
    print("Server shutdown test PASSED")
