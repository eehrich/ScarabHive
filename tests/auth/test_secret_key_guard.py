"""The API does not start with a JWT signing key others could sign tokens with.

An unset ${AUTH_SECRET_KEY} in the config expands to "" and used to sign every
token with an empty key; a short key is guessable. Both stop the start. A
published key -- the AuthConfig default, the development key the repository's
config/config.yaml ships -- lets anyone who read the repository sign an admin
token: that logs an error, and auth.reject_default_secret_key: true makes it a
startup error too. The shipped key stays a warning so that an existing
installation still starts after its next restart.
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

import pytest
import yaml

from agent_system.auth.security import WeakSecretKeyError, check_secret_key
from agent_system.config.models import AuthConfig

SHIPPED_CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _shipped_key() -> str:
    return yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8"))["auth"]["secret_key"]


@pytest.mark.parametrize("key", ["", "   ", "short-key-of-31-characters-xxxx"])
def test_an_empty_or_short_key_is_refused(key):
    with pytest.raises(WeakSecretKeyError):
        check_secret_key(key)


@pytest.mark.parametrize("key", [AuthConfig().secret_key, _shipped_key()], ids=["model default", "shipped config"])
def test_a_published_key_warns_and_is_refused_when_asked(key, caplog):
    with caplog.at_level(logging.ERROR, logger="agent_system.auth.security"):
        check_secret_key(key)
    assert "published default" in caplog.text
    with pytest.raises(WeakSecretKeyError):
        check_secret_key(key, reject_public=True)


def test_a_random_key_passes_without_a_word(caplog):
    with caplog.at_level(logging.WARNING, logger="agent_system.auth.security"):
        check_secret_key(secrets.token_hex(32), reject_public=True)
    assert caplog.text == ""


def _auth_config(tmp_path, auth_lines: str) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        'name: "probe-hive"\nnetwork:\n  ssl_verify: true\nlogging:\n  enabled: false\n'
        f"auth:\n  enabled: true\n  database_path: {tmp_path / 'users.db'}\n{auth_lines}",
        encoding="utf-8")
    return config


def test_the_api_does_not_start_on_an_unset_key_variable(monkeypatch, tmp_path):
    from agent_system import app as app_mod

    monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(WeakSecretKeyError, match="empty"):
        app_mod.build_app(str(_auth_config(tmp_path, '  secret_key: "${AUTH_SECRET_KEY}"\n')))


def test_the_api_refuses_the_default_key_only_when_the_config_asks(monkeypatch, tmp_path):
    from agent_system import app as app_mod

    monkeypatch.chdir(tmp_path)
    app_mod.build_app(str(_auth_config(tmp_path, "")))  # the AuthConfig default: an error in the log, and it starts
    with pytest.raises(WeakSecretKeyError, match="published default"):
        app_mod.build_app(str(_auth_config(tmp_path, "  reject_default_secret_key: true\n")))


def test_the_key_is_checked_before_any_tool_server_starts(monkeypatch, tmp_path):
    # A service manager restarts a server that exits: a refused key must stop it before the
    # whole bootstrap, or every restart builds all tool servers again first.
    from agent_system import app as app_mod
    from agent_system.services import initialization_service

    def bootstrap(self, *args, **kwargs):
        raise AssertionError("the tool servers started before the key was checked")

    monkeypatch.setattr(initialization_service.InitializationService, "bootstrap_and_inject", bootstrap)
    monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(WeakSecretKeyError):
        app_mod.build_app(str(_auth_config(tmp_path, '  secret_key: "${AUTH_SECRET_KEY}"\n')))


def test_a_published_key_is_reported_in_the_api_log(monkeypatch, tmp_path):
    # The one warning that anyone can sign an admin token must reach the log file, not only a
    # console that nobody captures.
    from agent_system import app as app_mod

    monkeypatch.chdir(tmp_path)
    config = _auth_config(tmp_path, "")
    config.write_text(config.read_text(encoding="utf-8").replace(
        "logging:\n  enabled: false\n",
        "logging:\n  enabled: true\n  level: INFO\n  file_api: logs/api-probe.log\n  rotation_enabled: false\n"),
        encoding="utf-8")
    app_mod.build_app(str(config))
    assert "published default" in (tmp_path / "logs" / "api-probe.log").read_text(encoding="utf-8")
