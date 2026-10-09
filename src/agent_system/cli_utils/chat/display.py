"""The chat's terminal display: the live region and the column arithmetic under it.

ChatRenderer is the front-panel display style translated to the terminal.
One line per operation, keyed like the WebUI (request_id or server): the
progress line is rewritten in place and the end line replaces it, instead
of every phase appending chronologically. Thinking tokens become a live
counter line ("Thinking… (~120 tokens · 4s)") rather than a token flood.

Its own module because all of it is cursor arithmetic -- how many columns a
line really takes, which row a key owns, what may print into the region
behind its back (console logging: _silence_stdout_logging) -- and none of it
knows about sessions, commands or the agent.
"""
from __future__ import annotations

import logging
import shutil
import sys
import time
import unicodedata
from contextlib import contextmanager
from typing import Any, Iterator, Optional, TextIO

from ..common import reassert_vt

logger = logging.getLogger(__name__)

# Line identity for the thinking counter -- never collides with request_ids.
_THINKING_KEY = "__thinking__"
# Repaint throttle for the counter line: every delta would be ~50 repaints/s.
_PAINT_INTERVAL_S = 0.1

_UNICODE_SYMBOLS = {"run": "▸", "ok": "✓", "err": "✗", "think": "✻", "cut": "…",
                    "sep": " · ", "up": "↑", "down": "↓"}
_ASCII_SYMBOLS = {"run": ">", "ok": "+", "err": "x", "think": "*", "cut": "...",
                  "sep": ", ", "up": "in ", "down": "out "}


def _pick_symbols(out: TextIO) -> dict[str, str]:
    """Unicode symbols where the stream can encode them, ASCII otherwise."""
    encoding = getattr(out, "encoding", None) or "utf-8"
    try:
        "".join(_UNICODE_SYMBOLS.values()).encode(encoding)
        return _UNICODE_SYMBOLS
    except (UnicodeEncodeError, LookupError):
        return _ASCII_SYMBOLS


def _char_width(ch: str) -> int:
    """Display columns one character occupies."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Columns `text` occupies -- NOT len().

    The region's cursor arithmetic assumes one printed line is one physical
    line. A CJK character or emoji is two columns wide, so a chunk measured
    with len() can wrap and silently shift every offset above it.
    """
    return sum(_char_width(ch) for ch in text)


def _cut_to_width(text: str, limit: int) -> str:
    """Longest prefix of `text` that fits into `limit` columns."""
    if limit <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        w = _char_width(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out)


class ChatRenderer:
    """Live per-operation status display for the chat REPL.

    ANSI mode keeps a LIVE REGION at the bottom of the output: every operation
    key owns one line in it, exactly like a row in the WebUI front panel. An
    update for any key moves the cursor up to its line and rewrites it in
    place, so "step 1/500" becomes "step 2/500" on the same line even when
    other operations report in between. The cursor always rests at the start
    of the line BELOW the region. Anything that is not a status line --
    narration, answers -- commits the region (its lines stay as printed) and
    a fresh region grows afterwards.

    Non-ANSI mode falls back to chronological lines (today's behaviour), so a
    piped/dumb-terminal chat still reads sensibly.
    """

    def __init__(self, ansi: bool, out: Optional[TextIO] = None,
                 width_override: Optional[int] = None,
                 height_override: Optional[int] = None) -> None:
        self.ansi = ansi
        self.out = out if out is not None else sys.stdout
        self.sym = _pick_symbols(self.out)
        self._width_override = width_override
        self._height_override = height_override
        # Live region state: key -> absolute line index since region start.
        self._lines: dict[str, int] = {}
        self._total = 0
        # Width the region's lines were laid out at. A mid-turn resize reflows
        # every already-printed line, so the recorded offsets stop matching
        # physical lines -- the region has to end rather than climb blindly.
        self._region_width: Optional[int] = None
        # Thinking counter state
        self._thinking_active = False
        self._thinking_tokens = 0
        self._thinking_t0 = 0.0
        self._last_paint = 0.0
        # Text shown on the cursor's resting line while a turn runs (what the
        # user is typing). It carries NO newline, so it occupies the line the
        # cursor rests on anyway and never enters _total/_lines -- the offset
        # arithmetic stays exactly as it is without it.
        self._input_row: Optional[str] = None

    # ------------------------------------------------------------------ util

    def _width(self) -> int:
        if self._width_override:
            return self._width_override
        try:
            return max(shutil.get_terminal_size().columns, 20)
        except Exception:
            return 100

    def _fit(self, text: str, indent: str = "") -> str:
        """Single line, capped to terminal width.

        The indent is applied AFTER folding -- ``str.split()`` would eat the
        leading spaces otherwise. The cap happens on PLAIN text; colour is
        applied afterwards around the already-capped string, so an escape
        sequence can never be cut in half. A wrapped line would break the CR
        rewrite -- the cursor only returns to the start of the last physical
        line.
        """
        single = (indent + " ".join(text.split())).expandtabs(4)
        limit = self._width() - 1
        if display_width(single) <= limit:
            return single
        cut = self.sym["cut"]
        return _cut_to_width(single, max(limit - display_width(cut), 1)) + cut

    def _colored(self, text: str, color: Optional[str]) -> str:
        # Deliberately NOT colorize(): that helper consults the global
        # color_mode, while this renderer's ansi flag was decided by the REPL
        # once. Two authorities over the same escape codes would disagree
        # exactly when it matters (tests, piped chat).
        if color and self.ansi:
            return f"\x1b[{color}m{text}\x1b[0m"
        return text

    # ------------------------------------------------- low-level line output

    def _usable_height(self) -> int:
        """How far up the cursor may travel and still hit its line.

        A line scrolled out of the viewport can't be reached -- ``ESC[nA``
        clips at the top edge and the rewrite lands on the WRONG line. Keep a
        safety margin of two rows.
        """
        if self._height_override:
            return self._height_override
        try:
            return max(shutil.get_terminal_size().lines - 2, 4)
        except Exception:
            return 20

    def _paint(self, key: str, text: str) -> None:
        """Write `text` onto `key`'s line in the live region.

        Existing key within cursor reach: move up, rewrite in place, move
        back. Otherwise: append a new bottom line and (re)bind the key to it
        -- which also covers a line that scrolled out of reach; it simply
        continues further down instead of corrupting the viewport.
        """
        if not self.ansi:
            self._write_plain(text)
            return
        with self._region_write():
            offset = self._total - self._lines[key] if key in self._lines else None
            if offset is not None and offset <= self._usable_height():
                self.out.write(f"\x1b[{offset}A\r\x1b[K{text}\x1b[{offset}B\r")
            else:
                self._lines[key] = self._total
                self._total += 1
                self.out.write(text + "\n")

    def _write_plain(self, text: str) -> None:
        """Non-ANSI output: the line as it comes, chronologically."""
        self.out.write(text + "\n")
        self.out.flush()

    @contextmanager
    def _region_write(self) -> Iterator[None]:
        """Around every write into the live region (_paint, println).

        Before it the console is made fit for escapes, a resized terminal ends
        the region, and the input row is blanked so the write starts on a
        clean line; after it the input row is drawn again and all is flushed.
        """
        # Any subprocess a tool spawned may have reset the console's VT flag
        # (MSYS bash does, on every start) -- re-assert before painting, or
        # everything from here on renders as literal escapes.
        reassert_vt()
        self._check_resize()
        self._erase_input_row()
        yield
        self._draw_input_row()
        self.out.flush()

    # ------------------------------------------------------------ input row

    def set_input_row(self, text: Optional[str]) -> None:
        """Show (or clear with None) the live input line below the region."""
        if not self.ansi:
            return
        self._erase_input_row()
        self._input_row = text
        self._draw_input_row()
        self.out.flush()

    def _erase_input_row(self) -> None:
        """Blank the resting line so a region write starts from a clean row."""
        if self.ansi and self._input_row is not None:
            self.out.write("\r\x1b[K")

    def _draw_input_row(self) -> None:
        """Redraw the input line WITHOUT a newline -- it must not become a row."""
        if self.ansi and self._input_row is not None:
            # _fit keeps it to one physical line: a soft wrap here would add a
            # line the region does not know about and shift every offset.
            self.out.write(self._colored(self._fit(self._input_row), "36"))

    def _check_resize(self) -> None:
        """End the region when the terminal width changed under us."""
        width = self._width()
        if self._region_width is None:
            self._region_width = width
        elif width != self._region_width:
            self._commit_region()
            self._region_width = width

    def _commit_region(self) -> None:
        """Freeze the live region: lines stay as printed, tracking resets."""
        self._lines.clear()
        self._total = 0
        self._region_width = None

    def commit(self) -> None:
        """End the live region before output that bypasses this renderer.

        Anything printed directly to stdout (rich-rendered answers) is
        invisible to the offset arithmetic and would corrupt every climb --
        the region has to end first.
        """
        self._commit_region()

    def println(self, text: str = "", color: Optional[str] = None) -> None:
        """Print committed text INSIDE the region, as anonymous lines.

        The region stays alive: operation rows above keep updating because
        every physical line printed here is counted in the offsets -- a
        coordinator step simply climbs OVER the narration to its own row.
        Long text is hard-wrapped so the count matches physical lines (a
        soft-wrapped line would silently shift every offset above it),
        measured in COLUMNS not code points, broken at word boundaries, and
        colour is applied per chunk AFTER slicing so no escape is ever cut.
        """
        if not self.ansi:
            self._write_plain(text)
            return
        with self._region_write():
            width = max(self._width() - 1, 10)
            for logical in text.splitlines() or [""]:
                for chunk in self._wrap(logical.expandtabs(4), width):
                    self.out.write(self._colored(chunk, color) if chunk else "")
                    self.out.write("\n")
                    self._total += 1

    @staticmethod
    def _wrap(line: str, width: int) -> list[str]:
        """Break `line` into chunks of at most `width` COLUMNS, on words.

        Leading whitespace is preserved and re-applied to every chunk (hanging
        indent): splitting on " " drops it, which silently un-indented every
        nested line -- visible only in ANSI mode, since the non-ANSI path
        writes the string untouched.

        textwrap can't be used: it counts code points, so a CJK line would
        come back too wide and wrap again in the terminal -- the exact
        corruption the hard wrap exists to prevent.
        """
        if not line:
            return [""]
        body = line.lstrip(" ")
        indent = line[: len(line) - len(body)]
        if not body:
            return [line]
        if indent:
            inner = max(width - display_width(indent), 8)
            return [indent + chunk for chunk in ChatRenderer._wrap(body, inner)]
        chunks: list[str] = []
        current = ""
        used = 0
        for word in line.split(" "):
            piece = word if not current else " " + word
            if used + display_width(piece) > width and current:
                chunks.append(current)
                current, used = "", 0
                piece = word
            # A single word wider than the line still has to be split.
            while display_width(piece) > width:
                head = _cut_to_width(piece, width - used) if used else _cut_to_width(piece, width)
                if not head:
                    chunks.append(current)
                    current, used = "", 0
                    continue
                chunks.append(current + head)
                piece = piece[len(head):]
                current, used = "", 0
            current += piece
            used += display_width(piece)
        if current or not chunks:
            chunks.append(current)
        return chunks

    def close(self) -> None:
        """End of turn: nothing may stay live or half-counted."""
        if self._thinking_active:
            self.thinking_done()
        if self.ansi and self._input_row is not None:
            self._erase_input_row()
            self._input_row = None
            self.out.flush()
        self._commit_region()

    # ---------------------------------------------------------- status events

    def handle_status(self, event: Any) -> None:
        """Render a StatusEvent (duck-typed: phase/server/message/...)."""
        try:
            phase = event.phase.value if hasattr(event.phase, "value") else str(event.phase)
            server = str(event.server)
            message = str(event.message or "")
            key = getattr(event, "request_id", None) or server
            depth = getattr(event, "depth_level", 0) or 0
            level = getattr(event, "level", "info")
        except Exception:
            logger.debug("Malformed status event: %r", event, exc_info=True)
            return

        # The thinking counter owns a line in the region like any other key,
        # so a status line arriving mid-thought no longer interrupts it --
        # both keep updating their own rows, front-panel style.

        indent = "  " * min(depth, 4)

        if phase in ("start", "progress"):
            line = self._fit(f"{self.sym['run']} {server}: {message}", indent)
            self._paint(key, self._colored(line, "34"))
        elif phase == "end":
            line = self._fit(f"{self.sym['ok']} {server}: {message}", indent)
            if level == "warning":
                line = self._colored(line, "33")
            else:
                # Green tick, default text: colour applied to the already-
                # capped line start, never inside the cap.
                line = line.replace(self.sym["ok"], self._colored(self.sym["ok"], "32"), 1)
            self._paint(key, line)
        elif phase == "error":
            line = self._fit(f"{self.sym['err']} {server}: {message}", indent)
            self._paint(key, self._colored(line, "31"))
        # Unknown phases are dropped on purpose: display-only surface.

    def error_line(self, text: str) -> None:
        """Errors print IN FULL, wrapped -- never capped to one line.

        What the user needs to act on (provider response body, missing env
        var, URL) sits at the END of these messages; _fit would drop exactly
        that and leave it only in the log.
        """
        for logical in str(text).splitlines() or [""]:
            self.println(logical, color="31")

    # ------------------------------------------------------- thinking counter

    def thinking_delta(self) -> None:
        """One streamed delta arrived (~1 token on streaming providers)."""
        now = time.monotonic()
        if not self._thinking_active:
            self._thinking_active = True
            self._thinking_tokens = 0
            self._thinking_t0 = now
            self._last_paint = 0.0
        self._thinking_tokens += 1
        if self.ansi and (now - self._last_paint) >= _PAINT_INTERVAL_S:
            self._paint_thinking(final=False)
            self._last_paint = now

    def thinking_tick(self) -> None:
        """Heartbeat: refresh the elapsed seconds while nothing streams."""
        if self._thinking_active and self.ansi:
            self._paint_thinking(final=False)

    def thinking_done(self) -> None:
        if not self._thinking_active:
            return
        self._paint_thinking(final=True)
        self._thinking_active = False
        # Unbind so the NEXT thinking block gets its own region line instead
        # of overwriting this block's final "Thought" state.
        self._lines.pop(_THINKING_KEY, None)

    def _paint_thinking(self, final: bool) -> None:
        secs = int(time.monotonic() - self._thinking_t0)
        label = "Thought" if final else "Thinking" + self.sym["cut"]
        text = self._fit(
            f"{self.sym['think']} {label} "
            f"(~{self._thinking_tokens} tokens{self.sym['sep']}{secs}s)"
        )
        # Non-ANSI never paints intermediate states (thinking_delta gates on
        # ansi); the one call from thinking_done gives it the summary line.
        self._paint(_THINKING_KEY, self._colored(text, "90"))

    # ------------------------------------------------------------- narration

    def narration(self, text: str) -> None:
        """Intermediate assistant text between tool calls, dimmed.

        This is the step content that used to stream grey token-by-token; in
        chat mode it appears once, whole, after the step's LLM call finishes.
        """
        cleaned = text.strip()
        if not cleaned:
            return
        for raw_line in cleaned.splitlines():
            # Blank lines stay blank -- no escape pair wrapped around nothing.
            self.println(raw_line, color="90" if raw_line.strip() else None)


def _silence_stdout_logging() -> list[tuple[Any, int]]:
    """Mute console log handlers writing to stdout, remembering their levels.

    setup_logging attaches a StreamHandler(sys.stdout) to the root logger.
    Those lines land in the SAME stream as the live region but are invisible
    to its line accounting, so every later cursor climb lands too high and
    overwrites a foreign line. File handlers keep logging -- nothing is lost,
    it just stops corrupting the display.
    """
    silenced: list[tuple[Any, int]] = []
    for handler in list(logging.getLogger().handlers):
        if isinstance(handler, logging.FileHandler):
            continue
        if not isinstance(handler, logging.StreamHandler):
            continue
        if getattr(handler, "stream", None) not in (sys.stdout, sys.stderr):
            continue
        silenced.append((handler, handler.level))
        handler.setLevel(logging.CRITICAL + 1)
    return silenced


def _restore_logging(silenced: list[tuple[Any, int]]) -> None:
    for handler, level in silenced:
        try:
            handler.setLevel(level)
        except Exception:
            logger.debug("Could not restore log handler level", exc_info=True)
