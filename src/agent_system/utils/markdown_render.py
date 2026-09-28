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
from html import escape as html_escape
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# Reusable, config-keyed markdown.Markdown instances. Building one is not free,
# and the instance is stateful (accumulates reference/footnote definitions), so
# we cache per (tables, code, line_breaks) combination and reset() before every conversion.
# A lock serialises the reset()+convert() pair in case a caller ever drives this
# from a worker thread (the asyncio callers are already single-threaded).
_converters: dict[tuple[bool, bool, bool], object | None] = {}
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


def _get_converter(tables: bool, code: bool, line_breaks: bool = True):
    """Return a cached markdown.Markdown for this config, or None if the
    ``markdown`` library is not installed."""
    key = (tables, code, line_breaks)
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
    # nl2br: single newlines become <br> so list items / lines don't collapse -- right for a
    # model's answer, wrong for a document hard-wrapped at 76 columns (the help viewer).
    if line_breaks:
        extensions.append("nl2br")

    converter = markdown.Markdown(
        extensions=extensions,
        extension_configs={"fenced_code": {"lang_prefix": "language-"}},
        output_format="html5",
    )
    # The raw-HTML block parser never returns on input like "x<!-->" (measured
    # with Markdown 3.10: its comment handling rewinds to offset 1). That hung
    # the event loop on the reply and again on every reload of the session.
    # Raw HTML is the sanitiser's job anyway; without this step it arrives as
    # inline HTML or text.
    converter.preprocessors.deregister("html_block")
    _converters[key] = converter
    return converter


#: What Markdown produces. Raw HTML in a model's answer passes the markdown
#: library untouched, and a model's answer carries whatever a tool fetched --
#: so everything else is removed, not just what looks dangerous. No <img>: a
#: remote image is a request the reader's browser makes to an address the text
#: chose, which is how a prompt-injected page exfiltrates.
DEFAULT_ALLOWED_TAGS = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "strong", "em",
    "code", "pre", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th",
    "td", "a", "blockquote",
})

#: Attributes kept per tag; every other attribute goes.
_ALLOWED_ATTRS = {"a": {"href", "title"}, "code": {"class"},
                  "img": {"src", "alt", "title"}, "ol": {"start"}}
_URL_ATTRS = {"href", "src"}
_SAFE_SCHEMES = {"http", "https", "mailto"}
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_CODE_CLASS = re.compile(r"^language-[\w+#.\-]+$")

#: HTML element names. A disallowed one is removed; anything else in angle
#: brackets is a placeholder the model wrote ("DELTA_DOC=<id>", "B<xx>") and is
#: shown as text -- a browser would swallow it as an unknown element.
_HTML_ELEMENTS = frozenset("""
    a abbr address area article aside audio b base bdi bdo blockquote body br
    button canvas caption cite code col colgroup data datalist dd del details dfn
    dialog div dl dt em embed fieldset figcaption figure font footer form frame
    frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe img input ins
    kbd label legend li link main map mark marquee math menu meta meter nav
    noembed noframes noscript object ol optgroup option output p param picture
    plaintext pre progress q rp rt ruby s samp script search section select slot
    small source span strike strong style sub summary sup svg table tbody td
    template textarea tfoot th thead time title tr track tt u ul var video wbr xmp
""".split())

#: Removed together with their content, which is code, not prose. Any other
#: disallowed tag only loses the tag: its text stays, escaped -- a model that
#: writes "the <svg> element" unfenced must not lose the rest of its answer.
_DROP_CONTENT = frozenset({"script", "style"})


def _safe_url(value: str) -> bool:
    # Browsers ignore control characters and whitespace inside a scheme
    # ("java\tscript:"); the parser has already decoded character references.
    squeezed = re.sub(r"[\x00-\x20\x7f]", "", value)
    scheme = _SCHEME.match(squeezed)
    return scheme is None or scheme.group(1).lower() in _SAFE_SCHEMES


class _AllowlistSanitizer(HTMLParser):
    """Rebuilds the document from parsed tokens: only allowed tags with allowed,
    re-quoted attributes are written, all text is escaped. Nothing of the input
    reaches the output verbatim, so markup the browser would read differently
    from this parser cannot survive as markup."""

    def __init__(self, allowed_tags: frozenset[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.allowed_tags = allowed_tags
        self.out: list[str] = []
        # Text inside an open <script>/<style>, held back until its end tag.
        # Never closed, it was prose after all and comes back as text.
        self.dropped: list[str] | None = None
        # The allowed tags written and not closed yet. A model writes "the
        # <strong> tag" unfenced: left open, it would take over everything the
        # page shows after this text, so every tag is closed where its parent
        # closes, or at the end.
        self.open: list[str] = []

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], closed: bool) -> None:
        if tag in _DROP_CONTENT:
            if not closed and self.dropped is None:
                self.dropped = []
            return
        if self.dropped is not None:
            return
        if tag not in self.allowed_tags:
            if tag not in _HTML_ELEMENTS:
                self.out.append(html_escape(self.get_starttag_text() or "", quote=False))
            return
        # nl2br puts <br> between the rows of a raw HTML table; outside a cell
        # a browser hoists each one above the table as an empty line.
        if tag == "br" and "table" in self.open and not {"td", "th"} & set(self.open):
            return
        kept = []
        for name, value in attrs:
            if value is None or name not in _ALLOWED_ATTRS.get(tag, ()):
                continue
            if name in _URL_ATTRS and not _safe_url(value):
                continue
            if tag == "code" and name == "class" and not _CODE_CLASS.match(value):
                continue
            if name == "start" and not value.isdigit():
                continue
            kept.append(f' {name}="{html_escape(value, quote=True)}"')
        self.out.append(f"<{tag}{''.join(kept)}>")
        if not closed and tag not in _VOID_TAGS:
            self.open.append(tag)

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT:
            self.dropped = None
        elif self.dropped is not None:
            return
        elif tag in self.allowed_tags:
            if tag in self.open:  # an end tag nothing opened is dropped
                while (inner := self.open.pop()) != tag:
                    self.out.append(f"</{inner}>")
                self.out.append(f"</{tag}>")
        elif tag not in _HTML_ELEMENTS:
            self.out.append(html_escape(f"</{tag}>", quote=False))

    def handle_data(self, data):
        if self.dropped is not None:
            self.dropped.append(data)
        else:
            self.out.append(_escape_text(data, in_code="code" in self.open))

    def close(self):
        super().close()
        if self.dropped is not None:
            # HTMLParser keeps an unterminated script body in rawdata instead
            # of reporting it.
            rest = "".join(self.dropped) + self.rawdata
            self.out.append(_escape_text(rest))
            self.rawdata = ""
        self.dropped = None
        self.out.extend(f"</{tag}>" for tag in reversed(self.open))
        self.open = []


_VOID_TAGS = frozenset({"br", "hr", "img"})


def _escape_text(text: str, in_code: bool = False) -> str:
    # Close to the markdown library: '"' stays raw in prose and becomes
    # &quot; inside <code>. The library itself writes &quot; only in fenced
    # blocks and leaves inline code raw -- both render the same.
    # SubAgentMixin._parse_json_result reads either form.
    escaped = html_escape(text, quote=False)
    return escaped.replace('"', "&quot;") if in_code else escaped


def _sanitize_html(html: str, allowed_tags: frozenset[str] = DEFAULT_ALLOWED_TAGS) -> str:
    """Keep only allowed tags and attributes; escape everything else as text.

    ponytail: attribute values come back entity-decoded the way html.unescape
    does it, so a raw ``<a href="?a=1&copy=2">`` loses its ``&copy`` to ``©``.
    Markdown links escape ``&`` first and are unaffected; fix by re-reading the
    raw attribute text if raw-HTML links ever matter.
    """
    parser = _AllowlistSanitizer(allowed_tags)
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # html.parser raises AssertionError on "<![1" (3.12)
        # Also catches a bug in the handlers above -- hence the exception type.
        logger.warning("HTML sanitiser failed (%s: %s); showing the message as text",
                       type(exc).__name__, exc)
        return _escape_text(html)
    return "".join(parser.out)


_BLOCK_TAGS = r"(?:table|thead|tbody|tr|td|th|pre|ul|ol|li|blockquote|div|h[1-6])"
_WRAPPED_BLOCK = re.compile(rf"<p>(\s*<({_BLOCK_TAGS})\b.*?</\2>\s*)</p>", re.DOTALL)
_HAS_BLOCK_TAG = re.compile(rf"<{_BLOCK_TAGS}\b")


def _unwrap_raw_blocks(html: str) -> str:
    """A raw HTML block the model wrote (a <table>) arrives wrapped in <p> now
    that the html_block step is off; a browser would close the <p> in front of
    the table and leave an empty paragraph behind it."""
    return _WRAPPED_BLOCK.sub(r"\1", html)


# Indented by spaces: a tab is four columns, and an item indented that far under a paragraph continues it.
_LIST_ITEM = re.compile(r" {0,3}([-*+]|\d+[.)])\s")
#: What may interrupt a paragraph (CommonMark): a bullet, or a numbered list that starts at 1 --
#: "a priority outside 1 to\n10. A refusal ..." is a sentence, not a list that loses its 10.
_LIST_START = re.compile(r" {0,3}([-*+]|1[.)])\s")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


def _lists_after_paragraphs(source: str) -> str:
    """A document's list right under a paragraph line gets the blank line Python-Markdown needs.

    GitHub shows ``Intro\\n- a\\n- b`` as a paragraph and a list; Python-Markdown folds the
    items into the paragraph. Code fences are left as they are: one closes only with its own
    character, at least as long (a ``~~~`` line inside a backtick fence is code).
    """
    out: list[str] = []
    fence = ""
    previous = ""
    for line in re.split(r"\r\n|\r|\n", source):
        mark = _FENCE.match(line)
        if mark and not fence:
            fence = mark.group(1)
        elif mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence) \
                and not line[mark.end():].strip():
            fence = ""
        elif (not fence and _LIST_START.match(line) and previous.strip()
              and not _LIST_ITEM.match(previous) and not previous[:1].isspace()):
            out.append("")
        out.append(line)
        previous = line
    return "\n".join(out)


def _fix_list_formatting(html: str) -> str:
    """Rescue lists that LLMs wrote without the required blank line, so they
    render inline inside a single <p> (``Intro - a - b - c``)."""
    def _fix(match: re.Match) -> str:
        content = match.group(1)
        if _HAS_BLOCK_TAG.search(content):
            # Raw HTML (a table cell "Mo - Fr"), not a sentence with dashes.
            return match.group(0)
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
    allowed_tags: frozenset[str] | set[str] | None = None,
    line_breaks: bool = True,
) -> str | None:
    """Convert Markdown ``text`` to Prism-ready HTML.

    Returns the HTML string, or ``None`` when conversion is not possible
    (empty/non-string input, or the ``markdown`` library is unavailable) so
    callers can fall back to escaped plain text. ``allowed_tags`` narrows or
    widens :data:`DEFAULT_ALLOWED_TAGS` for the sanitiser. ``line_breaks=False`` reads text as a
    document rather than a chat answer: a single newline is a space, as Markdown has it, and the
    chat's list rescue stays off (a list under a paragraph line still shows, as on GitHub).
    """
    if not text or not isinstance(text, str):
        return None
    converter = _get_converter(tables, code, line_breaks)
    if converter is None:
        return None

    source = extract_markdown_content(text)
    if not line_breaks:
        source = _lists_after_paragraphs(source)
    with _lock:
        converter.reset()
        html = converter.convert(source)
    if line_breaks:  # the chat's rescue splits at every " - ": in a document that is a dash in an item
        html = _fix_list_formatting(html)
    html = _unwrap_raw_blocks(html)
    html = _remove_table_inline_styles(html)
    if sanitize:
        tags = DEFAULT_ALLOWED_TAGS if allowed_tags is None else frozenset(allowed_tags)
        html = _sanitize_html(html, tags)
    return html
