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


class TestAllowlistSanitizer:
    """The regex filter only removed event handlers whose value stood in
    quotes: ``<img src=x onerror=alert(1)>`` went through to innerHTML. Model
    answers carry whatever a tool fetched, so the output is now rebuilt from
    an allowlist instead."""

    @pytest.mark.parametrize("payload", [
        "<img src=x onerror=alert(1)>",
        "<svg onload=alert(1)>",
        '<a href="x" onclick=alert(1)>link</a>',
        "<a href=javascript:alert(1)>link</a>",
        '<a href="java\tscript:alert(1)">link</a>',
        '<a href="&#106;avascript:alert(1)">link</a>',
        '<a href="data:text/html,x">link</a>',
        '<iframe src="https://evil.example"></iframe>',
        '<div style="background:url(javascript:alert(1))">x</div>',
        '<code class="language-py onmouseover=alert(1)">x</code>',
    ])
    def test_no_active_markup_survives(self, payload):
        html = markdown_to_html(f"Text {payload} end")

        lowered = html.lower()
        for marker in ("<img", "<svg", "<iframe", " on", "javascript:", "data:", "style="):
            assert marker not in lowered, (marker, html)

    @pytest.mark.parametrize("href", [
        "javascript:alert(1)",
        "java\tscript:alert(1)",
        " JAVASCRIPT:alert(1)",
        "&#106;avascript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html,x",
    ])
    def test_a_link_to_a_script_loses_its_target(self, href):
        # Browsers drop tabs and newlines inside a scheme, so "java\tscript:"
        # runs even though the string "javascript:" never appears.
        html = markdown_to_html(f'Text <a href="{href}">link</a> end')

        assert "href" not in html, html
        assert "link" in html

    def test_a_web_link_keeps_its_target(self):
        html = markdown_to_html('<a href="https://example.com/a?b=1&c=2">x</a> [y](/rel)')

        assert 'href="https://example.com/a?b=1&amp;c=2"' in html
        assert 'href="/rel"' in html

    def test_markdown_output_is_unchanged(self):
        source = ('# T\n\nA **b** `c` [l](https://example.com "t").\n\n'
                  "| A |\n|---|\n| 1 |\n\n```python\nx = '<b>' + \"q\"\n```\n\n> q")

        assert markdown_to_html(source) == markdown_to_html(source, sanitize=False)

    def test_a_placeholder_in_angle_brackets_stays_visible(self):
        html = markdown_to_html("Reicht er DELTA_DOC=<id> weiter?")

        assert "DELTA_DOC=&lt;id&gt;" in html

    def test_an_unclosed_script_does_not_swallow_the_answer(self):
        html = markdown_to_html("Erwähnt <script> im Text und schreibt weiter")

        assert "schreibt weiter" in html
        assert "<script" not in html

    def test_a_tag_named_in_prose_is_closed_within_its_paragraph(self):
        # the page puts several messages side by side: an open tag would take over all that follow
        html = markdown_to_html("Use the <strong> tag.\n\nWrap it in a <table> element.\n\nThen </em> stray.")

        assert html == ("<p>Use the <strong> tag.</strong></p>\n"
                        "<p>Wrap it in a <table> element.</table></p>\n"
                        "<p>Then  stray.</p>")

    def test_a_tag_left_open_at_the_end_is_closed(self):
        from agent_system.utils.markdown_render import _sanitize_html

        assert _sanitize_html("<ul><li><code>x") == "<ul><li><code>x</code></li></ul>"

    def test_the_allowlist_can_be_narrowed(self):
        html = markdown_to_html("| A |\n|---|\n| 1 |\n\n**fett**",
                                allowed_tags={"p", "strong"})

        assert "<table" not in html
        assert "<strong>fett</strong>" in html

    @pytest.mark.parametrize("text", ["x<!-->", "a <!-- --!> b", "Siehe `a;<!-->` hier"])
    def test_a_broken_comment_does_not_hang_the_renderer(self, text):
        # Markdown 3.10's raw-HTML block parser never returns on these; the
        # API event loop hung on the reply and on every reload of the session.
        # In a subprocess: a hung render holds the module lock, and in-process
        # it would stall every later test instead of failing this one.
        import subprocess
        import sys
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src"
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from agent_system.utils.markdown_render import markdown_to_html;"
                "print(markdown_to_html(sys.argv[2]))")
        try:
            done = subprocess.run([sys.executable, "-c", code, str(src), text],
                                  capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail("markdown_to_html did not return")

        assert done.returncode == 0, done.stderr
        assert "&lt;!--" in done.stdout

    def test_a_raw_html_table_stays_a_table(self):
        html = markdown_to_html(
            "Vorher\n\n<table>\n<tr><td>Mo - Fr</td><td>9 - 17 - 18</td></tr>\n</table>\n\nNachher")

        assert "<td>Mo - Fr</td>" in html and "<td>9 - 17 - 18</td>" in html
        assert "<ul>" not in html
        assert "<p><table>" not in html
        assert "<br>" not in html.split("<table>")[1].split("</table>")[0]

    def test_markup_the_parser_rejects_comes_back_as_text(self):
        """"<![1 ..." made html.parser raise up to Python 3.13; from 3.14 it reads it as a comment,
        like a browser, and the comment is dropped. Either way no tag in it comes out live -- and
        where the parser rejects it, the whole message comes back as text."""
        from html.parser import HTMLParser

        from agent_system.utils.markdown_render import _sanitize_html

        markup = '<p>a</p><![1 <img src=x onerror=alert(1)>'
        html = _sanitize_html(markup)

        assert "<img" not in html
        try:
            parser = HTMLParser()
            parser.feed(markup)
            parser.close()  # as the sanitiser does: a version may reject it only there
        except Exception:
            assert "<p>" not in html and "&lt;img" in html
        else:
            assert html == "<p>a</p>"

    def test_a_sanitiser_that_fails_shows_the_message_as_text(self, monkeypatch):
        """Whatever makes the parse fail -- a parser that rejects the markup, a bug in a handler --
        the message is shown as text, not half-sanitised."""
        from agent_system.utils import markdown_render

        def breaks(self, tag, attrs):
            raise RuntimeError("a handler bug")

        monkeypatch.setattr(markdown_render._AllowlistSanitizer, "handle_starttag", breaks)

        html = markdown_render._sanitize_html('<p>a <img src=x onerror=alert(1)></p>')

        assert "<" not in html.replace("&lt;", "")
        assert "&lt;img" in html
