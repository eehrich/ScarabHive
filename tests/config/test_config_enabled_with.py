"""enabled_with: a server is on only while the server it names is, whichever layer switches that one."""
from __future__ import annotations

import logging

import yaml

from agent_system.config.models.agent import PluginsConfig
from agent_system.config.settings import load_settings


def states(**servers):
    return {name: server.enabled for name, server in PluginsConfig(servers=servers).servers.items()}


def test_a_server_follows_the_one_it_names():
    assert states(tool={"enabled": False}, agent={"enabled": True, "enabled_with": "tool"}) == {"tool": False, "agent": False}
    assert states(tool={"enabled": True}, agent={"enabled": True, "enabled_with": "tool"}) == {"tool": True, "agent": True}


def test_its_own_off_still_holds():
    assert states(tool={"enabled": True}, agent={"enabled": False, "enabled_with": "tool"})["agent"] is False


def test_a_chain_goes_by_its_end_whatever_the_order():
    assert states(a={"enabled": True, "enabled_with": "b"}, b={"enabled": True, "enabled_with": "c"},
                  c={"enabled": False}) == {"a": False, "b": False, "c": False}
    assert states(a={"enabled": True, "enabled_with": "b"}, b={"enabled": True, "enabled_with": "c"},
                  c={"enabled": True}) == {"a": True, "b": True, "c": True}


def test_a_circle_stays_off():
    assert states(a={"enabled": True, "enabled_with": "b"}, b={"enabled": True, "enabled_with": "a"}) == {"a": False, "b": False}


def test_a_name_no_server_has_leaves_it_off_and_is_said(caplog):
    with caplog.at_level(logging.WARNING):
        assert states(agent={"enabled": True, "enabled_with": "gone"}) == {"agent": False}
    assert "enabled with 'gone'" in caplog.text


def test_without_it_nothing_changes():
    assert states(a={"enabled": True}, b={"enabled": False}) == {"a": True, "b": False}


def test_the_local_layer_switches_a_plugin_and_its_agents_go_with_it(tmp_path):
    """The way a machine does it: the plugin's file ships both, config/local.yaml switches the plugin alone."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text("includes:\n  - parts/*.yaml\n", encoding="utf-8")
    (config / "parts").mkdir()
    (config / "parts" / "tracker.yaml").write_text(yaml.safe_dump({"plugins": {"servers": {
        "tracker": {"type": "tracker", "enabled": False},
        "tracker_agent": {"type": "basic_agent", "enabled": True, "enabled_with": "tracker"},
        "tracker_helper": {"type": "basic_agent", "enabled": True, "enabled_with": "tracker"},
    }}}), encoding="utf-8")

    servers = load_settings(str(config / "config.yaml")).plugins.servers
    assert not servers["tracker"].enabled and not servers["tracker_agent"].enabled, "shipped off, agents too"

    (config / "local.yaml").write_text(yaml.safe_dump({"plugins": {"servers": {
        "tracker": {"enabled": True}, "tracker_helper": {"enabled": False}}}}), encoding="utf-8")
    servers = load_settings(str(config / "config.yaml")).plugins.servers
    assert servers["tracker"].enabled and servers["tracker_agent"].enabled, "one switch"
    assert not servers["tracker_helper"].enabled, "one agent stays off by its own entry"

    (config / "local.yaml").write_text(yaml.safe_dump({"plugins": {"servers": {
        "tracker_agent": {"enabled": True}}}}), encoding="utf-8")
    assert not load_settings(str(config / "config.yaml")).plugins.servers["tracker_agent"].enabled, "no agent without its plugin"
