"""agent-api loads the config AGENT_CONFIG_PATH names, as agent-cli and agent-run do,
and logs where its config says, relative to the working directory.

run() and build_app() used to take <source>/config/config.yaml whatever the
variable said, and /health read that file a second time on its own -- so a
container or a service unit that named its config got the repository's. The
early log (before the config is loaded) went to <source>/logs/api.log, while
the configured one goes to logs/api.log in the working directory: a server or
test started elsewhere wrote into the source tree.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from agent_system import app as app_mod


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

    app_mod.run()

    [app] = served
    assert app.state.config_path == str(config)
    health = TestClient(app).get("/health").json()
    assert (health["name"], health["version"]) == ("probe-hive", "9.9.9")


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
