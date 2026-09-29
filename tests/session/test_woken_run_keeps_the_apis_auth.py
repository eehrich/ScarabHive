"""A run the API wakes judges its user as the API does.

The woken run is a process of its own (`agent-cli run --woken`) and loads its config from disk. With auth.enabled
switched off there while the API still enforced what it started with (until a restart), every user it acted for was
an owner to its plugins (stategraph, setup) -- found by agentsystem-c3.
"""
import os
import subprocess
import sys

import pytest

from agent_system.auth import security
from agent_system.config import settings
from agent_system.config.settings import AUTH_REQUIRED_ENV, load_settings
from agent_system.core import session_presence as sp

# The root conftest puts a refusal in its place while a test runs; taken at collection, before it does. Nothing
# starts here all the same: Popen is replaced below.
SPAWN_WAKE = sp.spawn_wake
assert SPAWN_WAKE.__name__ == "spawn_wake", "collected after the guard went in: this would test the guard"


@pytest.fixture
def config_on_disk(tmp_path):
    def write(enabled: bool) -> str:
        path = tmp_path / "config.yaml"
        path.write_text(f"auth:\n  enabled: {str(enabled).lower()}\n  secret_key: \"{'k' * 40}\"\n", encoding="utf-8")
        return str(path)
    return write


@pytest.mark.parametrize("disk, woken_under_auth, expected", [
    (False, True, True),    # the API that woke it enforces auth: the run too, whatever the disk says by now
    (False, False, False),  # not woken, or woken by a process without auth: the disk decides
    (True, False, True),    # tighten only: nothing turns auth off
    (True, True, True),
])
def test_a_run_loads_auth_on_when_the_api_that_woke_it_enforces_it(config_on_disk, monkeypatch, disk,
                                                                   woken_under_auth, expected):
    monkeypatch.setattr(settings, "AUTH_REQUIRED_BY_WAKER", woken_under_auth)

    assert load_settings(config_on_disk(disk)).auth.enabled is expected


# What a process started with the variable makes of it: whether it counts as woken under auth, its config, and
# what a process it starts in turn -- a terminal command, a test run -- finds in its environment.
PROBE = f"""
import subprocess, sys
from agent_system.config import settings
child = subprocess.run([sys.executable, "-c", "import os; print(os.environ.get({AUTH_REQUIRED_ENV!r}))"],
                       capture_output=True, text=True, check=True).stdout.strip()
print(settings.AUTH_REQUIRED_BY_WAKER, settings.load_settings(sys.argv[1]).auth.enabled, child)
"""


@pytest.mark.parametrize("value, expected", [
    ("1", "True True None"),    # the run judges with auth on; what it starts otherwise does not inherit that
    ("0", "False False None"),  # tighten only
])
def test_the_run_takes_the_variable_out_of_its_environment(config_on_disk, value, expected):
    env = {**os.environ, AUTH_REQUIRED_ENV: value}

    probe = subprocess.run([sys.executable, "-c", PROBE, config_on_disk(False)], env=env,
                           capture_output=True, text=True, timeout=60)

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip().splitlines()[-1] == expected


def _spawned_env(monkeypatch) -> dict:
    """The environment spawn_wake starts its run with (the process itself is not started)."""
    seen = {}

    class Started:
        pid = os.getpid()

    def popen(command, **kwargs):
        seen.update(kwargs["env"])
        return Started()

    monkeypatch.setattr(sp.subprocess, "Popen", popen)
    monkeypatch.setattr(sp, "_wake_log", lambda: subprocess.DEVNULL)
    SPAWN_WAKE("s-1", "ada", 1)
    return seen


def test_the_api_that_enforces_auth_tells_the_run_it_wakes(monkeypatch):
    for name in ("SECRET_KEY", "ALGORITHM", "ACCESS_TOKEN_EXPIRE_MINUTES", "REFRESH_TOKEN_EXPIRE_DAYS"):
        monkeypatch.setattr(security, name, getattr(security, name))  # restored after the test
    monkeypatch.setattr(security, "AUTH_ENFORCED", False)
    monkeypatch.setattr(settings, "AUTH_REQUIRED_BY_WAKER", False)

    security.set_jwt_config(secret_key="k" * 40)  # what the API does at start with auth on

    assert _spawned_env(monkeypatch)[AUTH_REQUIRED_ENV] == "1"


def test_a_process_without_auth_tells_nothing_and_a_woken_run_passes_it_on(monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENFORCED", False)
    monkeypatch.setattr(settings, "AUTH_REQUIRED_BY_WAKER", False)
    assert AUTH_REQUIRED_ENV not in _spawned_env(monkeypatch)

    monkeypatch.setattr(settings, "AUTH_REQUIRED_BY_WAKER", True)  # this process is a run such an API woke

    assert _spawned_env(monkeypatch)[AUTH_REQUIRED_ENV] == "1"
