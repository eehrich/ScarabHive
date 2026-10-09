"""The route rules in an include of their own (config/security.yaml): an include the master names by its path may
set auth's endpoint_security, llm_security and plugin_security -- nothing else of auth, and never an include a glob
matched, which takes in every plugin's agents/*.yaml."""
import logging
from pathlib import Path

import pytest
import yaml

from agent_system.config.settings import AUTH_RULE_SECTIONS, _expand_includes, load_settings

REPO = Path(__file__).resolve().parents[2]

RULES = {"endpoint_security": {"default_policy": "require_auth",
                               "rules": [{"pattern": "GET /from-the-include", "policy": "allow_anonymous"}]},
         "llm_security": {"max_requests_per_hour_anonymous": 7},
         "plugin_security": {"default_min_role": "admin"}}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path):
    folder = tmp_path / "config"
    write(folder / "config.yaml", {"includes": ["security.yaml", "agents/*.yaml"],
                                   "auth": {"enabled": True, "secret_key": "the-masters-own-key-" * 2}})
    write(folder / "security.yaml", {})  # named, so it must be there; a test writes its rules
    return folder


def test_a_named_include_sets_the_route_rules_and_the_master_keeps_the_rest(config_dir):
    write(config_dir / "security.yaml", {"auth": RULES})

    auth = load_settings(str(config_dir / "config.yaml")).auth

    assert [rule.pattern for rule in auth.endpoint_security.rules] == ["GET /from-the-include"]
    assert (auth.llm_security.max_requests_per_hour_anonymous, auth.plugin_security.default_min_role) == (7, "admin")
    assert (auth.enabled, auth.secret_key) == (True, "the-masters-own-key-" * 2)


def test_a_named_include_cannot_set_the_rest_of_auth(config_dir, caplog):
    write(config_dir / "security.yaml", {"auth": {**RULES, "enabled": False, "secret_key": "from-the-include-" * 3}})

    with caplog.at_level(logging.WARNING):
        auth = load_settings(str(config_dir / "config.yaml")).auth

    assert (auth.enabled, auth.secret_key) == (True, "the-masters-own-key-" * 2)
    assert auth.plugin_security.default_min_role == "admin"  # the rules are still taken
    assert "auth.enabled, auth.secret_key is read from config.yaml only" in caplog.text, caplog.text


def test_an_include_a_glob_matched_sets_no_rule(config_dir, caplog):
    """A plugin's agents/*.yaml must not open a route by shipping one."""
    write(config_dir / "agents" / "some_plugin.yaml", {"auth": {"plugin_security": {"default_policy": "allow_anonymous"}}})

    with caplog.at_level(logging.WARNING):
        auth = load_settings(str(config_dir / "config.yaml")).auth

    assert auth.plugin_security.default_policy == "require_auth"
    assert "some_plugin.yaml: 'auth' is read from config.yaml only" in caplog.text, caplog.text


def test_a_section_commented_out_in_the_include_keeps_the_masters(config_dir):
    master = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
    master["auth"]["plugin_security"] = {"default_min_role": "admin"}
    write(config_dir / "config.yaml", master)
    (config_dir / "security.yaml").write_text("auth:\n  plugin_security:\n  llm_security:\n    "
                                              "max_requests_per_hour_anonymous: 7\n", encoding="utf-8")

    auth = load_settings(str(config_dir / "config.yaml")).auth

    assert (auth.plugin_security.default_min_role, auth.llm_security.max_requests_per_hour_anonymous) == ("admin", 7)


@pytest.mark.parametrize("state", ["missing", "broken"])
def test_the_start_fails_without_the_named_rules_file(config_dir, state):
    """Skipped, the start went on with the models' defaults: every plugin panel open to any user (default_min_role
    user). A file the master names by its path fails the start as the master itself does."""
    if state == "missing":
        (config_dir / "security.yaml").unlink()
    else:
        (config_dir / "security.yaml").write_text("auth:\n  plugin_security: [unclosed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="security.yaml"):
        load_settings(str(config_dir / "config.yaml"))


def test_a_master_that_still_names_a_missing_local_layer_starts(config_dir):
    """The tracked config.yaml named local.yaml until the loader read it itself; a machine that kept that line and
    has no local.yaml must still start: the local layer is optional."""
    master = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
    master["includes"].append("local.yaml")
    write(config_dir / "config.yaml", master)

    assert load_settings(str(config_dir / "config.yaml")).auth.enabled is True


def test_the_shipped_config_keeps_its_route_rules_in_security_yaml():
    master = REPO / "config" / "config.yaml"
    data = yaml.safe_load(master.read_text(encoding="utf-8"))
    shipped = yaml.safe_load((REPO / "config" / "security.yaml").read_text(encoding="utf-8"))

    assert "security.yaml" in data["includes"], "named by its path: a glob would not let it set auth"
    assert not set(AUTH_RULE_SECTIONS) & set(data["auth"]), "a rule section back in config.yaml: two places to look"
    assert set(shipped) == {"auth"} and set(shipped["auth"]) == set(AUTH_RULE_SECTIONS)
    assert "security.yaml" in _expand_includes(data, master)


def test_a_named_file_being_replaced_is_read_once_it_is_there(config_dir, monkeypatch):
    """Windows refuses a read while another process replaces the file (the Agent Editor saving): a named file that
    fails to load now fails the start, so the moment is waited out."""
    from agent_system.config import layers
    write(config_dir / "security.yaml", {"auth": RULES})
    real, refused = layers.Path.read_bytes, []

    def replacing(self):
        if self.name == "security.yaml" and len(refused) < 3:
            refused.append(self)
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real(self)
    monkeypatch.setattr(layers.Path, "read_bytes", replacing)
    monkeypatch.setattr(layers.time, "sleep", lambda seconds: None)

    auth = load_settings(str(config_dir / "config.yaml")).auth

    assert len(refused) == 3 and auth.plugin_security.default_min_role == "admin"
