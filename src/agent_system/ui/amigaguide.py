"""AmigaGuide: read a ``.guide`` database and lay out one of its nodes for the help viewer.

AmigaGuide is the hypertext help format of AmigaOS: one text file holds a
database of named nodes, each node a page of text with inline attributes and
link buttons::

    @database "ScarabHive"
    @author "..."
    @smartwrap
    @node main "ScarabHive"
    Welcome. @{" Quickstart " link quickstart} @{b}bold@{ub}
    @endnode

The subset read here (AmigaOS 3.1, v40):

* database: ``@database @author @(c) @$VER: @index @help @wordwrap @smartwrap``
* node: ``@node @endnode @title @toc @prev @next @index @help @wordwrap @smartwrap``
* inline: ``b ub i ui u uu plain``, ``fg``/``bg`` with the pens ``text shine shadow
  fill filltext background highlight``, ``jleft jcenter jright pard``, ``line par
  tab``, ``code body`` (v40: stop and resume wrapping), ``amigaguide``
* link buttons: ``@{"label" link <node> [line]}``, ``alink`` alike; ``<node>`` may
  name another database as ``file.guide/node``
* escapes: ``\\@`` and ``\\\\``
* ``@embed <file>``: the file inside the node, as written (a Markdown file rendered)

Extensions of this viewer: what Markdown has, in AmigaGuide's own syntax --
the format grows, it does not turn into Markdown:

* a paragraph starts with ``@{h1}`` ``@{h2}`` ``@{h3}`` (a heading, one line),
  ``@{bullet [level]}`` or ``@{number [level]}`` (list items; numbers count
  themselves) or ``@{quote}``; ``@{rule}`` draws a line
* inline code ``@{tt}``..``@{utt}``, strikethrough ``@{s}``..``@{us}``
* ``@{code <language>}`` .. ``@{body}``: a code block; ``@{code}`` alone keeps
  its AmigaOS meaning (lines as written, no wrapping)
* ``@{table}`` .. ``@{body}``: one row per line, cells split by ``|``, the first
  row is the header
* a web link ``@{"label" link https://...}`` (only ``http``, ``https``,
  ``mailto``), an image ``@{image <file> ["alt text"]}``

``@embed`` and ``@{image}`` read files next to the guide only, never above its
folder. A Markdown file (a README, a linked ``docs/*.md``) is rendered as
Markdown -- that is the file's format, not the guide's.

Anything that would run something (``system``, ``rx``, ``rxs``, ``beep``,
``close``, ``quit``) becomes an inert button: a guide is a document, not a
script. ``@remark``, ``@font``, ``@width`` and the other display hints are
ignored. What the reader cannot place goes to ``Guide.warnings`` (parsing) or
the layout's ``problems`` (inline commands, links) instead of disappearing.

Wrapping follows the database or node setting: none (every line as written,
no wrapping), ``@wordwrap`` (every line a paragraph that wraps), ``@smartwrap``
(lines up to a blank line form one paragraph).
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from html import escape as html_escape
from html import unescape as html_unescape
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterator
from urllib.parse import urlencode

from agent_system.utils.markdown_render import DEFAULT_ALLOWED_TAGS, markdown_to_html

PENS = frozenset({"text", "shine", "shadow", "fill", "filltext", "background", "highlight"})
#: Inline commands that run something on an Amiga; here they are shown, never executed.
INERT = frozenset({"system", "rx", "rxs", "beep", "close", "quit"})
#: Display hints of the original viewer that mean nothing in a browser.
IGNORED = frozenset({"remark", "font", "width", "height", "master", "tab", "onopen", "onclose", "macro",
                     "proportional", "keywords"})
_IGNORED_INLINE = frozenset({"lindent", "pari", "settabs", "cleartabs", "apen", "bpen"})
#: Text attributes switched on by their name and off by u<name>: bold, italic, underline, code, struck.
TOGGLES = ("b", "i", "u", "tt", "s")
HEADINGS = frozenset({"h1", "h2", "h3"})
#: What a paragraph can be besides a plain one.
BLOCKS = HEADINGS | {"bullet", "number", "quote"}
BULLETS = ("•", "◦", "▪")
#: A Markdown habit in a table: the line under the header, three dashes a cell at least. It carries nothing;
#: "- | -" (a row saying "none") is a row.
_SEPARATOR_CELL = re.compile(r":?-{3,}:?")
#: What would break a table cell into two lines, open a block inside it, or align it: a cell has no lines of its own.
CELL_BREAKERS = BLOCKS | {"line", "par", "rule", "code", "table", "body", "jleft", "jcenter", "jright", "pard"}
_WORDS = re.compile(r'"([^"]*)"|(\S+)')
#: A link to the web instead of a node. A fixed list: ``javascript:`` and friends stay node names.
WEB_LINK = re.compile(r"(https?://|mailto:)\S+", re.IGNORECASE)

#: What a file page may be (a README's docs/design.md) -- and only where document() allows it.
FILE_PAGES = frozenset({".md", ".markdown", ".txt"})
MARKDOWN = frozenset({".md", ".markdown"})
_TAG = re.compile(r'<(a|img)((?:\s+[\w-]+="[^"]*")*)\s*/?>')
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
#: The language of an @embed-ed file's code block, by its suffix.
CODE_LANGUAGES = {".py": "python", ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml",
                  ".js": "javascript", ".ts": "typescript", ".sh": "bash", ".html": "html", ".css": "css",
                  ".sql": "sql", ".xml": "xml", ".ini": "ini"}
#: A larger file is named, not shown: 2.8 MB of Markdown took four seconds to render, the renderer's lock held.
MAX_FILE_BYTES = 512 * 1024
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
#: A link of rendered Markdown that the viewer opens as a file page (written by _markdown_html).
_FILE_LINK = re.compile(r' data-file="([^"]*)"')

#: A link target resolved to (guide id, node key), or None when it names nothing.
Resolve = Callable[[str], "tuple[str, str] | None"]
#: An image file named in a guide, as the URL the viewer loads it from, or None when there is none.
ImageUrl = Callable[[str], "str | None"]


@dataclass(frozen=True)
class Markdown:
    """An @embed-ed Markdown file inside a node, shown rendered; ``base``: its folder, relative to the guide's."""
    text: str
    base: str = ""


@dataclass
class Node:
    name: str
    title: str
    #: (source line number, text) for every line between @node and @endnode that is not a command;
    #: an @embed-ed Markdown file is one entry of its own
    lines: list[tuple[int, str | Markdown]] = field(default_factory=list)
    toc: str | None = None
    prev: str | None = None
    next: str | None = None
    index: str | None = None
    help: str | None = None
    wrap: str | None = None       # "none" | "word" | "smart"; None: the database's
    generated: bool = False       # made by the viewer (the index), not written by the author
    #: the node's plain text, kept for search once laid out -- the guide is parsed anew when its files change
    search_text: str | None = field(default=None, repr=False, compare=False)


@dataclass
class Guide:
    id: str
    title: str = ""
    author: str = ""
    version: str = ""
    copyright: str = ""
    index: str | None = None
    help: str | None = None
    wrap: str = "none"
    #: by lower-case name, in file order -- node names are case-insensitive
    nodes: dict[str, Node] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: where the guide file lies: @embed and @{image} read from here; None for a generated guide
    folder: Path | None = None
    #: every file an @embed asked for, with its stamp taken before reading -- a missing one included,
    #: so that the guide shows it once it is there
    embeds: dict[Path, tuple[int, int] | None] = field(default_factory=dict)


def decode(raw: bytes) -> str:
    """A file's bytes as text: UTF-8, an editor's BOM dropped. A file without any UTF-8 in it comes from the
    Amiga or from Windows (cp1252 holds ISO-8859-1's letters); one stray byte in a UTF-8 file costs that byte,
    not the whole file."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8-sig", errors="replace")
    # counted, not just seen: in cp1252 "ß«" happens to be a valid UTF-8 pair among many invalid bytes
    broken = text.count("\N{REPLACEMENT CHARACTER}")
    if sum(ord(char) > 127 for char in text) - broken >= broken:
        return text
    return raw.decode("cp1252", errors="replace")


def lines_of(text: str) -> list[str]:
    """Lines split at line breaks only: str.splitlines() also splits at a form feed, or at 0x85 read as latin-1."""
    lines = _LINE_BREAK.split(text)
    return lines[:-1] if lines[-1] == "" else lines


def stamp(path: Path) -> tuple[int, int] | None:
    """(mtime, size) of a file, None when there is none: a guide is read again when a stamp changes."""
    try:
        stat = path.stat()
    except (OSError, ValueError):
        return None
    return stat.st_mtime_ns, stat.st_size


def _below(folder: Path | None, relative: str) -> Path | None:
    """``relative`` resolved below ``folder``, whether it exists or not; None when it names anything outside.

    An absolute, drive or UNC path is refused before the file system is asked: resolving
    ``//host/share/x`` on Windows is already a network request to that host. A NUL or a path
    too long for the system is no path at all (it raised, and the request answered 500).
    """
    if folder is None or not relative or PureWindowsPath(relative).anchor or PurePosixPath(relative).anchor:
        return None
    try:
        root = folder.resolve()
        path = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    return path if path.is_relative_to(root) else None


def inside(folder: Path | None, relative: str) -> Path | None:
    """``relative`` below ``folder`` as an existing file, or None -- never a path that climbs out."""
    path = _below(folder, relative)
    try:
        return path if path is not None and path.is_file() else None
    except (OSError, ValueError):
        return None


def document(folder: Path | None, relative: str) -> Path | None:
    """A documentation file the viewer may show as a page of its own, or None.

    The README at the top of the folder, or a Markdown or text file under ``docs/`` -- nothing
    else: every signed-in user reads the help, and a plugin folder also holds agent prompts,
    working notes and files git ignores.
    """
    path = inside(folder, relative)
    if folder is None or path is None or path.suffix.lower() not in FILE_PAGES:
        return None
    parts = path.relative_to(folder.resolve()).parts
    return path if parts[0].lower() == "docs" or (len(parts) == 1 and path.name.lower() == "readme.md") else None


def words(text: str) -> list[str]:
    """Arguments as AmigaGuide splits them: by blanks, a double-quoted run is one word."""
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _WORDS.finditer(text)]


def escape(text: str) -> str:
    """Plain text as guide text: nothing in it is read as a command."""
    return text.replace("\\", "\\\\").replace("@", "\\@")


def parse(text: str, guide_id: str, folder: Path | None = None) -> Guide:
    guide = Guide(id=guide_id, title=guide_id, folder=folder)
    node: Node | None = None
    for number, line in enumerate(lines_of(text), 1):
        command = re.match(r"@([^\s{][^\s]*)\s*(.*)$", line)
        if command is None:
            if node is not None:
                node.lines.append((number, line))
            continue
        name, rest = command.group(1).lower(), command.group(2).strip()
        args = words(rest)
        if name == "node":
            if node is not None:
                guide.warnings.append(f"line {number}: @node {args[:1]} before @endnode of {node.name!r}")
            node = _open_node(guide, args, number)
        elif name == "endnode":
            if node is None:
                guide.warnings.append(f"line {number}: @endnode without @node")
            node = None
        elif name in ("toc", "prev", "next", "index", "help"):
            if not args:
                guide.warnings.append(f"line {number}: @{name} needs a node")
            elif node is not None:
                setattr(node, name, args[0])
            elif name in ("index", "help"):
                setattr(guide, name, args[0])
            else:
                guide.warnings.append(f"line {number}: @{name} belongs inside a node")
        elif name in ("wordwrap", "smartwrap"):
            wrap = "word" if name == "wordwrap" else "smart"
            if node is not None:
                node.wrap = wrap
            else:
                guide.wrap = wrap
        elif name == "title":
            if node is None or not args:
                guide.warnings.append(f"line {number}: @title needs a title and belongs inside a node")
            else:
                node.title = args[0]
        elif name == "database":
            guide.title = args[0] if args else guide_id
        elif name == "author":
            guide.author = rest.strip('"')
        elif name == "(c)":
            guide.copyright = rest
        elif name == "$ver:":
            guide.version = rest
        elif name == "embed":
            _embed(guide, node, args, number)
        elif name not in IGNORED:
            guide.warnings.append(f"line {number}: unknown command @{command.group(1)}")
    if node is not None:
        guide.warnings.append(f"end of file: @endnode missing for {node.name!r}")
    if not guide.nodes:
        guide.warnings.append("no @node in the file")
    _give_index(guide)
    return guide


def _open_node(guide: Guide, args: list[str], number: int) -> Node:
    if not args:
        guide.warnings.append(f"line {number}: @node without a name")
        return Node("", "")  # swallows its lines; nothing can link to it
    node = Node(name=args[0], title=args[1] if len(args) > 1 else args[0])
    key = node.name.lower()
    if key in guide.nodes:
        guide.warnings.append(f"line {number}: node {node.name!r} defined twice; the first one counts")
        return node  # read, but not reachable
    guide.nodes[key] = node
    return node


def _embed(guide: Guide, node: Node | None, args: list[str], number: int) -> None:
    """@embed: the file joins the node, never read as guide commands."""
    wanted = _below(guide.folder, args[0] if args else "")
    if wanted is not None:
        guide.embeds[wanted] = stamp(wanted)  # before reading: a write in between shows next time
    path = inside(guide.folder, args[0] if args else "")
    lines = _file_lines(guide, path, number) if node is not None and path is not None else None
    if node is None or lines is None:
        guide.warnings.append(f"line {number}: @embed needs a node and a readable file next to the guide, "
                              f"got {args[:1]}")
        return
    node.lines += lines


def _file_lines(guide: Guide, path: Path, number: int) -> list[tuple[int, str | Markdown]] | None:
    """A file as node lines: Markdown rendered, anything else a code block in its language, as written.
    None when it cannot be read (an editor's save in between, a lock)."""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            return [(number, f"@{{i}}{escape(path.name)} is too large to show here ({size // 1024} KB).@{{ui}}")]
        text = decode(path.read_bytes())
    except OSError:
        return None
    suffix = path.suffix.lower()
    if suffix in MARKDOWN:
        base = path.parent.relative_to(guide.folder.resolve()).as_posix() if guide.folder is not None else ""
        return [(number, Markdown(text, base))]
    code = f"@{{code {CODE_LANGUAGES.get(suffix, 'text')}}}"
    return [(number, code), *((number, escape(line)) for line in lines_of(text)), (number, "@{body}")]


def _links(guide: Guide, lines: list[tuple[int, str | Markdown]]) -> Iterator[str]:
    """Where node lines link, relative to the guide's folder: the buttons, and the file links of Markdown in them
    -- taken from the rendered page itself, so what the viewer shows as a link is what opens, and nothing else."""
    for _, raw in lines:
        if isinstance(raw, Markdown):
            for target in _FILE_LINK.findall(_markdown_html(raw, guide, lambda path: None) or ""):
                yield html_unescape(target)
        else:
            for token in _inline(raw):
                if token[0] == "command" and token[2] is not None and len(token[1]) > 1 \
                        and token[1][0].lower() in ("link", "alink"):
                    yield token[1][1]


def linked_document(guide: Guide, relative: str) -> Path | None:
    """A documentation file (see document()) that the guide links to -- from a node, or from a file page a node
    links to, and so on -- or None. A file nobody links is no part of the documentation: a runbook with host
    names, a draft lying in docs/."""
    goal = document(guide.folder, relative)
    if goal is None:
        return None
    seen: set[Path] = set()
    targets = [target for node in guide.nodes.values() for target in _links(guide, node.lines)]
    while targets:
        path = document(guide.folder, targets.pop())
        if path is None or path in seen:
            continue
        if path == goal:
            return goal
        seen.add(path)
        targets += _links(guide, _file_lines(guide, path, 1) or [])
    return None


def file_node(guide: Guide, relative: str) -> Node | None:
    """A documentation file next to the guide as a page of its own -- what a file button or link opens."""
    path = linked_document(guide, relative)
    lines = _file_lines(guide, path, 1) if path is not None else None
    if path is None or lines is None:
        return None
    return Node(name=relative, title=path.name, lines=lines, generated=True)


def _give_index(guide: Guide) -> None:
    """The Index button's target: @index if declared, else a node called index, else a generated one."""
    if guide.index or not guide.nodes:
        return
    if "index" in guide.nodes:
        guide.index = "index"
        return
    entries = sorted((n for n in guide.nodes.values()), key=lambda n: n.title.lower())
    lines = [(0, f'@{{" {n.title.replace(chr(34), chr(39))} " link "{n.name}"}}') for n in entries]
    guide.nodes["index"] = Node("index", "Index", [(0, "@{code}"), *lines], generated=True)
    guide.index = "index"


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------

def _pieces(text: str) -> Iterator[tuple[str, int, int]]:
    """A line's raw pieces with their span: an ``escape`` (``\\@``, ``\\\\``, ``\\|``), a ``command``
    ``@{...}``, an ``unclosed`` ``@{`` (shown as text, and named), or a ``char``. The one scanner behind
    the text, the table cells and the block cuts.
    """
    i, closing = 0, None
    while i < len(text):
        if text[i] == "\\" and text[i + 1:i + 2] in ("@", "\\", "|"):
            yield "escape", i, i + 2
            i += 2
            continue
        if text.startswith("@{", i):
            closing = closing or _closings(text)
            end = closing(i + 2)
            if end is None:
                yield "unclosed", i, i + 2
                i += 2
            else:
                yield "command", i, end + 1
                i = end + 1
            continue
        yield "char", i, i + 1
        i += 1


def _inline(text: str) -> Iterator[tuple[str, Any]]:
    """("text", str) and ("command", [words], label-or-None) in the order they appear."""
    buffer: list[str] = []
    for kind, start, end in _pieces(text):
        if kind == "command":
            if buffer:
                yield "text", "".join(buffer)
                buffer = []
            body = text[start + 2:end - 1].strip()
            if body.startswith('"'):
                label, _, rest = body[1:].partition('"')
                yield "command", words(rest), label
            else:
                yield "command", words(body), None
        elif kind == "escape" and text[start + 1] != "|":
            buffer.append(text[start + 1])
        else:
            buffer.append(text[start:end])  # "\|" outside a table is what it says
    if buffer:
        yield "text", "".join(buffer)


def _command_name(text: str, start: int, end: int) -> str:
    body = text[start + 2:end - 1].strip()
    return "" if not body or body.startswith('"') else body.split()[0].lower()


def _segments(line: str) -> list[tuple[str, str | None]]:
    """A line cut before every ``@{table}`` and ``@{body}``, and after ``@{table}``: a table opens and
    closes between words (``@{table}A | B``, ``3 | 4 @{body}``). Each piece: (text, "table"/"body"/None
    for what it starts with). Found by the scanner, so a written-out ``\\@{body}`` is no cut."""
    marks: list[tuple[int, str | None]] = [(0, None)]
    for kind, start, end in _pieces(line):
        name = _command_name(line, start, end) if kind == "command" else ""
        if name in ("table", "body"):
            marks.append((start, name))
            if name == "table":
                marks.append((end, None))
    ends = [position for position, _ in marks[1:]] + [len(line)]
    return [(line[start:end], name) for (start, name), end in zip(marks, ends) if line[start:end].strip()]


def _closings(text: str) -> Callable[[int], int | None]:
    """Where a command body starting at a position ends: the next ``}`` outside double quotes, or None.

    One pass for the whole line: looking afresh from every ``@{`` ran to the end of the line each
    time, and a line of ``@{"`` took seconds. A ``}`` is outside the quotes seen from ``start`` when
    an even number of quotes lies between them -- when both have the same quote parity.
    """
    parity, quotes = [], 0
    for char in text:
        parity.append(quotes & 1)
        quotes += char == '"'
    after: list[list[int | None]] = [[None] * (len(text) + 1) for _ in range(2)]
    for i in range(len(text) - 1, -1, -1):
        after[0][i], after[1][i] = after[0][i + 1], after[1][i + 1]
        if text[i] == "}":
            after[parity[i]][i] = i
    return lambda start: after[parity[start]][start] if start < len(text) else None


def _cells(row: str) -> list[str]:
    """A table row's cells: split at ``|``, but not inside ``@{...}`` and not at ``\\|``; outer pipes optional."""
    cells: list[str] = []
    buffer: list[str] = []
    for kind, start, end in _pieces(row):
        piece = row[start:end]
        if kind == "escape" and piece == "\\|":
            buffer.append("|")
        elif kind == "char" and piece == "|":
            cells.append("".join(buffer))
            buffer = []
        else:
            buffer.append(piece)  # a command, and the other escapes, stay for _inline to read
    cells.append("".join(buffer))
    if row.lstrip().startswith("|") and not cells[0].strip():
        cells = cells[1:]
    if row.rstrip().endswith("|") and cells and not cells[-1].strip():
        cells = cells[:-1]
    return [cell.strip() for cell in cells]


class _Layout:
    def __init__(self, wrap: str, resolve: Resolve, image: ImageUrl, guide: Guide):
        self.guide = guide
        self.wrap = wrap
        self.resolve = resolve
        self.image_url = image
        self.lines: list[dict[str, Any]] = []
        self.problems: list[str] = []
        self.current: dict[str, Any] | None = None
        self.style: dict[str, Any] = {**{name: False for name in TOGGLES}, "fg": None, "bg": None}
        self.align = "left"
        self.code = False
        self.join = False   # the next content continues a smartwrap paragraph: one blank in between
        self.number = 0
        self.block: dict[str, Any] | None = None  # the paragraph under way, when it is a heading, item or quote
        self.counters: list[int] = []             # numbered items per level, while the list lasts
        self.region: dict[str, Any] | None = None  # a code block or table, collected until @{body}

    # -- output --------------------------------------------------------
    def _line(self, blank: bool = False) -> dict[str, Any]:
        if self.current is None:
            line: dict[str, Any] = {"n": self.number, "align": self.align,
                                    "wrap": self.wrap != "none" and not self.code, "spans": []}
            if self.block is not None and not blank:
                line.update(kind=self.block["kind"], level=self.block["level"])
                if self.block["marker"]:  # the item's first line carries its bullet or number
                    line["marker"] = self.block["marker"]
                    self.block["marker"] = None
            elif not blank and self.region is None:
                self.counters = []  # a plain paragraph ends a numbered list; a blank line does not
            self.current = line
            self.lines.append(line)
        return self.current

    def _classes(self) -> list[str]:
        classes = [key for key in TOGGLES if self.style[key]]
        classes += [f"{key}-{self.style[key]}" for key in ("fg", "bg") if self.style[key]]
        return classes

    def text(self, text: str) -> None:
        if self.join:
            text = text.lstrip()
            if not text:
                return
            self.join = False
            text = " " + text
        self._append(text)

    def _append(self, text: str) -> None:
        spans = self._line()["spans"]
        style = self._classes()
        if spans and "text" in spans[-1] and len(spans[-1]) == 2 and spans[-1]["style"] == style:
            spans[-1]["text"] += text
        else:
            spans.append({"text": text, "style": style})

    def button(self, span: dict[str, Any]) -> None:
        if self.join:
            self.join = False
            self._append(" ")
        span["style"] = self._classes()
        self._line()["spans"].append(span)

    def end_line(self) -> None:
        self.current = None
        self.join = False

    # -- blocks --------------------------------------------------------
    def start_block(self, name: str, argument: str) -> None:
        """A heading, list item or quote begins: a paragraph of its own from here."""
        self.end_line()
        level = min(int(argument), len(BULLETS)) if re.fullmatch(r"[0-9]{1,3}", argument) and int(argument) else 1
        marker = None
        if name == "number":
            del self.counters[level:]
            self.counters += [0] * (level - len(self.counters))
            self.counters[level - 1] += 1
            marker = f"{self.counters[level - 1]}."
        elif name == "bullet":
            del self.counters[level - 1:]  # a bullet between numbers starts their count anew
            marker = BULLETS[level - 1]
        else:
            self.counters = []
            level = int(name[1]) if name in HEADINGS else 1
        self.block = {"kind": name, "level": level, "marker": marker}

    def open_region(self, kind: str, language: str | None = None) -> None:
        self.close_open_region()
        self.end_line()
        self.block = None  # a step, its code, the next step: the count goes on
        self.region = {"kind": kind, "language": language, "start": len(self.lines), "n": self.number, "rows": [],
                       "separated": False}
        self.code = True

    def table_row(self, cells: list[list[tuple[str, Any]]]) -> None:
        for tokens in cells:
            self.end_line()
            self._line()  # a cell with nothing in it is still a cell
            for token in tokens:
                if token[0] == "text":
                    self.text(token[1])
                elif token[2] is None and token[1] and token[1][0].lower() in CELL_BREAKERS:
                    # a cell is one line: a break or a block would shift every cell after it
                    self.problems.append(f"line {self.number}: @{{{token[1][0]}}} does not belong in a table cell")
                else:
                    self.command(token[1], token[2])
        self.end_line()
        if self.region is not None:
            self.region["rows"].append(len(cells))

    def close_open_region(self) -> None:
        """A region something else has to end (a new one, a Markdown file, the node's end): closed, and named."""
        if self.region is not None:
            self.problems.append(f"line {self.region['n']}: @{{{self.region['kind']}}} without @{{body}}")
            self.close_region()

    def close_region(self) -> None:
        """@{body}: the collected lines become one code block or one table."""
        region, self.region = self.region, None
        if region is None:
            return
        self.end_line()
        self.code = False  # also when a Markdown file closed it: the lines after it wrap again
        taken = self.lines[region["start"]:]
        del self.lines[region["start"]:]
        entry: dict[str, Any] = {"n": region["n"], "align": "left", "wrap": False, "spans": [],
                                 "kind": region["kind"]}
        if region["kind"] == "code":
            entry.update(language=region["language"], rows=[line["spans"] for line in taken])
        else:
            rows, at = [], 0
            for size in region["rows"]:
                rows.append([line["spans"] for line in taken[at:at + size]])
                at += size
            if not rows:
                self.problems.append(f"line {region['n']}: @{{table}} without a row")
                return
            entry["rows"] = rows
        self.lines.append(entry)

    # -- commands ------------------------------------------------------
    def command(self, args: list[str], label: str | None) -> None:
        if label is not None:
            self.link(label, args)
            return
        if not args:
            return
        name, value = args[0].lower(), (args[1].lower() if len(args) > 1 else "")
        if name in TOGGLES:
            self.style[name] = True
        elif name[:1] == "u" and name[1:] in TOGGLES:
            self.style[name[1:]] = False
        elif name == "plain":
            self.style.update({key: False for key in TOGGLES})
        elif name in ("fg", "bg"):
            if value not in PENS:
                self.problems.append(f"line {self.number}: unknown pen {value!r} in @{{{name}}}")
            else:
                default = "text" if name == "fg" else "background"
                self.style[name] = None if value == default else value
        elif name in ("jleft", "jcenter", "jright", "pard"):
            self.align = {"jcenter": "center", "jright": "right"}.get(name, "left")
            # a line not yet drawn on takes it; a paragraph already under way keeps its own
            # (@{jcenter} Title, then @{jleft} on the next line, centres the title)
            if self.current is not None and not self.current["spans"]:
                self.current["align"] = self.align
        elif self.region is not None and self.region["kind"] == "code" and name in BLOCKS | {"rule"}:
            # a code block holds lines as written: a heading or an item in it would vanish with the block
            self.problems.append(f"line {self.number}: @{{{args[0]}}} does not belong in a code block")
        elif name in BLOCKS:
            self.start_block(name, value)
        elif name == "rule":
            self.end_line()
            self.block = None
            self.counters = []
            self.lines.append({"n": self.number, "align": "left", "wrap": True, "spans": [], "kind": "rule"})
        elif name == "line":
            self._line(blank=self.current is None)  # on a line of its own it is a blank line, and ends no list
            self.end_line()
        elif name == "par":
            self.end_line()
            self.block = None
            self._line(blank=True)
            self.end_line()
        elif name == "tab":
            self.text("\t")
        elif name == "code" and value:
            self.open_region("code", value)
        elif name == "table":
            self.open_region("table")
        elif name in ("code", "body"):
            if name == "body":
                self.close_region()
            self.code = name == "code"
            self.block = None
            self.end_line()
        elif name == "amigaguide":
            bold = self.style["b"]
            self.style["b"] = True
            self.text("AmigaGuide(R)")
            self.style["b"] = bold
        elif name == "image":
            self.image(args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else "")
        elif name not in _IGNORED_INLINE:
            self.problems.append(f"line {self.number}: unknown attribute @{{{args[0]}}}")

    def markdown(self, md: Markdown, guide: Guide) -> None:
        """A block of rendered Markdown, a line of its own; the text as written without a Markdown library."""
        self.close_open_region()  # an @embed inside a code block or table: it would vanish there
        self.end_line()
        rendered = _markdown_html(md, guide, self.image_url)
        if rendered is None:
            self.lines += [{"n": self.number, "align": "left", "wrap": True, "spans": [{"text": text, "style": []}]}
                           for text in md.text.splitlines()]
        else:
            self.lines.append({"n": self.number, "align": "left", "wrap": True, "spans": [], "html": rendered})

    def image(self, path: str, alt: str) -> None:
        url = self.image_url(path) if path else None
        if url is None:
            self.problems.append(f"line {self.number}: image {path!r} not found next to the guide")
            self.button({"text": f"[{alt or path or 'image'}]", "broken": path})
        else:
            self.button({"text": alt, "image": url})

    def link(self, label: str, args: list[str]) -> None:
        action = args[0].lower() if args else ""
        if action in ("link", "alink") and len(args) > 1 and WEB_LINK.fullmatch(args[1]):
            self.button({"text": label, "url": args[1]})
        elif action in ("link", "alink") and len(args) > 1:
            target = self.resolve(args[1])
            if target is None and document(self.guide.folder, args[1]) is not None:
                # as on the Amiga, a link may name a file instead of a node: the viewer shows it
                self.button({"text": label, "file": {"guide": self.guide.id, "file": args[1]}})
                return
            if target is None:
                self.problems.append(f"line {self.number}: link to {args[1]!r} leads nowhere")
                self.button({"text": label, "broken": args[1]})
                return
            line = int(args[2]) if len(args) > 2 and re.fullmatch(r"[0-9]{1,9}", args[2]) else 0
            self.button({"text": label, "link": {"guide": target[0], "node": target[1], "line": line}})
        else:
            if action not in INERT:
                self.problems.append(f"line {self.number}: unknown button action {action or '(none)'!r}")
            self.button({"text": label, "inert": action or "?"})


def _has_content(tokens: list[tuple[str, Any]]) -> bool:
    """Text, a button, an image or a break: a line of nothing but attribute changes makes no line of its own."""
    for token in tokens:
        if token[0] == "text" and token[1].strip():
            return True
        if token[0] == "command" and (token[2] is not None or (token[1] and token[1][0].lower() in
                                                                 ("line", "par", "tab", "amigaguide", "image"))):
            return True
    return False


def _text_line(out: _Layout, raw: str) -> None:
    """One line of guide text (or a piece of one, cut at a table's edge) through the layout."""
    tokens = list(_inline(raw))
    content = _has_content(tokens)
    if content:
        if out.code or out.wrap != "smart":
            out.end_line()
        elif out.current is not None:
            out.join = True
    for token in tokens:
        if token[0] == "text":
            if content:
                out.text(token[1])
        else:
            out.command(token[1], token[2])
    if out.code or out.wrap != "smart":
        out.end_line()
    if content and out.block is not None and (out.block["kind"] in HEADINGS or out.wrap != "smart"):
        out.end_line()  # a heading is one line; without smartwrap every line is a paragraph
        out.block = None


def layout(node: Node, guide: Guide, resolve: Resolve, image: ImageUrl = lambda path: None) -> dict[str, Any]:
    """A node as lines of styled spans and buttons -- what the viewer draws, and what search reads.

    Every line: ``{"n": source line, "align": left|center|right, "wrap": bool, "spans": [...]}``;
    a span is ``{"text", "style"}`` plus, for a button, ``link: {guide, node, line}``,
    ``url`` (the web), ``broken: target`` or ``inert: action``, or ``image: url`` with the
    alt text as ``text``. A line may be of a ``kind``: ``h1``-``h3``, ``bullet``/``number``
    (with ``level`` and, on an item's first line, ``marker``), ``quote``, ``rule``, ``code``
    (``language``, ``rows`` of spans) or ``table`` (``rows`` of cells of spans, the first the
    header); ``html`` holds a rendered Markdown file.
    """
    out = _Layout(node.wrap or guide.wrap, resolve, image, guide)
    for number, raw in node.lines:
        out.number = number
        if isinstance(raw, Markdown):
            out.markdown(raw, guide)
            continue
        raw = raw.rstrip()
        if not raw.strip() and out.region is not None and out.region["kind"] == "table":
            continue  # a blank line in a table is no row
        if not raw.strip():
            out.end_line()
            out.block = None
            out._line(blank=True)
            out.end_line()
            continue
        if any(kind == "unclosed" for kind, _, _ in _pieces(raw)):
            out.problems.append(f"line {number}: @{{ without its }} is shown as text; a literal one is \\@{{")
        for segment, starts in _segments(raw):
            region = out.region
            if region is not None and region["kind"] == "table" and starts is None:
                cells = _cells(segment)
                if (len(region["rows"]) == 1 and not region["separated"] and cells
                        and all(map(_SEPARATOR_CELL.fullmatch, cells))):
                    region["separated"] = True  # once, right under the header; a second one is a row
                else:
                    out.table_row([list(_inline(cell)) for cell in cells])
            else:
                _text_line(out, segment)
    out.close_open_region()
    return {"wrap": out.wrap, "lines": out.lines, "problems": out.problems}


def _markdown_html(md: Markdown, guide: Guide, image: ImageUrl) -> str | None:
    """A Markdown file as the chat renders it (sanitised), with its links and images made to work here.

    A web link opens a new tab; a relative link to a documentation file next to the guide opens
    that file in the viewer; any other link keeps its text and loses its target. A relative image
    comes from the guide's folder; a remote one is not loaded -- its alt text stands in for it.
    """
    rendered = markdown_to_html(md.text, allowed_tags=DEFAULT_ALLOWED_TAGS | {"img"}, line_breaks=False)
    if rendered is None:
        return None

    def local(target: str) -> str:
        return posixpath.normpath(posixpath.join(md.base, target.split("#")[0]))

    def tag(match: re.Match[str]) -> str:
        name = match.group(1)
        attrs = {key: html_unescape(value) for key, value in _ATTR.findall(match.group(2))}
        if name == "img":
            source = attrs.get("src", "")
            # a remote address is no file next to the guide: inside() finds none, the alt text stands in
            url = image(local(source)) if source else None
            if url is None:
                return html_escape(attrs.get("alt", ""))
            attrs.update({"src": url, "class": "pk-guide-image"})
        else:
            target = attrs.get("href")
            if target is None or target.startswith("#"):
                return match.group(0)
            if WEB_LINK.fullmatch(target):
                attrs.update({"target": "_blank", "rel": "noopener noreferrer"})
            else:
                path = local(target)
                if document(guide.folder, path) is None:
                    del attrs["href"]  # nothing the viewer can open
                else:
                    attrs.update({"href": f"?{urlencode({'guide': guide.id, 'file': path})}",
                                  "data-guide": guide.id, "data-file": path})
        return f"<{name}" + "".join(f' {key}="{html_escape(value)}"' for key, value in attrs.items()) + ">"

    return _TAG.sub(tag, rendered)


def _spans_text(spans: list[dict[str, Any]]) -> str:
    return "".join(span["text"] for span in spans)


def plain_text(laid_out: dict[str, Any]) -> str:
    """The text of a laid-out node, for search."""
    out = []
    for line in laid_out["lines"]:
        if "html" in line:
            out.append(html_unescape(re.sub(r"<[^>]+>", " ", line["html"])))
        elif line.get("kind") == "code":
            out += [_spans_text(row) for row in line["rows"]]
        elif line.get("kind") == "table":
            out += [" | ".join(_spans_text(cell) for cell in row) for row in line["rows"]]
        else:
            out.append(_spans_text(line["spans"]))
    return "\n".join(out)
