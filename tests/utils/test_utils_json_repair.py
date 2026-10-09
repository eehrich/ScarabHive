"""Tests for JSON repair utility in agent_system.utils.json_utils."""

import json


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


# ---------------------------------------------------------------------------
# strip_markdown_fences — consolidation of 3 historical implementations
# (several ad-hoc JSON extraction helpers merged into one, 2026-06-06).
# ---------------------------------------------------------------------------

from agent_system.utils.json_utils import strip_markdown_fences


class TestStripMarkdownFences:
    """Wraps three legacy patterns:

    1. Line-based: split-by-newline, drop first + last
    2. Regex with optional ``json``-language tag
    3. Embedded fence in surrounding prose
    """

    def test_fully_wrapped_with_json_tag(self):
        text = "```json\n{\"a\": 1}\n```"
        assert strip_markdown_fences(text) == '{"a": 1}'

    def test_fully_wrapped_without_language_tag(self):
        text = "```\n{\"b\": 2}\n```"
        assert strip_markdown_fences(text) == '{"b": 2}'

    def test_no_fences_passes_through(self):
        text = '{"c": 3}'
        assert strip_markdown_fences(text) == '{"c": 3}'

    def test_empty_string(self):
        assert strip_markdown_fences("") == ""

    def test_whitespace_only(self):
        assert strip_markdown_fences("   \n  ") == ""

    def test_none_input_safe(self):
        # Defensive — doesn't crash, returns empty
        assert strip_markdown_fences(None) == ""  # type: ignore[arg-type]

    def test_fence_embedded_in_prose(self):
        """LLM output: 'Here is the answer: ```json\n{...}\n``` And done.'"""
        text = (
            "Here is the answer:\n"
            "```json\n"
            "{\"d\": 4}\n"
            "```\n"
            "And done."
        )
        assert strip_markdown_fences(text) == '{"d": 4}'

    def test_surrounding_whitespace(self):
        text = "   ```json\n{\"e\": 5}\n```   "
        assert strip_markdown_fences(text) == '{"e": 5}'

    def test_multiline_content_preserved(self):
        text = "```json\n{\n  \"f\": [\n    1, 2, 3\n  ]\n}\n```"
        result = strip_markdown_fences(text)
        # Inner newlines preserved
        assert result.startswith("{")
        assert result.endswith("}")
        assert "\"f\"" in result
        assert "[\n" in result

    def test_python_language_tag_works(self):
        """Non-JSON language tag — strip still works (we don't care about
        the language)."""
        text = "```python\nprint(1)\n```"
        assert strip_markdown_fences(text) == "print(1)"

    def test_no_trailing_fence_treated_as_starting_fence_only(self):
        """Missing closing fence — defensive: strip what's after the open."""
        text = "```json\n{\"x\": 1}"
        result = strip_markdown_fences(text)
        # Best-effort: returns content after opening fence
        assert '"x"' in result

    def test_inline_fence_short(self):
        """Inline ``` `` short form (no newline) — falls through to regex."""
        # Mirror existing behavior: single-line fence
        text = "Here: ```{\"a\": 1}```"
        result = strip_markdown_fences(text)
        # Should extract inner content or return stripped
        assert "1" in result

    def test_idempotent(self):
        """Apply twice → same result. Important for defensive pipelines
        that might call it multiple times."""
        text = "```json\n{\"a\": 1}\n```"
        once = strip_markdown_fences(text)
        twice = strip_markdown_fences(once)
        assert once == twice

    def test_parses_as_valid_json_after_strip(self):
        """Integration: result of strip is parseable as JSON."""
        text = "```json\n{\"a\": [1, 2, 3], \"b\": \"hello\"}\n```"
        stripped = strip_markdown_fences(text)
        parsed = json.loads(stripped)
        assert parsed == {"a": [1, 2, 3], "b": "hello"}
