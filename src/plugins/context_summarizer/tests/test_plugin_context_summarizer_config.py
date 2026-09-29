"""Kommt an, was in der Konfiguration steht — und ist der Prompt nicht leer?

Der Server hielt eine ZWEITE Default-Tabelle und kopierte danach jeden Wert auf
die Hook-Implementierung, also NACH dem Aufloesen der schema.yaml-Defaults. Zwei
seiner Fallbacks waren falsch, und weil ``plugins.yaml`` beide Schluessel nicht
setzt, war der falsche Fallback immer der wirksame Wert:

* ``llm_profile`` fiel auf ``'fast'`` — ein Profil, das ``config/llm.yaml`` gar
  nicht kennt und das ``schema.yaml`` nicht einmal im Enum fuehrt. Die
  Client-Erzeugung scheiterte daran still, und der Summarizer benutzte
  stattdessen die LLM des Agenten.
* ``summary_prompt_template`` fiel auf ``''``. Der Prompt entsteht als
  ``template.replace('{messages}', …)`` — bei leerem Template ist das Ergebnis
  leer, die Nachrichten werden nicht einmal eingesetzt. Am Produktionspfad
  gemessen: Laenge 0. Der Summarizer rief das LLM mit einer leeren
  Nutzernachricht auf und setzte die Antwort an die Stelle echter Konversation.

Die Tests fahren deshalb die ECHTE ``config/plugins.yaml`` durch den ECHTEN
Server. Mit selbstgebauter Config waeren beide Luecken unsichtbar geblieben —
ein solcher Test setzt genau die Schluessel, an die der Autor gerade denkt.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from plugins.context_summarizer.server import ContextSummarizerServer

PLUGINS_YAML = Path("config/plugins.yaml")
PLUGIN_DIR = Path(__file__).parent.parent

#: yaml-Schluessel -> Attribut am Hook. Nur wo die Namen abweichen.
_ALIASES = {
    "summarization_trigger_percentage": "trigger_percentage",
    "summarization_chunk_size": "chunk_size",
    "preserve_recent_count": "preserve_recent",
    "preserve_system_messages": "preserve_system",
    "summary_prompt_template": "prompt_template",
    "min_summary_reduction": "min_reduction",
    "max_message_preview_length": "max_preview_length",
    "min_time_between_summarizations": "min_time_between",
    "max_tracked_sessions": "_max_tracked_sessions",
}


def _shipped() -> dict:
    data = yaml.safe_load(PLUGINS_YAML.read_text(encoding="utf-8"))
    return data["plugins"]["servers"]["context_summarizer"].get("config", {})


def _hook(config: dict):
    mcp = SimpleNamespace(config=dict(config), hook_config={},
                          name="context_summarizer")
    return ContextSummarizerServer(
        "context_summarizer", SimpleNamespace(), mcp)._hooks_impl


@pytest.mark.skipif(not PLUGINS_YAML.exists(), reason="kein config/plugins.yaml")
def test_every_shipped_setting_reaches_the_plugin():
    """Der Test, der die Luecke gefunden haette."""
    shipped = _shipped()
    assert shipped, "der context_summarizer-Block ist leer — Test waere gegenstandslos"

    hook = _hook(shipped)
    ignored = []
    for key, want in shipped.items():
        attr = _ALIASES.get(key, key)
        if not hasattr(hook, attr):
            ignored.append(f"{key}: kein Attribut '{attr}' am Plugin")
            continue
        got = getattr(hook, attr)
        same = (float(got) == float(want)
                if isinstance(want, (int, float)) and not isinstance(want, bool)
                else got == want)
        if not same:
            ignored.append(f"{key}: gesetzt {want!r}, wirksam {got!r}")

    assert not ignored, (
        "Werte aus config/plugins.yaml erreichen den Plugin nicht:\n  "
        + "\n  ".join(ignored))


def test_the_summarisation_prompt_is_never_empty():
    """Ein leeres Template macht den GANZEN Prompt leer.

    ``template.replace('{messages}', msgs)`` laeuft AUF dem Template — ist es
    leer, bleibt auch nach dem Ersetzen nichts uebrig. Geprueft wird am echten
    Server mit der echten Konfiguration, denn genau dort entstand der Schaden.
    """
    hook = _hook(_shipped())
    assert hook.prompt_template.strip(), (
        "prompt_template ist leer — der Summarizer wuerde das LLM mit einer "
        "leeren Nachricht aufrufen und die Antwort an die Stelle echter "
        "Konversation setzen")
    assert "{messages}" in hook.prompt_template, (
        "ohne den Platzhalter landen die Nachrichten nie im Prompt")


def test_the_configured_profile_exists():
    """Ein Profil, das es nicht gibt, schaltet die Funktion still ab.

    Bei ``'fast'`` scheiterte die Client-Erzeugung und der Summarizer benutzte
    ersatzweise die LLM des Agenten — ohne dass irgendetwas rot wurde.
    """
    hook = _hook(_shipped())
    declared = yaml.safe_load((PLUGIN_DIR / "schema.yaml").read_text(encoding="utf-8"))
    allowed = declared.get("config", {}).get("llm_profile", {}).get("enum")
    assert allowed, "schema.yaml fuehrt kein Enum fuer llm_profile — Test waere gegenstandslos"
    assert hook.llm_profile in allowed, (
        f"llm_profile={hook.llm_profile!r} steht nicht im Enum {allowed}")

    # Ueber load_settings, nicht config/llm.yaml direkt: die Profile leben in
    # MEHREREN Dateien (llm.yaml + llm_openrouter.yaml), und genau die
    # zusammengefuehrte Sicht entscheidet, ob die Client-Erzeugung gelingt.
    # Der Direktlese-Weg wurde rot, als das Profil auf or-deepseek-flash
    # wechselte — ein Fehlalarm des Tests, kein Fehler der Config.
    from agent_system.config.settings import load_settings
    known = set(load_settings().llm_system.profiles)
    assert len(known) >= 20, "Config kam nicht an — Test waere gegenstandslos"
    assert hook.llm_profile in known, (
        f"llm_profile={hook.llm_profile!r} existiert in keiner Profil-Datei "
        f"— die Client-Erzeugung faellt still auf die Agenten-LLM zurueck")
