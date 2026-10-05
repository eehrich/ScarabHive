"""resolve_api_key: which secret may go to which host.

Every key here is a fake value set with monkeypatch; nothing is sent anywhere.
"""
from __future__ import annotations

import pytest

from plugins.llm_common import api_keys
from plugins.llm_common.api_keys import key_var_for, resolve_api_key

FAKE = {"OPENAI_API_KEY": "fake-openai", "OPENROUTER_API_KEY": "fake-openrouter",
        "TYPESAFE_API_KEY": "fake-typesafe"}


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    for name, value in FAKE.items():
        monkeypatch.setenv(name, value)


def resolve(url, api_key=None, **kw):
    return resolve_api_key(api_key, url, default_base_url="https://openrouter.ai/api/v1",
                           provider="test", **kw)


@pytest.mark.parametrize("url, var", [
    ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    ("https://API.OpenAI.com/v1", "OPENAI_API_KEY"),
    ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ("https://eu.openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ("https://api.typesafe.ai/v1", "TYPESAFE_API_KEY"),
    ("http://localhost:1234/v1", "OPENAI_API_KEY"),
    ("http://ollama:11434/v1", "OPENAI_API_KEY"),
    ("http://192.168.1.20:8000/v1", "OPENAI_API_KEY"),
    ("http://[::1]:8000/v1", "OPENAI_API_KEY"),
    ("http://[fd00::5]:8000/v1", "OPENAI_API_KEY"),
    ("http://[::ffff:10.0.0.1]/v1", "OPENAI_API_KEY"),
    ("http://box.local/v1", "OPENAI_API_KEY"),
])
def test_known_and_local_hosts_get_their_variable(url, var):
    assert key_var_for(url) == var


@pytest.mark.parametrize("url", [
    "https://api.openai.com.evil.example/v1",   # the known host as a prefix
    "https://evilapi.openai.com/v1",            # the known host as a suffix without a dot
    "https://api.openai.com@evil.example/v1",   # userinfo, the real host follows the @
    "https://gw.example.com/openrouter.ai/v1",  # the known host only in the path
    "http://100.64.1.1/v1",                     # CGNAT is a carrier's, not ours
    "http://[::ffff:8.8.8.8]/v1",               # v4-mapped public address
    # Integer and hex spellings: the resolver reads both as 8.8.8.8, and the
    # name has no dot.
    "http://134744072/v1",
    "http://0x08080808/v1",
    # 6to4 and Teredo: Python calls them private, they route to public v4.
    "http://[2002:808:808::1]/v1",
    "http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/v1",
])
def test_no_env_key_for_a_foreign_host(url):
    assert key_var_for(url) is None
    with pytest.raises(ValueError, match="set `api_key:`"):
        resolve(url)


def test_explicit_key_wins_and_is_trimmed():
    assert resolve("https://gw.example.com/v1", api_key=" fake-explicit\n") == (
        "fake-explicit", "https://gw.example.com/v1")


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_explicit_key_falls_back_to_the_host_variable(blank):
    assert resolve("https://openrouter.ai/api/v1", api_key=blank)[0] == "fake-openrouter"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_env_key_is_missing(monkeypatch, blank):
    monkeypatch.setenv("OPENROUTER_API_KEY", blank)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY is required"):
        resolve(None)


def test_env_key_is_trimmed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter\n")
    assert resolve(None)[0] == "fake-openrouter"


def test_local_endpoint_of_another_wire_gets_no_key():
    assert resolve("http://laya:8000/v1", local_fallback=False) == ("", "http://laya:8000/v1")


def test_errors_never_carry_a_url_password(monkeypatch):
    with pytest.raises(ValueError) as foreign:
        resolve("https://user:fake-urlpass@gw.example.com/v1")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(ValueError) as missing:
        resolve("https://user:fake-urlpass@openrouter.ai/api/v1")
    for err in (foreign, missing):
        text = str(err.value)
        assert "fake-urlpass" not in text
        assert "user" not in text.split("//", 1)[1].split("/", 1)[0]


def test_errors_never_carry_a_key(monkeypatch):
    # Only the explicit-key and env paths know a key; neither may reach a message.
    monkeypatch.setenv("OPENAI_API_KEY", "fake-leak-me")
    with pytest.raises(ValueError) as err:
        resolve("https://gw.example.com/v1")
    assert "fake-leak-me" not in str(err.value)
    assert api_keys._LOCAL_KEY_VAR in str(err.value)
