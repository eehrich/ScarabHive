"""Servers a plugin type offers beyond the config (``Runtime.declare``): declared in every process that builds a
runtime, before anything is built, and then built like a configured one -- a stategraph machine's ``agent:`` block
is one (the stategraph_machine type's ``offered_servers``).
"""
from __future__ import annotations

import pytest

import agent_system.plugins as plugins_package
from agent_system.runtime import Runtime

from tests.bootstrap.test_runtime_declares_and_materializes import _agent_server, _config

AGENT = {"type": "basic_agent", "enabled": True, "agent_config": {"llm_profile": "normal"}}


@pytest.fixture
def offering(monkeypatch):
    """A plugin type ``offering_agent`` (the basic agent's factory) whose ``offered_servers`` is the test's."""
    offers = {}
    real = plugins_package.discover_all_plugins

    def discover(dirs=None):
        found = real(dirs=dirs)
        basic = found["basic_agent"]

        def factory(*args, **kwargs):
            return basic(*args, **kwargs)

        factory.__dict__.update(basic.__dict__)
        factory.offered_servers = lambda config: offers["servers"](config)
        return {**found, "offering_agent": factory}

    monkeypatch.setattr(plugins_package, "discover_all_plugins", discover)
    return offers


def test_an_offered_server_is_declared_as_the_config_names_one_and_built_at_the_start(offering, monkeypatch):
    monkeypatch.setattr(Runtime, "last_started", Runtime.last_started)  # start() sets it: given back afterwards
    offering["servers"] = lambda config: {"offered_probe": {**AGENT, "description": "offered"}}

    runtime = Runtime(_config({}))
    decl = runtime.describe("offered_probe")

    assert decl is not None and decl.offered_by == "offering_agent" and decl.type == "basic_agent"
    assert "offered_probe" in runtime.config.plugins.servers, "a lookup by name does not find it"
    runtime.start()
    assert runtime.registry.get("offered_probe")._description == "offered"


def test_a_reloaded_config_keeps_the_offered_servers(offering, monkeypatch):
    from agent_system.services.config_reload import reload_plugin_configs

    monkeypatch.setattr(Runtime, "last_started", Runtime.last_started)  # start() sets it: given back afterwards
    offering["servers"] = lambda config: {"offered_reload": {**AGENT, "description": "offered"}}
    runtime = Runtime(_config({})).start()
    fresh = _config({})  # read again from the files: the offer is in none of them

    report = reload_plugin_configs(fresh)

    assert fresh.plugins.servers["offered_reload"].description == "offered"
    assert "offered_reload" not in report["not_in_config"], report
    assert runtime.describe("offered_reload").offered_by == "offering_agent"


def test_a_name_the_config_holds_is_refused_and_said(offering):
    offering["servers"] = lambda config: {"taken": {**AGENT, "description": "offered"}}

    runtime = Runtime(_config({"taken": _agent_server(description="configured")}))

    assert runtime.describe("taken").offered_by is None
    assert runtime.config.plugins.servers["taken"].description == "configured"
    assert any("offered by 'offering_agent'" in problem and "taken" in problem for problem in runtime.problems), \
        runtime.problems


def test_an_offer_of_an_unknown_type_leaves_nothing_behind(offering):
    offering["servers"] = lambda config: {"odd": {"type": "no_such_type", "enabled": True}}

    runtime = Runtime(_config({}))

    assert runtime.describe("odd") is None and "odd" not in runtime.config.plugins.servers
    assert any("'odd'" in problem and "no_such_type" in problem for problem in runtime.problems), runtime.problems


def test_an_offer_that_fails_costs_its_servers_not_the_start(offering):
    def broken(config):
        raise RuntimeError("the machine folder is gone")

    offering["servers"] = broken

    runtime = Runtime(_config({"probe": _agent_server()}))

    assert runtime.describe("probe") is not None
    assert any("offering_agent" in problem and "the machine folder is gone" in problem for problem in runtime.problems)
