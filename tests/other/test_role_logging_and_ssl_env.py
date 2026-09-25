"""Process start shared by the API and the command line: the role's log file
and network.ssl_verify for the environment -- each was written out twice."""
from types import SimpleNamespace

import pytest

from agent_system.services.initialization_service import apply_ssl_verify_to_environment
from agent_system.utils import logging as logging_utils

SSL_ENV = ("PYTHONHTTPSVERIFY", "SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE")


def _logging_config(**overrides):
    values = dict(enabled=True, level="DEBUG", file="logs/agent.log", file_api=None, file_cli=None,
                  rotation_enabled=False, max_bytes=123, backup_count=4)
    return SimpleNamespace(**{**values, **overrides})


@pytest.fixture
def opened(monkeypatch):
    calls = []
    monkeypatch.setattr(logging_utils, "setup_logging",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or args[2])
    return calls


@pytest.mark.parametrize("config, role, expected", [
    (_logging_config(), "cli", "logs/agent-cli.log"),
    (_logging_config(), "api", "logs/agent-api.log"),
    (_logging_config(file="var/log/hive.txt"), "api", "var/log/hive-api.txt"),
    (_logging_config(file=None), "cli", "logs/agent-cli.log"),
    (_logging_config(file_cli="own/cli.log"), "cli", "own/cli.log"),
    (_logging_config(file_api="own/api.log"), "cli", "logs/agent-cli.log"),
])
def test_each_role_gets_its_own_file(opened, config, role, expected):
    from pathlib import Path

    assert Path(logging_utils.setup_role_logging(config, role)) == Path(expected)
    (enabled, level, _path), kwargs = opened[0]
    assert (enabled, level) == (True, "DEBUG")
    assert kwargs == dict(rotation_enabled=False, max_bytes=123, backup_count=4), "a rotation setting was dropped"


def _clear_ssl_env(monkeypatch):
    # setenv first: delenv only records a variable that is set, and one the
    # code under test sets afterwards would leak into every later test.
    for name in SSL_ENV:
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)


def test_ssl_verify_false_reaches_the_environment_without_clobbering(monkeypatch):
    _clear_ssl_env(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/own-ca.pem")

    apply_ssl_verify_to_environment(SimpleNamespace(network=SimpleNamespace(ssl_verify=False)))

    import os
    assert os.environ["PYTHONHTTPSVERIFY"] == "0"
    assert os.environ["CURL_CA_BUNDLE"] == "" and os.environ["REQUESTS_CA_BUNDLE"] == ""
    assert os.environ["SSL_CERT_FILE"] == "/etc/own-ca.pem", "a set certificate file was overwritten"


def test_ssl_verify_true_leaves_the_environment_alone(monkeypatch):
    _clear_ssl_env(monkeypatch)

    apply_ssl_verify_to_environment(SimpleNamespace(network=SimpleNamespace(ssl_verify=True)))

    import os
    assert not any(name in os.environ for name in SSL_ENV)
