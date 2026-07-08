"""Central Markdown → HTML rendering for the web UIs.

Single source of truth for turning agent-authored Markdown into the HTML the
frontends display: the main chat panel (via the ``markdown_formatter`` hook)
and the debate-forum panel both call :func:`markdown_to_html`, so the converter
config (extensions, Prism-compatible code classes, sanitisation) lives in ONE
place and cannot drift between callers.

Output is tuned for Prism.js: fenced code blocks get ``class="language-<lang>"``
so ``Prism.highlightAllUnder(...)`` can colour them on the client.

Callers keep their source text in its original Markdown form (e.g. the debate
forum stores raw Markdown in its DB and only converts on read) — this module
never persists anything.
"""
from __future__ import annotations

import logging
import re
import threading

logger = logging.getLogger(__name__)

# Reusable, config-keyed markdown.Markdown instances. Building one is not free,
# and the instance is stateful (accumulates reference/footnote definitions), so
# we cache per (tables, code) combination and reset() before every conversion.
# A lock serialises the reset()+convert() pair in case a caller ever drives this
# from a worker thread (the asyncio callers are already single-threaded).
_converters: dict[tuple[bool, bool], object | None] = {}
_lock = threading.Lock()

# Match a whole response wrapped in a ```markdown``` / ```md fence (LLMs sometimes
# wrap their entire Markdown answer in a code block).
_MD_WRAPPER_RE = re.compile(r"^```(?:markdown|md)\s*\n(.*?)\n```\s*$", re.DOTALL)


def extract_markdown_content(text: str) -> str:
    """Unwrap a response fully enclosed in a ```markdown``` / ```md fence.

    Returns the inner content, or the original text when there is no wrapper.
    """
    m = _MD_WRAPPER_RE.match(text.strip())
    return m.group(1) if m else text


def _get_converter(tables: bool, code: bool):
    """Return a cached markdown.Markdown for this config, or None if the
    ``markdown`` library is not installed."""
    key = (tables, code)
    if key in _converters:
        return _converters[key]
    try:
        import markdown
    except ImportError as e:  # pragma: no cover - depends on environment
        logger.warning("markdown library not available: %s", e)
        _converters[key] = None
        return None

    extensions: list[str] = []
    if tables:
        extensions.append("tables")
    if code:
        # fenced_code (NOT codehilite) → Prism-compatible ``language-`` classes.
        extensions.append("fenced_code")
    # nl2br: single newlines become <br> so list items / lines don't collapse.
    extensions.append("nl2br")

    converter = markdown.Markdown(
        extensions=extensions,
        extension_configs={"fenced_code": {"lang_prefix": "language-"}},
        output_format="html5",
    )
    _converters[key] = converter
    return converter


def _sanitize_html(html: str) -> str:
    """Strip the obvious XSS vectors from converted HTML.

    Mirrors the long-standing markdown_formatter behaviour: we trust the
    markdown library's structural output and only remove script tags, inline
    event handlers and ``javascript:`` URLs (a heavier solution would use
    bleach with a tag whitelist)."""
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'\son\w+\s*=\s*["\'][^"\']*["\']', "", html, flags=re.IGNORECASE)
    html = re.sub(r'href\s*=\s*["\']javascript:[^"\']*["\']', "", html, flags=re.IGNORECASE)
    return html


def _fix_list_formatting(html: str) -> str:
    """Rescue lists that LLMs wrote without the required blank line, so they
    render inline inside a single <p> (``Intro - a - b - c``)."""
    def _fix(match: re.Match) -> str:
        content = match.group(1)
        if content.count(" - ") >= 2 or content.count(" • ") >= 2:
            parts = re.split(r"\s[-•]\s", content)
            intro = parts[0].strip()
            items = [p.strip() for p in parts[1:] if p.strip()]
            out: list[str] = []
            if intro:
                out.append(f"<p>{intro}</p>")
            if items:
                out.append("<ul>")
                out.extend(f"<li>{it}</li>" for it in items)
                out.append("</ul>")
            return "".join(out)
        return match.group(0)

    return re.sub(r"<p>(.*?)</p>", _fix, html, flags=re.DOTALL)


def _remove_table_inline_styles(html: str) -> str:
    """Drop the ``style="text-align:..."`` the tables extension adds to cells,
    so CSS controls table styling."""
    html = re.sub(r'(<th[^>]*)\s+style="[^"]*"', r"\1", html)
    html = re.sub(r'(<td[^>]*)\s+style="[^"]*"', r"\1", html)
    return html


def markdown_to_html(
    text: str,
    *,
    tables: bool = True,
    code: bool = True,
    sanitize: bool = True,
) -> str | None:
    """Convert Markdown ``text`` to Prism-ready HTML.

    Returns the HTML string, or ``None`` when conversion is not possible
    (empty/non-string input, or the ``markdown`` library is unavailable) so
    callers can fall back to escaped plain text.
    """
    if not text or not isinstance(text, str):
        return None
    converter = _get_converter(tables, code)
    if converter is None:
        return None

    source = extract_markdown_content(text)
    with _lock:
        converter.reset()
        html = converter.convert(source)
    html = _fix_list_formatting(html)
    html = _remove_table_inline_styles(html)
    if sanitize:
        html = _sanitize_html(html)
    return html
