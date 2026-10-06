"""The help guides in the terminal chat: ``/help <topic>`` opens a node and lets the person browse.

The help library lays every node out (agent_system/ui/help.py, ``Library.page``); this module only
draws it for a terminal -- the counterpart of the web viewer static/kit/guide.js, with its buttons as
keys: a link number, ``b`` Retrace, ``n``/``p`` Browse, ``c`` Contents, ``i`` Index, ``h`` Help,
``/text`` a search, ``q`` or Enter back to the chat.

Nothing here talks to the agent: what the viewer shows never enters the conversation.
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Iterable

from ..ui.amigaguide import BULLETS
from ..ui.help import MANUAL, PLUGIN_INDEX, VIEWER_HELP, Library
from .chat import display_width

logger = logging.getLogger(__name__)

#: A run of text and the SGR parameters it is drawn with (empty: plain).
Piece = tuple[str, tuple[str, ...]]

SEARCH_LIMIT = 8
#: Topics that name a guide instead of asking the search.
SHORTCUTS = {"manual": {"guide": MANUAL, "node": "main"}, "plugins": {"guide": PLUGIN_INDEX, "node": "main"}}
#: Where the buttons lead before a node has said so (a search as the first page): the manual, as in guide.js.
MANUAL_NAV = {"contents": {"guide": MANUAL, "node": "main"}, "index": {"guide": MANUAL, "node": "index"},
              "help": {"guide": VIEWER_HELP[0], "node": VIEWER_HELP[1]}}
#: One line of 79 columns: the viewer's buttons (guide.js) as keys.
KEYS = "<n> link  b back  n/p browse  c contents  i index  h help  /text search  q chat"
MORE = "-- more: Enter = next page, q = stop -- "

#: A span's style classes (amigaguide._Layout._classes) as SGR parameters; the default pens draw nothing.
SGR = {"b": "1", "i": "3", "u": "4", "s": "9", "tt": "32",
       "fg-shine": "97", "fg-shadow": "90", "fg-fill": "94", "fg-filltext": "37", "fg-background": "2",
       "fg-highlight": "93", "bg-text": "40", "bg-shine": "47", "bg-shadow": "100", "bg-fill": "44",
       "bg-filltext": "47", "bg-highlight": "43"}
LINK = ("4", "36")
DIM = ("90",)
BOLD = ("1",)
HEADING = {"h1": ("1", "36"), "h2": ("1", "36"), "h3": ("1",)}
#: Headings are underlined in the text itself, so they stand out without colours too.
UNDERLINE = {"h1": "=", "h2": "-"}
_BLANKS = re.compile(r"[ \t\n]+")


def _width(pieces: Iterable[Piece]) -> int:
    return sum(display_width(text) for text, _ in pieces)


def _join(pieces: list[Piece], ansi: bool) -> str:
    """Pieces as one printable line; neighbours of one style share their escape codes."""
    merged: list[Piece] = []
    for text, codes in pieces:
        if merged and merged[-1][1] == codes:
            merged[-1] = (merged[-1][0] + text, codes)
        else:
            merged.append((text, codes))
    return "".join(f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if ansi and codes else text
                   for text, codes in merged).rstrip()


def _words(pieces: list[Piece]) -> list[list[Piece]]:
    """Pieces cut at blanks; a word may span several styles ("(" + code + ")")."""
    words: list[list[Piece]] = []
    word: list[Piece] = []
    for text, codes in pieces:
        for part in re.split(f"({_BLANKS.pattern})", text):
            if _BLANKS.fullmatch(part):
                if word:
                    words.append(word)
                    word = []
            elif part:
                word.append((part, codes))
    return words + [word] if word else words


def _chop(word: list[Piece], width: int) -> list[list[Piece]]:
    """A word wider than the line, cut into parts that fit."""
    parts: list[list[Piece]] = []
    part: list[Piece] = []
    used = 0
    for text, codes in word:
        for char in text:
            size = display_width(char)
            if part and used + size > width:
                parts.append(part)
                part, used = [], 0
            part.append((char, codes))
            used += size
    return parts + [part] if part else parts


def _wrap(pieces: list[Piece], width: int) -> list[list[Piece]]:
    """Pieces filled into lines of ``width`` columns at most, blanks between words collapsed."""
    lines: list[list[Piece]] = []
    line: list[Piece] = []
    used = 0
    for word in _words(pieces):
        for part in _chop(word, width) if _width(word) > width else [word]:
            size = _width(part)
            if line and used + 1 + size > width:
                lines.append(line)
                line, used = [], 0
            if line:  # the blank takes the style both neighbours share: an underlined label stays one line
                line.append((" ", line[-1][1] if line[-1][1] == part[0][1] else ()))
                used += 1
            line += part
            used += size
    return lines + [line] if line else lines or [[]]


def _fit(natural: list[int], room: int, least: int = 8) -> list[int]:
    """Column widths within ``room``: the widest column gives up a column at a time, none below ``least``."""
    widths = list(natural)
    while sum(widths) > room:
        widest = max(range(len(widths)), key=widths.__getitem__)
        if widths[widest] <= least:
            break  # too narrow for the table: it runs over the edge rather than into nothing
        widths[widest] -= 1
    return widths


class _HtmlLines(HTMLParser):
    """A rendered Markdown file (a README) as layout lines, the same model a guide node has."""

    INLINE = {"strong": "b", "b": "b", "em": "i", "i": "i", "code": "tt"}
    PARAGRAPHS = {"p", "div", "dt", "dd", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.styles: list[str] = []
        self.lists: list[list[Any]] = []    # [tag, count] per open list
        self.quote = 0
        self.pre: list[str] | None = None
        self.table: list[list[list[dict[str, Any]]]] | None = None
        self.cell: list[dict[str, Any]] | None = None
        self.link: dict[str, Any] | None = None

    def _spans(self) -> list[dict[str, Any]]:
        if self.cell is not None:
            return self.cell
        if self.current is None:
            self.current = {"wrap": True, "align": "left", "spans": []}
            if self.quote:
                self.current.update(kind="quote", level=1)
            self.lines.append(self.current)
        return self.current["spans"]

    def _end(self) -> None:
        self.current = None

    def _gap(self) -> None:
        """The space a browser puts between two blocks: a blank line (Drawing never draws two in a row)."""
        self._end()
        self.lines.append({"spans": []})

    def _block_end(self) -> None:
        if self.lists:
            self._end()  # the items of a list stay together
        else:
            self._gap()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag in self.INLINE:
            self.styles.append(self.INLINE[tag])
        elif tag in self.PARAGRAPHS:
            if self.current is not None and self.current["spans"]:
                self._end()  # an item's first paragraph (<li><p>) stays on the item's line
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._end()
            self._spans()
            self.current.update(kind=f"h{min(int(tag[1]), 3)}", level=min(int(tag[1]), 3))
        elif tag in ("ul", "ol"):
            self._end()
            self.lists.append([tag, int(values.get("start") or 1) - 1])
        elif tag == "li" and self.lists:
            self._end()
            kind, level = self.lists[-1], min(len(self.lists), len(BULLETS))
            kind[1] += 1
            self._spans()
            self.current.update(kind="number" if kind[0] == "ol" else "bullet", level=level,
                                marker=f"{kind[1]}." if kind[0] == "ol" else BULLETS[level - 1])
        elif tag == "blockquote":
            self._end()
            self.quote += 1
        elif tag == "pre":
            self._end()
            self.pre = []
        elif tag == "hr":
            self._end()
            self.lines.append({"kind": "rule", "spans": []})
        elif tag == "table":
            self._end()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.table.append([])
        elif tag in ("td", "th") and self.table:
            self.cell = []
            self.table[-1].append(self.cell)
            if tag == "th":
                self.styles.append("b")
        elif tag == "a":
            # what _markdown_html made a link to a documentation file next to the guide opens in the viewer
            target = {"guide": values["data-guide"], "file": values["data-file"]} \
                if values.get("data-guide") and values.get("data-file") else None
            self.link = {"text": "", "target": target, "href": values.get("href", "")}
        elif tag == "img":
            self._spans().append({"text": values.get("alt", ""), "style": [], "image": values.get("src", "")})

    def handle_endtag(self, tag: str) -> None:
        if tag in self.INLINE and self.styles:
            self.styles.pop()
        elif tag == "li":
            self._end()
        elif tag in ("p", "div", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._block_end()
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self._block_end()
        elif tag == "blockquote" and self.quote:
            self._gap()
            self.quote -= 1
        elif tag == "pre" and self.pre is not None:
            rows = "".join(self.pre).rstrip("\n").split("\n")
            self.lines.append({"kind": "code", "rows": [[{"text": row, "style": []}] for row in rows], "spans": []})
            self.pre = None
        elif tag in ("td", "th") and self.cell is not None:
            self.cell = None
            if tag == "th" and self.styles:
                self.styles.pop()
        elif tag == "table" and self.table is not None:
            rows, self.table = [row for row in self.table if row], None
            if rows:
                self.lines.append({"kind": "table", "rows": rows, "spans": []})
        elif tag == "a" and self.link is not None:
            link, self.link = self.link, None
            span = {"text": link["text"], "style": list(self.styles)}
            if link["target"]:
                span["file"] = link["target"]
            elif link["href"].startswith(("http://", "https://", "mailto:")) and link["href"] != link["text"].strip():
                span["url"] = link["href"]
            self._spans().append(span)

    def handle_data(self, data: str) -> None:
        if self.pre is not None:
            self.pre.append(data)
            return
        text = _BLANKS.sub(" ", data)
        if self.link is not None:
            self.link["text"] += text
            return
        if not text.strip() and (self.current is None and self.cell is None):
            return  # the line breaks between blocks
        self._spans().append({"text": text, "style": list(self.styles)})


def html_lines(html: str) -> list[dict[str, Any]]:
    parser = _HtmlLines()
    parser.feed(html)
    parser.close()
    return parser.lines


#: What a stream that cannot encode them (a cp1252 console) gets for the drawing's own glyphs and the guides'
#: punctuation -- turned into "?" otherwise.
_ASCII = str.maketrans({"─": "-", "│": "|", "“": '"', "”": '"', "‘": "'", "’": "'", "·": "-", "…": "...",
                        "–": "-", "—": "--", **{bullet: "*" for bullet in BULLETS}})


def _unicode_ok(stream: Any) -> bool:
    try:
        ("─│“”·…" + "".join(BULLETS)).encode(getattr(stream, "encoding", None) or "utf-8")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class Drawing:
    """A page drawn for a terminal of ``width`` columns: its lines, and the targets of its numbered links."""

    def __init__(self, width: int, unicode_ok: bool = True) -> None:
        self.width = max(width, 20)
        self.unicode = unicode_ok
        self.lines: list[list[Piece]] = []
        #: what link [n] opens: a node {guide, node} or a documentation file {guide, file}
        self.links: list[dict[str, Any]] = []

    def add(self, pieces: list[Piece]) -> None:
        if pieces or (self.lines and self.lines[-1]):  # no blank line at the top, never two in a row
            self.lines.append(pieces)

    def spans(self, spans: list[dict[str, Any]], base: tuple[str, ...] = ()) -> list[Piece]:
        pieces: list[Piece] = []
        for span in spans:
            codes = base + tuple(SGR[name] for name in span.get("style", ()) if name in SGR)
            label = " ".join(span["text"].split())
            target = span.get("link") or span.get("file")
            if target:
                self.links.append(target)
                pieces += [(f"[{len(self.links)}]", LINK), (" ", ()), (label, codes + LINK)]
            elif span.get("url"):
                pieces += [(label, codes), (" ", ()), (f"<{span['url']}>", DIM)]
            elif "image" in span:
                pieces.append((f"[image: {label or 'image'}]", DIM))
            elif span.get("broken"):
                pieces.append((label, codes + DIM))
            else:
                pieces.append((span["text"], codes))
        return pieces

    def header(self, page: dict[str, Any]) -> None:
        where = f"{page['guide']}/{page['file'] or page['node']}"
        if page["database"] != page["title"]:
            where = f"{page['database']} · {where}"
        self.add([(page["title"], BOLD), (f"   {where}", DIM)])
        self.add([])

    def line(self, line: dict[str, Any]) -> None:
        if "html" in line:
            for one in html_lines(line["html"]):
                self.line(one)
            return
        kind = line.get("kind")
        if kind == "rule":
            self.add([(("─" if self.unicode else "-") * min(self.width, 72), DIM)])
        elif kind == "code":
            self.add([])
            for row in line["rows"]:
                self.lines.append([("    ", ())] + self.spans(row, DIM))  # as written: blank rows too
            self.add([])
        elif kind == "table":
            self.table(line["rows"])
        else:
            self.paragraph(line, kind)

    def paragraph(self, line: dict[str, Any], kind: str | None) -> None:
        pieces = self.spans(line["spans"], HEADING.get(kind or "", ()))
        if not "".join(text for text, _ in pieces).strip():
            self.add([])
            return
        first = rest = ""
        if kind in ("bullet", "number"):
            marker = line.get("marker")
            if marker in BULLETS and not self.unicode:
                marker = "*-+"[BULLETS.index(marker)]
            indent = "  " * line.get("level", 1)
            first = indent + (f"{marker} " if marker else "  ")
            rest = indent + " " * (len(first) - len(indent))
        elif kind == "quote":
            first = rest = "  │ " if self.unicode else "  | "
        if not line.get("wrap"):
            self.add([(first, ())] + pieces)  # as written; a terminal folds what is too long
            return
        room = self.width - len(first)
        rows = _wrap(pieces, room)
        for number, row in enumerate(rows):
            spare = room - _width(row)
            pad = {"center": spare // 2, "right": spare}.get(line.get("align", "left"), 0)
            self.add([((first if number == 0 else rest) + " " * pad, ())] + row)
        if kind in UNDERLINE:
            self.add([(UNDERLINE[kind] * max(map(_width, rows)), HEADING[kind])])

    def table(self, rows: list[list[list[dict[str, Any]]]]) -> None:
        """Aligned columns; a cell too wide for its column wraps inside it, nothing is cut away."""
        cells = [[self.spans(cell) for cell in row] for row in rows]  # links numbered row by row
        count = max(map(len, cells))
        cells = [row + [[]] * (count - len(row)) for row in cells]
        gap = "  "
        natural = [max(_width(row[column]) for row in cells) for column in range(count)]
        widths = _fit(natural, self.width - 2 - len(gap) * (count - 1))
        self.add([])
        for number, row in enumerate(cells):
            wrapped = [_wrap(cell, widths[column]) for column, cell in enumerate(row)]
            for at in range(max(map(len, wrapped))):
                out: list[Piece] = [("  ", ())]
                for column, lines in enumerate(wrapped):
                    part = lines[at] if at < len(lines) else []
                    if number == 0:
                        part = [(text, BOLD + codes) for text, codes in part]
                    out += part + [(" " * (widths[column] - _width(part)) + gap, ())]
                self.lines.append(out)
            if number == 0:
                self.lines.append([("  " + gap.join(("─" if self.unicode else "-") * w for w in widths), DIM)])
        self.add([])

    def hits(self, query: str, result: dict[str, Any]) -> None:
        note = f"   ({result['fallback']} search)" if result.get("fallback") else ""
        self.add([(f"Help for “{query}”", BOLD), (note, DIM)])
        self.add([])
        for hit in result["hits"]:
            self.links.append({"guide": hit["guide"], "node": hit["node"]})
            self.add([(f"[{len(self.links)}]", LINK), (" ", ()), (hit["title"], BOLD + LINK),
                      (f"   {hit['database']} · {hit['guide']}/{hit['node']}", DIM)])
            for row in _wrap([(hit.get("snippet") or "", ())], self.width - 4):
                self.add([("    ", ())] + row)
            self.add([])


def _place(target: dict[str, Any]) -> dict[str, str]:
    """What a link, a nav entry or a hit names: a node, or a documentation file next to a guide."""
    if target.get("file"):
        return {"guide": target["guide"], "file": target["file"]}
    return {"guide": target["guide"], "node": target["node"]}


async def _find(library: Library, query: str) -> dict[str, Any]:
    # imported here: the search is its own module, loaded only when somebody asks
    from ..ui.help_index import find_help
    return await find_help(library, query, limit=SEARCH_LIMIT)


class Viewer:
    """The guide viewer of one /help: what is on screen, the trail Retrace walks back, the numbered links.

    ``read(prompt)`` reads a line (raises EOFError/KeyboardInterrupt to leave); ``run(coroutine)``
    returns (finished, result) -- the chat's own interruptible runner. Not ``interactive`` (stdin or
    stdout is no terminal): pages are printed whole and nothing is asked.
    """

    def __init__(self, library: Library, *, read: Callable[[str], str],
                 run: Callable[[Awaitable[Any]], tuple[bool, Any]], ansi: bool, interactive: bool,
                 width: int | None = None, height: int | None = None) -> None:
        self.library = library
        self.read = read
        self.run = run
        self.ansi = ansi
        self.interactive = interactive
        self.size = (width, height)
        self.unicode = _unicode_ok(sys.stdout)
        self.trail: list[dict[str, Any]] = []
        self.shown: dict[str, Any] | None = None   # {"place": ...} or {"query": ..., "result": ...}
        self.nav: dict[str, Any] = MANUAL_NAV
        self.links: list[dict[str, Any]] = []

    # -- output ----------------------------------------------------------
    def _terminal(self) -> tuple[int, int]:
        width, height = self.size
        measured = shutil.get_terminal_size((80, 24))  # asked per page: the window may have changed
        return width or measured.columns, height or measured.lines

    def _print(self, line: str) -> None:
        print(line if self.unicode else line.translate(_ASCII))

    def say(self, text: str, codes: tuple[str, ...] = ()) -> None:
        self._print(_join([(text, codes)], self.ansi))

    def show(self, drawing: Drawing) -> None:
        """A drawn page, a screen at a time on a terminal; ``q`` at the pause skips the rest."""
        self.links = drawing.links
        width, height = self._terminal()
        room, used = max(height - 2, 5), 0
        for pieces in drawing.lines:
            rows = max(1, -(-_width(pieces) // max(width, 1)))
            if self.interactive and used and used + rows > room:
                if self.read(MORE).strip().lower() == "q":
                    return
                used = 0
            self._print(_join(pieces, self.ansi))
            used += rows

    def _drawing(self) -> Drawing:
        return Drawing(self._terminal()[0], self.unicode)

    # -- pages -----------------------------------------------------------
    def _step(self, retrace: bool) -> None:
        """The trail after a page arrived: a retrace takes its station off, anything else puts the page left on."""
        if retrace:
            self.trail.pop()
        elif self.shown is not None:
            self.trail.append(self.shown)

    def open(self, target: dict[str, Any], retrace: bool = False) -> bool:
        place = _place(target)
        try:
            page = self.library.page(place["guide"], place.get("node", "main"), place.get("file"))
        except KeyError as error:
            self.say(f"Cannot open {place['guide']}/{place.get('file') or place.get('node')}: {error.args[0]}")
            return False
        self._step(retrace)
        self.shown = {"place": _place(page)}
        self.nav = page["nav"]
        drawing = self._drawing()
        drawing.header(page)
        for line in page["lines"]:
            drawing.line(line)
        self.show(drawing)
        return True

    def search(self, query: str) -> bool:
        self.say("Searching the help ...", DIM)  # the first search builds the index: seconds
        finished, result = self.run(_find(self.library, query))
        if not finished:
            raise KeyboardInterrupt  # the runner said it was cancelled: Ctrl-C leaves the viewer
        if result.get("exact"):
            return self.open(result["exact"])
        if not result.get("hits"):
            self.say(f"Nothing in the help matches “{query}”.")
            return False
        self.results(query, result)
        return True

    def results(self, query: str, result: dict[str, Any], retrace: bool = False) -> None:
        self._step(retrace)
        self.shown = {"query": query, "result": result}
        drawing = self._drawing()
        drawing.hits(query, result)
        self.show(drawing)
        if not self.interactive:
            self.say("Open one with /help <guide>/<node>.", DIM)

    def retrace(self) -> None:
        if not self.trail:
            self.say("Nothing to go back to.")
            return
        back = self.trail[-1]  # stays on the trail until its page arrived, as in guide.js
        if "query" in back:
            self.results(back["query"], back["result"], retrace=True)
        else:
            self.open(back["place"], retrace=True)

    # -- the session -----------------------------------------------------
    def start(self, topic: str) -> bool:
        """The first page for ``/help <topic>``; False when there is none."""
        target = SHORTCUTS.get(topic.lower())
        if target is None and "/" in topic and " " not in topic:
            found = self.library.resolve(MANUAL, topic)  # an address the hits list shows: guide/node
            target = {"guide": found[0], "node": found[1]} if found else None
        return self.open(target) if target else self.search(topic)

    def browse(self) -> None:
        """The ``help>`` prompt, until the person goes back to the chat."""
        while True:
            self.say(KEYS, DIM)
            answer = self.read("help> ").strip()
            key = answer.lower()
            if key in ("", "q"):
                return
            if answer.isdecimal():
                number = int(answer)
                if 1 <= number <= len(self.links):
                    self.open(self.links[number - 1])
                else:
                    self.say(f"No link {number} on this page.")
            elif key == "b":
                self.retrace()
            elif key in ("n", "p") and self.shown is not None and "query" in self.shown:
                self.say("Browse moves between nodes; this is a list of search hits.")
            elif key in ("n", "p", "c", "i", "h"):
                name = {"n": "next", "p": "prev", "c": "contents", "i": "index", "h": "help"}[key]
                target = self.nav.get(name)
                if target:
                    self.open(target)
                else:
                    self.say({"next": "This is the last node.", "prev": "This is the first node."}.get(
                        name, f"This guide has no {name} node."))
            elif answer.startswith("/") and answer[1:].strip():
                self.search(answer[1:].strip())
            else:
                self.say(f"Unknown key {answer!r}.")


def open_help(topic: str, config: Any, *, read: Callable[[str], str],
              run: Callable[[Awaitable[Any]], tuple[bool, Any]], ansi: bool, interactive: bool) -> None:
    """``/help <topic>`` in the chat: the viewer, until the person goes back. Never raises into the chat."""
    plugins = getattr(config, "plugins", None)
    try:
        viewer = Viewer(Library(plugins.plugin_dirs or [] if plugins is not None else []),
                        read=read, run=run, ansi=ansi, interactive=interactive)
        if viewer.start(topic) and interactive:
            viewer.browse()
    except (EOFError, KeyboardInterrupt):
        print()  # Ctrl-C, Ctrl-D: back to the chat, on a line of its own
    except Exception as error:  # a broken guide or search costs the help, not the chat
        logger.warning("/help %s failed", topic, exc_info=True)
        print(f"The help could not be shown: {error}")
