"""The capability gate in front of multimodal input.

Until 2026-08-22 only the HTTP API checked whether a model can take an image;
`agent-cli --image` and agent_run attached the picture to whatever the chain
picked, and the complaint came back from the provider — late and unspecific.
The check that existed for it (`validate_capability_request`) had no caller
and is gone.

The other half of this file: `multimodal` was a config shorthand that expanded
in ONE direction. True set all three input flags — overriding an explicit
`video_input: false` — and False did nothing while reading like a statement.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm import capabilities as caps_mod
from agent_system.llm.capabilities import ModelCapabilities, ensure_model_supports


@pytest.fixture
def registry(monkeypatch):
    """A registry with exactly what the test says — no config, no disk."""
    def install(**models):
        monkeypatch.setattr(caps_mod, "_capabilities_registry", dict(models))
        # Ohne das laedt der Gate beim ersten Aufruf die echte Config nach und
        # ueberschreibt genau die Tabelle, die der Test gerade gestellt hat.
        monkeypatch.setattr(caps_mod, "_registry_loaded", True)
    return install


class TestTheGateAnswersForKnownModels:
    def test_a_text_only_model_refuses_an_image(self, registry):
        registry(**{"text-only": ModelCapabilities(image_input=False)})
        problem = ensure_model_supports("text-only", images=1)
        assert problem and "image_input" in problem and "text-only" in problem

    def test_a_model_that_can_take_it_passes(self, registry):
        registry(**{"seeing": ModelCapabilities(image_input=True)})
        assert ensure_model_supports("seeing", images=3) is None

    def test_every_modality_is_checked(self, registry):
        registry(**{"m": ModelCapabilities(image_input=True, audio_input=False,
                                           video_input=False)})
        assert ensure_model_supports("m", images=1) is None
        assert "audio_input" in ensure_model_supports("m", audio=1)
        assert "video_input" in ensure_model_supports("m", video=1), \
            "video was the modality nobody ever checked"

    def test_no_attachments_no_verdict(self, registry):
        registry(**{"text-only": ModelCapabilities()})
        assert ensure_model_supports("text-only") is None


class TestTheGateStaysOutOfWhatItDoesNotKnow:
    """A veto based on a default would block every model the registry happens
    not to carry — the registry is a convenience, not a permit office."""

    def test_an_unknown_model_is_not_refused(self, registry):
        registry(**{"known": ModelCapabilities(image_input=True)})
        assert ensure_model_supports("stranger/model", images=1) is None

    def test_no_model_name_is_not_refused(self, registry):
        registry()
        assert ensure_model_supports(None, images=1) is None


class TestEveryEntryPointAsks:
    """Three ways in, one gate. The wiring is what was missing, not the check."""

    @pytest.mark.parametrize("module", [
        "src/agent_system/app.py",
        "src/agent_system/agent_cli.py",
        "src/agent_system/agent_run.py",
    ])
    def test_the_entry_point_calls_the_gate(self, module):
        src = (REPO_ROOT / module).read_text(encoding="utf-8")
        assert "ensure_model_supports(" in src, (
            f"{module} attaches media without asking whether the model can "
            f"take it")


class TestTheMultimodalShorthandIsGone:
    def test_it_is_refused_with_a_way_out(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="image_input"):
            ModelCapabilitiesConfig.model_validate({"multimodal": True})

    def test_a_typo_no_longer_passes_silently(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ModelCapabilitiesConfig.model_validate({"image_inupt": True})

    def test_the_explicit_flags_still_work(self):
        c = ModelCapabilitiesConfig.model_validate(
            {"image_input": True, "video_input": False})
        assert (c.image_input, c.video_input) == (True, False)


class TestOneDefinition:
    def test_the_runtime_class_is_the_config_class(self):
        assert issubclass(ModelCapabilities, ModelCapabilitiesConfig)
        assert set(ModelCapabilities.model_fields) == set(ModelCapabilitiesConfig.model_fields), \
            "two field lists again — a new capability would have to be added twice"

    def test_the_only_query_survived_the_merge(self):
        c = ModelCapabilities(image_input=True)
        assert c.has_capability("image_input") is True
        assert c.has_capability("audio_input") is False


class TestAProviderStringWithTwoOwners:
    """Mehrere Config-Eintraege zeigen routinemaessig auf dasselbe Modell —
    batch, nostream, unlimited. Solange sie sich ueber die Eingaenge einig
    sind, ist der Alias eindeutig; sind sie es nicht, entschied bisher die
    Reihenfolge im dict, welche Antwort der Gate gibt."""

    def _registry_from(self, entries, monkeypatch):
        from types import SimpleNamespace
        cfg = SimpleNamespace(llm_system=SimpleNamespace(models={
            name: SimpleNamespace(model=actual,
                                  capabilities=ModelCapabilitiesConfig(**caps))
            for name, (actual, caps) in entries.items()}))
        import agent_system.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "load_settings", lambda *a, **k: cfg)
        return caps_mod.load_capabilities_from_config()

    def test_an_entry_name_outranks_someone_elses_alias(self, monkeypatch):
        """'claude-sonnet-5' ist ein eigener Eintrag UND der model-String von
        '-thinking' und '-batch'. Deren Alias hat den echten Eintrag
        ueberschrieben — der Gate antwortete fuer 'claude-sonnet-5' mit den
        Faehigkeiten eines anderen Modells."""
        reg = self._registry_from({
            "sonnet": ("sonnet", {"image_input": True}),
            "sonnet-thinking": ("sonnet", {"image_input": False}),
        }, monkeypatch)
        assert reg["sonnet"].image_input is True

    def test_agreeing_owners_keep_the_alias(self, monkeypatch):
        reg = self._registry_from({
            "gpt-5": ("gpt-5.4", {"image_input": True}),
            "gpt-5-batch": ("gpt-5.4", {"image_input": True, "streaming": False}),
        }, monkeypatch)
        assert reg["gpt-5.4"].image_input is True

    def test_disagreeing_owners_lose_the_alias(self, monkeypatch):
        reg = self._registry_from({
            "gpt-5": ("gpt-5.4", {"image_input": True}),
            "gpt-5-blind": ("gpt-5.4", {"image_input": False}),
        }, monkeypatch)
        assert "gpt-5.4" not in reg,             "the gate would answer image_input from whichever entry came last"
        assert reg["gpt-5"].image_input is True, "the config keys stay usable"


class TestTheConfigIsReadOncePerProcess:
    def test_a_second_call_does_not_reload(self, monkeypatch):
        calls = []

        def counting_load(config_path=None):
            calls.append(config_path)
            return {"m": ModelCapabilities(image_input=True)}

        monkeypatch.setattr(caps_mod, "load_capabilities_from_config", counting_load)
        monkeypatch.setattr(caps_mod, "_capabilities_registry", {})
        monkeypatch.setattr(caps_mod, "_registry_loaded", False)

        ensure_model_supports("m", images=1)
        ensure_model_supports("m", images=1)
        ensure_model_supports("other", images=1)
        assert len(calls) == 1, (
            "load_settings() re-reads every YAML file and drops the plugin "
            "cache — once per request is not acceptable")

    def test_an_empty_registry_is_not_retried_forever(self, monkeypatch):
        calls = []
        monkeypatch.setattr(caps_mod, "load_capabilities_from_config",
                            lambda config_path=None: calls.append(1) or {})
        monkeypatch.setattr(caps_mod, "_capabilities_registry", {})
        monkeypatch.setattr(caps_mod, "_registry_loaded", False)

        ensure_model_supports("m", images=1)
        ensure_model_supports("m", images=1)
        assert len(calls) == 1, "a broken config must not be re-parsed per request"
