"""Tests für ``agent-cli run --llm-params KEY=VALUE ...``.

Die Werte müssen auto-getypt werden (die LLMModelConfig-Re-Validierung in
``resolve_llm_config_for_agent`` braucht echte Typen, keine Strings).
Review-Befunde: unbekannte Keys und Einträge ohne ``=`` schlagen HART fehl —
sonst (a) deutet ``resolve_llm_params`` ein Dict aus lauter Fremd-Keys als
profil-gekeyte Form und der Override verschwindet lautlos, (b) verschluckt
das greedy ``nargs='+'`` den Task-String und der Agent läuft still mit dem
Default-Task.
"""

from __future__ import annotations

import pytest

from agent_system.agent_cli import parse_llm_params_args


class TestParseLlmParamsArgs:
    def test_none_and_empty(self):
        assert parse_llm_params_args(None) is None
        assert parse_llm_params_args([]) is None

    def test_typing(self):
        parsed = parse_llm_params_args([
            "thinking_level=max",
            "max_tokens=16384",
            "request_timeout=1.5",
            "include_thoughts=false",
            "thinking_budget=none",
        ])
        assert parsed == {
            "thinking_level": "max",
            "max_tokens": 16384,
            "request_timeout": 1.5,
            "include_thoughts": False,
            "thinking_budget": None,
        }
        assert isinstance(parsed["max_tokens"], int)
        assert isinstance(parsed["request_timeout"], float)
        assert parsed["include_thoughts"] is False

    def test_literal_none_survives_as_string(self):
        """``thinking_level=none`` ist ein WERT, kein „nicht gesetzt".

        Die generische Auto-Typisierung machte daraus Python ``None`` — und
        ein fehlendes Feld heißt beim Provider Default, bei DeepSeek also
        ``high``. Wer über die CLI das Denken abschalten wollte, kaufte
        stillschweigend das meiste davon (am Produktionspfad gemessen: der
        Request ging ohne ``reasoning``-Feld raus).

        Die Regel ist generisch: akzeptiert das Zielfeld die Schreibweise als
        ``Literal``, gewinnt der String. ``prompt_cache_marker_style`` hat
        dasselbe ``none`` und hing an derselben Falle.
        """
        assert parse_llm_params_args(["thinking_level=none"]) == {
            "thinking_level": "none"}
        assert parse_llm_params_args(["thinking_level=NONE"]) == {
            "thinking_level": "none"}
        assert parse_llm_params_args(["prompt_cache_marker_style=none"]) == {
            "prompt_cache_marker_style": "none"}

    def test_none_still_means_unset_where_no_literal_says_otherwise(self):
        """Die Ausnahme darf nicht auf Felder ohne solchen Wert ausstrahlen.

        ``temperature`` kennt kein ``"none"`` — dort ist ``none`` weiterhin
        „nicht gesetzt", sonst kaeme ein String in ein float-Feld.
        """
        assert parse_llm_params_args(["temperature=none"]) == {
            "temperature": None}
        assert parse_llm_params_args(["thinking_budget=none"]) == {
            "thinking_budget": None}

    def test_zero_stays_int_zero(self):
        # max_tokens=0 hat System-Semantik (Cap wird nicht gesendet) —
        # muss als int 0 ankommen, nicht als String/None.
        parsed = parse_llm_params_args(["max_tokens=0"])
        assert parsed == {"max_tokens": 0}
        assert parsed["max_tokens"] == 0
        assert isinstance(parsed["max_tokens"], int)

    def test_entry_without_equals_raises(self):
        # Der Klassiker: --llm-params VOR dem Task → nargs='+' frisst den
        # Task-String. Still überspringen hieße Default-Task — hart abbrechen.
        with pytest.raises(ValueError, match="expected KEY=VALUE"):
            parse_llm_params_args(["thinking_level=max", "Schreibe ein Kinderbuch"])

    def test_unknown_key_raises_with_field_list(self):
        # Tippfehler-Keys dürfen NICHT lautlos verschwinden
        # (resolve_llm_params würde das Dict als profil-gekeyt deuten).
        with pytest.raises(ValueError, match="unknown --llm-params key"):
            parse_llm_params_args(["temperatur=0.5"])
        try:
            parse_llm_params_args(["max_token=1000"])
        except ValueError as e:
            assert "max_tokens" in str(e)  # Feldliste hilft beim Korrigieren
        else:
            pytest.fail("expected ValueError for unknown key")

    def test_value_with_equals_sign(self):
        # Nur am ERSTEN '=' splitten (Werte dürfen '=' enthalten).
        parsed = parse_llm_params_args(["base_url=http://host/v1?key=abc"])
        assert parsed == {"base_url": "http://host/v1?key=abc"}
