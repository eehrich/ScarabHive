"""An allowed tool pattern that matches no tool is logged, not silently empty."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent_system.servers.agent import tool_schema_builder
from agent_system.servers.agent.tool_schema_builder import ToolSchemaBuilder

LOGGER = "agent_system.servers.agent.tool_schema_builder"


class _Integration:
    """Discovery found the tally server with one tool; comfyui is configured but off."""

    system_config = SimpleNamespace(
        plugins=SimpleNamespace(servers={"tally": SimpleNamespace(enabled=True),
                                         "comfyui": SimpleNamespace(enabled=False)}),
        external_servers=SimpleNamespace(remote_servers={"everything": SimpleNamespace(enabled=True)}),
    )

    async def build_tool_schemas(self, tools):
        return [{"type": "function", "function": {"name": "tally_count"}}], {"tally_count": "tally"}


@pytest.fixture(autouse=True)
def _fresh_reports(monkeypatch):
    monkeypatch.setattr(tool_schema_builder, "_REPORTED_UNMATCHED", set())


async def _build(allowed, agent="writer"):
    builder = ToolSchemaBuilder(agent, _Integration(), lambda name: None)
    return await builder.build_schemas(["tally"], allowed_patterns=allowed)


def _messages(caplog, level):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == level]


async def test_a_pattern_without_the_instance_prefix_warns_with_the_prefixed_name(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        schemas, *_ = await _build(["tally/count"])

    assert schemas == []
    [warning] = _messages(caplog, logging.WARNING)
    assert "'writer'" in warning and "'tally/count'" in warning
    assert "tally/tally_count" in warning


async def test_a_typo_names_the_closest_tools_and_a_matching_pattern_stays_quiet(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        schemas, *_ = await _build(["tally/tally_cuont", "tally/*"])

    assert len(schemas) == 1
    [warning] = _messages(caplog, logging.WARNING)
    assert "'tally/tally_cuont'" in warning and "closest: tally/tally_count" in warning


async def test_it_is_logged_once_per_agent_and_pattern(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _build(["nosuch/*"])
        await _build(["nosuch/*"])
        await _build(["nosuch/*"], agent="other")

    assert len(_messages(caplog, logging.WARNING)) == 2


async def test_a_disabled_server_is_info_and_an_external_pattern_is_not_judged(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _build(["comfyui/*", "everything.echo", "ever*", "tally/*"])

    assert _messages(caplog, logging.WARNING) == []
    [info] = [m for m in _messages(caplog, logging.INFO) if "tools.allowed" in m]
    assert "'comfyui/*'" in info and "disabled" in info


async def test_a_glob_that_also_reaches_an_enabled_server_still_warns(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _build(["*/no_such_tool", "wether.get_forecast"])

    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 2, warnings


async def test_an_enabled_server_without_tools_says_so(caplog):
    _Integration.system_config.plugins.servers["silent"] = SimpleNamespace(enabled=True)
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await _build(["silent/*"])
    finally:
        del _Integration.system_config.plugins.servers["silent"]

    [warning] = _messages(caplog, logging.WARNING)
    assert "server 'silent' is enabled but offered this agent no tools" in warning
