"""agent-api loads the config AGENT_CONFIG_PATH names, as agent-cli and agent-run do,
logs where its config says, relative to the working directory, and listens where it
says (config/local.yaml over the shipped 127.0.0.1; the VS Code tasks leave it to it).

run() and build_app() used to take <source>/config/config.yaml whatever the
variable said, and /health read that file a second time on its own -- so a
container or a service unit that named its config got the repository's. The
early log (before the config is loaded) went to <source>/logs/api.log, while
the configured one goes to logs/api.log in the working directory: a server or
test started elsewhere wrote into the source tree.
"""
from __future__ import annotations

import json
import os
import runpy
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_system import app as app_mod
from agent_system import own_console


def _config(tmp_path, extra=""):
    config = tmp_path / "config.yaml"
    # ssl_verify: true -- the model's default (false) makes build_app write
    # PYTHONHTTPSVERIFY and friends into os.environ, where nothing restores them
    config.write_text('name: "probe-hive"\nversion: "9.9.9"\nnetwork:\n  ssl_verify: true\n' + extra,
                      encoding="utf-8")
    return config


def test_the_api_runs_on_the_config_the_variable_names(monkeypatch, tmp_path):
    config = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(config))
    # run() sets these with setdefault; set here, monkeypatch restores them afterwards
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    served = []
    monkeypatch.setattr(app_mod.uvicorn, "run", lambda app, **kwargs: served.append(app))
    # as serve() leaves it: on Windows run() would start a real server on a console of its own first
    monkeypatch.setattr(own_console, "_serving", True)

    app_mod.run()

    [app] = served
    assert app.state.config_path == str(config)
    health = TestClient(app).get("/health").json()
    assert (health["name"], health["version"]) == ("probe-hive", "9.9.9")


def _start(monkeypatch, tmp_path, config):
    """What run() meets at a start, up to uvicorn.run: the call it makes is recorded, not served."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(config))
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    served = []
    monkeypatch.setattr(app_mod.uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.setattr(own_console, "_serving", True)
    return served


@pytest.mark.parametrize("local, host", [("network:\n  host: 192.0.2.10\n", "192.0.2.10"), (None, "127.0.0.1")])
def test_the_api_listens_where_the_config_says_and_this_machine_s_layer_wins(monkeypatch, tmp_path, local, host):
    # A machine opens the API to its network in config/local.yaml, never in a shipped file:
    # the shipped config says 127.0.0.1, and the VS Code tasks name no host of their own.
    config = _config(tmp_path, "  host: 127.0.0.1\n  port: 8123\n")
    if local:
        (tmp_path / "local.yaml").write_text(local, encoding="utf-8")
    served = _start(monkeypatch, tmp_path, config)

    app_mod.run()

    [kwargs] = served
    assert (kwargs["host"], kwargs["port"]) == (host, 8123)
    assert kwargs["timeout_graceful_shutdown"] > 0, "an open stream would hold up every stop"


def test_python_m_agent_system_app_serves_from_the_module_the_code_imports(monkeypatch, tmp_path):
    # Run as __main__, app.py is a second copy of itself: its globals (_session_service, ...) are not the ones
    # the lazy `from agent_system.app import ...` read, and a session lookup answered 503.
    _start(monkeypatch, tmp_path, _config(tmp_path))
    started = []
    monkeypatch.setattr(app_mod, "run", lambda: started.append(True))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # runpy: agent_system.app is imported already
        runpy.run_module("agent_system.app", run_name="__main__")

    assert started == [True]


def test_the_vs_code_api_tasks_leave_host_and_port_to_the_config():
    path = Path(__file__).parents[2] / ".vscode" / "tasks.json"
    if not path.is_file():
        pytest.skip("no .vscode/tasks.json in this checkout (the public export leaves it out)")
    api = [task for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]
           if task["label"].startswith("AgentSystem: Run API")]
    assert len(api) == 2, "the API tasks were renamed: this test looks at none"
    for task in api:
        # through app.run, which reads network.host/port; uvicorn's own CLI binds 127.0.0.1 whatever they say
        assert "agent_system.app:run" in task["args"], task["label"]
        assert not {"--host", "--port"} & set(task["args"]), task["label"]


def test_the_early_log_is_written_in_the_working_directory(monkeypatch, tmp_path):
    config = _config(tmp_path)
    monkeypatch.chdir(tmp_path)

    app_mod.build_app(str(config))

    assert (tmp_path / "logs" / "api.log").is_file()


def test_a_working_directory_it_cannot_write_to_does_not_stop_the_start(monkeypatch, tmp_path):
    # A service unit without WorkingDirectory, logging switched off: the early log
    # must not demand a logs/ directory the configured logging never asks for.
    config = _config(tmp_path, "logging:\n  enabled: false\n")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    if os.access(locked, os.W_OK):  # root, or Windows: the mode does not lock the directory
        locked.chmod(0o755)
        pytest.skip("this user can write into a 0o555 directory")
    monkeypatch.chdir(locked)
    try:
        app_mod.build_app(str(config))
    finally:
        locked.chmod(0o755)
    assert not (locked / "logs").exists()


def test_the_old_names_of_the_shared_services_are_read_only():
    """agent_system.app still answers a read of its former globals (code outside this repository
    reads them there); a write would reach no reader -- they all read app_state -- and leave an
    attribute that shadows the read for the rest of the process, so it fails loudly."""
    import pytest

    from agent_system import app_state

    assert app_mod._session_service is app_state.session_service
    with pytest.raises(AttributeError, match="app_state.session_service"):
        app_mod._session_service = object()
    with pytest.raises(AttributeError, match="app_state.app_registry"):
        del app_mod._app_registry
    app_mod.some_other_attribute = 1   # everything else is an ordinary module attribute
    del app_mod.some_other_attribute
