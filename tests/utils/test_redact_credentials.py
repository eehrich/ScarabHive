"""Credentials stay out of what is shown or logged: the config an admin reads, the URL httpx logs, a tool's parameters.

httpx logs every request at INFO with its whole URL, and the Gemini REST client put its key into the query; the
agent logs every tool call's parameters at INFO, an SSH password among them; GET /admin/config returned the
signing key and every provider key in clear.
"""
import logging

import pytest

from agent_system.utils.logging import KeyInPathFilter, loggable_path, setup_logging
from agent_system.utils.redact import MASK, is_secret_name, mask_url_credentials, redact_secrets

KEY = "AIza" + "Sy-only-its-holder-may-use-0123456789"  # split: the export masks the whole literal


@pytest.mark.parametrize("url, shown", [
    (f"https://g.example/v1beta/models/m:generateContent?key={KEY}", "https://g.example/v1beta/models/m:generateContent?key=***"),
    (f"https://g.example/m:streamGenerateContent?key={KEY}&alt=sse", "https://g.example/m:streamGenerateContent?key=***&alt=sse"),
    (f"https://mcp.example/mcp?profile=a&api_key={KEY}", "https://mcp.example/mcp?profile=a&api_key=***"),
    (f"https://x.example/?Access_Token={KEY}#top", "https://x.example/?Access_Token=***#top"),
    ("https://x.example/search?q=monkey=1", "https://x.example/search?q=monkey=1"),  # no parameter called key
    ("sorted(items, key=len)", "sorted(items, key=len)"),  # no query
    ("redis://:s3cret-pw@cache:6379/0", "redis://:***@cache:6379/0"),
    ("postgres://app:s3cret-pw@db.example/books", "postgres://app:***@db.example/books"),
    ("ssh://git@forge.example:22/repo.git", "ssh://git@forge.example:22/repo.git"),  # a user, no password
    ("http://[::1]:8000/x?y=a@b", "http://[::1]:8000/x?y=a@b"),  # a port, no userinfo
])
def test_a_credential_in_a_url_is_masked(url, shown):
    assert mask_url_credentials(url) == shown
    assert loggable_path(url) == shown


def test_the_url_httpx_logs_keeps_no_key(tmp_path):
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    path = tmp_path / "api.log"
    setup_logging(enabled=True, level="INFO", file_path=str(path), rotation_enabled=False)
    try:
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s %d %s"', "POST",
                                        f"https://g.example/m:generateContent?key={KEY}", "HTTP/1.1", 200, "OK")
        for handler in root.handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            if handler not in before:
                handler.close()
        for handler in before:
            root.addHandler(handler)
        root.setLevel(level)
    assert "generateContent?key=*** " in text, text
    assert KEY not in text


@pytest.mark.parametrize("name", ["password", "api_key", "secret_key", "token", "access_token", "Authorization",
                                  "X-API-Key", "client_secret", "passphrase", "private_key"])
def test_a_credentials_name_is_known(name):
    assert is_secret_name(name)


@pytest.mark.parametrize("name", ["token_env", "api_key_env", "max_tokens", "username", "host", "key_path", "keys"])
def test_other_names_are_not(name):
    assert not is_secret_name(name)


def test_tool_parameters_lose_their_credentials_and_keep_everything_else():
    class Token:  # a cancellation token among the parameters: the very object, untouched
        pass

    token = Token()
    params = {"name": "db1", "host": "192.0.2.10", "username": "root", "auth_method": "password",
              "password": "Pr0d-R00t-Passw0rd!", "options": [{"api_key": KEY, "port": 22}],
              "url": f"https://x.example/?token={KEY}", "empty_password": "", "_cancellation": token}

    shown = redact_secrets(params)

    assert shown["password"] == MASK and shown["options"][0] == {"api_key": MASK, "port": 22}
    assert shown["url"] == "https://x.example/?token=***"
    assert shown["empty_password"] == ""  # nothing set: shown as nothing, not as a masked value
    assert shown["_cancellation"] is token and shown["host"] == "192.0.2.10"
    assert params["password"] == "Pr0d-R00t-Passw0rd!"  # a copy: the call still gets its arguments
    assert KEY not in str(shown)


@pytest.mark.parametrize("url", [f"https://x.example/?api_key={KEY}", f"redis://:{KEY}@cache:6379/0"])
def test_the_filter_masks_a_tool_call_logged_with_a_url_credential(url):
    record = logging.LogRecord("agent_system", logging.INFO, __file__, 1, "Invoking tool %s with params %s",
                               ("fetch", {"url": url}), None)
    KeyInPathFilter().filter(record)
    assert KEY not in record.getMessage()


def test_a_connection_url_in_the_config_loses_its_password():
    shown = redact_secrets({"redis_url": f"redis://:{KEY}@cache:6379/0"})
    assert shown == {"redis_url": "redis://:***@cache:6379/0"}


def test_the_admin_config_shows_no_credential(monkeypatch, tmp_path):
    import httpx
    import anyio
    from agent_system import app as app_mod
    from agent_system.auth import security

    signing_key = "an-own-signing-key-of-this-test-0123456789"
    monkeypatch.setenv("REDACT_TEST_PROVIDER_KEY", KEY)
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        'name: "probe-hive"\nlogging:\n  enabled: false\n'
        f'auth:\n  enabled: false\n  secret_key: "{signing_key}"\n  database_path: {tmp_path / "users.db"}\n'
        "llm_system:\n  default_profile: probe\n  models:\n    probe-model:\n      provider: openai\n"
        '      model: gpt-x\n      api_key: "${REDACT_TEST_PROVIDER_KEY}"\n'
        "  profiles:\n    probe:\n      model_ref: probe-model\n",
        encoding="utf-8")
    monkeypatch.setattr(security, "SECRET_KEY", signing_key)
    app = app_mod.build_app(str(config))

    async def dump():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/admin/config", timeout=60.0)

    answer = anyio.run(dump)

    assert answer.status_code == 200, answer.text
    assert KEY not in answer.text and signing_key not in answer.text
    assert answer.json()["auth"]["secret_key"] == MASK
