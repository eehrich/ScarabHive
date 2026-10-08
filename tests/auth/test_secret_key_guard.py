"""The API does not start with a JWT signing key others could sign tokens with.

An unset ${AUTH_SECRET_KEY} in the config expands to "" and used to sign every
token with an empty key; a short key is guessable. Both stop the start. A
published key -- the AuthConfig default, the development key the repository's
config/config.yaml ships -- lets anyone who read the repository sign an admin
token: that logs an error, and auth.reject_default_secret_key: true makes it a
startup error too. The shipped key stays a warning so that an existing
installation on loopback still starts after its next restart; a server that
listens beyond loopback refuses it, and the AuthConfig default, which an auth
section without a key gets, stops the start in every case.
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

import pytest
import yaml

from agent_system.auth.security import PUBLISHED_SIGNING_KEYS, MIN_SECRET_KEY_LENGTH, WeakSecretKeyError, check_secret_key
from agent_system.config.models import AuthConfig

SHIPPED_CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


@pytest.fixture(autouse=True)
def _on_loopback(monkeypatch):
    # HOST in this shell would move every start below beyond loopback
    monkeypatch.delenv("HOST", raising=False)


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


@pytest.mark.parametrize("key", [key for key in PUBLISHED_SIGNING_KEYS if len(key) >= MIN_SECRET_KEY_LENGTH])
def test_every_key_the_repository_printed_is_known_to_the_guard(key, caplog):
    # The guard knew five of them; nine more of 32 characters or more started without a word,
    # even with reject_default_secret_key.
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


def _shipped_key_line() -> str:
    return f'  secret_key: "{_shipped_key()}"\n'


def test_the_api_refuses_the_shipped_key_only_when_the_config_asks(monkeypatch, tmp_path):
    from agent_system import app as app_mod

    monkeypatch.chdir(tmp_path)
    app_mod.build_app(str(_auth_config(tmp_path, _shipped_key_line())))  # an error in the log, and it starts
    with pytest.raises(WeakSecretKeyError, match="published default"):
        app_mod.build_app(str(_auth_config(tmp_path, _shipped_key_line() + "  reject_default_secret_key: true\n")))


@pytest.mark.parametrize("how", ["HOST", "network.host"])
def test_a_server_beyond_loopback_refuses_the_shipped_key(monkeypatch, tmp_path, how):
    # Beyond loopback anyone the server answers could sign an admin token with it.
    from agent_system import app as app_mod

    monkeypatch.chdir(tmp_path)
    config = _auth_config(tmp_path, _shipped_key_line())
    if how == "HOST":
        monkeypatch.setenv("HOST", "0.0.0.0")
    else:
        monkeypatch.delenv("HOST", raising=False)
        config.write_text(config.read_text(encoding="utf-8").replace(
            "network:\n", "network:\n  host: 0.0.0.0\n"), encoding="utf-8")
    with pytest.raises(WeakSecretKeyError, match="published default"):
        app_mod.build_app(str(config))


@pytest.mark.parametrize("host, loopback", [("127.0.0.1", True), ("127.0.0.2", True), ("::1", True),
                                            ("[::1]", True), ("localhost", True), ("0.0.0.0", False),
                                            ("::", False), ("192.168.1.10", False), ("myhost.lan", False)])
def test_what_counts_as_loopback(host, loopback):
    from agent_system.app import _is_loopback_host

    assert _is_loopback_host(host) is loopback


def test_an_auth_section_without_a_key_does_not_start(monkeypatch, tmp_path):
    # Without the line the AuthConfig default applies, and it stands in this repository.
    from agent_system import app as app_mod

    monkeypatch.chdir(tmp_path)
    with pytest.raises(WeakSecretKeyError, match="published default"):
        app_mod.build_app(str(_auth_config(tmp_path, "")))


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
    config = _auth_config(tmp_path, _shipped_key_line())
    config.write_text(config.read_text(encoding="utf-8").replace(
        "logging:\n  enabled: false\n",
        "logging:\n  enabled: true\n  level: INFO\n  file_api: logs/api-probe.log\n  rotation_enabled: false\n"),
        encoding="utf-8")
    app_mod.build_app(str(config))
    assert "published default" in (tmp_path / "logs" / "api-probe.log").read_text(encoding="utf-8")
