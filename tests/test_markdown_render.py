"""Tests for the central Markdown → HTML renderer (agent_system.utils.markdown_render).

Shared by the markdown_formatter hook (main chat panel) and the debate forum.
"""
import pytest

from agent_system.utils.markdown_render import (
    extract_markdown_content,
    markdown_to_html,
)

# All conversion tests need the optional ``markdown`` dependency.
pytest.importorskip("markdown")


def test_basic_elements():
    html = markdown_to_html("# Titel\n\nEin **fetter** Text mit `code`.")
    assert "<h1>Titel</h1>" in html
    assert "<strong>fetter</strong>" in html
    assert "<code>code</code>" in html


def test_lists():
    html = markdown_to_html("- eins\n- zwei\n- drei")
    assert html.count("<li>") == 3


def test_fenced_code_has_prism_language_class():
    html = markdown_to_html("```python\ndef f():\n    return 1\n```")
    # fenced_code with lang_prefix='language-' → Prism.highlightAllUnder colours it
    assert '<pre><code class="language-python">' in html


def test_tables_without_inline_styles():
    html = markdown_to_html("| A | B |\n|:--|--:|\n| 1 | 2 |")
    assert "<table>" in html and "<th" in html
    # the tables extension's inline text-align styles must be stripped
    assert "style=" not in html


def test_sanitizes_script_and_handlers():
    html = markdown_to_html('<script>alert(1)</script>Hallo **x**')
    assert "<script" not in html
    assert "<strong>x</strong>" in html


def test_sanitize_can_be_disabled():
    html = markdown_to_html("<script>x</script>", sanitize=False)
    assert "<script" in html


def test_empty_or_non_string_returns_none():
    assert markdown_to_html("") is None
    assert markdown_to_html(None) is None
    assert markdown_to_html(123) is None


def test_unwraps_markdown_code_fence():
    wrapped = "```markdown\n# Echt\n```"
    assert extract_markdown_content(wrapped) == "# Echt"
    # and end-to-end the wrapper is not rendered as a code block
    html = markdown_to_html(wrapped)
    assert "<h1>Echt</h1>" in html


def test_reset_between_calls_no_reference_leak():
    """The shared converter must reset() between calls so a reference-link
    definition in one message doesn't leak into the next."""
    first = markdown_to_html("[x][ref]\n\n[ref]: https://example.com")
    assert "https://example.com" in first
    # A later message that references [ref] without defining it must NOT resolve.
    second = markdown_to_html("[y][ref]")
    assert "https://example.com" not in second
