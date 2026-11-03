"""parse_entrypoint: one reading of ``entrypoint`` for discovery and catalog."""

import pytest

from agent_system.plugins.plugin_manifest import parse_entrypoint


@pytest.mark.parametrize("metadata, expected", [
    ({}, ("plugin", "PLUGIN_FACTORY")),
    ({"entrypoint": "server:FACTORY"}, ("server", "FACTORY")),
    ({"entrypoint": "server"}, ("plugin", "PLUGIN_FACTORY")),
    ({"entrypoint": 3}, ("plugin", "PLUGIN_FACTORY")),
    ({"entrypoint": "a:b:c"}, ("a", "b:c")),
    (None, ("plugin", "PLUGIN_FACTORY")),
])
def test_parse_entrypoint(metadata, expected):
    assert parse_entrypoint(metadata) == expected
