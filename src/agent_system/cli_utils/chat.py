"""Interactive chat mode (REPL) for agent-cli.

Two pieces live here:

- ChatRenderer: the front-panel display style translated to the terminal.
  One line per operation, keyed like the WebUI (request_id or server): the
  progress line is rewritten in place and the end line replaces it, instead
  of every phase appending chronologically. Thinking tokens become a live
  counter line ("Thinking… (~120 tokens · 4s)") rather than a token flood.
- run_chat_loop / run_chat_turn: the REPL around the one-shot turn.
  One persistent event loop for all turns (LLM/http clients pool their
  connections per loop -- a loop per turn would break on turn 2), sync
  input() between turns so Ctrl-C behaves natively at the prompt.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import shutil
import sys
import time
import unicodedata
from typing import Any, Optional, TextIO

from ..llm.pricing import normalize_usage, resolve_call_cost
from .common import (
    format_output_with_hooks,
    reassert_vt,
    render_with_rich,
    restore_console_input_mode,
    snapshot_console_input_mode,
    supports_color,
)

logger = logging.getLogger(__name__)

# Line identity for the thinking counter -- never collides with request_ids.
_THINKING_KEY = "__thinking__"
# Repaint throttle for the counter line: every delta would be ~50 repaints/s.
_PAINT_INTERVAL_S = 0.1
# Graceful-cancel budget after Ctrl-C before the turn task is cancelled hard.
_CANCEL_GRACE_S = 15.0

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
            self.out.write(text + "\n")
            self.out.flush()
            return
        # Any subprocess a tool spawned may have reset the console's VT flag
        # (MSYS bash does, on every start) -- re-assert before painting, or
        # everything from here on renders as literal escapes.
        reassert_vt()
        self._check_resize()
        self._erase_input_row()
        offset = self._total - self._lines[key] if key in self._lines else None
        if offset is not None and offset <= self._usable_height():
            self.out.write(f"\x1b[{offset}A\r\x1b[K{text}\x1b[{offset}B\r")
        else:
            self._lines[key] = self._total
            self._total += 1
            self.out.write(text + "\n")
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
            self.out.write(text + "\n")
            self.out.flush()
            return
        reassert_vt()  # see _paint
        self._check_resize()
        self._erase_input_row()
        width = max(self._width() - 1, 10)
        for logical in text.splitlines() or [""]:
            for chunk in self._wrap(logical.expandtabs(4), width):
                self.out.write(self._colored(chunk, color) if chunk else "")
                self.out.write("\n")
                self._total += 1
        self._draw_input_row()
        self.out.flush()

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


# --------------------------------------------------------------------- turn


async def run_chat_turn(
    agent: Any,
    task: Any,
    session_id: str,
    renderer: ChatRenderer,
    *,
    show_status: bool = True,
    llm_override: Any = None,
    llm_profile_info: Optional[str] = None,
    state: Optional[dict] = None,
) -> dict:
    """Run one task through the agent, feeding all output into the renderer.

    `state` is a caller-owned dict: the request_id is written into it as soon
    as the start event arrives, so a Ctrl-C handler outside this coroutine can
    cancel the in-flight request even though this coroutine never returned.
    """
    from ..mcp.status import status_bus

    result: dict[str, Any] = {"summary": None, "cancelled": False, "errors": [],
                              "usage": {}}
    last_call_usage: Any = None  # to spot the final event repeating it
    if state is None:
        state = {}

    queue = await status_bus.subscribe() if show_status else None
    consumer: Optional[asyncio.Task] = None

    if queue is not None:
        async def _consume() -> None:
            while True:
                event = await queue.get()
                try:
                    renderer.handle_status(event)
                except Exception:
                    logger.debug("Renderer failed on status event", exc_info=True)

        consumer = asyncio.create_task(_consume())

    try:
        async for ev in agent.run_events(
            task,
            session_id=session_id,
            llm_override=llm_override,
            llm_profile_info_override=llm_profile_info,
        ):
            t = ev.get("type")
            if t == "start":
                state["request_id"] = ev.get("request_id")
            elif t in ("thinking_delta", "reasoning_delta"):
                # Gated like the one-shot's token stream: --no-status means
                # stdout carries answers only.
                if show_status:
                    renderer.thinking_delta()
            elif t == "thinking_complete":
                renderer.thinking_done()
                assistant = ev.get("assistant") or {}
                content = assistant.get("content")
                # Only intermediate steps (with tool calls) are narrated here;
                # the last step's content arrives again as the final event and
                # would show twice otherwise.
                if show_status and assistant.get("tool_calls") and isinstance(content, str):
                    renderer.narration(content)
                # Usage rides on thinking_complete per LLM call; sum them so a
                # multi-step turn reports the whole turn, not just the last call.
                _accumulate_usage(result["usage"], ev.get("usage"),
                                  *_call_pricing_key(agent))
                last_call_usage = ev.get("usage")
            elif t == "heartbeat":
                if show_status:
                    renderer.thinking_tick()
            elif t == "final":
                result["summary"] = ev.get("summary") or ""
                # The final event usually REPEATS the last LLM call's usage
                # (server.py: final_event["usage"] = llm_out["usage"]), which
                # already arrived as thinking_complete -- summing both counted
                # that call twice and inflated every turn.
                # But after max_steps a SEPARATE final-answer call runs that has
                # no thinking_complete of its own, and that one must count. Same
                # payload => the repeat; anything else => a real extra call.
                final_usage = ev.get("usage")
                if final_usage is not None and final_usage != last_call_usage:
                    _accumulate_usage(result["usage"], final_usage,
                                      *_call_pricing_key(agent))
            elif t == "error":
                message = str(ev.get("message") or "unknown error")
                result["errors"].append(message)
                renderer.error_line(f"ERROR: {message}")
            elif t == "cancelled":
                result["cancelled"] = True
            elif t == "end":
                break
            # Everything else (thinking step markers, continuation, ...) is
            # progress bookkeeping without display value in this mode.
    finally:
        if queue is not None:
            # Drain deterministically: the final PHASE_END may be queued but
            # not yet consumed when run_events finishes (same race the
            # one-shot path guards against).
            try:
                while not queue.empty():
                    renderer.handle_status(queue.get_nowait())
            except Exception:
                logger.debug("Error draining status queue", exc_info=True)
            if consumer is not None:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            try:
                status_bus.unsubscribe(queue)
            except Exception:
                logger.debug("Failed to unsubscribe status queue", exc_info=True)
        renderer.close()

    return result


# --------------------------------------------------------------------- REPL


_COMMAND_ALIASES = {
    "/exit": "exit", "/quit": "exit", "/q": "exit", "/bye": "exit",
    "/new": "new",
    "/session": "session",
    "/sessions": "sessions",
    "/resume": "resume",
    "/history": "history", "/hist": "history",
    "/last": "last",
    "/tools": "tools",
    "/skills": "skills",
    "/costs": "costs", "/cost": "costs",
    "/help": "help", "/?": "help", "/h": "help",
}


# A command word: a single "/name" token, no further slash, no dot. That is
# what separates a mistyped command from a path -- "/h" is a typo the user
# wants flagged, "/etc/nginx/nginx.conf" is ordinary input for a sysadmin
# agent and firing an LLM turn on either extreme is wrong.
_COMMAND_WORD = re.compile(r"^/[A-Za-z?][A-Za-z0-9_-]*$")


def parse_chat_command(line: str) -> tuple[Optional[str], str]:
    """Split a prompt line into (command, payload).

    Returns ("unknown", line) for something that LOOKS like a command but
    isn't one, so the REPL can say so instead of silently spending a turn on
    it. Anything else starting with "/" is a normal message. "//" is the
    literal escape for a message that really has to start with a command word.
    """
    stripped = line.strip()
    if stripped.startswith("//"):
        return None, stripped[1:]
    if not stripped.startswith("/"):
        return None, stripped
    word, _, rest = stripped.partition(" ")
    command = _COMMAND_ALIASES.get(word.lower())
    if command is not None:
        return command, rest.strip()
    if _COMMAND_WORD.match(word):
        return "unknown", word
    return None, stripped


def suggest_command(word: str) -> Optional[str]:
    """Closest known command for a typo, or None.

    Prefixes first: "/h" is the common abbreviation-style slip, and difflib
    scores it far below any cutoff against "/help" (2 chars against 5).
    """
    lowered = word.lower()
    prefixed = sorted((c for c in _COMMAND_ALIASES if c.startswith(lowered)), key=len)
    if prefixed:
        return prefixed[0]
    matches = difflib.get_close_matches(lowered, _COMMAND_ALIASES, n=1, cutoff=0.6)
    return matches[0] if matches else None


_HELP_TEXT = '''\
Commands:
  /exit, /quit, /q   end the chat (Ctrl-D / Ctrl-Z+Enter work too)
  /new               start a fresh session (current one stays saved)
  /session           show the current session and how to resume it
  /sessions          list recent sessions
  /resume <id>       continue an earlier session
  /tools [filter]    tools this agent really has (not what it claims)
  /skills            skill bundles it loads
  /costs             session cost so far, including sub-agents
  /history [n]       show the last n exchanges (default 6)
  /last              tool calls and results of the last turn, in full
  /help, /h          this help

Input:
  """               start/end a multi-line message (paste code between them)
  \\ at line end      continue on the next line
  //text             send a message that starts with a command word

  Ctrl-C             cancel the running turn; twice at the prompt exits'''


_FENCE = '"""'
# Key polling cadence while a turn runs. The loop is awake anyway (run_events
# polls its LLM task every 0.1s), so this costs nothing new.
_KEY_POLL_S = 0.05


class _KeyReader:
    """Non-blocking keyboard reader for the duration of one turn.

    Windows uses msvcrt, which reads the console through CONIN$ WITHOUT
    touching the console mode -- important here, because every child shell a
    tool spawns resets that mode (see common.reassert_vt) and a mode-based
    reader would silently lose its setting mid-turn.

    POSIX uses select() on stdin, which needs cbreak+noecho to deliver keys
    before Enter; the original terminal attributes are restored in close(),
    because tools spawn shells that inherit this terminal and expect canonical
    mode.

    Any failure disables the reader rather than taking the turn down: typing
    ahead is a convenience, the turn is the work.
    """

    def __init__(self, active: bool = True) -> None:
        self.buffer = ""
        self.enabled = False
        self._saved_attrs = None
        self._fd = None
        if not active:
            return
        try:
            if not sys.stdin.isatty():
                return  # piped input: kbhit never sees it, select would lie
        except Exception:
            return
        try:
            if os.name == "nt":
                import msvcrt  # noqa: F401
                self.enabled = True
            else:
                import termios
                import tty
                self._fd = sys.stdin.fileno()
                # POSIX-only; mypy runs against the Windows stubs of these.
                self._saved_attrs = termios.tcgetattr(self._fd)  # type: ignore[attr-defined]
                tty.setcbreak(self._fd)  # type: ignore[attr-defined]
                self.enabled = True
        except Exception as e:
            logger.debug("Type-ahead disabled: %s", e)
            self.enabled = False

    def close(self) -> None:
        if self._saved_attrs is not None and self._fd is not None:
            try:
                import termios
                termios.tcsetattr(  # type: ignore[attr-defined]
                    self._fd, termios.TCSADRAIN, self._saved_attrs)  # type: ignore[attr-defined]
            except Exception:
                logger.debug("Could not restore terminal attributes", exc_info=True)
        self._saved_attrs = None
        self.enabled = False

    def _read_chars(self) -> str:
        """All characters available right now, without blocking."""
        if not self.enabled:
            return ""
        chars = []
        try:
            if os.name == "nt":
                import msvcrt
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    # Arrow/function keys arrive as a two-call prefix; consume
                    # the second half so it does not land in the buffer.
                    if ch in ("\x00", "\xe0"):
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                        continue
                    chars.append(ch)
            else:
                import select
                # Read from the RAW fd, not sys.stdin: TextIOWrapper.read(1)
                # pulls up to 8 KB into its own decode buffer and hands back one
                # character -- select() only polls the fd and would report "not
                # ready" while the rest sits there unread.
                fd = sys.stdin.fileno()
                while select.select([fd], [], [], 0)[0]:
                    data = os.read(fd, 1024)
                    if not data:
                        break
                    chars.append(data.decode(sys.stdin.encoding or "utf-8",
                                             errors="replace"))
        except Exception as e:
            logger.debug("Key read failed, disabling type-ahead: %s", e)
            self.enabled = False
        return "".join(chars)

    def poll(self) -> list[str]:
        """Consume pending keys. Returns every line finished in this batch.

        A list, not one line: _read_chars drains everything available at once,
        so a paste of several lines arrives in a single call. Keeping one slot
        silently dropped all but the last.
        """
        submitted: list[str] = []
        chars = self._read_chars()
        index = 0
        while index < len(chars):
            ch = chars[index]
            index += 1
            if ch == "\x1b":
                # CSI/SS3 escape sequence (arrow keys, Home, F-keys). Only the
                # ESC byte was skipped before, so the rest ("[A") landed in the
                # buffer as ordinary printable text.
                if index < len(chars) and chars[index] in ("[", "O"):
                    index += 1
                    while index < len(chars) and not ("@" <= chars[index] <= "~"):
                        index += 1
                    index += 1  # the final byte terminates the sequence
                continue
            if ch in ("\r", "\n"):
                text = self.buffer.strip()
                self.buffer = ""
                if text:  # a bare Enter is not a message
                    submitted.append(text)
            elif ch in ("\b", "\x7f"):
                self.buffer = self.buffer[:-1]
            elif ch == "\x15":            # Ctrl-U: clear the line
                self.buffer = ""
            elif ch == "\x03":
                # Ctrl-C as DATA rather than a signal -- happens when a child
                # shell left the console without ENABLE_PROCESSED_INPUT. Treat
                # it as "discard what I typed", never as text.
                self.buffer = ""
                submitted.clear()
            elif ch >= " ":
                self.buffer += ch
        return submitted


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


def _read_input(prompt: str, cont_prompt: str = "... ", echo: bool = False) -> str:
    """Read one message, which may span several lines.

    Pasting a stack trace or a code block used to fire ONE TURN PER LINE:
    line 1 started a task and the rest sat in the console buffer, launching
    back to back afterwards. Two ways out, both familiar from peer CLIs:
    a triple-quote fence around a block, and a trailing backslash.
    """
    def _next() -> Optional[str]:
        try:
            line = input(cont_prompt)
        except EOFError:
            return None
        if echo:
            print(line)
        return line

    first = input(prompt)
    if echo:
        print(first)

    stripped = first.strip()
    if stripped.startswith(_FENCE):
        rest = stripped[len(_FENCE):]
        # Whole block on one line: \"\"\"text\"\"\"
        if rest.endswith(_FENCE) and len(rest) >= len(_FENCE):
            return rest[: -len(_FENCE)]
        lines = [rest] if rest else []
        while (line := _next()) is not None:
            if line.strip().endswith(_FENCE):
                head = line.strip()[: -len(_FENCE)]
                if head:
                    lines.append(head)
                break
            lines.append(line)
        return "\n".join(lines)

    if first.endswith("\\"):
        lines = [first[:-1]]
        while (line := _next()) is not None:
            if line.endswith("\\"):
                lines.append(line[:-1])
                continue
            lines.append(line)
            break
        return "\n".join(lines)

    return first


class _ChatContext:
    """Everything one REPL needs, bundled so helpers stay signature-sane."""

    def __init__(self, *, agent: Any, entry_name: str, session_service: Any,
                 session_user: str, session_id: str, was_new_session: bool,
                 llm_profile: str, llm_override: Any,
                 llm_profile_info: Optional[str], show_status: bool,
                 session_manager: Any = None,
                 template_vars: Optional[dict] = None) -> None:
        self.agent = agent
        self.entry_name = entry_name
        self.session_service = session_service
        self.session_manager = session_manager
        self.session_user = session_user
        self.session_id = session_id
        self.was_new_session = was_new_session
        self.llm_profile = llm_profile
        self.llm_override = llm_override
        self.llm_profile_info = llm_profile_info
        self.show_status = show_status
        # CLI --vars overrides: /new has to re-apply them, otherwise a fresh
        # session silently falls back to the agent config's defaults.
        self.template_vars = dict(template_vars or {})
        # Which session id actually reached disk (None until the first save):
        # the exit message must not claim a save that never happened.
        self.last_saved: Optional[str] = None
        # Cumulative usage across the chat, for the exit line.
        self.total_usage: dict[str, float] = {}

    def pricing_key(self) -> tuple[Optional[str], bool]:
        """(model id, is_batch) for the central cost estimator."""
        client = self.llm_override or getattr(self.agent, "llm", None)
        model = getattr(client, "model", None)
        # isinstance-str guard mirrors the usage tracker: a plain mock must not
        # look batchy and halve the estimate.
        provider = getattr(client, "batch_provider", None)
        return (str(model) if model else None,
                isinstance(provider, str) and bool(provider))

    def llm_label(self) -> str:
        """Profile plus the model behind it.

        llm_profile_info only exists when --llm/--llm-params was passed; in the
        normal case the banner would just echo the profile name back at the
        user, who already typed it.
        """
        if self.llm_profile_info:
            return self.llm_profile_info
        try:
            client = getattr(self.agent, "llm", None)
            model = getattr(client, "model", None)
            if model:
                return f"{self.llm_profile} ({model})"
        except Exception:
            logger.debug("Could not resolve model for banner", exc_info=True)
        return self.llm_profile


def _init_fresh_session(ctx: _ChatContext) -> str:
    """Create a new session id and seed the tracker like the CLI bootstrap does."""
    from ..utils.id import short_id

    new_id = short_id()
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is not None:
        tracker.set_session_messages(new_id, [])
        agent_config = getattr(ctx.agent, "agent_config", None)
        config_vars = getattr(agent_config, "template_vars", None) if agent_config else None
        # Same order as the CLI bootstrap: config defaults first, then the
        # --vars the user passed on the command line.
        merged = dict(config_vars or {})
        merged.update(ctx.template_vars)
        if merged:
            tracker.set_session_template_vars(new_id, merged)
        tracker.set_session_metadata(new_id, {
            "user_id": ctx.session_user,
            "agent_name": ctx.entry_name,
            "llm_profile": ctx.llm_profile,
        })
    return new_id


def _resume_hint(ctx: "_ChatContext", session_id: str) -> str:
    """The exact command that brings this session back."""
    parts = ["agent-cli chat", f"--session {session_id}", f"--agent {ctx.entry_name}"]
    if ctx.session_user != "cli_user":
        parts.append(f"--session-user {ctx.session_user}")
    return " ".join(parts)


def _call_pricing_key(agent: Any) -> tuple[Optional[str], bool]:
    """(model, is_batch) of the client that just ran -- read per call.

    Pricing the whole session with one model was wrong as soon as a fallback
    switched profiles or a step used a different client.
    """
    client = getattr(agent, "llm", None)
    model = getattr(client, "model", None)
    provider = getattr(client, "batch_provider", None)
    # isinstance-str guard mirrors the usage tracker: a bare mock must not look
    # batchy and halve the estimate.
    return (str(model) if model else None,
            isinstance(provider, str) and bool(provider))


def _merge_totals(total: dict, turn: dict) -> None:
    """Fold a finished turn's already-resolved totals into the session sum."""
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens",
                "cache_write_tokens", "cost", "cost_unpriced_calls"):
        value = turn.get(key)
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
    if turn.get("cost_is_estimate"):
        total["cost_is_estimate"] = True


def _accumulate_usage(total: dict, usage: Any, model: Optional[str] = None,
                      is_batch: bool = False) -> None:
    """Add one LLM call's usage AND its resolved cost into the running total.

    Cost is resolved per call, not once over the summed tokens: a session can
    span several models, and mixing a billed figure from one provider with
    estimated tokens from another produced a number that was both too low and
    labelled as exact.
    """
    if not isinstance(usage, dict):
        return
    call = normalize_usage(usage)
    total["prompt_tokens"] = total.get("prompt_tokens", 0) + call.prompt_tokens
    total["completion_tokens"] = (
        total.get("completion_tokens", 0) + call.completion_tokens)
    total["cached_tokens"] = total.get("cached_tokens", 0) + call.cached_tokens
    total["cache_write_tokens"] = (
        total.get("cache_write_tokens", 0) + call.cache_write_tokens)

    cost, estimated = resolve_call_cost(usage, model, is_batch=is_batch)
    if cost is not None:
        total["cost"] = total.get("cost", 0.0) + cost
        # One estimated call makes the SUM an estimate -- anything else would
        # present a partly guessed total as billing.
        total["cost_is_estimate"] = total.get("cost_is_estimate", False) or estimated
    else:
        # Tokens counted, price unknown: the total is incomplete and has to say so.
        total["cost_unpriced_calls"] = total.get("cost_unpriced_calls", 0) + 1


def _format_usage(totals: dict, elapsed: float, sym: dict) -> str:
    """One dim footer line: tokens, cache hit rate, cost, wall time.

    The cost is already resolved per call by _accumulate_usage -- this only
    renders it. An estimated total carries a leading ~, and calls whose price
    could not be determined are named rather than silently omitted.
    """
    def _short(n: float) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"

    prompt = totals.get("prompt_tokens", 0) or 0
    completion = totals.get("completion_tokens", 0) or 0
    cached = totals.get("cached_tokens", 0) or 0

    parts = []
    if prompt or completion:
        head = f"{sym['up']}{_short(prompt)}"
        if prompt and cached:
            head += f" ({cached * 100 // prompt}% cached)"
        parts.append(f"{head} {sym['down']}{_short(completion)}")

    cost = totals.get("cost")
    if cost is not None:
        marker = "~$" if totals.get("cost_is_estimate") else "$"
        text = f"{marker}{cost:.4f}"
        unpriced = totals.get("cost_unpriced_calls", 0)
        if unpriced:
            text += f" +{unpriced} unpriced"
        parts.append(text)

    mins, secs = divmod(int(elapsed), 60)
    parts.append(f"{mins}m{secs:02d}s" if mins else f"{secs}s")
    return sym["sep"].join(parts)


def _looks_like_command(text: str) -> bool:
    """Whether a stored user message is really a slash command.

    Commands never reach the agent -- but before "/h" became an alias, unknown
    ones were passed through as messages and are now sitting in old sessions.
    They are not part of the conversation and would only add noise.
    """
    stripped = text.strip()
    return bool(stripped) and bool(_COMMAND_WORD.match(stripped.split(" ")[0]))


def _decode(text: Any) -> Any:
    """Parse a JSON payload, or None when it is not JSON."""
    if not isinstance(text, str):
        return text if isinstance(text, dict) else None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _one_line(value: Any, limit: int = 60) -> str:
    """Compact single-line form of a tool argument or result value."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_call_summary(call: Any) -> tuple[str, Any]:
    """(name, arguments) of a tool call in either dict shape."""
    if not isinstance(call, dict):
        return "?", None
    fn = call.get("function") or {}
    # `or` rather than a get-default: an explicit null would slip through.
    name = fn.get("name") or call.get("name") or "?"
    return str(name), fn.get("arguments") or call.get("arguments")


def _render_tool_call(renderer: ChatRenderer, call: Any, full: bool) -> None:
    """One tool request: compact for /history, key-per-line for /last."""
    name, arguments = _tool_call_summary(call)
    data = _decode(arguments)
    if not full:
        if isinstance(data, dict):
            inner = ", ".join(f"{k}={_one_line(v, 40)}" for k, v in data.items())
        else:
            inner = _one_line(arguments or "", 80)
        renderer.println(f"  → {name}({_one_line(inner, 100)})", color="34")
        return

    renderer.println(f"→ {name}", color="34")
    if not isinstance(data, dict):
        for line in str(arguments or "").splitlines():
            renderer.println(f"    {line}", color="90")
        return
    for key, value in data.items():
        # Escaped newlines are what made this a wall of text -- render the
        # value as the lines it actually is.
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines = text.splitlines()
        if len(lines) <= 1:
            renderer.println(f"    {key}: {text}", color="90")
        else:
            renderer.println(f"    {key}:", color="90")
            for line in lines:
                renderer.println(f"      {line}", color="90")


def _render_tool_result(renderer: ChatRenderer, message: Any, full: bool) -> None:
    """One tool result, mirroring _render_tool_call's two modes."""
    raw = _message_text(message)
    data = _decode(raw)
    if not full:
        if isinstance(data, dict):
            status = data.get("status", "")
            body = data.get("content") or data.get("stdout") or data.get("changes") or ""
            renderer.println(f"  ← {status} {_one_line(body, 70)}".rstrip(), color="32")
        else:
            renderer.println(f"  ← {_one_line(raw, 90)}", color="32")
        return

    if isinstance(data, dict):
        for key, value in data.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines = text.splitlines()
            if len(lines) <= 1:
                renderer.println(f"    {key}: {text}", color="32")
            else:
                renderer.println(f"    {key}:", color="32")
                for line in lines:
                    renderer.println(f"      {line}", color="32")
    else:
        for line in raw.splitlines():
            renderer.println(f"    {line}", color="32")


def _message_text(message: Any) -> str:
    """Readable text of a ChatMessage whose content may be multimodal."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or f"[{item.get('type', 'part')}]")
            else:
                parts.append(getattr(item, "text", None) or f"[{getattr(item, 'type', 'part')}]")
        return " ".join(p for p in parts if p)
    return "" if content is None else str(content)


def _session_messages(ctx: "_ChatContext") -> list:
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is None:
        return []
    try:
        return list(tracker.get_session_messages(ctx.session_id) or [])
    except Exception:
        logger.debug("Could not read session messages", exc_info=True)
        return []


def _show_history(ctx: "_ChatContext", renderer: ChatRenderer, payload: str) -> None:
    """Print the recent exchange: what was asked, what came back.

    The live region collapses each turn into a few lines and the answer
    scrolls away -- this is the only way back to it without leaving the chat.
    """
    try:
        limit = max(int(payload), 1) if payload else 6
    except ValueError:
        print(f"Usage: /history [count]   (got: {payload})")
        return

    messages = _session_messages(ctx)
    if not messages:
        print("No messages in this session yet.")
        return

    def _is_real_turn(message: Any) -> bool:
        """A user message that actually went to the agent."""
        if getattr(message, "role", None) != "user":
            return False
        text = _message_text(message).strip()
        return bool(text) and not _looks_like_command(text)

    # Count backwards in USER turns, so "6" means six exchanges rather than
    # six raw messages (a single turn can hold a dozen tool messages).
    start = 0
    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        if _is_real_turn(messages[i]):
            seen += 1
            if seen >= limit:
                start = i
                break
    if not seen:
        print("No agent exchanges in this session yet.")
        return

    print(f"Last {seen} exchange(s) of session {ctx.session_id}:")
    for message in messages[start:]:
        role = getattr(message, "role", "?")
        text = _message_text(message).strip()

        if role == "user":
            # Leftovers from when unknown commands were passed through as
            # messages; they are not part of the conversation.
            if not text or _looks_like_command(text):
                continue
            renderer.println("")
            renderer.println(f"› {text}")
        elif role == "assistant":
            if text:
                renderer.println(text, color="90")
            for call in getattr(message, "tool_calls", None) or []:
                _render_tool_call(renderer, call, full=False)
        elif role == "tool":
            _render_tool_result(renderer, message, full=False)
    renderer.commit()
    print("(/last shows the last turn's tool traffic in full)")


def _show_last(ctx: "_ChatContext", renderer: ChatRenderer) -> None:
    """Full tool calls and results of the most recent turn.

    Chat collapses every tool call to one status line, so what a tool actually
    RETURNED is invisible -- this is the chat equivalent of run's --show-mcp.
    """
    messages = _session_messages(ctx)
    last_user = None
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if getattr(message, "role", None) != "user":
            continue
        text = _message_text(message).strip()
        if text and not _looks_like_command(text):
            last_user = i
            break
    if last_user is None:
        print("No turn to show yet.")
        return

    shown = 0
    for message in messages[last_user + 1:]:
        role = getattr(message, "role", "?")
        if role == "assistant":
            for call in getattr(message, "tool_calls", None) or []:
                _render_tool_call(renderer, call, full=True)
                shown += 1
        elif role == "tool":
            _render_tool_result(renderer, message, full=True)
    renderer.commit()
    if not shown:
        print("The last turn used no tools.")


def _usage_tracker(ctx: "_ChatContext") -> Any:
    """The registered context_usage_tracker's UsageTracker, or None.

    It hooks every LLM call in the process, so it is the ONLY source that also
    sees sub-agent calls -- those run in their own sub-sessions and never
    appear in the coordinator's run_events stream.
    """
    registry = getattr(ctx.agent, "registry", None)
    if registry is None:
        return None
    try:
        server = registry.get("context_usage_tracker")
    except Exception:
        logger.debug("Usage tracker not registered", exc_info=True)
        return None
    tracker = getattr(server, "tracker", None)
    return tracker if hasattr(tracker, "get_statistics") else None


def _show_costs(ctx: "_ChatContext", renderer: ChatRenderer) -> None:
    """Session cost INCLUDING sub-agents, from the usage tracker.

    The turn footer only sums what the coordinator's own event stream carries.
    Sub-agents bill against the same wallet but report through their own
    sub-sessions, so a number built from the stream alone is silently too low.
    """
    tracker = _usage_tracker(ctx)
    if tracker is None:
        print("The context_usage_tracker plugin is not active -- no per-call "
              "records to add up.")
        print(f"This chat's own turns: {_format_usage(ctx.total_usage, 0, renderer.sym)}")
        return
    try:
        stats = tracker.get_statistics(session_id=ctx.session_id)
    except Exception as e:
        logger.error("Failed to read usage statistics: %s", e, exc_info=True)
        print(f"Could not read usage statistics: {e}")
        return
    totals = (stats or {}).get("totals")
    if not totals:
        print("No LLM calls recorded for this session yet.")
        return

    def _short(n: float) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"

    known = totals.get("cost_known_calls", 0)
    estimated = totals.get("cost_estimated_calls", 0)
    samples = (stats.get("timespan") or {}).get("sample_count", 0)
    # Calls the tracker saw but could price neither way -- naming them keeps
    # the total from looking complete when it is not.
    unpriced = max(0, samples - known)

    print(f"Session {ctx.session_id} (incl. sub-agents):")
    renderer.println(
        f"  calls        {samples}"
        + (f"  ({estimated} estimated)" if estimated else ""), color="90")
    renderer.println(
        f"  tokens       {renderer.sym['up']}{_short(totals.get('prompt_tokens', 0))}"
        f"  {renderer.sym['down']}{_short(totals.get('completion_tokens', 0))}"
        f"  cache {totals.get('cache_hit_rate', 0.0):.0f}%", color="90")
    marker = "~$" if estimated else "$"
    renderer.println(f"  cost         {marker}{totals.get('cost', 0.0):.4f}"
                     + (f"  ({unpriced} unpriced)" if unpriced else ""), color="90")
    renderer.commit()
    if estimated:
        print("  ~ = estimated from config/llm_pricing.yaml, not provider billing")


async def _show_tools(ctx: "_ChatContext", renderer: ChatRenderer, payload: str) -> None:
    """List the tools the agent REALLY has, grouped by server.

    Asking the model instead is unreliable: it answers from the names in its
    schema, so "do you have tavily_search" gets a No when the tool is called
    tavily_search_web_search. This reads the same filtered schema the model is
    given, so the answer is the ground truth.
    """
    lister = getattr(ctx.agent, "_list_usable_tools_with_details", None)
    if lister is None:
        print("This agent cannot report its tools.")
        return
    try:
        tools = await lister({})
    except Exception as e:
        logger.error("Failed to list tools: %s", e, exc_info=True)
        print(f"Could not list tools: {e}")
        return
    if not tools:
        print("This agent has no tools (tools.allowed is empty = deny-all).")
        return

    needle = payload.strip().lower()
    if needle:
        tools = [t for t in tools
                 if needle in t.get("name", "").lower()
                 or needle in (t.get("description") or "").lower()]
        if not tools:
            print(f"No tool matches '{payload}'.")
            return

    # Group by the server prefix, which is how they are configured.
    groups: dict[str, list[dict]] = {}
    known = sorted(_server_names(ctx), key=len, reverse=True)
    for tool in tools:
        name = tool.get("name", "?")
        # Longest registered server prefix wins, so coder_file_ops_read_file
        # groups under coder_file_ops and not under a shorter "coder". The
        # equality case covers single-tool servers, where the tool carries the
        # server's bare name (sequential_thinking, todo).
        server = next((s for s in known if name == s or name.startswith(s + "_")), None)
        if server is None:
            # No registered server matches. Splitting on "_" invented groups
            # ("sequential" next to "sequential_thinking"); say it plainly.
            server = "(unknown server)"
        groups.setdefault(server, []).append(tool)

    total = sum(len(v) for v in groups.values())
    print(f"{total} tool(s) available to {ctx.entry_name}"
          + (f" matching '{payload}'" if needle else "") + ":")
    for server in sorted(groups):
        renderer.println(f"{server}", color="34")
        for tool in groups[server]:
            name = tool.get("name", "?")
            first_line = " ".join((tool.get("description") or "").split())
            renderer.println(f"  {name}"
                             + (f"  -- {_one_line(first_line, 70)}" if first_line else ""),
                             color="90")
    renderer.commit()


def _server_names(ctx: "_ChatContext") -> list:
    registry = getattr(ctx.agent, "registry", None)
    try:
        return list(registry.list()) if registry is not None else []
    except Exception:
        logger.debug("Could not read registry server names", exc_info=True)
        return []


def _show_skills(ctx: "_ChatContext", renderer: ChatRenderer) -> None:
    """Which skill bundles this agent loads, and how."""
    agent_config = getattr(ctx.agent, "agent_config", None)
    skills = getattr(agent_config, "skills", None) if agent_config else None
    if not skills:
        print(f"{ctx.entry_name} uses no skills.")
        return
    always = list(getattr(skills, "always", None) or (
        skills.get("always") if isinstance(skills, dict) else []) or [])
    on_demand = list(getattr(skills, "on_demand", None) or (
        skills.get("on_demand") if isinstance(skills, dict) else []) or [])
    if always:
        renderer.println("always (in every prompt):", color="34")
        for name in always:
            renderer.println(f"  {name}", color="90")
    if on_demand:
        renderer.println("on demand (description only, body pulled when needed):", color="34")
        for name in on_demand:
            renderer.println(f"  {name}", color="90")
    if not always and not on_demand:
        print(f"{ctx.entry_name} uses no skills.")
    renderer.commit()


async def _list_sessions(ctx: _ChatContext) -> None:
    """Show the most recent sessions of this user."""
    if ctx.session_manager is None:
        print("Session listing is unavailable.")
        return
    try:
        sessions = await ctx.session_manager.list_sessions(ctx.session_user)
    except Exception as e:
        logger.error("Failed to list sessions: %s", e, exc_info=True)
        print(f"Could not list sessions: {e}")
        return
    if not sessions:
        print(f"No sessions for user '{ctx.session_user}'.")
        return
    print(f"Recent sessions for '{ctx.session_user}':")
    for entry in sessions[:10]:
        sid = entry.get("session_id", "?")
        marker = "*" if sid == ctx.session_id else " "
        title = (entry.get("title") or "Untitled")[:48]
        count = entry.get("message_count", len(entry.get("messages", []) or []))
        print(f" {marker} {sid}  {count:>4} msg  {entry.get('agent_name', '?')}  {title}")
    print("Use /resume <id> to continue one.")


async def _resume_session(ctx: _ChatContext, session_id: str) -> bool:
    """Load an earlier session into the running agent."""
    try:
        exists, count = await ctx.session_service.load_and_restore_session(
            ctx.agent, ctx.session_user, session_id
        )
    except Exception as e:
        logger.error("Failed to resume session %s: %s", session_id, e, exc_info=True)
        print(f"Could not resume '{session_id}': {e}")
        return False
    if not exists:
        print(f"No session '{session_id}' for user '{ctx.session_user}'.")
        return False
    ctx.session_id = session_id
    ctx.was_new_session = False
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is not None:
        # The turn loop reads metadata for tool context; without this the
        # resumed session would still carry the previous one's values.
        tracker.set_session_metadata(session_id, {
            "user_id": ctx.session_user,
            "agent_name": ctx.entry_name,
            "llm_profile": ctx.llm_profile,
        })
    print(f"({count} messages restored)")
    return True


async def _save_session(ctx: _ChatContext) -> bool:
    try:
        return bool(await ctx.session_service.save_session(
            agent=ctx.agent,
            user_id=ctx.session_user,
            session_id=ctx.session_id,
            agent_name=ctx.entry_name,
            llm_profile=ctx.llm_profile,
            was_new_session=ctx.was_new_session,
        ))
    except Exception as e:
        logger.error("Failed to save chat session: %s", e, exc_info=True)
        print(f"Warning: failed to save session: {e}", file=sys.stderr)
        return False


async def _poll_typed_input(reader: _KeyReader, renderer: ChatRenderer,
                            ctx: "_ChatContext", state: dict) -> None:
    """Show what the user types mid-turn and hand finished lines to the agent.

    The agent drains injected messages at step boundaries, so a line sent here
    lands in the conversation at the next step -- it does not interrupt the
    running one. That is the same contract the WebUI has.
    """
    prompt = "» "
    last_shown = None
    try:
        while reader.enabled:
            for submitted in reader.poll():
                command, _payload = parse_chat_command(submitted)
                if command is not None or submitted.startswith("/"):
                    # Slash commands are REPL-level, not messages. Sending
                    # "/exit" to the LLM because it was typed a second earlier
                    # would give identical keystrokes two different meanings.
                    renderer.println(
                        f"» {submitted}  (commands only work at the prompt)",
                        color="33")
                    continue
                request_id = state.get("request_id")
                delivered = False
                if request_id:
                    try:
                        delivered = bool(
                            await ctx.agent.append_user_message(request_id, submitted)
                        )
                    except Exception:
                        logger.debug("append_user_message failed", exc_info=True)
                if delivered:
                    renderer.println(f"» {submitted}", color="36")
                    renderer.println("  (queued -- the agent picks it up at its "
                                     "next step)", color="90")
                else:
                    # Nothing running to take it: queue it for the next prompt
                    # rather than dropping what the user typed. A list, so a
                    # second line does not overwrite the first.
                    state.setdefault("typed_queue", []).append(submitted)
                    state["typed_ahead"] = state["typed_queue"][0]
                    renderer.println(f"» {submitted}  (kept for the next turn)",
                                     color="36")
                last_shown = None
            if reader.buffer != last_shown:
                renderer.set_input_row(prompt + reader.buffer if reader.buffer else None)
                last_shown = reader.buffer
            await asyncio.sleep(_KEY_POLL_S)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Type-ahead poller stopped", exc_info=True)


def _execute_turn(loop: asyncio.AbstractEventLoop, ctx: _ChatContext,
                  task: str, renderer: ChatRenderer) -> dict:
    """One turn on the persistent loop, with two-stage Ctrl-C handling."""
    state: dict[str, Any] = {}
    turn = loop.create_task(run_chat_turn(
        ctx.agent, task, ctx.session_id, renderer,
        show_status=ctx.show_status,
        llm_override=ctx.llm_override,
        llm_profile_info=ctx.llm_profile_info,
        state=state,
    ))
    # Type-ahead needs the live region to place its input line, so it rides
    # along with ANSI mode.
    reader = _KeyReader(active=renderer.ansi)
    poller = (loop.create_task(_poll_typed_input(reader, renderer, ctx, state))
              if reader.enabled else None)
    try:
        result = loop.run_until_complete(turn)
    except KeyboardInterrupt:
        result = _cancel_turn(loop, ctx, turn, state, renderer)
    except Exception as e:
        # One broken turn (LLM auth, network, agent bug) must not end the
        # whole chat: report it and hand the user the next prompt.
        logger.error("Chat turn failed: %s", e, exc_info=True)
        renderer.close()
        print(f"Turn failed: {e}", file=sys.stderr)
        result = {"summary": None, "cancelled": False, "errors": [str(e)]}
    finally:
        # The reader owns terminal state on POSIX -- it has to be restored on
        # every exit, including Ctrl-C, or the shell stays in cbreak.
        _stop_typing(loop, reader, poller, renderer, state)
    for key in ("typed_ahead", "typed_partial"):
        if state.get(key):
            result[key] = state[key]
    return result


def _stop_typing(loop: asyncio.AbstractEventLoop, reader: _KeyReader,
                 poller: Optional["asyncio.Task"], renderer: ChatRenderer,
                 state: dict) -> None:
    """End the type-ahead poller and hand a half-typed line to the next prompt."""
    # reader.close() restores the POSIX terminal; it must run even if the
    # teardown below is interrupted. KeyboardInterrupt is a BaseException, so
    # `except Exception` around the loop call would NOT have covered a second
    # Ctrl-C -- and the shell would stay in cbreak/noecho afterwards.
    try:
        if poller is not None:
            poller.cancel()
            try:
                loop.run_until_complete(asyncio.gather(poller, return_exceptions=True))
            except BaseException:
                logger.debug("Type-ahead poller teardown failed", exc_info=True)
        renderer.set_input_row(None)
        # A line typed but NEVER SUBMITTED is kept under its own key: it must
        # not become the next task. state["typed_ahead"] auto-runs, and running
        # a half sentence the user never pressed Enter on -- as a full billed
        # turn, right after they hit Ctrl-C to stop spending -- is the opposite
        # of what they asked for. It gets shown, not executed.
        if reader.buffer.strip():
            state["typed_partial"] = reader.buffer.strip()
    finally:
        reader.close()


def _cancel_turn(loop: asyncio.AbstractEventLoop, ctx: _ChatContext,
                 turn: "asyncio.Task", state: dict, renderer: ChatRenderer) -> dict:
    """Ctrl-C during a turn: graceful cancel first, hard cancel as fallback."""
    renderer.close()
    print("\nCancelling turn... (Ctrl-C again to force)", file=sys.stderr)
    request_id = state.get("request_id")
    if request_id:
        # Graceful: flips the cancellation token, the agent unwinds and
        # yields its cancelled/end events through the normal path.
        # KeyboardInterrupt is NOT an Exception -- a second Ctrl-C here has to
        # be caught explicitly or it escapes the REPL and kills the chat, the
        # opposite of the "again to force" we just promised.
        try:
            loop.run_until_complete(
                asyncio.wait_for(ctx.agent.cancel_request(request_id), timeout=5)
            )
        except KeyboardInterrupt:
            logger.debug("second Ctrl-C during graceful cancel")
        except Exception:
            logger.debug("cancel_request failed", exc_info=True)
    try:
        # wait_for cancels the task itself if the grace period runs out.
        result = loop.run_until_complete(asyncio.wait_for(turn, _CANCEL_GRACE_S))
        # Race: the turn may have COMPLETED between Ctrl-C and here. A
        # finished answer is shown, not discarded as "cancelled".
        if result.get("summary") is None:
            result["cancelled"] = True
        return result
    except (KeyboardInterrupt, asyncio.TimeoutError, asyncio.CancelledError):
        turn.cancel()
        loop.run_until_complete(asyncio.gather(turn, return_exceptions=True))
    except Exception:
        logger.debug("Turn unwind failed", exc_info=True)
    renderer.close()
    return {"summary": None, "cancelled": True, "errors": []}


def _render_answer(loop: asyncio.AbstractEventLoop, ctx: _ChatContext,
                   renderer: ChatRenderer, summary: str) -> None:
    # rich prints straight to stdout, invisible to the region's offsets.
    renderer.commit()
    print()
    try:
        # output_format=None lets the central helper honour --color; the
        # one-shot path hardcodes 'ansi' here, which chat deliberately doesn't.
        formatted, content_format = loop.run_until_complete(format_output_with_hooks(
            output=summary,
            agent_instance=ctx.agent,
            session_id=ctx.session_id,
            request_id="cli_display",
        ))
        if content_format == "ansi":
            render_with_rich(formatted)
        else:
            print(formatted)
    except Exception:
        logger.debug("Answer formatting failed, printing raw", exc_info=True)
        print(summary)
    print()  # region is already committed; plain spacing line


def run_chat_loop(
    *,
    agent: Any,
    entry_name: str,
    session_service: Any,
    session_user: str,
    session_id: str,
    was_new_session: bool,
    llm_profile: str,
    llm_override: Any = None,
    llm_profile_info: Optional[str] = None,
    show_status: bool = True,
    initial_task: Optional[str] = None,
    session_manager: Any = None,
    template_vars: Optional[dict] = None,
) -> None:
    """The chat REPL. Owns one event loop for its whole lifetime."""
    ctx = _ChatContext(
        agent=agent, entry_name=entry_name, session_service=session_service,
        session_user=session_user, session_id=session_id,
        was_new_session=was_new_session, llm_profile=llm_profile,
        llm_override=llm_override, llm_profile_info=llm_profile_info,
        show_status=show_status, session_manager=session_manager,
        template_vars=template_vars,
    )
    ansi = supports_color()
    renderer = ChatRenderer(ansi=ansi)
    unicode_ok = renderer.sym is _UNICODE_SYMBOLS
    prompt = "❯ " if unicode_ok else "> "
    cont_prompt = "… " if unicode_ok else "... "

    # POSIX line editing + in-process history. NOT on Windows: importing
    # readline there activates pyreadline3 when installed, which replaces
    # input()'s console handling with its own raw-mode loop -- the "every key
    # prints a space, Enter/Ctrl-C dead" trap. The Windows console host
    # provides line editing and history natively.
    if os.name != "nt":
        try:
            import readline  # noqa: F401
        except ImportError:
            pass

    # Known-good input mode, restored before every prompt: child shells
    # (terminal.execute -> MSYS bash) switch the console's INPUT mode too,
    # which kills Enter/Backspace and turns Ctrl-C into a plain character.
    input_mode = snapshot_console_input_mode()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Console logging would write into the live region behind its back.
    silenced = _silence_stdout_logging()
    chat_started = time.monotonic()

    dash = "─" if unicode_ok else "-"
    rule = dash * min(shutil.get_terminal_size((80, 20)).columns, 72)
    print(rule)
    print(f"Chat with {ctx.entry_name}   LLM: {ctx.llm_label()}   Session: {ctx.session_id}")
    print("Type /help for commands, /exit to quit. Ctrl-C cancels the running turn.")
    print(rule)

    pending: Optional[str] = initial_task.strip() if initial_task else None
    interrupts = 0  # consecutive Ctrl-C at the prompt; two in a row exit
    try:
        while True:
            if pending is not None:
                task, pending = pending, None
                print(f"{prompt}{task}")  # keep the transcript complete
            else:
                restore_console_input_mode(input_mode)
                try:
                    task = _read_input(prompt, cont_prompt=cont_prompt,
                                       echo=not sys.stdin.isatty())
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    interrupts += 1
                    if interrupts >= 2:
                        print("\nBye.")
                        break
                    print("\n(Ctrl-C again or /exit to quit)")
                    continue
            interrupts = 0
            task = task.strip()
            if not task:
                continue

            command, payload = parse_chat_command(task)
            if command == "exit":
                break
            if command == "new":
                ctx.session_id = _init_fresh_session(ctx)
                ctx.was_new_session = True
                print(f"New session: {ctx.session_id}")
                continue
            if command == "session":
                print(f"Session: {ctx.session_id}  (user: {ctx.session_user})")
                print(f"Resume with: {_resume_hint(ctx, ctx.session_id)}")
                continue
            if command == "sessions":
                loop.run_until_complete(_list_sessions(ctx))
                continue
            if command == "resume":
                if not payload:
                    print("Usage: /resume <session-id>   (/sessions lists them)")
                elif loop.run_until_complete(_resume_session(ctx, payload)):
                    print(f"Resumed session: {ctx.session_id}")
                continue
            if command == "tools":
                loop.run_until_complete(_show_tools(ctx, renderer, payload))
                continue
            if command == "skills":
                _show_skills(ctx, renderer)
                continue
            if command == "costs":
                _show_costs(ctx, renderer)
                continue
            if command == "history":
                _show_history(ctx, renderer, payload)
                continue
            if command == "last":
                _show_last(ctx, renderer)
                continue
            if command == "help":
                print(_HELP_TEXT)
                continue
            if command == "unknown":
                hint = suggest_command(payload)
                did_you_mean = f"  Did you mean {hint}?" if hint else ""
                print(f"Unknown command: {payload}{did_you_mean}")
                print(f"/help lists the commands; //{payload[1:]} sends it as a message.")
                continue

            started = time.monotonic()
            result = _execute_turn(loop, ctx, task, renderer)

            # A line the user SUBMITTED during the turn but that never reached
            # the agent becomes the next task -- they pressed Enter on it.
            pending = result.get("typed_ahead") or None
            # A half-typed fragment is only shown; auto-running it would spend
            # money on something the user never sent.
            if result.get("typed_partial"):
                print(renderer._colored(
                    f"(unsent: {result['typed_partial']})", "90"))

            if result.get("cancelled"):
                # Cancel means STOP. Anything queued from this turn is dropped:
                # firing a new billed turn right after Ctrl-C is the opposite of
                # what was asked for.
                if pending:
                    print(renderer._colored(f"(dropped: {pending})", "90"))
                    pending = None
                print("Turn cancelled.", file=sys.stderr)
                continue  # nothing new worth saving; next turn saves anyway

            summary = result.get("summary")
            if summary:
                _render_answer(loop, ctx, renderer, summary)
            elif not result.get("errors"):
                print(renderer._colored("(no answer returned)", "90"))

            usage = result.get("usage") or {}
            _merge_totals(ctx.total_usage, usage)
            if ctx.show_status:
                print(renderer._colored(
                    _format_usage(usage, time.monotonic() - started,
                                  renderer.sym), "90"))

            try:
                saved = loop.run_until_complete(_save_session(ctx))
            except KeyboardInterrupt:
                # Ctrl-C during the save must not take the chat down with it.
                print("\n(save interrupted)", file=sys.stderr)
                saved = False
            if saved:
                ctx.last_saved = ctx.session_id
                ctx.was_new_session = False
    finally:
        _restore_logging(silenced)
        if ctx.total_usage:
            print(renderer._colored(
                "Session total: " + _format_usage(
                    ctx.total_usage, time.monotonic() - chat_started,
                    renderer.sym),
                "90"))
        if ctx.last_saved:
            print(f"Session saved: {ctx.last_saved}", file=sys.stderr)
            print(f"Resume with: {_resume_hint(ctx, ctx.last_saved)}", file=sys.stderr)
        try:
            # Mirror asyncio.run's teardown: background tasks spawned during
            # the turns (e.g. the cancellation manager's timeout monitor) must
            # be cancelled, or close() logs "Task was destroyed but it is
            # pending" through a half-torn-down logging stack.
            pending_tasks = asyncio.all_tasks(loop)
            for task_obj in pending_tasks:
                task_obj.cancel()
            if pending_tasks:
                loop.run_until_complete(
                    asyncio.gather(*pending_tasks, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            logger.debug("Event loop teardown failed", exc_info=True)
        loop.close()
