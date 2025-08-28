import importlib
import os
import yaml

from scripts.backlog_tool import values


def reload_values():
    importlib.reload(values)


def test_defaults_when_missing(tmp_path, monkeypatch):
    # point BACKLOG_VALUES to a non-existent file to force defaults
    missing = tmp_path / "no_such.yaml"
    monkeypatch.setenv('BACKLOG_VALUES', str(missing))
    reload_values()
    cfg = values.load()
    assert isinstance(cfg, dict)
    assert 'allowed_statuses' in cfg
    assert 'symbol_map' in cfg


def test_env_override_file_loaded(tmp_path, monkeypatch):
    data = {
        'allowed_statuses': ['custom_status'],
        'symbol_map': {'X': 'open'},
    }
    p = tmp_path / 'bv.yaml'
    p.write_text(yaml.safe_dump(data), encoding='utf-8')
    monkeypatch.setenv('BACKLOG_VALUES', str(p))
    reload_values()
    cfg = values.load()
    # loader does shallow-merge semantics: provided lists/dicts replace defaults
    assert cfg['allowed_statuses'] == ['custom_status']
    assert cfg['symbol_map'] == {'X': 'open'}


def test_get_helper_returns_default(monkeypatch):
    # Ensure get() returns provided default when key missing
    monkeypatch.delenv('BACKLOG_VALUES', raising=False)
    reload_values()
    assert values.get('this_key_should_not_exist', 'the-default') == 'the-default'
