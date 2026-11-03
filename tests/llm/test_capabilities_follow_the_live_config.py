"""The attachment gate reads the config the process RUNS on.

It used to build its table from a bare load_settings() on first use: the
default config path, whatever --config the process was started with -- and a
second full load that drops the plugin caches on the way. A model defined only
in the config the process was given was "unknown" to the gate, so an image
went unchecked to a text-only model.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.config.settings import load_settings
from agent_system.llm import capabilities as caps_mod
from agent_system.llm.capabilities import ensure_model_supports

MODEL = "probe-text-only-model"


def _write_config(directory: Path, image_input: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    master = directory / "elsewhere.yaml"
    master.write_text(
        "llm_system:\n"
        "  models:\n"
        f"    {MODEL}:\n"
        "      provider: openai_httpx\n"
        "      model: probe/provider-string\n"
        "      capabilities:\n"
        f"        image_input: {str(image_input).lower()}\n",
        encoding="utf-8")
    return master


@pytest.fixture
def fresh_registry(monkeypatch):
    """No registry yet; restored after the test."""
    monkeypatch.setattr(caps_mod, "_capabilities_registry", {})
    monkeypatch.setattr(caps_mod, "_registry_loaded", False)


def test_bootstrap_fills_the_gate_from_the_given_config(tmp_path, fresh_registry, monkeypatch):
    from agent_system.services.initialization_service import InitializationService

    config = load_settings(str(_write_config(tmp_path, image_input=False)))
    loads = []
    real = caps_mod.load_capabilities_from_config

    def spy(config_path=None, *, config=None):
        loads.append(config_path if config is None else "live")
        return real(config_path, config=config)
    monkeypatch.setattr(caps_mod, "load_capabilities_from_config", spy)

    InitializationService(config)

    assert loads == ["live"], "bootstrap must hand over the config it runs on"
    problem = ensure_model_supports(MODEL, images=1)
    assert problem and MODEL in problem and "image_input" in problem
    assert ensure_model_supports("probe/provider-string", images=1), \
        "the provider string of the live config's model is known too"
    assert loads == ["live"], "the gate re-loaded a config after bootstrap"


def test_a_config_reload_rebuilds_the_gate(tmp_path, fresh_registry):
    from agent_system.llm.capabilities import init_capabilities_registry
    from agent_system.services.config_reload import reload_plugin_configs

    init_capabilities_registry(
        config=load_settings(str(_write_config(tmp_path / "before", image_input=False))))
    assert ensure_model_supports(MODEL, images=1)

    reload_plugin_configs(
        load_settings(str(_write_config(tmp_path / "after", image_input=True))))

    assert ensure_model_supports(MODEL, images=1) is None, \
        "the model given image_input still refuses an image after the reload"
