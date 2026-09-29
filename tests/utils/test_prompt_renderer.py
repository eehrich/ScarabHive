"""Tests for prompt_renderer module.

Prompts are markdown-only (whole file = system_prompt, Jinja2-rendered). The
former multi-section YAML format was removed — a .yaml/.yml path now raises a
clear error with migration guidance.
"""
from __future__ import annotations

import time

import pytest
from pathlib import Path

from agent_system.utils.prompt_renderer import (
    render_prompts,
    strip_prompt_comments,
    _is_text_template,
    _render_text_template,
    get_datetime_context,
)


class TestIsTextTemplate:
    """Tests for _is_text_template() file extension detection."""

    @pytest.mark.parametrize("path,expected", [
        # Text/markdown files
        ("config/prompts/agent.md", True),
        ("config/prompts/agent.txt", True),
        ("config/prompts/agent.markdown", True),
        ("AGENT.MD", True),  # Case insensitive
        ("path/to/file.TXT", True),
        ("readme.MARKDOWN", True),
        
        # YAML files (should return False)
        ("config/prompts/system_prompt.yaml", False),
        ("config/prompts/agent.yml", False),
        ("config/prompts/agent.YAML", False),
        
        # Other extensions (should return False)
        ("config/prompts/agent.json", False),
        ("config/prompts/agent.py", False),
        ("config/prompts/agent.html", False),
    ])
    def test_file_extension_detection(self, path: str, expected: bool) -> None:
        """Test that file extensions are correctly detected."""
        assert _is_text_template(path) == expected


class TestRenderTextTemplate:
    """Tests for _render_text_template() direct text rendering."""

    def test_simple_markdown_template(self, tmp_path: Path) -> None:
        """Test rendering a simple markdown file without variables."""
        md_file = tmp_path / "simple.md"
        md_file.write_text("# Agent Prompt\n\nYou are a helpful assistant.", encoding="utf-8")
        
        result = _render_text_template(str(md_file), {})
        
        assert "system_prompt" in result
        assert result["system_prompt"] == "# Agent Prompt\n\nYou are a helpful assistant."

    def test_markdown_with_jinja_variables(self, tmp_path: Path) -> None:
        """Test rendering markdown with Jinja2 template variables."""
        md_file = tmp_path / "with_vars.md"
        md_file.write_text(
            "# {{ agent_name }} Agent\n\n"
            "Current date: {{ current_date }}\n"
            "You are {{ role }}.",
            encoding="utf-8"
        )
        
        context = {
            "agent_name": "Research",
            "current_date": "2025-12-15",
            "role": "a research assistant",
        }
        result = _render_text_template(str(md_file), context)
        
        assert result["system_prompt"] == (
            "# Research Agent\n\n"
            "Current date: 2025-12-15\n"
            "You are a research assistant."
        )

    def test_empty_markdown_file(self, tmp_path: Path) -> None:
        """Test rendering an empty markdown file."""
        md_file = tmp_path / "empty.md"
        md_file.write_text("", encoding="utf-8")
        
        result = _render_text_template(str(md_file), {})
        
        assert result["system_prompt"] == ""

    def test_markdown_with_invalid_jinja_syntax(self, tmp_path: Path) -> None:
        """Test graceful fallback when Jinja2 syntax is invalid."""
        md_file = tmp_path / "invalid.md"
        md_file.write_text("Content with {{ invalid syntax", encoding="utf-8")
        
        # Should not raise, should fallback to raw content
        result = _render_text_template(str(md_file), {})
        
        assert "system_prompt" in result
        assert "{{ invalid syntax" in result["system_prompt"]


class TestHtmlCommentsNeverReachTheModel:
    """Agents that edit a prompt leave notes in <!-- --> and rely on the model
    never seeing them. Stripped from the template source, not the rendered
    text: what a variable brings in stays as it is."""

    def test_comments_are_gone_and_leave_no_blank_lines(self, tmp_path: Path) -> None:
        md_file = tmp_path / "agent.md"
        md_file.write_text(
            "# Agent\n"
            "<!-- why this rule exists: book 77 -->\n"
            "Rule one. <!-- inline note --> Rule two.\n"
            "  <!--\n  a note over\n  several lines\n  -->\n"
            "End.",
            encoding="utf-8")

        result = render_prompts(str(md_file), {}, auto_datetime=False)

        assert result["system_prompt"] == "# Agent\nRule one.  Rule two.\nEnd."

    def test_prose_between_two_comments_survives(self, tmp_path: Path) -> None:
        md_file = tmp_path / "agent.md"
        md_file.write_text("<!-- a --> keep me\n<!-- b -->\nkeep me too", encoding="utf-8")

        result = render_prompts(str(md_file), {}, auto_datetime=False)

        assert result["system_prompt"] == " keep me\nkeep me too"

    def test_an_included_partial_is_stripped_too(self, tmp_path: Path) -> None:
        (tmp_path / "partial.md").write_text("<!-- partial note -->\nShared rule.", encoding="utf-8")
        md_file = tmp_path / "agent.md"
        md_file.write_text('Own rule.\n{% include "partial.md" %}', encoding="utf-8")

        result = render_prompts(str(md_file), {}, auto_datetime=False)

        assert result["system_prompt"] == "Own rule.\nShared rule."

    def test_text_from_a_variable_keeps_its_comments(self, tmp_path: Path) -> None:
        md_file = tmp_path / "agent.md"
        md_file.write_text("<!-- note -->\nChapter:\n{{ chapter }}", encoding="utf-8")

        result = render_prompts(str(md_file), {"chapter": "a <!-- b --> c"}, auto_datetime=False)

        assert result["system_prompt"] == "Chapter:\na <!-- b --> c"

    def test_the_raw_fallback_is_stripped_as_well(self, tmp_path: Path) -> None:
        # A broken template ships unrendered -- still without its notes.
        md_file = tmp_path / "agent.md"
        md_file.write_text("<!-- note -->\nContent with {{ invalid syntax", encoding="utf-8")

        result = render_prompts(str(md_file), {}, auto_datetime=False)

        assert "note" not in result["system_prompt"]
        assert "{{ invalid syntax" in result["system_prompt"]

    def test_a_second_comment_after_prose_keeps_the_line_break(self) -> None:
        source = "Rule one. <!-- a --> <!-- b -->\nRule two."
        assert strip_prompt_comments(source) == "Rule one.  \nRule two."

    def test_crlf_comment_lines_leave_no_blank_lines(self) -> None:
        # Jinja's loader reads bytes, so an include still carries its \r\n here.
        assert strip_prompt_comments("A\r\n<!-- note -->\r\nB") == "A\r\nB"

    def test_an_unclosed_comment_keeps_the_rest_and_warns(self, caplog) -> None:
        with caplog.at_level("WARNING", logger="agent_system.utils.prompt_renderer"):
            result = strip_prompt_comments("keep\n<!-- open\nrest", "agent.md")

        assert result == "keep\n<!-- open\nrest"
        assert "agent.md" in caplog.text

    def test_many_unclosed_openers_stay_linear(self) -> None:
        # Rescanning to the end from every opener is quadratic: at 1.4 MB a
        # str.find loop takes ~3.6 s (a regex minutes), one scan ~0.2 ms.
        source = "<!-- yyyyyyyyyyyyyyyyyyyyyyyyyyyyyy\n" * 40000
        started = time.perf_counter()
        strip_prompt_comments(source)
        assert time.perf_counter() - started < 1.0

    def test_many_comments_on_one_line_stay_linear(self) -> None:
        # Looking for the line's start and end per comment is quadratic on a
        # line without newlines: 440 KB took ~2.3 s that way.
        source = "x<!-- a -->" * 80000
        started = time.perf_counter()
        assert strip_prompt_comments(source) == "x" * 80000
        assert time.perf_counter() - started < 1.0


class TestYamlRejected:
    """YAML prompt templates are no longer supported — they must fail loudly
    with a migration hint rather than silently embedding raw YAML."""

    def test_yaml_path_raises(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "old.yaml"
        yaml_file.write_text("system_prompt: |\n  hi\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no longer supported"):
            render_prompts(str(yaml_file), {}, auto_datetime=False)

    def test_yml_path_raises(self, tmp_path: Path) -> None:
        yml_file = tmp_path / "old.yml"
        yml_file.write_text("system_prompt: hi\n", encoding="utf-8")
        with pytest.raises(ValueError, match="markdown"):
            render_prompts(str(yml_file), {}, auto_datetime=False)


class TestRenderPromptsMarkdown:
    """Tests for render_prompts() with markdown/text templates (new feature)."""

    def test_markdown_template(self, tmp_path: Path) -> None:
        """Test rendering a markdown template via render_prompts()."""
        md_file = tmp_path / "agent.md"
        md_file.write_text(
            "# Research Agent\n\n"
            "You are a research assistant that helps find information.\n\n"
            "## Guidelines\n"
            "- Be concise\n"
            "- Cite sources\n",
            encoding="utf-8"
        )
        
        result = render_prompts(str(md_file), {}, auto_datetime=False)
        
        assert "system_prompt" in result
        assert "# Research Agent" in result["system_prompt"]
        assert "## Guidelines" in result["system_prompt"]
        assert len(result) == 1  # Only system_prompt section

    def test_txt_template(self, tmp_path: Path) -> None:
        """Test rendering a .txt template."""
        txt_file = tmp_path / "agent.txt"
        txt_file.write_text(
            "You are a coding assistant.\n"
            "Help users write clean, efficient code.",
            encoding="utf-8"
        )
        
        result = render_prompts(str(txt_file), {}, auto_datetime=False)
        
        assert "system_prompt" in result
        assert "coding assistant" in result["system_prompt"]

    def test_markdown_with_jinja_and_datetime(self, tmp_path: Path) -> None:
        """Test markdown template with both custom vars and auto datetime."""
        md_file = tmp_path / "agent.md"
        md_file.write_text(
            "# {{ agent_name }} Agent\n\n"
            "Current step: {{ current_step }}/{{ max_steps }}\n"
            "Date: {{ current_date }}\n",
            encoding="utf-8"
        )
        
        context = {
            "agent_name": "Writer",
            "current_step": 5,
            "max_steps": 20,
        }
        result = render_prompts(str(md_file), context, auto_datetime=True)
        
        assert "# Writer Agent" in result["system_prompt"]
        assert "Current step: 5/20" in result["system_prompt"]
        assert "Date: 20" in result["system_prompt"]  # 20XX-XX-XX

    def test_markdown_uppercase_extension(self, tmp_path: Path) -> None:
        """Test that uppercase .MD extension works."""
        md_file = tmp_path / "agent.MD"
        md_file.write_text("You are an assistant.", encoding="utf-8")
        
        result = render_prompts(str(md_file), {}, auto_datetime=False)
        
        assert result["system_prompt"] == "You are an assistant."

    def test_markdown_extension_full(self, tmp_path: Path) -> None:
        """Test .markdown extension."""
        md_file = tmp_path / "agent.markdown"
        md_file.write_text("Full markdown extension test.", encoding="utf-8")
        
        result = render_prompts(str(md_file), {}, auto_datetime=False)
        
        assert result["system_prompt"] == "Full markdown extension test."


class TestGetDatetimeContext:
    """Tests for get_datetime_context() helper."""

    def test_datetime_context_keys(self) -> None:
        """Test that all expected keys are present."""
        ctx = get_datetime_context()
        
        expected_keys = [
            "current_date",
            "current_time",
            "current_datetime",
            "current_timezone",
            "current_location",
            "tomorrow_date",
            "current_weekday",
            "current_month",
            "current_year",
            "unix_timestamp",
        ]
        for key in expected_keys:
            assert key in ctx, f"Missing key: {key}"

    def test_datetime_context_with_timezone(self) -> None:
        """Test datetime context with specific timezone."""
        ctx = get_datetime_context(timezone_str="Europe/Berlin", location="Berlin")
        
        assert ctx["current_timezone"] == "Europe/Berlin"
        assert ctx["current_location"] == "Berlin"

    def test_datetime_context_utc_default(self) -> None:
        """Test datetime context defaults to UTC."""
        ctx = get_datetime_context()
        
        assert ctx["current_timezone"] == "UTC"


class TestEdgeCases:
    """Edge case and integration tests."""

    def test_file_not_found(self, tmp_path: Path) -> None:
        """Test that FileNotFoundError is raised for a missing markdown file."""
        with pytest.raises(FileNotFoundError):
            render_prompts(str(tmp_path / "nonexistent.md"), {})

    def test_unicode_content(self, tmp_path: Path) -> None:
        """Test templates with unicode content."""
        md_file = tmp_path / "unicode.md"
        md_file.write_text(
            "# Spécial Àgent 🤖\n\n"
            "Ümlauts: äöü\n"
            "Symbols: ★ ☆ ♠ ♣ ♥ ♦\n"
            "Japanese: こんにちは\n",
            encoding="utf-8"
        )
        
        result = render_prompts(str(md_file), {}, auto_datetime=False)
        
        assert "Spécial Àgent 🤖" in result["system_prompt"]
        assert "äöü" in result["system_prompt"]
        assert "こんにちは" in result["system_prompt"]
