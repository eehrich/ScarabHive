"""Guards for who may write which OKF bundle, on the RESOLVED configuration.

The sysadmin's ``infra`` bundle is injected into its prompt every turn. It has
an instance of its own (``sysadmin_okf``), and the shared ``okf`` -- held by
okf_agent, amiga_coder and the writer agents -- carves it out. Both halves are
checked with the real server's sandbox, not against the YAML text.
"""
from __future__ import annotations

import pytest

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from plugins.okf.server import OkfServer

INFRA = "data/okf/infra"


@pytest.fixture(scope="module")
def config():
    return load_settings()


@pytest.fixture(scope="module")
def sysadmin(config):
    cfg = get_tool_server_config("sysadmin_agent", config)
    assert cfg is not None, "sysadmin_agent is not configured"
    return cfg


def server(config, name: str) -> OkfServer:
    cfg = get_tool_server_config(name, config)
    assert cfg is not None and cfg.enabled, f"{name} is not configured"
    return OkfServer(name, config, cfg)


def allows(agent_cfg, tool: str) -> bool:
    srv, name = tool.split("/", 1)
    return tool_matches_patterns(name, srv, agent_cfg.agent_config.tools.allowed)


def test_sysadmin_holds_its_own_instance_not_the_shared_one(sysadmin):
    assert allows(sysadmin, "sysadmin_okf/sysadmin_okf_write_concept")
    assert allows(sysadmin, "sysadmin_okf/sysadmin_okf_search")
    assert not allows(sysadmin, "okf/okf_write_concept")


def test_the_hook_runs_on_its_instance_and_its_bundle_is_inside_it(sysadmin, config):
    overrides = getattr(sysadmin.agent_config.hooks, "overrides", None) or {}
    entry = overrides.get("sysadmin_okf.okf_context_injection")
    assert entry is not None, "the hook is keyed by instance name"
    get = entry.get if isinstance(entry, dict) else lambda k: getattr(entry, k, None)
    assert get("enabled") is True
    assert get("hook_bundle") == INFRA
    assert "okf.okf_context_injection" not in overrides, "the shared hook would find nothing"
    server(config, "sysadmin_okf")._resolve_bundle(INFRA)  # raises when outside


def test_sysadmin_okf_reaches_only_infra(config):
    with pytest.raises(ValueError):
        server(config, "sysadmin_okf")._resolve_bundle("data/okf/amiga")


@pytest.mark.parametrize("bundle", [INFRA, "data/okf", f"{INFRA}/runbooks"])
def test_the_shared_okf_cannot_reach_infra(config, bundle):
    """``data/okf`` itself too: as a bundle root, a concept path ``/infra/x.md``
    and its reindex would write into infra."""
    with pytest.raises(ValueError):
        server(config, "okf")._resolve_bundle(bundle)


def test_the_shared_okf_still_reaches_the_other_bundles(config):
    shared = server(config, "okf")
    for bundle in ("data/okf/amiga", "data/okf/zustandsgraph", "data/okf/writer_library"):
        shared._resolve_bundle(bundle)


def test_every_bundle_with_its_own_instance_is_carved_out_of_the_shared_one(config):
    """An own instance keeps its agent in, not the others out: the shared okf
    must refuse every bundle another okf instance is rooted at."""
    shared = server(config, "okf")
    owned = []
    for name in config.plugins.servers:
        cfg = get_tool_server_config(name, config)
        if name == "okf" or cfg is None or cfg.type != "okf" or not cfg.enabled:
            continue
        owned += list(getattr(cfg, "allowed_directories", None) or [])
    assert len(owned) >= 4, owned
    for bundle in owned:
        with pytest.raises(ValueError):
            shared._resolve_bundle(bundle)
