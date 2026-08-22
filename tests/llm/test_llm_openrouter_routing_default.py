"""System-weiter Default fuer die OpenRouter-Anbieterwahl.

``llm_system.openrouter_routing`` legt ein ``provider``-Objekt unter JEDES
Modell, das mit OpenRouter redet — z.B. ``{sort: price}`` fuer den jeweils
guenstigsten Anbieter, ohne 37 Modelleintraege anzufassen.

Drei Dinge muessen halten, und jedes davon ist schon einmal schiefgegangen:

* Der Wert muss am Produktionspfad ANKOMMEN. Geprueft wird deshalb durch
  ``resolve_llm_config_for_agent()`` — die eine Stelle, durch die Agent- und
  Profil-Pfad beide laufen — und fuer den letzten Hop durch ``make_llm()``
  selbst, denn zwischen Aufloeser und Client liegt eine Uebergabe pro
  Provider-Zweig, die einzeln vergessen werden kann.
* Er darf NUR an OpenRouter gehen. ``provider`` ist ein OpenRouter-Body-Feld;
  an einem fremden Endpunkt waere es ein unbekannter Key im Request.
* Massgeblich ist die WIRKSAME base_url. ``make_llm()`` setzt fuer
  ``openai_responses`` mangels base_url OpenRouter ein — ein solcher Eintrag
  redet mit OpenRouter, obwohl im Config-Feld nichts steht.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
)
from agent_system.llm.factory import resolve_llm_config_for_agent


def _real_make_llm():
    """conftest.py ersetzt ``make_llm`` global durch einen Fake, damit beim
    Bootstrap keine Sockets aufgehen. Fuer die Verdrahtungstests brauchen wir
    das Original — es liegt unter ``_orig_make_llm``."""
    from agent_system.llm import clients
    return getattr(clients, "_orig_make_llm", clients.make_llm)

#: Am Datei-Anker, nicht am CWD — sonst ueberspringt sich der Katalogteil
#: lautlos, sobald pytest aus einem Unterverzeichnis laeuft.
OPENROUTER_YAML = REPO_ROOT / "config" / "llm_openrouter.yaml"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


def _resolve(model: LLMModelConfig, openrouter_routing: dict | None = None) -> dict:
    """Ein Modell durch den echten Aufloeser schicken."""
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            openrouter_routing=openrouter_routing,
            profiles={"p": LLMProfile(model_ref="m")},
            models={"m": model},
        ),
    )
    return resolve_llm_config_for_agent(cfg, AgentConfig(llm_profile="p"))


def _openrouter_model(**overrides) -> LLMModelConfig:
    return LLMModelConfig(
        provider="openai_httpx", model="some/model",
        base_url=OPENROUTER_URL, **overrides)


class TestSystemDefaultReachesOpenRouterModels:
    def test_default_lands_on_a_model_without_own_routing(self):
        kwargs = _resolve(_openrouter_model(), {"sort": "price"})
        assert kwargs["provider_routing"] == {"sort": "price"}

    def test_absent_default_changes_nothing(self):
        """Aus ist aus — ohne den Schalter bleibt der Request wie bisher."""
        assert "provider_routing" not in _resolve(_openrouter_model())

    def test_model_entry_survives_without_the_switch(self):
        kwargs = _resolve(_openrouter_model(provider_routing={"order": ["a"]}))
        assert kwargs["provider_routing"] == {"order": ["a"]}

    def test_host_match_ignores_case(self):
        """Der Riegel vergleicht kleingeschrieben — sonst entscheidet die
        Schreibweise in der yaml darueber, ob der Schalter wirkt."""
        model = _openrouter_model()
        model.base_url = "HTTPS://OpenRouter.AI/api/v1"
        assert _resolve(model, {"sort": "price"})["provider_routing"] == {
            "sort": "price"}


class TestEffectiveBaseUrlDecides:
    """``make_llm()`` setzt pro Provider eine andere Default-base_url ein.

    ``openai_responses`` ohne base_url landet bei OpenRouter (clients.py),
    ``openai_httpx`` bei api.openai.com. Wer nur das Config-Feld ansieht,
    behandelt beide gleich — und liegt bei einem von beiden falsch.
    """

    def test_responses_without_base_url_counts_as_openrouter(self):
        model = LLMModelConfig(provider="openai_responses", model="m")
        assert _resolve(model, {"sort": "price"})["provider_routing"] == {
            "sort": "price"}

    def test_responses_pointed_elsewhere_stays_out(self):
        model = LLMModelConfig(provider="openai_responses", model="m",
                               base_url="https://api.openai.com/v1")
        assert "provider_routing" not in _resolve(model, {"sort": "price"})

    def test_the_configured_default_matches_make_llm(self):
        """Anti-Drift: der Riegel ahmt clients.py nach. Aendert sich dort die
        Default-base_url, muss das hier auffallen — sonst zeigt der Riegel auf
        einen Endpunkt, den es nicht mehr gibt."""
        client = _real_make_llm()("openai_responses", "m", "sk-test", None,
                                  None, "openai_compat", 60)
        assert "openrouter.ai" in str(client.base_url).lower(), (
            "openai_responses defaultet nicht mehr auf OpenRouter — "
            "_targets_openrouter() in factory.py zieht die falsche Grenze")


class TestModelEntryWinsPerKey:
    def test_own_order_is_kept_alongside_the_default(self):
        """``order`` haelt den impliziten Prompt-Cache warm — der globale
        Schalter darf es nicht wegwischen."""
        kwargs = _resolve(
            _openrouter_model(provider_routing={"order": ["google-vertex"]}),
            {"sort": "price"})
        assert kwargs["provider_routing"] == {
            "sort": "price", "order": ["google-vertex"]}

    def test_own_value_beats_the_default_on_the_same_key(self):
        kwargs = _resolve(
            _openrouter_model(provider_routing={"sort": "throughput"}),
            {"sort": "price"})
        assert kwargs["provider_routing"] == {"sort": "throughput"}

    def test_nested_values_are_replaced_whole_not_merged(self):
        """Bewusst FLACH: ein Deep-Merge auf einem freien Dict waere die
        groessere Ueberraschung. Der Preis dafuer steht hier fest, damit
        niemand ihn spaeter versehentlich umdreht — der completion-Deckel des
        Defaults ueberlebt einen eigenen max_price NICHT."""
        kwargs = _resolve(
            _openrouter_model(provider_routing={"max_price": {"prompt": 5}}),
            {"max_price": {"prompt": 1, "completion": 2}})
        assert kwargs["provider_routing"] == {"max_price": {"prompt": 5}}


class TestForeignEndpointsStayUntouched:
    """``provider`` ist ein OpenRouter-Feld — anderswo ein Fremdkoerper."""

    @pytest.mark.parametrize("provider,base_url", [
        ("openai_httpx", None),
        ("openai_httpx", "https://api.openai.com/v1"),
        ("openai_httpx", "https://generativelanguage.googleapis.com/v1beta/openai"),
        ("openai_httpx", "http://localhost:11434/v1"),
        ("ollama", None),
    ])
    def test_default_does_not_leak(self, provider, base_url):
        model = LLMModelConfig(provider=provider, model="m", base_url=base_url)
        assert "provider_routing" not in _resolve(model, {"sort": "price"})

    def test_own_routing_still_passes_through(self):
        """Wer es am Modell ausdruecklich hinschreibt, bekommt es — der
        Schalter entscheidet nur ueber den DEFAULT."""
        model = LLMModelConfig(
            provider="openai_httpx", model="m",
            base_url="https://api.openai.com/v1",
            provider_routing={"order": ["x"]})
        assert _resolve(model, {"sort": "price"})["provider_routing"] == {
            "order": ["x"]}


class TestMakeLlmForwardsRoutingPerProviderBranch:
    """Der letzte Hop: jeder Provider-Zweig reicht ``provider_routing`` einzeln
    weiter. Faellt einer aus, verliert er das Routing lautlos — der Aufloeser
    daneben bleibt gruen, weil er seine Arbeit ja getan hat.
    """

    @pytest.mark.parametrize("provider", ["openai_httpx", "openai_responses"])
    def test_routing_reaches_the_client(self, provider):
        client = _real_make_llm()(provider, "some/model", "sk-test",
                                  OPENROUTER_URL, 200000, "openai_compat", 60,
                                  provider_routing={"sort": "price"})
        assert getattr(client, "provider_routing", None) == {"sort": "price"}, (
            f"make_llm() reicht provider_routing im {provider}-Zweig nicht "
            f"weiter — das Routing faellt still unter den Tisch")


@pytest.mark.skipif(not OPENROUTER_YAML.exists(), reason="keine llm_openrouter.yaml")
class TestAgainstTheShippedModels:
    """Am ECHTEN Modellkatalog, nicht an einem Nachbau.

    Ein selbstgebautes Modell hat genau die base_url, an die der Autor gerade
    denkt. Der Schalter soll aber die ausgelieferten Eintraege erwischen.

    Beide Tests sichern ihre GRUNDMENGE ab: eine leere Menge erfuellt jede
    All-Aussage, ein Filter der nichts mehr findet waere sonst von einem
    funktionierenden Schalter nicht zu unterscheiden.
    """

    @staticmethod
    def _shipped() -> dict[str, LLMModelConfig]:
        """Der Katalog kommt aus der gemergten Config, nicht aus einer einzelnen
        Datei: seit Modelle voneinander erben (``extends``, aufgeloest in
        settings) ist ein roher YAML-Eintrag unvollstaendig — er traegt nur
        noch, was er gegenueber seinem Elternteil aendert."""
        from agent_system.config.settings import load_settings

        return dict(load_settings().llm_system.models or {})

    def test_every_shipped_openrouter_model_gets_the_default(self):
        shipped = self._shipped()
        via_openrouter = {
            name: model for name, model in shipped.items()
            if "openrouter.ai" in (model.base_url or "").lower()}
        assert len(via_openrouter) >= 30, (
            f"nur {len(via_openrouter)} OpenRouter-Modelle im Katalog gefunden "
            f"(von {len(shipped)}) — der Test misst nicht mehr, was er soll")

        missed = [
            name for name, model in via_openrouter.items()
            if _resolve(model, {"sort": "price"}).get(
                "provider_routing", {}).get("sort") != "price"
        ]
        assert not missed, f"Schalter erreicht diese Modelle nicht: {missed}"

    def test_shipped_order_entries_are_not_overwritten(self):
        shipped = self._shipped()
        with_order = {name: model for name, model in shipped.items()
                      if (model.provider_routing or {}).get("order")}
        assert len(with_order) >= 20, (
            f"nur {len(with_order)} Eintraege mit provider_routing.order — "
            f"der Test misst nicht mehr, was er soll")

        losses = [
            name for name, model in with_order.items()
            if _resolve(model, {"sort": "price"})["provider_routing"].get("order")
            != model.provider_routing["order"]
        ]
        assert not losses, f"order verloren gegangen bei: {losses}"
