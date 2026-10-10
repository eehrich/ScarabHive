"""agent_system.config.local_layer: what the Setup panel and the install scripts write -- a key into config/local.env,
this installation's own signing key named in config/local.yaml -- and that a start then takes it."""
import os

import pytest
import yaml

from agent_system.auth.security import check_secret_key
from agent_system.config import environment, local_layer, settings
from agent_system.config.local_layer import ensure_signing_key, main, signing_key_at_restart, write_secret

SHIPPED = "published-signing-key-replace-with-your-own-0000000000"  # config/config.yaml's, public


@pytest.fixture
def own_process(monkeypatch):
    monkeypatch.setattr(environment, "_secrets_from_file", {})
    monkeypatch.setenv(settings.SECRETS_FROM_FILE_ENV, os.environ.get(settings.SECRETS_FROM_FILE_ENV, ""))
    for name in (local_layer.SIGNING_KEY_VARIABLE, "LAYER_TEST_KEY"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def master(tmp_path, own_process):
    """A master config as the repository ships it: authentication on, the public key."""
    path = tmp_path / "config.yaml"
    path.write_text(f"auth:\n  enabled: true\n  secret_key: \"{SHIPPED}\"\n", encoding="utf-8")
    return path


def local(master):
    return yaml.safe_load((master.parent / "local.yaml").read_text(encoding="utf-8"))


class TestWriteSecret:
    def test_a_line_is_replaced_in_place_and_a_later_one_of_the_name_dropped(self, master):
        env = master.parent / "local.env"
        env.write_text("# mine\nLAYER_TEST_KEY=old\nOTHER=kept\nLAYER_TEST_KEY=shadowed\n", encoding="utf-8")

        write_secret(master, "LAYER_TEST_KEY", "  new-value  ")

        assert env.read_text(encoding="utf-8") == "# mine\nLAYER_TEST_KEY=new-value\nOTHER=kept\n"

    def test_a_new_name_is_appended_and_the_file_made(self, master):
        write_secret(master, "LAYER_TEST_KEY", "v1")
        write_secret(master, "OTHER", "v2")

        assert settings._read_secrets_file(master.parent / "local.env") == {"LAYER_TEST_KEY": "v1", "OTHER": "v2"}

    def test_a_commented_line_of_the_name_stays_a_comment(self, master):
        env = master.parent / "local.env"
        env.write_text("# LAYER_TEST_KEY=example\n", encoding="utf-8")

        write_secret(master, "LAYER_TEST_KEY", "real")

        assert env.read_text(encoding="utf-8") == "# LAYER_TEST_KEY=example\nLAYER_TEST_KEY=real\n"

    @pytest.mark.parametrize("name, value", [("1BAD", "v"), ("A B", "v"), ("OK", ""), ("OK", "   "),
                                             ("OK", "two\nlines"), ("OK", "nul\0"), ("OK", '"quoted"'),
                                             ("OK", "a\u2028AUTH_SECRET_KEY=mine"), ("OK", "a\x85B=c"),
                                             ("OK", "a\x0cB=c"), ("OK", "tab\there")])
    def test_what_a_secrets_file_cannot_hold_is_refused_and_nothing_written(self, master, name, value):
        with pytest.raises(ValueError):
            write_secret(master, name, value)
        assert not (master.parent / "local.env").exists()

    def test_saves_at_the_same_time_lose_no_line(self, master):
        """Two tabs of the panel, each save on a thread of its own: read, change, write -- the later write dropped
        the other's line, or Windows refused the replace of a file another thread was replacing."""
        import threading
        names = [f"LAYER_TEST_{i}" for i in range(24)]
        start = threading.Barrier(len(names))
        errors = []

        def save(name):
            start.wait()
            try:
                write_secret(master, name, f"value-of-{name}")
            except Exception as error:  # noqa: BLE001 -- the finding is any error at all
                errors.append(error)
        threads = [threading.Thread(target=save, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, errors
        assert settings._read_secrets_file(master.parent / "local.env") == {name: f"value-of-{name}" for name in names}

    def test_a_utf16_file_is_read_and_rewritten_as_utf8(self, master):
        """PowerShell 5.1's `>` writes UTF-16 with a BOM."""
        env = master.parent / "local.env"
        env.write_bytes("OTHER=kept\n".encode("utf-16"))

        write_secret(master, "LAYER_TEST_KEY", "v")

        assert env.read_bytes().decode("utf-8").splitlines() == ["OTHER=kept", "LAYER_TEST_KEY=v"]  # no BOM either

    def test_a_file_that_is_no_utf8_is_not_rewritten(self, master):
        env = master.parent / "local.env"
        env.write_bytes("LAYER_TEST_KEY=wert-mit-ü\n".encode("cp1252"))

        with pytest.raises(ValueError, match="not UTF-8"):
            write_secret(master, "LAYER_TEST_KEY", "x")
        assert env.read_bytes() == "LAYER_TEST_KEY=wert-mit-ü\n".encode("cp1252")


class TestEnsureSigningKey:
    def test_a_fresh_installation_gets_an_own_key_that_a_start_takes(self, master):
        message = ensure_signing_key(master)

        key = signing_key_at_restart(master)
        assert "restart" in message and key != SHIPPED, message
        check_secret_key(key, reject_public=True)  # own: long, not public
        assert local(master) == {"auth": {"secret_key": "${AUTH_SECRET_KEY}"}}
        assert settings._read_secrets_file(master.parent / "local.env") == {"AUTH_SECRET_KEY": key}
        assert settings.load_settings(str(master)).auth.secret_key == key  # the loader agrees with the prediction

    def test_a_second_run_keeps_the_key(self, master):
        ensure_signing_key(master)
        first = signing_key_at_restart(master)

        assert "already" in ensure_signing_key(master)
        assert signing_key_at_restart(master) == first

    def test_an_own_key_in_the_master_is_left_alone(self, master):
        master.write_text("auth:\n  secret_key: \"" + "o" * 40 + "\"\n", encoding="utf-8")

        assert "already" in ensure_signing_key(master)
        assert not (master.parent / "local.yaml").exists() and not (master.parent / "local.env").exists()

    def test_a_key_the_caller_knows_is_public_is_replaced(self, master):
        known = "k" * 40  # long enough for the start check, but printed somewhere (status.SHIPPED_SIGNING_KEYS)
        master.write_text(f"auth:\n  secret_key: \"{known}\"\n", encoding="utf-8")

        ensure_signing_key(master, known={known})

        assert signing_key_at_restart(master) not in (known, None)

    def test_the_local_layer_keeps_what_it_holds_and_its_auth_indent(self, master):
        (master.parent / "local.yaml").write_text(
            "network:\n  host: 0.0.0.0\nauth:\n    # the machine's own\n    reject_default_secret_key: true\n",
            encoding="utf-8")

        ensure_signing_key(master)

        assert local(master) == {"network": {"host": "0.0.0.0"},
                                 "auth": {"reject_default_secret_key": True, "secret_key": "${AUTH_SECRET_KEY}"}}

    def test_a_utf16_local_layer_gets_the_entry(self, master):
        (master.parent / "local.yaml").write_bytes("network:\n  host: 0.0.0.0\n".encode("utf-16"))

        ensure_signing_key(master)

        assert local(master) == {"network": {"host": "0.0.0.0"}, "auth": {"secret_key": "${AUTH_SECRET_KEY}"}}

    def test_an_empty_auth_section_in_the_local_layer_gets_the_entry(self, master):
        (master.parent / "local.yaml").write_text("auth:\nnetwork:\n  host: 0.0.0.0\n", encoding="utf-8")

        ensure_signing_key(master)

        assert local(master)["auth"] == {"secret_key": "${AUTH_SECRET_KEY}"}

    def test_a_weak_key_the_local_layer_sets_itself_is_a_hands_to_change(self, master):
        (master.parent / "local.yaml").write_text(f"auth:\n  secret_key: \"{SHIPPED}\"\n", encoding="utf-8")

        with pytest.raises(ValueError, match="sets auth.secret_key itself"):
            ensure_signing_key(master)
        assert not (master.parent / "local.env").exists()

    def test_a_variable_of_the_real_environment_cannot_be_overruled_and_nothing_is_touched(self, master, monkeypatch):
        """local.yaml pointed at the variable before the refusal: the next start took the environment's weak key."""
        monkeypatch.setenv("AUTH_SECRET_KEY", "short")

        with pytest.raises(ValueError, match="environment"):
            ensure_signing_key(master)
        assert not (master.parent / "local.env").exists() and not (master.parent / "local.yaml").exists()

    def test_a_weak_value_in_local_env_is_replaced(self, master):
        (master.parent / "local.env").write_text("AUTH_SECRET_KEY=short\n", encoding="utf-8")

        ensure_signing_key(master)

        check_secret_key(signing_key_at_restart(master), reject_public=True)

    def test_a_broken_local_layer_is_not_touched(self, master):
        broken = "auth: [unclosed\n"
        (master.parent / "local.yaml").write_text(broken, encoding="utf-8")

        with pytest.raises(ValueError, match="does not load"):
            ensure_signing_key(master)
        assert (master.parent / "local.yaml").read_text(encoding="utf-8") == broken


def test_without_a_path_it_takes_the_config_the_api_takes(master, tmp_path, monkeypatch):
    """No path: the config agent-api loads (paths.default_config_path), not config/config.yaml where the shell
    stands -- run from elsewhere, the key went beside another file or nowhere."""
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(master))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main(["signing-key"]) == 0

    assert local(master)["auth"]["secret_key"] == local_layer.SIGNING_KEY_REFERENCE


def test_the_install_scripts_command(master, capsys):
    assert main(["signing-key", str(master)]) == 0
    assert main(["signing-key", str(master)]) == 0
    assert "already" in capsys.readouterr().out
    assert main([]) == 2
    assert main(["signing-key", str(master.parent / "missing.yaml")]) == 1
