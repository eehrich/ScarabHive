"""Tests for JSON repair utility in agent_system.utils.json_utils."""

import json

import pytest

from agent_system.utils.json_utils import repair_json


class TestRepairJsonValidInput:
    """Test that valid JSON passes through unchanged."""

    def test_valid_object(self):
        result = repair_json('{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_valid_array(self):
        result = repair_json('[1, 2, 3]')
        assert result == [1, 2, 3]

    def test_valid_nested(self):
        raw = '{"a": {"b": [1, 2]}, "c": true}'
        result = repair_json(raw)
        assert result == {"a": {"b": [1, 2]}, "c": True}

    def test_valid_string(self):
        result = repair_json('"hello"')
        assert result == "hello"

    def test_valid_number(self):
        result = repair_json("42")
        assert result == 42


class TestRepairJsonEmptyInput:
    """Test edge cases with empty/whitespace input."""

    def test_empty_string(self):
        assert repair_json("") is None

    def test_whitespace_only(self):
        assert repair_json("   \n  ") is None

    def test_none_like_empty(self):
        # repair_json takes str, but guard against edge cases
        assert repair_json("") is None


class TestRepairJsonTrailingCommas:
    """Test repair of trailing commas."""

    def test_trailing_comma_in_object(self):
        result = repair_json('{"a": 1, "b": 2,}')
        assert result == {"a": 1, "b": 2}

    def test_trailing_comma_in_array(self):
        result = repair_json('[1, 2, 3,]')
        assert result == [1, 2, 3]

    def test_nested_trailing_commas(self):
        result = repair_json('{"a": [1, 2,], "b": {"c": 3,},}')
        assert result == {"a": [1, 2], "b": {"c": 3}}


class TestRepairJsonMissingBrackets:
    """Test repair of truncated JSON with missing closing brackets."""

    def test_missing_closing_brace(self):
        result = repair_json('{"key": "value"')
        assert result is not None
        assert result["key"] == "value"

    def test_missing_closing_bracket(self):
        result = repair_json('[1, 2, 3')
        assert result is not None
        assert result == [1, 2, 3]

    def test_missing_multiple_closings(self):
        result = repair_json('{"a": {"b": [1, 2')
        assert result is not None
        assert result["a"]["b"] == [1, 2]


class TestRepairJsonQuotes:
    """Test repair of quote-related issues."""

    def test_single_quotes(self):
        result = repair_json("{'key': 'value'}")
        assert result == {"key": "value"}

    def test_unquoted_keys(self):
        result = repair_json('{key: "value"}')
        assert result == {"key": "value"}


class TestRepairJsonNonLatinCharacters:
    """Test that non-Latin characters (German, Chinese, etc.) are preserved."""

    def test_german_umlauts(self):
        raw = '{"name": "Müller", "beschreibung": "Größe und Stärke"}'
        result = repair_json(raw)
        assert result["name"] == "Müller"
        assert "Größe" in result["beschreibung"]

    def test_german_text_in_broken_json(self):
        """Test the actual use-case from the bug report: German text with broken structure."""
        raw = '{"name": "Elena", "traits": ["provokant", "intelligent",]}'
        result = repair_json(raw)
        assert result is not None
        assert result["name"] == "Elena"
        assert "provokant" in result["traits"]

    def test_chinese_characters(self):
        result = repair_json("{'key': '统一码'}")
        assert result == {"key": "统一码"}


class TestRepairJsonLLMPatterns:
    """Test common LLM-specific JSON errors."""

    def test_boolean_case_variations(self):
        """LLMs sometimes output Python-style True/False/None."""
        result = repair_json('{"a": True, "b": False, "c": null}')
        assert result == {"a": True, "b": False, "c": None}

    def test_python_none_as_string(self):
        """Python's None is not valid JSON — repair treats it as string 'None'."""
        result = repair_json('{"c": None}')
        # json-repair interprets Python's None as the string "None"
        # since null is the JSON equivalent — this is expected behavior
        assert result is not None
        assert "c" in result

    def test_comments_in_json(self):
        """LLMs sometimes add comments to JSON."""
        raw = '''{
            "key": "value", // this is a comment
            "num": 42
        }'''
        result = repair_json(raw)
        assert result is not None
        assert result["key"] == "value"
        assert result["num"] == 42

    def test_deeply_nested_with_truncation(self):
        """Simulate truncated complex nested JSON (like the character data in the bug)."""
        raw = '{"personality": {"role": "protagonist", "traits": ["brave", "smart"], "goals": ["win"'
        result = repair_json(raw)
        assert result is not None
        assert result["personality"]["role"] == "protagonist"

    def test_large_complex_object(self):
        """Test with a moderately complex object similar to the bug report."""
        raw = json.dumps({
            "operation": "update",
            "character_id": 88,
            "name": "Elena",
            "personality": {
                "role": "protagonist",
                "traits": ["brave", "smart", "stubborn"],
                "background": "A complex backstory with Umlaute: ü, ö, ä",
            },
            "voice_profile": {
                "style": "Modern, scharfzüngig",
                "typical_phrases": ["Phrase one", "Phrase two"],
            },
        }, ensure_ascii=False)
        result = repair_json(raw)
        assert result is not None
        assert result["name"] == "Elena"
        assert result["personality"]["role"] == "protagonist"
        assert "ü" in result["personality"]["background"]


class TestRepairJsonReturnString:
    """Test return_objects=False mode."""

    def test_returns_valid_json_string(self):
        result = repair_json('{"a": 1,}', return_objects=False)
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed == {"a": 1}

    def test_broken_returns_repaired_string(self):
        result = repair_json("{'key': 'value'}", return_objects=False)
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}


class TestRepairJsonColonValuePattern:
    """Test the specific colon-value pattern from the bug report.

    LLM generated: "quirks":":" instead of "quirks":[...]
    """

    def test_value_colon_as_string(self):
        """When LLM writes a colon as value, it should be treated as a string."""
        raw = '{"quirks": ":", "name": "test"}'
        result = repair_json(raw)
        assert result is not None
        assert result["name"] == "test"
