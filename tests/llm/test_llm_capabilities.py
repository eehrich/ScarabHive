"""Tests for the LLM capabilities system."""

from agent_system.llm import capabilities as caps_mod
from agent_system.llm.capabilities import (
    ModelCapability,
    ModelCapabilities,
    init_capabilities_registry,
)


class TestModelCapability:
    """Test ModelCapability enum."""

    def test_capability_values(self):
        """Test capability enum values."""
        assert ModelCapability.TOOLS.value == "tools"
        assert ModelCapability.IMAGE_INPUT.value == "image_input"
        assert ModelCapability.AUDIO_INPUT.value == "audio_input"
        assert ModelCapability.VIDEO_INPUT.value == "video_input"

    def test_capability_from_string(self):
        """Test creating capability from string."""
        assert ModelCapability("tools") == ModelCapability.TOOLS
        assert ModelCapability("image_input") == ModelCapability.IMAGE_INPUT


class TestModelCapabilities:
    """Test ModelCapabilities class."""

    def test_default_capabilities(self):
        """Since the merge (2026-08-22) the defaults come from
        ModelCapabilitiesConfig -- there were two classes with the same fields and
        different defaults (tools: False here, True there). The config side is
        authoritative: it is what the model entries are validated with."""
        caps = ModelCapabilities()
        assert caps.tools is True
        assert caps.image_input is False
        assert caps.audio_input is False
        assert caps.video_input is False
        assert caps.streaming is True

    def test_has_capability_by_enum(self):
        """Test has_capability with enum."""
        caps = ModelCapabilities(tools=True, image_input=True)
        assert caps.has_capability(ModelCapability.TOOLS) is True
        assert caps.has_capability(ModelCapability.IMAGE_INPUT) is True
        assert caps.has_capability(ModelCapability.AUDIO_INPUT) is False

    def test_has_capability_by_string(self):
        """Test has_capability with string."""
        caps = ModelCapabilities(tools=True, image_input=True)
        assert caps.has_capability("tools") is True
        assert caps.has_capability("image_input") is True
        assert caps.has_capability("audio_input") is False


class TestTheRegistryComesFromTheMergedConfig:
    def test_it_is_filled_and_reachable_by_both_names(self, monkeypatch):
        """Not a config test: not which model can do what, but that the
        table comes into existence at all -- and is reachable under both names,
        the config key and the provider string. The gate hits exactly here."""
        monkeypatch.setattr(caps_mod, "_capabilities_registry", {})
        monkeypatch.setattr(caps_mod, "_registry_loaded", False)
        init_capabilities_registry()
        registry = caps_mod._capabilities_registry
        assert registry, "registry empty — the merged configuration did not load"
        assert all(isinstance(c, ModelCapabilities) for c in registry.values())
        assert any("/" in name for name in registry), \
            "no provider-style model string in the registry — agent.llm.model would miss"
