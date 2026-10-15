"""analyze_plugin_config reads a templated schema.yaml the way the runtime renders it.

It used to delete the template tags with a regex: ``{{ name }}`` inside a
quoted string became an empty pair of quotes inside the quotes, and both branches of an ``{% if %}`` landed in one
document, so 51 of 63 plugin schemas failed to parse and their ``config:``
defaults were never reported.
"""
from __future__ import annotations

from pathlib import Path

from scripts.analyze_plugin_config import ConfigExtractor

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "plugins"


def test_a_templated_schema_gives_its_config_defaults(capsys):
    # tavily_search: "{{ name }}" inside quotes and an if/else around the tools
    extractor = ConfigExtractor(PLUGINS / "tavily_search")
    extractor._analyze_schema_yaml()
    assert "Could not parse" not in capsys.readouterr().out
    assert extractor.config_params.get("cache_ttl") == 1800



def test_a_config_written_as_json_schema_gives_its_properties():
    extractor = ConfigExtractor(PLUGINS / "comfyui")
    extractor._analyze_schema_yaml()
    assert "type" not in extractor.config_params
    assert "host" in extractor.config_params


def test_a_default_that_is_a_call_is_read_whole():
    from scripts.analyze_plugin_config import _argument_at

    code = 'getattr(config, "every", self.config.get("every_s", 300)) + 1'
    start = code.index("self.")
    assert _argument_at(code, start) == 'self.config.get("every_s", 300)'
    assert _argument_at('x.get("k", "a,b)")', 10) == '"a,b)"'
