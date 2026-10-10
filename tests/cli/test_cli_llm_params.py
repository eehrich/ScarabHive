"""Tests for ``agent-cli run --llm-params KEY=VALUE ...``.

The values must be auto-typed (the LLMModelConfig re-validation in
``resolve_llm_config_for_agent`` needs real types, not strings).
Review findings: unknown keys and entries without ``=`` fail HARD —
otherwise (a) ``resolve_llm_params`` reads a dict made only of foreign keys as
the profile-keyed form and the override vanishes silently, (b) the greedy
``nargs='+'`` swallows the task string and the agent silently runs with the
default task.
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
        """``thinking_level=none`` is a VALUE, not "unset".

        The generic auto-typing turned it into Python ``None`` — and a missing
        field means the provider default, which for DeepSeek is ``high``.
        Whoever wanted to switch thinking off via the CLI silently bought the
        most of it (measured on the production path: the request went out
        without a ``reasoning`` field).

        The rule is generic: if the target field accepts the spelling as a
        ``Literal``, the string wins. ``prompt_cache_marker_style`` has the
        same ``none`` and was caught by the same trap.
        """
        assert parse_llm_params_args(["thinking_level=none"]) == {
            "thinking_level": "none"}
        assert parse_llm_params_args(["thinking_level=NONE"]) == {
            "thinking_level": "none"}
        assert parse_llm_params_args(["prompt_cache_marker_style=none"]) == {
            "prompt_cache_marker_style": "none"}

    def test_none_still_means_unset_where_no_literal_says_otherwise(self):
        """The exception must not spill over to fields without such a value.

        ``temperature`` has no ``"none"`` — there ``none`` still means
        "unset", otherwise a string would end up in a float field.
        """
        assert parse_llm_params_args(["temperature=none"]) == {
            "temperature": None}
        assert parse_llm_params_args(["thinking_budget=none"]) == {
            "thinking_budget": None}

    def test_zero_stays_int_zero(self):
        # max_tokens=0 has system semantics (the cap is not sent) —
        # must arrive as int 0, not as string/None.
        parsed = parse_llm_params_args(["max_tokens=0"])
        assert parsed == {"max_tokens": 0}
        assert parsed["max_tokens"] == 0
        assert isinstance(parsed["max_tokens"], int)

    def test_entry_without_equals_raises(self):
        # The classic: --llm-params BEFORE the task → nargs='+' eats the
        # task string. Skipping silently would mean the default task — abort hard.
        with pytest.raises(ValueError, match="expected KEY=VALUE"):
            parse_llm_params_args(["thinking_level=max", "Schreibe ein Kinderbuch"])

    def test_unknown_key_raises_with_field_list(self):
        # Typo keys must NOT vanish silently
        # (resolve_llm_params would read the dict as profile-keyed).
        with pytest.raises(ValueError, match="unknown --llm-params key"):
            parse_llm_params_args(["temperatur=0.5"])
        try:
            parse_llm_params_args(["max_token=1000"])
        except ValueError as e:
            assert "max_tokens" in str(e)  # the field list helps to correct it
        else:
            pytest.fail("expected ValueError for unknown key")

    def test_value_with_equals_sign(self):
        # Split only at the FIRST '=' (values may contain '=').
        parsed = parse_llm_params_args(["base_url=http://host/v1?key=abc"])
        assert parsed == {"base_url": "http://host/v1?key=abc"}
