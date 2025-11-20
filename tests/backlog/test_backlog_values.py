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


def test_symbol_map_contains_red_cross_mapping():
    """Test that the symbol map correctly maps red cross to terminal statuses."""
    cfg = values.load()
    symbol_map = cfg['symbol_map']

    # Check that red cross is mapped to terminal statuses
    assert '❌' in symbol_map
    red_cross_statuses = symbol_map['❌']

    # Should be a list containing the terminal statuses
    assert isinstance(red_cross_statuses, list)
    assert 'cancelled' in red_cross_statuses
    assert 'rejected' in red_cross_statuses
    assert 'reverted' in red_cross_statuses


def test_acceptable_terminal_contains_cancelled_statuses():
    """Test that acceptable_terminal list includes cancelled/failed/rejected/reverted."""
    cfg = values.load()
    terminal_list = cfg['acceptable_terminal']

    assert 'cancelled' in terminal_list
    assert 'rejected' in terminal_list
    assert 'reverted' in terminal_list
    assert 'done' in terminal_list  # Should also include successful completion


def test_allowed_statuses_includes_terminal_states():
    """Test that allowed_statuses includes all terminal states."""
    cfg = values.load()
    allowed = cfg['allowed_statuses']

    terminal_states = ['cancelled', 'failed', 'rejected', 'reverted']
    for state in terminal_states:
        assert state in allowed, f"Terminal state '{state}' should be in allowed_statuses"
