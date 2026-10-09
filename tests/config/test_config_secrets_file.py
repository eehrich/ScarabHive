"""config/secrets.env as the loader reads it: whatever an editor or a shell wrote, a start never fails on it."""
import logging
import os

import pytest

from agent_system.config import environment


@pytest.fixture
def load(tmp_path, monkeypatch):
    """Load a secrets file of this test's own into a process of its own: what it takes goes with the test."""
    monkeypatch.setattr(environment, "_secrets_from_file", {})
    monkeypatch.setenv(environment.SECRETS_FROM_FILE_ENV, os.environ.get(environment.SECRETS_FROM_FILE_ENV, ""))
    for name in ("SECRETS_TEST_FIRST", "SECRETS_TEST_SECOND"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)

    def load(data: bytes) -> dict:
        path = tmp_path / "secrets.env"
        path.write_bytes(data)
        environment._load_secrets_file(path)
        return {name: os.environ.get(name) for name in ("SECRETS_TEST_FIRST", "SECRETS_TEST_SECOND")}
    return load


LINES = "SECRETS_TEST_FIRST=first-value\nSECRETS_TEST_SECOND=second-value\n"


@pytest.mark.parametrize("data", [LINES.encode("utf-8-sig"), LINES.encode("utf-16"),
                                  b"\xfe\xff" + LINES.encode("utf-16-be")],
                         ids=["utf-8-with-bom", "utf-16-powershell", "utf-16-big-endian"])
def test_every_name_is_read_whatever_the_file_starts_with(load, data):
    """A BOM made the first name `\\ufeffSECRETS_TEST_FIRST`; PowerShell 5.1's `>` writes UTF-16."""
    assert load(data) == {"SECRETS_TEST_FIRST": "first-value", "SECRETS_TEST_SECOND": "second-value"}


def test_a_line_that_is_no_text_is_left_out_alone_and_named(load, caplog):
    """Lines added in cp1252 (PowerShell 5.1's Add-Content): the loader promises never to raise, and one line must
    not take every key with it -- a signing key given by ${VAR} would be "" then. A comment is lost to nobody."""
    caplog.set_level(logging.WARNING, logger=environment.logger.name)

    data = "# Schlüssel\nSECRETS_TEST_FIRST=wert-mit-ü\nSECRETS_TEST_SECOND=second-value\n".encode("cp1252")

    assert load(data) == {"SECRETS_TEST_FIRST": None, "SECRETS_TEST_SECOND": "second-value"}
    assert "line 2: not UTF-8 text" in caplog.text and "line 1" not in caplog.text, caplog.text
    assert "wert" not in caplog.text


def test_a_utf16_file_cut_short_keeps_every_line_but_the_cut(load, caplog):
    """A byte appended in another encoding (Git Bash's `echo ... >>`): the lines before it load."""
    caplog.set_level(logging.WARNING, logger=environment.logger.name)

    data = LINES.encode("utf-16") + b"\n"

    assert load(data) == {"SECRETS_TEST_FIRST": "first-value", "SECRETS_TEST_SECOND": "second-value"}
    assert "not UTF-16 text" in caplog.text, caplog.text


def refusing(monkeypatch, refused: str, error: Exception):
    """The system's setenv refusing one variable: Windows past its length limit (ValueError), a setenv that fails
    for lack of memory (OSError)."""
    real = os.putenv

    def putenv(key, value):
        if refused in str(key).upper():
            raise error
        real(key, value)
    monkeypatch.setattr(os, "putenv", putenv)


@pytest.mark.parametrize("error", [ValueError("the environment variable is longer than 32767 characters"),
                                   OSError(12, "Cannot allocate memory")], ids=["too-long", "setenv-fails"])
def test_a_value_the_environment_refuses_is_left_out_and_the_rest_handed_on(load, caplog, monkeypatch, error):
    caplog.set_level(logging.WARNING, logger=environment.logger.name)
    refusing(monkeypatch, "SECRETS_TEST_FIRST", error)

    assert load(LINES.encode("utf-8")) == {"SECRETS_TEST_FIRST": None, "SECRETS_TEST_SECOND": "second-value"}
    assert "SECRETS_TEST_FIRST: not taken by the environment" in caplog.text, caplog.text
    assert "SECRETS_TEST_SECOND:" in os.environ[environment.SECRETS_FROM_FILE_ENV]


@pytest.mark.skipif(os.name != "nt", reason="only Windows limits a variable's length")
@pytest.mark.parametrize("value", ["x" * 40000, "x" * 32748 + chr(0x1F600)], ids=["long", "at-the-limit-in-characters"])
def test_the_prediction_leaves_out_what_a_start_cannot_take(tmp_path, monkeypatch, value):
    """A start leaves a value past Windows' limit out: the prediction must not expand with it. The limit counts
    UTF-16 units -- 32767 characters with one past U+FFFF are one too many."""
    monkeypatch.delenv("SECRETS_TEST_LONG", raising=False)
    with pytest.raises(ValueError):
        os.environ["SECRETS_TEST_LONG"] = value  # the fixture's premise: a start could not take it either
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    (tmp_path / "secrets.env").write_text(f"SECRETS_TEST_LONG={value}\nSECRETS_TEST_SHORT=kept\n", encoding="utf-8")

    env = environment.environment_at_restart(str(tmp_path / "config.yaml"))

    assert "SECRETS_TEST_LONG" not in env and env["SECRETS_TEST_SHORT"] == "kept"


def test_a_hand_on_the_environment_refuses_leaves_the_keys_loaded(load, caplog, monkeypatch):
    """Thousands of names outgrow a Windows variable: the keys are there, the processes this one starts not told."""
    caplog.set_level(logging.WARNING, logger=environment.logger.name)
    refusing(monkeypatch, environment.SECRETS_FROM_FILE_ENV, ValueError("longer than 32767 characters"))

    assert load(LINES.encode("utf-8")) == {"SECRETS_TEST_FIRST": "first-value", "SECRETS_TEST_SECOND": "second-value"}
    assert "are not told" in caplog.text, caplog.text


def test_a_line_appended_in_another_encoding_is_named_even_when_it_decodes(load, caplog):
    """ASCII after UTF-16 (Git Bash's `echo KEY=value >>`) pairs into other characters when its length is even:
    no decoding error, and no `=` either."""
    caplog.set_level(logging.WARNING, logger=environment.logger.name)

    assert load(LINES.encode("utf-16") + b"NEW_KEY=value12\n")["SECRETS_TEST_SECOND"] == "second-value"
    assert "line 3: no KEY=value" in caplog.text, caplog.text


def test_a_value_holding_a_replacement_character_is_kept_beside_a_broken_line(load):
    value = f"has-{chr(0xFFFD)}-inside"
    data = f"SECRETS_TEST_FIRST={value}\n".encode("utf-8") + "SECRETS_TEST_SECOND=wert-mit-ü\n".encode("cp1252")

    assert load(data) == {"SECRETS_TEST_FIRST": value, "SECRETS_TEST_SECOND": None}


def test_a_value_no_environment_takes_is_left_out_and_the_rest_handed_on(load, caplog):
    """A NUL stopped the loop halfway: the keys before it loaded, the processes this one starts never told."""
    caplog.set_level(logging.WARNING, logger=environment.logger.name)

    assert load(b"SECRETS_TEST_FIRST=cut\x00short\nSECRETS_TEST_SECOND=second-value\n") == {
        "SECRETS_TEST_FIRST": None, "SECRETS_TEST_SECOND": "second-value"}
    assert "line 1: a NUL character" in caplog.text and "cut" not in caplog.text, caplog.text
    assert "SECRETS_TEST_SECOND:" in os.environ[environment.SECRETS_FROM_FILE_ENV]


def test_a_variable_of_undecodable_bytes_does_not_stop_the_prediction(tmp_path, monkeypatch):
    """On POSIX such a variable arrives as lone surrogates, which no plain UTF-8 encode takes."""
    monkeypatch.setenv("SECRETS_TEST_ODD", "\udcfc")
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")

    assert environment.environment_at_restart(str(tmp_path / "config.yaml"))["SECRETS_TEST_ODD"] == "\udcfc"
