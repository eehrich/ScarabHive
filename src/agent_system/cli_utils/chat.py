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
import codecs
import difflib
import json
import logging
import os
import shutil
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TextIO

from ..llm.pricing import normalize_usage, resolve_call_cost
from .common import (
    format_output_with_hooks,
    reassert_vt,
    render_with_rich,
    restore_console_input_mode,
    snapshot_console_input_mode,
    supports_color,
)
from .attachments import sort_attachments
from .session_listing import DEFAULT_LIMIT, parse_limit, print_sessions
from .session_defaults import session_defaults
from ..core.session_presence import SessionBusy, presence_for

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
    from ..tools.status import status_bus

    result: dict[str, Any] = {"summary": None, "cancelled": False, "errors": [],
                              "usage": {}}
    last_call_usage: Any = None  # to spot the final event repeating it
    call_key: Optional[tuple[Optional[str], bool]] = None  # who answered it
    if state is None:
        state = {}
    # The same dict: a turn cancelled from outside never returns, and what
    # its calls cost has to reach the session total anyway.
    state["usage"] = result["usage"]

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
                call_usage = ev.get("usage")
                if call_usage is not None:
                    # Only a call that REPORTED usage may set the key: the
                    # server also emits thinking_complete without usage (and
                    # with its own model on it), and a later step back on the
                    # base model would then re-label the escalated call's
                    # tokens -- and size the context bar with the wrong window.
                    call_key = _call_pricing_key(agent, llm_override, ev)
                    _accumulate_usage(result["usage"], call_usage, *call_key)
                # Only a call that REPORTED usage becomes the reference. The
                # server emits thinking_complete without it (server.py: the
                # empty-assistant branch, and both `if usage` guards), and
                # letting that reset the reference to None cost twice: the
                # context fill fell back to nothing, and the final event no
                # longer recognized itself as a repeat -- so the last call
                # was billed a second time.
                if call_usage is not None:
                    last_call_usage = call_usage
            elif t == "heartbeat":
                if show_status:
                    renderer.thinking_tick()
            elif t == "final":
                result["summary"] = ev.get("summary") or ""
                # The final event usually REPEATS the last LLM call's usage
                # (server.py: final_event["usage"] = llm_out["usage"]), which
                # already arrived as thinking_complete -- summing both counted
                # that call twice and inflated every turn.
                # A final whose usage no thinking_complete reported is a call of
                # its own and must count (the max-steps call was one, before it
                # became a regular step). Same payload => the repeat; anything
                # else => a real extra call.
                final_usage = ev.get("usage")
                if final_usage is not None and final_usage != last_call_usage:
                    # The final event names no model; the last call's does.
                    _accumulate_usage(result["usage"], final_usage, *(
                        call_key or _call_pricing_key(agent, llm_override, ev)))
                    last_call_usage = final_usage
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

    # How full the window is after this turn: the LAST call's prompt (the whole
    # history as the model saw it) plus what it answered. Summing every call
    # would report the turn's throughput instead -- a multi-step turn sends the
    # same history again and again.
    if last_call_usage is not None:
        call = normalize_usage(last_call_usage)
        result["context_tokens"] = call.prompt_tokens + call.completion_tokens
    # The window of the client the turn ran on: after --llm or /model that is
    # the override, and the fill was computed against the old model's size.
    # Only when that client is also the one that ANSWERED: a fallback runs on
    # a third client whose window nobody here knows, and dividing its tokens
    # by this one states a fill that is not true. Without a window the footer
    # shows the tokens alone, which is what we actually know.
    client = llm_override or getattr(agent, "llm", None)
    window = getattr(client, "context_window", None)
    answered_by = call_key[0] if call_key else None
    if (isinstance(window, int) and window > 0
            and (answered_by is None or answered_by == getattr(client, "model", None))):
        result["context_window"] = window

    return result


# --------------------------------------------------------------------- REPL


# The catalogue, the parser and the typo hints live in
# agent_system.chat_commands so the web UI resolves a line exactly the way the
# terminal does. Imported into this namespace because the REPL below (and its
# tests) call them by these names.
from agent_system.chat_actions import (  # noqa: E402
    context_breakdown,
    live_context_window,
    measured_context,
    message_text,
    one_line,
    split_off_last_exchange,
    starts_a_turn,
    tool_call_summary,
    transcript_markdown,
)
from agent_system.chat_commands import (  # noqa: E402
    CLI as _CLI_SURFACE,
    PluginCommand,
    apply_vars,
    commands_for,
    group_tools_by_server,
    needs_escape as _needs_escape,
    parse_chat_command,
    parse_vars,
    resolve as resolve_chat_input,
    runnable_skill_names,
    store_vars,
    suggest_command,
)
from agent_system.plugin_commands import (  # noqa: E402
    collect_plugin_commands,
    help_lines as _plugin_help_lines,
    run_plugin_command,
    spellings as _plugin_spellings,
)


def _help_text(skills: Sequence[str] = (),
               plugin_commands: Sequence[PluginCommand] = ()) -> str:
    """Help built from the shared catalogue, plus the terminal-only input hints.

    Generated rather than written out: a second hand-kept list is a list that
    drifts, and the web UI renders the same commands from the same source.
    """
    width = max((len(c.display) for c in commands_for(_CLI_SURFACE)), default=0)
    lines = ["Commands:"]
    lines += [f"  {c.display:<{width}}   {c.summary}" for c in commands_for(_CLI_SURFACE)]
    lines += _plugin_help_lines(plugin_commands)
    if skills:
        lines += ["", "Skills (run one directly, arguments are passed to it):"]
        lines += [f"  /{name}" for name in skills]
    lines += [
        "",
        "Input:",
        '  """               start/end a multi-line message (paste code between them)',
        "  \\ at line end     continue on the next line",
        "  //text             send a message that starts with a command word",
        "",
        "  Ctrl-C             cancel the running turn; twice at the prompt exits",
    ]
    return "\n".join(lines)


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
        # One decoder across reads: a paste over 1 KB splits a multi-byte
        # character between two os.read() calls, and decoding each read on its
        # own turned both halves into U+FFFD in the text sent to the agent.
        self._decoder = codecs.getincrementaldecoder(
            getattr(sys.stdin, "encoding", None) or "utf-8")(errors="replace")
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
                    chars.append(self._decoder.decode(data))
        except Exception as e:
            logger.debug("Key read failed, disabling type-ahead: %s", e)
            self.enabled = False
        # getwch() hands out UTF-16 code units: an emoji arrives as two lone
        # surrogates, which the message sanitizer drops. Pair them up.
        return "".join(chars).encode("utf-16", "surrogatepass").decode(
            "utf-16", "replace")

    def poll(self) -> list[str]:
        """Consume pending keys. Returns the message finished in this batch.

        _read_chars drains everything available at once, so a paste of several
        lines arrives in one call -- and becomes ONE message, the way a paste
        at the prompt does. Sending each line on its own gave a pasted stack
        trace as twenty messages. Nobody types a line and its Enter within one
        poll interval, so lines finished together were pasted together.
        """
        lines: list[str] = []
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
                if ch == "\r" and index < len(chars) and chars[index] == "\n":
                    index += 1  # a pasted CRLF ends one line, not two
                lines.append(self.buffer)
                self.buffer = ""
            elif ch in ("\b", "\x7f"):
                self.buffer = self.buffer[:-1]
            elif ch == "\x15":            # Ctrl-U: clear the line
                self.buffer = ""
            elif ch == "\x03":
                # Ctrl-C as DATA rather than a signal -- happens when a child
                # shell left the console without ENABLE_PROCESSED_INPUT. Treat
                # it as "discard what I typed", never as text.
                self.buffer = ""
                lines.clear()
            elif ch >= " " or ch == "\t":
                self.buffer += ch
        # A bare Enter is not a message; the blank lines and the indentation
        # INSIDE a pasted block are part of it.
        message = "\n".join(lines).strip()
        return [message] if message else []


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


#: A stored message longer than this is not a thing anyone wants back in a
#: one-line prompt. It is also how a /skill invocation looks in the session:
#: the EXPANDED skill body is what gets stored, 6-33 KB of it, so the first
#: arrow-up would paste a whole SKILL.md over the prompt.
_HISTORY_MAX_CHARS = 2000


def _history_seed(ctx: "_ChatContext") -> list[str]:
    """The prompt history of a session: its own user messages, oldest first.

    Nothing is stored for this. The session already IS the record of what was
    asked, so resuming one brings its history back, and no second copy can
    drift away from the transcript. The gap that leaves is slash commands:
    they are REPL-level and never enter the session, so they live in the
    history only until the process ends.
    """
    seed: list[str] = []
    for message in _session_messages(ctx):
        if getattr(message, "role", None) != "user":
            continue
        text = _message_text(message).strip()
        # Consecutive repeats add nothing but distance to the older entries.
        if not text or (seed and seed[-1] == text):
            continue
        if len(text) > _HISTORY_MAX_CHARS:
            continue
        # "//compact" is stored as "/compact"; recalled raw it would RUN the
        # command instead of re-sending the message. needs_escape lives beside
        # the unescape it inverts -- a wider rule here would hand the agent a
        # message one slash longer than the one it was sent.
        if _needs_escape(text):
            text = "/" + text
        seed.append(text)
    return seed


def _posix_readline_fallback() -> None:
    """Stdlib line editing when prompt_toolkit is not available.

    POSIX only, and that restriction is the point: on Windows importing
    readline activates pyreadline3, which replaces input()'s console
    handling with its own raw-mode loop -- every key prints a space, Enter
    and Ctrl-C go dead. No arrow keys is a nuisance; no prompt is an outage.
    """
    if os.name == "nt":
        return
    try:
        import readline  # noqa: F401
    except ImportError:
        pass


class _PromptEditor:
    """The prompt's line editor, with the session's history behind it.

    An object rather than two closures because the history is not static:
    ``/new`` and ``/resume`` swap the session underneath the REPL, and turns
    that never pass through the prompt (an ``initial_task``, a line typed
    ahead during a turn) still belong in it.

    Two prompt sessions on purpose: the continuation lines of a fenced paste
    get the same editor but NOT the history. Otherwise pasting twenty lines
    of a stack trace buries the last twenty things actually typed. Only the
    first one completes: the continuation lines of a paste are content.
    """

    def __init__(self, prompt_session_cls: Any, history_cls: Any,
                 seed: Sequence[str], key_bindings: Any = None,
                 completer: Any = None) -> None:
        self._prompt_session_cls = prompt_session_cls
        self._history_cls = history_cls
        self._key_bindings = key_bindings
        self._completer = completer
        # Same bindings as the main prompt: Ctrl-Z has to mean end-of-input at
        # the "... " prompt too, which is exactly where a person reaches for it
        # to get out of a fence they opened by accident.
        self._continuation = prompt_session_cls(
            history=history_cls(), key_bindings=key_bindings)
        #: Commands typed in this process; they never enter a session.
        self._commands: list[str] = []
        self.reseed(seed)

    def reseed(self, seed: Sequence[str]) -> None:
        """Point the history at a different session's messages.

        A fresh prompt session, not just a fresh history object: the editor's
        buffer builds its working lines from the history it was constructed
        with, so replacing the entries alone would leave the old ones
        reachable.

        Slash commands stay: they are typed at this prompt, never stored in a
        session, so they belong to the process -- the /resume just typed
        included. Which lines those were is the REPL's answer, not a guess
        from their shape: only it knows whether /x:y named a plugin command.
        """
        self._history = self._history_cls()
        for entry in [*seed, *self._commands]:
            self._history.append_string(entry)
        self._session = self._prompt_session_cls(
            history=self._history, key_bindings=self._key_bindings,
            completer=self._completer)

    def remember(self, text: str) -> None:
        """Record a turn that never passed through the prompt."""
        stripped = (text or "").strip()
        if stripped:
            self._history.append_string(stripped)

    def remember_command(self, text: str) -> None:
        """Record that this line ran as a command, so a reseed keeps it."""
        stripped = (text or "").strip()
        if stripped and (not self._commands or self._commands[-1] != stripped):
            self._commands.append(stripped)

    def read(self, prompt: str) -> str:
        return self._session.prompt(prompt)

    def read_continuation(self, prompt: str) -> str:
        return self._continuation.prompt(prompt)


def _path_candidates(word: str) -> list[tuple[str, str]]:
    """Files and directories under what has been typed of a path so far.

    Written back with the separator the person typed. The editor offers what
    starts with their word, and ``src\\age`` never starts with
    ``src/agent_system`` -- on Windows that is every second path.

    Not prompt_toolkit's PathCompleter, though it is installed: it reads the
    WHOLE line as the path, so it would need the argument cut out for it
    anyway, it appends no separator behind a directory (its own decision),
    and going through it would move the knowledge of what exists out of the
    REPL into the editor, where the other completions cannot follow it.
    """
    separator = "\\" if "\\" in word and "/" not in word else "/"
    directory, slash, prefix = word.replace("\\", "/").rpartition("/")
    try:
        entries = sorted(Path(directory or ".").iterdir())
    except OSError:
        return []  # no such directory yet: the person is still typing it
    head = (directory + slash).replace("/", separator)
    candidates = []
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        is_dir = entry.is_dir()
        candidates.append((head + entry.name + (separator if is_dir else ""),
                           "dir" if is_dir else ""))
    return candidates[:200]


# Commands whose argument is the REST OF THE LINE, spaces and all -- see
# _handle_attach: a quoting grammar would cost more than typing /attach twice.
_WHOLE_LINE_ARGUMENT = ("attach", "export")


def _completion_word(line: str) -> str:
    """The part of *line* a Tab replaces.

    The last word -- except for the commands that read the whole rest of the
    line as one value. Splitting THOSE at the last space offered the entries
    of the current directory as the continuation of ``C:\\Program Fil`` and
    wrote one into the middle of the path.
    """
    head, separator, payload = line.partition(" ")
    if separator and parse_chat_command(head)[0] in _WHOLE_LINE_ARGUMENT:
        return payload.lstrip()
    return line.rsplit(" ", 1)[-1]


def _completions_for(ctx: "_ChatContext", skill_names: Sequence[str],
                     line: str) -> list[tuple[str, str]]:
    """(value, hint) pairs that could continue the line being typed.

    The REPL knows what exists -- its commands, this agent's plugin commands,
    the skills on disk, the sessions of this user -- so the knowledge stays
    here and the editor only asks. A plain message gets NOTHING: an offer in
    the middle of a sentence is noise, and prompt_toolkit completes while the
    person types.
    """
    head, separator, _ = line.partition(" ")
    if not separator:
        if not head.startswith("/"):
            return []
        spellings = _plugin_spellings(ctx.plugin_commands)
        return ([(alias, command.summary)
                 for command in commands_for(_CLI_SURFACE) for alias in command.aliases]
                + [(f"/{spelling}", command.summary)
                   for spelling, command in zip(spellings, ctx.plugin_commands)]
                + [(f"/{name}", "skill") for name in skill_names])

    command, _payload = parse_chat_command(head)
    word = _completion_word(line)
    if command == "model":
        profiles = _llm_profiles(ctx)
        return [(name, _one_line(getattr(profiles[name], "description", "") or "", 60))
                for name in sorted(profiles)]
    if command == "agent":
        return [(name, "agent") for name in _agent_names(ctx)]
    if command == "resume":
        # Whatever the last listing knows; /sessions and a bare /resume fill
        # it. Reading the store HERE is not possible -- the completer runs
        # inside prompt_toolkit's own loop, not the REPL's.
        return [(entry["session_id"],
                 _one_line(" ".join((entry.get("title") or "").split()), 60))
                for entry in _resumable_sessions(ctx)]
    if command == "attach":
        return [("clear", "drop what is queued")] + _path_candidates(word)
    if command == "export":
        return _path_candidates(word)
    if command == "vars":
        tracker: Any = getattr(ctx.agent, "_session_tracker", None)
        current: dict[str, Any] = {}
        try:
            current = dict(tracker.get_session_template_vars(ctx.session_id) or {})
        except Exception:
            logger.debug("Could not read template vars for completion", exc_info=True)
        return ([("unset", "remove one"), ("clear", "empty them")]
                + [(f"{name}=", str(value)[:60]) for name, value in sorted(current.items())])
    return []


def _build_completer(suggest: Callable[[str], list[tuple[str, str]]]) -> Any:
    """A prompt_toolkit completer that asks *suggest* what fits the line.

    Never raises into the prompt: an exception thrown while completing takes
    the editor down mid-keystroke, and losing the prompt is worse than losing
    a suggestion.

    Threaded, because prompt_toolkit completes WHILE TYPING and runs a plain
    completer inline in its own loop: listing a directory for /attach is disk
    I/O on every keystroke, and one unreachable network share would freeze
    the prompt for the whole SMB timeout -- including the Ctrl-C out of it.
    """
    from prompt_toolkit.completion import Completer, Completion, ThreadedCompleter

    class _ChatCompleter(Completer):  # type: ignore[misc]
        def get_completions(self, document: Any, complete_event: Any) -> Any:
            line = document.text_before_cursor
            if "\n" in line:
                return  # a pasted block is content, not a command
            if document.text_after_cursor.strip():
                # Only at the end of the line. The span that gets replaced is
                # measured from the cursor backwards, so completing in the
                # middle left the rest of the word standing: "/res|ume abc"
                # became "/resumeume abc".
                return
            word = _completion_word(line)
            try:
                candidates = suggest(line)
            except Exception:
                logger.debug("Completion failed", exc_info=True)
                return
            for value, hint in candidates:
                if value.startswith(word):
                    yield Completion(value, start_position=-len(word),
                                     display_meta=hint)

    return ThreadedCompleter(_ChatCompleter())


def _build_prompt_editor(
    seed: Sequence[str],
    suggest: Optional[Callable[[str], list[tuple[str, str]]]] = None,
) -> Optional[_PromptEditor]:
    """Line editing with an arrow-up history, or None to stay on input().

    Arrow-up recalling the previous message is what every shell and every
    peer CLI does, and stdlib readline cannot deliver it here: on Windows
    importing it activates pyreadline3, whose raw-mode loop replaces
    input()'s console handling -- the "every key prints a space, Enter and
    Ctrl-C dead" trap. prompt_toolkit drives the console itself on both
    platforms and raises the same KeyboardInterrupt/EOFError at the prompt
    that the REPL already handles, so Ctrl-C keeps its meaning.
    """
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
    except ImportError:
        logger.debug("prompt_toolkit missing; prompt falls back to input()")
        _posix_readline_fallback()
        return None
    try:
        return _PromptEditor(PromptSession, InMemoryHistory, seed,
                             key_bindings=_prompt_key_bindings(),
                             completer=_build_completer(suggest) if suggest else None)
    except Exception:
        # No console to drive (MSYS, a stray pipe) is no reason to lose the
        # prompt -- input() still reads lines, just without the arrow keys.
        logger.debug("Could not start prompt_toolkit", exc_info=True)
        _posix_readline_fallback()
        return None


def _skip_piped_bom() -> None:
    """Decode piped stdin as utf-8-sig, so a leading byte-order mark is dropped.

    Windows PowerShell 5.1 prefixes everything it pipes into a native process
    with a UTF-8 BOM -- measured: ``b'\\xef\\xbb\\xbf/compact\\n/exit\\n'``, also
    with ``$OutputEncoding`` set to UTF-8 without BOM. input() then returns
    ``'\\ufeff/compact'``; strip() keeps the BOM, so the line was no command and
    went to the model as a billed message. utf-8-sig drops the mark only at
    the start of the stream, never a U+FEFF inside the text.

    Must run before the first read: a TextIOWrapper refuses a new encoding
    once it has decoded data.

    Only a stream that starts with the mark is switched -- the mark says it
    is UTF-8, whatever the stream decoded as before (without Python's UTF-8
    mode that is cp1252 on Windows, where it read as "ï»¿/compact"). A pipe
    without one keeps its encoding: an ANSI file piped in stays readable.
    ``errors`` is named again because a new encoding resets it to strict,
    and one stray byte would then end the chat with a UnicodeDecodeError.
    """
    stream: Any = sys.stdin
    try:
        # peek() waits for the first bytes, which input() is about to do anyway.
        # ponytail: one peek. A producer that delivers the mark in pieces keeps
        # its encoding; read the first bytes properly if that ever shows up.
        if stream.buffer.peek(len(codecs.BOM_UTF8))[:len(codecs.BOM_UTF8)] != codecs.BOM_UTF8:
            return
        stream.reconfigure(encoding="utf-8-sig", errors="replace")
    except Exception:
        logger.debug("Could not switch piped stdin to utf-8-sig", exc_info=True)


def _prompt_key_bindings() -> Any:
    """Restore Ctrl-Z's old meaning on Windows: end of input.

    input() read Ctrl-Z as EOF there, and the REPL leaves on EOF. An editor
    inserts the raw \\x1a instead, which survives .strip() and would be sent
    to the agent as a message -- a billed turn for a keystroke that used to
    quit. POSIX keeps prompt_toolkit's default (a literal character; the
    terminal's own SIGTSTP never reaches the editor).
    """
    if os.name != "nt":
        return None
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("c-z")
    def _(event: Any) -> None:
        event.app.exit(exception=EOFError)

    return bindings


def _read_input(prompt: str, cont_prompt: str = "... ", echo: bool = False,
                read_line: Optional[Callable[[str], str]] = None,
                read_cont: Optional[Callable[[str], str]] = None) -> str:
    """Read one message, which may span several lines.

    Pasting a stack trace or a code block used to fire ONE TURN PER LINE:
    line 1 started a task and the rest sat in the console buffer, launching
    back to back afterwards. Two ways out, both familiar from peer CLIs:
    a triple-quote fence around a block, and a trailing backslash.

    ``read_line``/``read_cont`` inject the editing frontend
    (_build_line_readers). Both fall back to ``input`` -- resolved per call,
    not captured at import, so patching builtins.input still works.
    """
    def _next() -> Optional[str]:
        try:
            line = (read_cont or read_line or input)(cont_prompt)
        except EOFError:
            return None
        if echo:
            print(line)
        return line

    first = (read_line or input)(prompt)
    if echo:
        print(first)

    if "\n" in first:
        # Already a whole message: a recalled multi-line entry, or a bracketed
        # paste the editor delivered in one piece. Running it through the rules
        # below would let its own leading fence or trailing backslash drop the
        # REPL into the continuation prompt, waiting for an end that is already
        # in the string -- which reads as a hang.
        return first

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
                 template_vars: Optional[dict] = None,
                 llm_params: Optional[dict] = None,
                 session_title: Optional[str] = None,
                 attachments: Sequence[str] = ()) -> None:
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
        # ...and under which agent. /agent switches entry_name, and the
        # exit hint would then offer the session of the agent before it
        # with the new agent's name -- a command the CLI does NOT refuse:
        # an explicit --agent outranks the record, loads the foreign
        # session and writes the wrong agent into it on the first save.
        self.last_saved_agent: Optional[str] = None
        # Cumulative usage across the chat, for the exit line.
        self.total_usage: dict[str, float] = {}
        # Files queued by /attach (or --attach) for the NEXT message (absolute
        # or relative paths, already validated to exist when queued).
        self.attachments: list[str] = list(attachments)
        # --llm-params: they go with every profile /model switches to.
        self.llm_params = dict(llm_params or {})
        # --session-title names the session the chat started on, and only it:
        # dropped once written, and when /new or /resume leaves that session.
        self.session_title = session_title
        # The agent's slash commands, collected once by the REPL. Here so that
        # a line typed MID-TURN is classified by the same rule as one typed at
        # the prompt -- "/plugin:command" is claimed by nothing else.
        self.plugin_commands: list[PluginCommand] = []
        # Session records for "/resume" without an id and for completing one,
        # newest first. Filled on demand, never at startup: listing them walks
        # the index AND stats every session for children, which is a cost the
        # chat should pay when someone asks for it, not on every start.
        self.recent_sessions: list[dict] = []

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


def _report_what_stays_behind(ctx: "_ChatContext", previous: str) -> None:
    """Say what the session being left takes with it, and what waits here.

    Both ways out of a session pass here (`/new`, `/agent` and `/resume`): a
    title typed with `/rename` before the first message has no record to go
    into and dies with the session -- and losing it without a word looks like
    a bug. Queued attachments do NOT die; they are simply easy to forget
    once the chat says "New session".

    What it does NOT say is whether that title reached the disk. A /rename of
    a session that HAS a record writes it and keeps ctx.session_title only so
    a later save cannot put the old name back -- "nothing written yet" was a
    plain lie about that session.
    """
    if ctx.session_title:
        print(f"(the title '{ctx.session_title}' stays with {previous} -- "
              f"the session you are going to starts unnamed)")
    if ctx.attachments:
        print(f"({len(ctx.attachments)} attachment(s) stay queued for the "
              f"next message -- /attach clear drops them)")


def _open_fresh_session(ctx: "_ChatContext", editor: Optional["_PromptEditor"]) -> str:
    """Move the chat onto a brand-new session and let go of the old one.

    The history belongs to the session, so it changes with it -- otherwise
    the fresh prompt keeps offering the abandoned conversation while the
    transcript shows the new one.
    """
    previous = ctx.session_id
    _report_what_stays_behind(ctx, previous)
    ctx.session_id = _init_fresh_session(ctx)
    ctx.was_new_session = True
    ctx.session_title = None
    _hold_session(ctx, ctx.session_id)
    _release_session(ctx, previous)
    if editor:
        editor.reseed(_history_seed(ctx))
    return ctx.session_id


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


def _hold_session(ctx: "_ChatContext", session_id: str) -> bool:
    """Session presence (core/session_presence.py): chat holds the session it
    has open. The conversation stays in memory between turns, so no woken run
    may take the session up meanwhile; its input waits for the next turn.

    The new session is taken before the old one is let go, and False -- with
    nothing taken and nothing let go -- means another process runs it."""
    presence = presence_for(getattr(ctx.agent, "system_config", None))
    if presence is None:
        return True
    try:
        presence.hold(session_id, ctx.session_user, ctx.entry_name)
    except SessionBusy as busy:
        print(f"{busy}. Nothing changed here.")
        return False
    return True


def _release_session(ctx: "_ChatContext", session_id: Optional[str]) -> None:
    """Let go of a session the chat left; input that came in for it wakes it."""
    presence = presence_for(getattr(ctx.agent, "system_config", None))
    if presence is not None and session_id:
        presence.release(session_id, ctx.session_user)


def _resume_hint(ctx: "_ChatContext", session_id: str,
                 agent_name: Optional[str] = None) -> str:
    """The exact command that brings this session back."""
    parts = ["agent-cli chat", f"--session {session_id}",
             f"--agent {agent_name or ctx.entry_name}"]
    if ctx.session_user != "cli_user":
        parts.append(f"--session-user {ctx.session_user}")
    return " ".join(parts)


def _call_pricing_key(agent: Any, override: Any = None,
                      event: Any = None) -> tuple[Optional[str], bool]:
    """(model, is_batch) of the client that just ran -- read per call.

    Pricing the whole session with one model was wrong as soon as a fallback
    switched profiles or a step used a different client.

    The event names the model that answered the call (a fallback, or a step
    walking around a blocked LLM, runs on neither the override nor the
    agent's own client). Without that, the override comes first, and that is
    not cosmetic: --llm and /model hand the turn a different client while
    ``agent.llm`` stays the agent's own, so reading the agent alone quoted the
    price of the model that did NOT run.

    The isinstance-str guard on the provider mirrors the usage tracker: a
    plain mock must not look batchy and halve the estimate.
    """
    model = event.get("model") if isinstance(event, dict) else None
    if isinstance(model, str) and model:
        return model, event.get("batch") is True
    client = override or getattr(agent, "llm", None)
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


def _format_usage(totals: dict, elapsed: float, sym: dict,
                  context: Optional[tuple[int, Optional[int]]] = None) -> str:
    """One dim footer line: context fill, tokens, cache hit rate, cost, time.

    The cost is already resolved per call by _accumulate_usage -- this only
    renders it. An estimated total carries a leading ~, and calls whose price
    could not be determined are named rather than silently omitted.

    `context` is (tokens_in_window, window_size) for a single turn; the
    session total has no such thing and passes None.
    """
    def _short(n: float) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"

    prompt = totals.get("prompt_tokens", 0) or 0
    completion = totals.get("completion_tokens", 0) or 0
    cached = totals.get("cached_tokens", 0) or 0
    written = totals.get("cache_write_tokens", 0) or 0

    parts = []
    if context:
        used, window = context
        if window:
            # The window is a round number by nature -- "272k" reads better
            # than the "272.0k" the generic short form would give it.
            parts.append(f"ctx {_short(used)}/{window // 1000}k "
                         f"({used * 100 // window}%)")
        else:
            parts.append(f"ctx {_short(used)}")

    if prompt or completion:
        head = f"{sym['up']}{_short(prompt)}"
        # Always shown once anything was sent: 0% is the interesting case --
        # it means the prefix cache is not being hit at all. Writes are named
        # separately because "0% cached" alone cannot tell a broken cache from
        # the first turn of a working one, which is exactly the confusion that
        # cost an hour when Claude's cache_control was being dropped.
        if prompt:
            rate = f"{cached * 100 // prompt}% cached"
            if written:
                rate += f", +{_short(written)} written"
            head += f" ({rate})"
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
    # Output speed, the number people compare between models: generated
    # tokens over wall time. Prompt tokens are not "generated" and would
    # inflate it by the whole history on every turn.
    # Only per turn (`context` marks one): across a whole session the elapsed
    # time includes the user thinking and typing, so the rate would say more
    # about the human than about the model.
    if context and completion and elapsed > 0:
        parts.append(f"{completion / elapsed:.0f} tok/s")
    return sym["sep"].join(parts)


def _decode(text: Any) -> Any:
    """Parse a JSON payload, or None when it is not JSON."""
    if not isinstance(text, str):
        return text if isinstance(text, dict) else None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


_one_line = one_line


_tool_call_summary = tool_call_summary


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
            # The LINE, not the raw text: a value ending in "\n" is one line,
            # and printing it whole put a blank line under it.
            renderer.println(f"    {key}: {lines[0] if lines else ''}", color="90")
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
                # see _render_tool_call: the line, not the raw text
                renderer.println(f"    {key}: {lines[0] if lines else ''}", color="32")
            else:
                renderer.println(f"    {key}:", color="32")
                for line in lines:
                    renderer.println(f"      {line}", color="32")
    else:
        for line in raw.splitlines():
            renderer.println(f"    {line}", color="32")


#: One reader for both shapes of a message, shared with the web surface.
_message_text = message_text


def _session_messages(ctx: "_ChatContext") -> list:
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is None:
        return []
    try:
        return list(tracker.get_session_messages(ctx.session_id) or [])
    except Exception:
        logger.debug("Could not read session messages", exc_info=True)
        return []


#: /history and /last cut at the same place /undo does.
_is_real_turn = starts_a_turn


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
            if not text:
                continue
            renderer.println("")
            renderer.println(f"› {text}")
        elif role == "assistant":
            if text:
                renderer.println(text, color="90")
            for call in getattr(message, "tool_calls", None) or []:
                _render_tool_call(renderer, call, full=False)
        elif role == "developer":
            # Not a turn anybody took -- the run putting something in front of
            # the model. Hidden, the history reads as if the agent knew things
            # nobody had told it.
            if not text:
                continue
            renderer.println("")
            renderer.println(f"[note] {text}", color="90")
        elif role == "tool":
            _render_tool_result(renderer, message, full=False)
    renderer.commit()
    print("(/last shows the last turn's tool traffic in full)")


def _show_last(ctx: "_ChatContext", renderer: ChatRenderer) -> None:
    """Full tool calls and results of the most recent turn.

    Chat collapses every tool call to one status line, so what a tool actually
    RETURNED is invisible -- this is the chat equivalent of run's --show-tools.
    """
    messages = _session_messages(ctx)
    last_user = None
    for i in range(len(messages) - 1, -1, -1):
        if _is_real_turn(messages[i]):
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


def _drop_last_exchange(ctx: "_ChatContext") -> Optional[Any]:
    """Remove the last question and everything that answered it.

    Returns the MESSAGE, not its text: an attachment makes it multimodal, and
    ``/retry`` has to send the parts again. Reading it back as text handed the
    model "what is wrong here? [image_url]" -- a billed turn about nothing.

    The agent's own message list is what gets cut; the session file follows on
    the next save. A retry that left the first attempt in the history would
    ask the model to improve on an answer it can still see.
    """
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is None:
        return None
    kept, dropped = split_off_last_exchange(_session_messages(ctx))
    if dropped is None:
        return None
    try:
        tracker.set_session_messages(ctx.session_id, kept)
    except Exception as e:
        logger.error("Could not drop the last exchange: %s", e, exc_info=True)
        print(f"Could not drop the last exchange: {e}")
        return None
    return dropped


def _export_transcript(ctx: "_ChatContext", payload: str) -> None:
    """Write the conversation to a markdown file.

    An existing file is never overwritten: the obvious name (`/export`
    without a path) is the same for every export of one session, and losing
    yesterday's transcript to today's would be silent.
    """
    messages = _session_messages(ctx)
    if not messages:
        print("Nothing to export -- this session has no messages yet.")
        return
    try:
        path = Path(payload.strip() or f"chat-{ctx.session_id}.md").expanduser()
    except RuntimeError as e:
        # A "~name" with no home behind it RAISES -- `/export ~$notes.md`, the
        # lock file Word leaves next to a document. Nothing catches around the
        # dispatch, so it took the whole chat down. /attach learned this once.
        print(f"Cannot write there: {e}")
        return
    if path.exists():
        print(f"{path} exists already -- /export <path> writes somewhere else.")
        return

    try:
        path.write_text(
            transcript_markdown(messages, agent_name=ctx.entry_name,
                                session_id=ctx.session_id, llm=ctx.llm_label()),
            encoding="utf-8")
    except OSError as e:
        print(f"Could not write {path}: {e}")
        return
    print(f"Written: {path.resolve()}")


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


#: What each part of the window is called on screen, biggest-first order is
#: decided by the numbers, not by this.
_CONTEXT_LABELS = {
    "tool_results": "tool results",
    "answers": "answers",
    "questions": "your messages",
    "system_prompt": "system prompt",
    "tools": "tool schemas",
    "other": "other messages",
}


async def _show_context(ctx: "_ChatContext", renderer: ChatRenderer) -> None:
    """What fills the context window of this session.

    Two blocks that are never mixed: what the provider COUNTED on the last
    call (the usage tracker has it, including the window it was counted
    against), and what this conversation holds NOW, estimated per kind. A
    category worked out as "measured minus estimated" would look exact and
    carry the error of both.

    The split is the point. "42k of 200k" says the window is filling; only
    the split says the tool results are doing it, and that is the one the
    person can act on -- /undo, /new, or a narrower tool.
    """
    messages = _session_messages(ctx)
    prompt, tools = "", []
    notes = []
    try:
        prompt, tools = await ctx.agent.describe_context_inputs(ctx.session_id)
    except Exception as e:  # noqa: BLE001 - a missing line, not the end of the command
        logger.debug("Could not read the context inputs: %s", e)
        notes.append("  (the system prompt and the tool schemas could not be "
                     "read -- they are missing below)")

    last = measured_context(ctx.agent, ctx.session_id)
    # The window the NEXT call runs against. The measured line brings its own:
    # /model changes the model and with it the size, and one share against the
    # other would state a fill that is not true.
    window = live_context_window(ctx.agent, ctx.llm_override)
    breakdown = context_breakdown(messages, system_prompt=prompt, tools=tools)

    # Everything through the renderer, and committed at the end: a bare print
    # lands on the row the live region redraws and is invisible to its offset
    # arithmetic, so the next turn paints over this output.
    renderer.println(
        f"Context of {ctx.session_id} ({ctx.entry_name} on {ctx.llm_label()}):")
    for note in notes:
        renderer.println(note, color="90")
    if last.get("prompt_tokens"):
        measured_window = last.get("window", 0)
        share = (f"  ({last['prompt_tokens'] / measured_window:.0%})"
                 if measured_window else "")
        cached = (f", {last['cached']:,} of them cached"
                  if last.get("cached") else "")
        renderer.println(
            f"  last call     {last['prompt_tokens']:>8,}"
            + (f" of {measured_window:,}" if measured_window else "") + share + cached
            + ("   [stale: the context was rewritten since]"
               if last.get("is_stale") else ""),
            color="90")
    renderer.println("  ---- and what the conversation holds now, estimated ----",
                     color="90")
    _print_context_lines(renderer, breakdown, window)
    renderer.commit()


def _print_context_lines(renderer: ChatRenderer, breakdown: dict, window: int) -> None:
    """The estimated split, biggest first, and what it adds up to."""
    parts = sorted(breakdown["parts"].items(), key=lambda kv: -kv[1]["tokens"])
    width = max((len(_CONTEXT_LABELS.get(name, name)) for name, _ in parts), default=0)
    for name, part in parts:
        # Nothing in it, no line: an agent with no tools does not need a row
        # saying so, and a fresh session would otherwise list three zeroes.
        if not part["tokens"]:
            continue
        counted = part["count"]
        unit = "tools" if name == "tools" else "messages"
        detail = f"   {counted} {unit}" if name not in ("system_prompt",) else ""
        renderer.println(
            f"  {_CONTEXT_LABELS.get(name, name):<{width}}  {part['tokens']:>8,}{detail}",
            color="90")
    total = breakdown["total"]
    renderer.println(f"  {'together':<{width}}  {total:>8,}"
                     + (f"   of {window:,}  ({total / window:.0%})" if window else ""),
                     color="90")


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


def _build_profile(ctx: "_ChatContext", wanted: str) -> tuple[Any, str]:
    """(client, label) for LLM profile *wanted*; raises if it cannot be built.

    The switch ``--llm`` performs, with the ``--llm-params`` of this chat
    applied to the new profile the way the command line applies them: a
    ``thinking_level=max`` typed at the start must not vanish on /model.
    Changes nothing, so an interrupt while it builds leaves the chat as it was.
    """
    from ..llm.factory import create_llm_from_profile, resolve_llm_config_for_agent
    from ..config.models import AgentConfig

    system_config: Any = getattr(ctx.agent, "system_config", None)
    params = ctx.llm_params or None
    client = create_llm_from_profile(
        config=system_config, llm_profile=wanted, llm_params=params)
    resolved = resolve_llm_config_for_agent(system_config, AgentConfig(llm_profile=wanted))
    label = f"{wanted}:{resolved.spec.provider}/{resolved.spec.model}"
    if params:
        label += " +params(" + ",".join(f"{k}={v}" for k, v in params.items()) + ")"
    return client, label


def _use_profile(ctx: "_ChatContext", wanted: str,
                 built: Optional[tuple[Any, str]] = None) -> None:
    """Point the chat at LLM profile *wanted* (built here unless *built*)."""
    client, label = built or _build_profile(ctx, wanted)
    ctx.llm_override = client
    ctx.llm_profile = wanted
    ctx.llm_profile_info = label
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is not None:
        # Same three keys the bootstrap writes; the turn loop reads them for
        # tool context, and the session record is what a later resume reads.
        tracker.set_session_metadata(ctx.session_id, {
            "user_id": ctx.session_user,
            "agent_name": ctx.entry_name,
            "llm_profile": wanted,
        })


def _llm_profiles(ctx: "_ChatContext") -> dict:
    """The configured LLM profiles -- one reader for the switch and its Tab."""
    llm_system = getattr(getattr(ctx.agent, "system_config", None), "llm_system", None)
    return dict(getattr(llm_system, "profiles", None) or {})


def _switch_model(ctx: "_ChatContext", payload: str) -> bool:
    """Show or change the LLM profile this chat runs on; True if it changed.

    The next turn reads ctx.llm_override, and the choice goes into the
    session metadata so continuing the session later starts on it again
    (agent_cli.stored_session_settings reads it back) -- the caller writes
    the record at once.
    """
    profiles = _llm_profiles(ctx)
    wanted = payload.strip()

    if not wanted:
        print(f"LLM: {ctx.llm_label()}")
        if not profiles:
            print("  (no profiles configured)")
            return False
        current = ctx.llm_profile
        for name in sorted(profiles):
            marker = "*" if name == current else " "
            description = getattr(profiles[name], "description", "") or ""
            print(f" {marker} {name:32} {_one_line(description, 60)}")
        print("  /model <profile> switches; it applies to the next message.")
        return False

    if wanted not in profiles:
        close = difflib.get_close_matches(wanted, sorted(profiles), n=1, cutoff=0.6)
        print(f"Unknown LLM profile: {wanted}"
              + (f"   Did you mean {close[0]}?" if close else ""))
        print("  /model lists them.")
        return False

    try:
        _use_profile(ctx, wanted)
    except Exception as e:
        # The old client is still good; a failed switch must not end the chat.
        logger.error("Could not switch LLM profile to %s: %s", wanted, e, exc_info=True)
        print(f"Could not switch to '{wanted}': {e}")
        print(f"Staying on {ctx.llm_label()}.")
        return False

    print(f"LLM: {ctx.llm_profile_info}   (from the next message on)")
    return True


def _agent_names(ctx: "_ChatContext") -> list[str]:
    """Agents this configuration defines -- the CLI's own gate, not a copy.

    Reading it a second time here is how a listing and its factory drift
    apart: /agent would offer a name that _build_entry_agent then rejects.
    """
    from ..agent_cli import agent_entry_names

    return agent_entry_names(getattr(ctx.agent, "system_config", None))


def _agent_for(ctx: "_ChatContext", name: str) -> Any:
    """The agent object for *name*, through the CLI's own factory.

    Not a second copy of it: that one applies the MERGED server config, and
    the copy this chat would grow instead is how an agent ends up with a
    quietly downgraded max_steps.
    """
    from ..agent_cli import entry_agent

    config: Any = getattr(ctx.agent, "system_config", None)
    registry: Any = getattr(ctx.agent, "registry", None)
    return entry_agent(name, config, registry, ctx.session_service)


def _switch_agent(ctx: "_ChatContext", payload: str) -> bool:
    """Show or change the agent this chat talks to; True if it changed.

    A switch ALWAYS starts a new session, and that is the whole difficulty:
    a session carries the agent it ran with (cli_utils/session_defaults.py),
    so continuing this one under another agent would run it with foreign
    tools and a foreign prompt, and the next save would write the new name
    over its record. The caller does the session part -- holding the new one
    before letting the old one go.
    """
    wanted = payload.strip()
    names = _agent_names(ctx)

    if not wanted:
        print(f"Agent: {ctx.entry_name}")
        if not names:
            print("  (this config defines no agents)")
            return False
        for name in names:
            print(f" {'*' if name == ctx.entry_name else ' '} {name}")
        print("  /agent <name> switches; the chat starts a new session for it.")
        return False

    if wanted == ctx.entry_name:
        print(f"Already on {ctx.entry_name}.")
        return False
    if wanted not in names:
        close = difflib.get_close_matches(wanted, names, n=1, cutoff=0.6)
        print(f"Unknown agent: {wanted}"
              + (f"   Did you mean {close[0]}?" if close else ""))
        print("  /agent lists them.")
        return False

    try:
        agent = _agent_for(ctx, wanted)
    except SystemExit:
        # The factory exits the process when it cannot build one. Not from
        # inside a REPL: the person is mid-conversation.
        print(f"Could not build agent '{wanted}'.")
        return False
    except Exception as e:
        logger.error("Could not switch to agent %s: %s", wanted, e, exc_info=True)
        print(f"Could not switch to '{wanted}': {e}")
        print(f"Staying on {ctx.entry_name}.")
        return False

    ctx.agent = agent
    ctx.entry_name = wanted
    # The new agent's own LLM, not the one the old one was switched to: a
    # /model choice belongs to the agent it was made for, and the override
    # would keep answering for an agent that never asked for it. That also
    # ends a --llm given on the command line, which is worth saying: the
    # banner would otherwise name a profile nobody chose here.
    if ctx.llm_override is not None:
        print(f"({ctx.llm_profile_info or ctx.llm_profile} no longer applies -- "
              f"{wanted} answers on its own profile; /model switches it)")
    ctx.llm_override = None
    ctx.llm_profile_info = None
    ctx.llm_profile = (getattr(getattr(agent, "agent_config", None),
                               "default_llm_profile", None) or ctx.llm_profile)
    try:
        ctx.plugin_commands = list(collect_plugin_commands(agent))
    except Exception as e:  # noqa: BLE001 - a broken schema must not end the chat
        logger.error("Could not collect the commands of %s: %s", wanted, e, exc_info=True)
        print(f"({wanted} has no plugin commands here: {e})")
        ctx.plugin_commands = []
    return True


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

    # Grouped by the server prefix, which is how they are configured -- the
    # rule lives in chat_commands so the browser shows the same list.
    groups = group_tools_by_server(tools, _server_names(ctx))

    total = sum(len(v) for _, v in groups)
    print(f"{total} tool(s) available to {ctx.entry_name}"
          + (f" matching '{payload}'" if needle else "") + ":")
    for server, server_tools in groups:
        renderer.println(f"{server}", color="34")
        for tool in server_tools:
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


def _skill_registry(ctx: "_ChatContext"):
    """The registry, scanned with the roots the CONFIG resolves to.

    Never a hardcoded path: ``skills.skill_dirs`` may use wildcards
    (``skills/*/``) and the operator decides how deep that goes. Falling back
    to the defaults only when nothing is configured mirrors the skills plugin.
    """
    from agent_system.skills import get_skill_registry
    from agent_system.skills.registry import default_skill_dirs

    system_config = getattr(ctx.agent, "system_config", None)
    configured = list(getattr(getattr(system_config, "skills", None), "skill_dirs", []) or [])
    registry = get_skill_registry()
    registry.ensure_discovered(configured or list(default_skill_dirs()))
    return registry


def _available_skills(ctx: "_ChatContext") -> list[str]:
    """Names that can be invoked as /name. Never raises -- it runs per prompt.

    Only those: the registry also accepts a name like "3d-print", which the
    parser never reads as a command word, and a skill called "tools" loses to
    the built-in. /help and the typo hints offered both, and neither ran.
    """
    try:
        names = [skill.name for skill in _skill_registry(ctx).list_skills()]
    except Exception as e:  # noqa: BLE001 - a broken skill dir must not kill the REPL
        logger.debug("Could not list skills: %s", e)
        return []
    return runnable_skill_names(names)


def _expand_skill(ctx: "_ChatContext", name: str, arguments: str) -> Optional[str]:
    """The message a /skill invocation turns into, or None if it cannot be read."""
    from agent_system.skills import invoke

    try:
        skill = _skill_registry(ctx).get(name)
        if skill is None:
            print(f"Skill '{name}' is no longer available.")
            return None
        return invoke(skill, arguments)
    except (OSError, UnicodeDecodeError) as e:
        # A SKILL.md re-saved in another encoding mid-chat is still listed.
        print(f"Could not read skill '{name}': {e}")
        return None


def _show_skills(ctx: "_ChatContext", renderer: ChatRenderer) -> None:
    """What can be run, and which bundles this agent loads.

    Both halves, because they are different questions and the runnable list is
    the one the web UI can answer too. Showing only the agent's config here
    while the browser showed the runnable list made one command mean two
    things depending on where it was typed.
    """
    runnable = _available_skills(ctx)
    if runnable:
        renderer.println("you can run (arguments are passed to the skill):", color="34")
        for name in runnable:
            renderer.println(f"  /{name}", color="90")

    agent_config = getattr(ctx.agent, "agent_config", None)
    skills = getattr(agent_config, "skills", None) if agent_config else None
    if not skills:
        print(f"{ctx.entry_name} loads no skills into its prompt.")
        renderer.commit()
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
        print(f"{ctx.entry_name} loads no skills into its prompt.")
    renderer.commit()


def _through_before_the_interrupt(task: "asyncio.Task") -> bool:
    """Whether *task* finished with a result before the Ctrl-C landed.

    asyncio does not stop the loop for a task that finished with a
    KeyboardInterrupt, so waiting again HANGS -- the caller has to read the
    task instead. And the work is done: calling it cancelled throws away a
    result that already changed the world.
    """
    return task.done() and not task.cancelled() and task.exception() is None


def _drain(loop: asyncio.AbstractEventLoop, task: "asyncio.Task", what: str) -> None:
    """Cancel *task* and wait it out -- it must not finish LATER.

    Three attempts, because every further Ctrl-C interrupts the wait: a task
    left pending on the shared loop runs on inside the next
    ``run_until_complete``. That is the bug this whole path exists for -- a
    compaction rewriting the message list of the turn after it, a save
    writing while a woken process has taken the session up.
    """
    task.cancel()
    for _ in range(3):
        if task.done():
            break
        try:
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        except KeyboardInterrupt:
            logger.debug("another Ctrl-C while %s unwound", what)
    else:
        logger.warning("%s did not unwind and is still pending", what)
        return
    if not task.cancelled() and task.exception() is not None:
        # Read once: an exception nobody retrieves is reported by asyncio at
        # garbage collection, long after the command it belonged to.
        logger.debug("%s ended with %r", what, task.exception())


def _run_interruptible(loop: asyncio.AbstractEventLoop, coro: Any,
                       what: str) -> tuple[bool, Any]:
    """Run *coro* on the REPL's loop; Ctrl-C cancels it, not the chat.

    (finished, result). Driven as a TASK, not as a bare coroutine:
    ``run_until_complete`` leaves the future PENDING on KeyboardInterrupt, so
    the work would quietly finish inside the NEXT turn -- a compaction
    resuming there rewrites the very message list that turn is reading, after
    the person was told it had been interrupted. And the interrupt itself
    used to end the whole chat with a traceback.
    """
    task = loop.create_task(coro)
    try:
        return True, loop.run_until_complete(task)
    except KeyboardInterrupt:
        if _through_before_the_interrupt(task):
            # A /resume that got through has already switched the session.
            # Reporting it as cancelled made the caller let go of the hold on
            # the session the chat was now writing to -- and keep the one on
            # the session it had left.
            return True, task.result()
        _drain(loop, task, what)
        print(f"\n({what} cancelled)", file=sys.stderr)
        return False, None


def _finish_save(loop: asyncio.AbstractEventLoop, task: "asyncio.Task") -> Any:
    """Wait out a save a Ctrl-C interrupted; a second one abandons it."""
    print("\n(finishing the save -- Ctrl-C again to abandon it)", file=sys.stderr)
    try:
        return loop.run_until_complete(task)
    except KeyboardInterrupt:
        if _through_before_the_interrupt(task):
            return task.result()
        _drain(loop, task, "the save")
        print("(save abandoned)", file=sys.stderr)
        return False


def _save_now(loop: asyncio.AbstractEventLoop, ctx: "_ChatContext") -> bool:
    """Save the session; a Ctrl-C lets the save finish, a second one stops it.

    A save left pending finished unseen during the next command, after the
    chat had said it was interrupted -- and after /new it could still be
    writing while a woken process took the session up.
    """
    task = loop.create_task(_save_session(ctx))
    try:
        saved = loop.run_until_complete(task)
    except KeyboardInterrupt:
        if task.done():
            # A save that got through counts -- the interrupt was a moment
            # too late. One that died inside its own code did not.
            if not _through_before_the_interrupt(task):
                print("\n(save interrupted)", file=sys.stderr)
                return False
            saved = task.result()
        else:
            saved = _finish_save(loop, task)
    if saved:
        ctx.last_saved = ctx.session_id
        ctx.last_saved_agent = ctx.entry_name
        ctx.was_new_session = False
        ctx.session_title = None  # written; a later save keeps it
    return bool(saved)


def _run_plugin_command(loop: asyncio.AbstractEventLoop, ctx: "_ChatContext",
                        commands: Sequence[PluginCommand], qualified: str,
                        payload: str) -> None:
    """Run one plugin command on the REPL's loop and print what it says."""
    match = next(c for c in commands if c.qualified == qualified)
    finished, output = _run_interruptible(loop, run_plugin_command(
        ctx.agent, match, payload,
        session_id=ctx.session_id, user_id=ctx.session_user), f"/{match.name}")
    if finished:
        print(output)


async def _load_recent_sessions(ctx: _ChatContext) -> list[dict]:
    """This user's sessions, newest first, remembered on the context.

    One source for three readers: `/sessions`, `/resume` without an id, and
    the completion behind `/resume <Tab>`. Never raises -- a broken index
    must not take a command down with it.
    """
    if ctx.session_manager is None:
        return []
    try:
        ctx.recent_sessions = list(
            await ctx.session_manager.list_root_sessions(ctx.session_user) or [])
    except Exception as e:  # noqa: BLE001 - a broken index is not fatal here
        # Said out loud, like the listing next door: what comes back is the
        # PREVIOUS list, and a bare /resume acts on it. "No earlier session"
        # would be a lie about the index, and console logging is off by now.
        logger.error("Could not list sessions: %s", e, exc_info=True)
        print(f"Could not list sessions: {e}   (using what was listed before)")
    return ctx.recent_sessions


def _resumable_sessions(ctx: _ChatContext) -> list[dict]:
    """The listed sessions this chat could actually take over.

    Its own agent's, and not the one already open: a session carries the agent
    that ran it, and _resume_session refuses a foreign one. Offering those
    anyway meant a bare /resume announced a session and then bounced it --
    with two agents in the config that is the normal case, not the edge.
    An entry with no agent name (an index written before that field) is left
    in: refusing it here would hide a session that resumes fine.
    """
    return [entry for entry in ctx.recent_sessions
            if entry.get("session_id") and entry.get("session_id") != ctx.session_id
            and (entry.get("agent_name") or ctx.entry_name) == ctx.entry_name]


def _last_session(ctx: _ChatContext) -> Optional[dict]:
    """The newest session this chat can continue."""
    return next(iter(_resumable_sessions(ctx)), None)


async def _resume_into(ctx: _ChatContext, session_id: str, previous: str) -> bool:
    """Take *session_id* over, and let go of whichever session is left behind.

    The hold comes BEFORE the load -- a session another process is running
    must not be pulled out from under it -- and the release is in a
    ``finally``: a Ctrl-C lands inside the load, and a hold taken there and
    never given back locks the session for the rest of the process.
    """
    if not _hold_session(ctx, session_id):
        return False  # another process runs it: the chat stays where it is
    switched = False
    try:
        switched = await _resume_session(ctx, session_id)
    finally:
        _release_session(ctx, previous if switched else session_id)
    return switched


async def _resume_last_session(ctx: _ChatContext, previous: str) -> bool:
    """`/resume` without an id: continue where this user last left off."""
    await _load_recent_sessions(ctx)
    entry = _last_session(ctx)
    if entry is None:
        print("No earlier session to continue. /sessions lists them.")
        return False
    session_id = entry["session_id"]
    title = " ".join((entry.get("title") or "Untitled").split())
    print(f"Resuming {session_id} -- {_one_line(title, 60)}")
    return await _resume_into(ctx, session_id, previous)


async def _rename_current_session(ctx: _ChatContext, title: str) -> bool:
    """Give the open session a title, the one `/sessions` shows."""
    if not title:
        print("Usage: /rename <title>")
        return False
    if ctx.was_new_session or ctx.session_manager is None:
        # Nothing on disk yet: the title rides along with the first save,
        # exactly as --session-title does.
        ctx.session_title = title
        print(f"Title: {title}   (written with the first message)")
        return True
    try:
        await ctx.session_manager.rename_session(ctx.session_user, ctx.session_id, title)
    except Exception as e:
        logger.error("Could not rename session %s: %s", ctx.session_id, e, exc_info=True)
        print(f"Could not rename the session: {e}")
        return False
    # The record is written; a later save must not put the old one back.
    ctx.session_title = title
    print(f"Title: {title}")
    return True


async def _list_sessions(ctx: _ChatContext, payload: str = "") -> None:
    """Show this user's own sessions -- `/sessions [count]`, 0 for all."""
    limit, complaint = parse_limit(payload, DEFAULT_LIMIT)
    if complaint:
        # Same voice as /history next door: a discarded argument that still
        # prints a plausible listing is indistinguishable from a honoured one.
        print(f"Usage: /sessions [count]   (got: {complaint})")
        return
    # What it printed is what the completion and a bare /resume read -- taken
    # from the listing it already did, not from a second walk of the index.
    listed = await print_sessions(
        ctx.session_manager, ctx.session_user,
        limit=limit,
        current_session_id=ctx.session_id,
        more_hint="/sessions <count>, /sessions 0 for all",
        footer="Use /resume <id> to continue one.",
    )
    if listed:
        ctx.recent_sessions = listed


async def _resume_session(ctx: _ChatContext, session_id: str) -> bool:
    """Load an earlier session into the running agent, on the session's LLM.

    A session brings its agent and its LLM (cli_utils/session_defaults.py).
    One of another agent is refused: loaded here it ran with this agent's
    tools and prompt, and the next save wrote this agent's name over its
    record. Its own profile is switched to, as ``--session <id>`` would.
    """
    system_config = getattr(ctx.agent, "system_config", None)
    stored_agent, stored_llm = await session_defaults(
        ctx.session_manager, ctx.session_user, session_id, system_config)
    if stored_agent and stored_agent != ctx.entry_name:
        print(f"Session '{session_id}' belongs to {stored_agent}; this chat runs "
              f"{ctx.entry_name}.")
        print(f"Continue it with: {_resume_hint(ctx, session_id, stored_agent)}")
        return False
    # Built before ANYTHING happens: nothing is loaded and nothing switches
    # until the session's own LLM stands. Running it on this chat's profile
    # instead would look harmless and then write that profile over the
    # session's record on the next save -- a failed switch destroying the
    # very choice it was trying to honour.
    built = None
    if stored_llm and stored_llm != ctx.llm_profile:
        try:
            built = _build_profile(ctx, stored_llm)
        except Exception as e:
            logger.error("Could not switch to the session's profile %s: %s",
                         stored_llm, e, exc_info=True)
            print(f"Session '{session_id}' runs on LLM '{stored_llm}', which "
                  f"cannot be started here: {e}")
            print(f"Continue it with: {_resume_hint(ctx, session_id)} "
                  f"--llm <profile>")
            return False
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
    # The title belonged to the session being left -- from --session-title or
    # from a /rename it never got to write. Named, then dropped.
    _report_what_stays_behind(ctx, ctx.session_id)
    ctx.session_id = session_id
    ctx.was_new_session = False
    ctx.session_title = None
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
    if built is not None and stored_llm:
        _use_profile(ctx, stored_llm, built)
        print(f"LLM: {ctx.llm_profile_info}   (the session's own)")
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
            title=ctx.session_title,
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
                # Slash commands are REPL-level, not messages. Sending "/exit"
                # to the LLM because it was typed a second earlier would give
                # identical keystrokes two different meanings.
                #
                # The prompt's own rule, through the prompt's own function,
                # with the prompt's own plugin commands: a command is ONE line
                # and a known word -- "/context_engineer:compact" included,
                # which only `resolve` ever claims. Several lines are a
                # message, and so is "/etc/nginx/nginx.conf". Refusing a block
                # because some line in it reads like a command threw away
                # pasted output (`ls /` alone carries "/tmp" and "/opt") while
                # the same paste at the prompt went through. Skills are not
                # offered here: there is no turn to expand one into.
                resolution = resolve_chat_input(submitted, (), ctx.plugin_commands)
                if resolution.kind != "message":
                    renderer.println(
                        f"» {submitted}  (commands only work at the prompt)",
                        color="33")
                    continue
                # "//compact" reaches the agent as "/compact", exactly as it
                # would from the prompt. The RAW line is what gets queued
                # below: that one passes the prompt again and is unescaped
                # there -- unescaping twice would hand it a command.
                message = resolution.payload
                request_id = state.get("request_id")
                delivered = False
                if request_id:
                    try:
                        delivered = bool(
                            await ctx.agent.append_user_message(request_id, message)
                        )
                    except Exception:
                        logger.debug("append_user_message failed", exc_info=True)
                if delivered:
                    # It became part of the conversation without ever passing
                    # the prompt, so nothing else would put it in the history.
                    editor = state.get("editor")
                    if editor is not None:
                        editor.remember(submitted)
                    renderer.println(f"» {submitted}", color="36")
                    renderer.println("  (queued -- the agent picks it up at its "
                                     "next step)", color="90")
                else:
                    # Nothing running to take it: queue it for the next prompt
                    # rather than dropping what the user typed. A list, so a
                    # second line does not overwrite the first.
                    state.setdefault("typed_queue", []).append(submitted)
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


async def _handle_vars(ctx: _ChatContext, renderer: ChatRenderer, payload: str) -> None:
    """List, set, unset or clear the template variables of THIS session.

    The same variables ``--vars`` fills at startup. The agent server reads them
    once per turn and lets them override the agent config, so a change here
    lands on the NEXT message -- never on the turn already running.

    Session-scoped on purpose: ``/new`` re-applies the command line's --vars,
    not these. A variable typed into one conversation has no business following
    the person into the next one.

    A line with ANY bad entry is refused whole. Applying the good half of
    ``/vars lang=de 8ball=x`` would leave the person guessing which half took.
    """
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is None:
        print("This agent keeps no session variables.")
        return

    request = parse_vars(payload)
    if request.errors:
        for problem in request.errors:
            renderer.println(f"  {problem}", color="33")
        renderer.println("Nothing changed. Usage: /vars [KEY=VALUE ...] | "
                         "/vars unset KEY | /vars clear", color="90")
        renderer.commit()
        return

    current = dict(tracker.get_session_template_vars(ctx.session_id) or {})
    if not request.is_query:
        current = apply_vars(current, request)
        try:
            await store_vars(tracker, ctx.session_manager,
                             ctx.session_user, ctx.session_id, current)
        except Exception as e:
            # Say it. A removal that only happened in memory comes back on the
            # next /resume, and a silent failure here looks exactly like
            # success until then.
            logger.debug("Persisting session vars failed", exc_info=True)
            renderer.println(f"  warning: not saved to disk ({e})", color="33")

    if not current:
        renderer.println("No session variables set.", color="90")
    else:
        renderer.println(f"Session variables ({ctx.session_id}):", color="34")
        width = max(len(key) for key in current)
        for key in sorted(current):
            # A far longer limit than /tools uses for descriptions: the point
            # of this command is to SEE the value, so cutting it at 60 would
            # defeat it. The cap only exists because a plugin may park a large
            # JSON blob in a session variable.
            renderer.println(f"  {key:<{width}}  {_one_line(current[key], 200)}",
                             color="90")
    if not request.is_query:
        renderer.println("  (takes effect on the agent's next step)", color="90")
    renderer.commit()


def _handle_attach(ctx: _ChatContext, payload: str) -> None:
    """Queue one file for the next message, list the queue, or clear it.

    The REST OF THE LINE is ONE path -- Windows paths contain spaces, and a
    quoting grammar would cost more than typing /attach once per file.
    """
    from ..utils.multimodal_processor import detect_file_type

    payload = payload.strip().strip('"').strip("'")
    if not payload:
        if not ctx.attachments:
            print("No attachments queued. Usage: /attach <path>")
        else:
            for path in ctx.attachments:
                print(f"  {path} [{detect_file_type(path)}]")
        return
    if payload.lower() == "clear":
        ctx.attachments.clear()
        print("Attachments cleared.")
        return
    # The same sorter the command line uses: it answers "can this be sent and
    # as what" once, for both surfaces. The hand-written copy that stood here
    # called Path.expanduser(), which RAISES for a ~name it cannot resolve --
    # `/attach ~$notes.md`, the lock file Word leaves next to a document, took
    # the whole chat session down, because nothing catches around the dispatch.
    from pathlib import Path as _Path
    kinds, problems = sort_attachments([payload])
    if problems:
        print(problems[0])
        return
    kind, path = next((k, p) for k, group in kinds.items() for p in group)
    ctx.attachments.append(path)
    print(f"Attached ({len(ctx.attachments)}): {_Path(path).name} [{kind}] "
          f"-- sent with the next message.")


def _task_with_attachments(ctx: _ChatContext, task: str,
                           renderer: ChatRenderer) -> Any:
    """The queued files plus *task* as one multimodal message, or None.

    None means: do not send. The queue is kept in that case so the person
    can fix the problem (switch profile, drop a file) without re-attaching;
    it is cleared only when the message actually goes out.
    """
    from ..llm.capabilities import capability_model_name, ensure_model_supports
    from ..utils.multimodal_processor import create_multimodal_message_extended

    # /attach already refused what cannot be sent, so a problem here means the
    # file changed under us since it was queued -- say which one, keep the rest.
    kinds, problems = sort_attachments(ctx.attachments)
    for problem in problems:
        print(f"Not sent: {problem}")
    if problems:
        return None

    # The per-request override wins over the agent's default -- one rule for
    # the HTTP API, the chat and both command-line entry points.
    problem = ensure_model_supports(
        capability_model_name(ctx.llm_override, ctx.agent),
        images=len(kinds["image"]), audio=len(kinds["audio"]))
    if problem:
        print(f"Not sent: {problem}")
        print(renderer._colored(f"(kept text: {task})", "90"))
        return None
    try:
        message = create_multimodal_message_extended(
            text=task,
            image_paths=kinds["image"] or None,
            audio_paths=kinds["audio"] or None,
            text_file_paths=kinds["text"] or None,
        )
    except Exception as e:
        print(f"Attachment failed, nothing sent: {e}")
        return None
    print(renderer._colored(
        f"(sending with {len(ctx.attachments)} attachment(s))", "90"))
    ctx.attachments.clear()
    return message


def _execute_turn(loop: asyncio.AbstractEventLoop, ctx: _ChatContext,
                  task: str, renderer: ChatRenderer,
                  editor: Optional["_PromptEditor"] = None) -> dict:
    """One turn on the persistent loop, with two-stage Ctrl-C handling.

    ``editor`` travels in the turn state so that a line typed AHEAD, which is
    delivered to the running agent instead of going through the prompt, still
    reaches the input history.
    """
    state: dict[str, Any] = {"editor": editor}
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
        result = {"summary": None, "cancelled": False, "errors": [str(e)],
                  "usage": state.get("usage") or {}}
    finally:
        # The reader owns terminal state on POSIX -- it has to be restored on
        # every exit, including Ctrl-C, or the shell stays in cbreak.
        _stop_typing(loop, reader, poller, renderer, state)
    for key in ("typed_queue", "typed_partial"):
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
        # not become the next task. state["typed_queue"] auto-runs, and running
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
        # Race: the turn may have COMPLETED between Ctrl-C and here -- the
        # agent was already finishing, its token gone, so no "cancelled"
        # came. A finished answer is shown, not discarded; the Ctrl-C still
        # counts for what was queued behind it.
        if result.get("summary") is None:
            result["cancelled"] = True
        result["interrupted"] = True
        return result
    except (KeyboardInterrupt, asyncio.TimeoutError, asyncio.CancelledError):
        # The biggest task in the file, and the one that must not be left
        # pending: its unwinding drains the status queue, unsubscribes and
        # closes the renderer -- inside whatever run_until_complete comes
        # next, drawing into the region the next prompt owns by then.
        _drain(loop, turn, "the turn")
    except Exception:
        logger.debug("Turn unwind failed", exc_info=True)
    renderer.close()
    # The turn's own result is gone with it; its usage lives on in the state.
    return {"summary": None, "cancelled": True, "errors": [],
            "usage": state.get("usage") or {}}


def _render_answer(loop: asyncio.AbstractEventLoop, ctx: _ChatContext,
                   renderer: ChatRenderer, summary: str) -> None:
    # rich prints straight to stdout, invisible to the region's offsets.
    renderer.commit()
    print()
    try:
        # output_format=None lets the central helper honour --color; the
        # one-shot path hardcodes 'ansi' here, which chat deliberately doesn't.
        # A Ctrl-C here skips the formatting, not the answer or the save.
        finished, formatted_output = _run_interruptible(loop, format_output_with_hooks(
            output=summary,
            agent_instance=ctx.agent,
            session_id=ctx.session_id,
            request_id="cli_display",
        ), "formatting")
        formatted, content_format = formatted_output if finished else (summary, "text")
        if content_format == "ansi":
            render_with_rich(formatted)
        else:
            print(formatted)
    except KeyboardInterrupt:
        print("\n(display interrupted)", file=sys.stderr)
    except Exception:
        logger.debug("Answer formatting failed, printing raw", exc_info=True)
        print(summary)
    print()  # region is already committed; plain spacing line


def _close_own_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Tear down a loop the REPL created itself (standalone use, tests)."""
    try:
        # Shut MCP down ON THIS loop, before closing it. The standalone
        # caller has no later shutdown on this loop, while every
        # subprocess transport (the terminal plugin's shells) belongs to
        # it. Skipping this fired their __del__ against a closed loop and
        # printed a "ValueError: I/O operation on closed pipe" cascade
        # after the goodbye message.
        try:
            from ..tools.integration import shutdown_tools
            loop.run_until_complete(shutdown_tools())
        except Exception:
            logger.debug("MCP shutdown on the chat loop failed", exc_info=True)

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
        # Subprocess transports are torn down by the executor thread pool;
        # without this the interpreter can outrun it and __del__ still
        # lands on a closed loop.
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:
        logger.debug("Event loop teardown failed", exc_info=True)
    loop.close()


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
    loop: Optional[asyncio.AbstractEventLoop] = None,
    llm_params: Optional[dict] = None,
    session_title: Optional[str] = None,
    attachments: Sequence[str] = (),
) -> None:
    """The chat REPL. Drives one event loop for its whole lifetime.

    Pass ``loop`` to run the turns on a loop the caller keeps alive -- the CLI
    hands over its shared bootstrap loop, because the external MCP connections
    made there only make progress while THAT loop runs. Without ``loop`` the
    REPL creates and, at the end, tears down a private one (standalone use and
    the tests).

    ``session_id`` comes in held by the caller (session presence,
    core/session_presence.py) and the REPL takes that over: it holds what /new
    and /resume switch to, and lets go of the open session however it ends,
    from its first line on.

    ``attachments`` go with the first message, as ``/attach`` would send them."""
    ctx = _ChatContext(
        agent=agent, entry_name=entry_name, session_service=session_service,
        session_user=session_user, session_id=session_id,
        was_new_session=was_new_session, llm_profile=llm_profile,
        llm_override=llm_override, llm_profile_info=llm_profile_info,
        show_status=show_status, session_manager=session_manager,
        template_vars=template_vars, llm_params=llm_params,
        session_title=session_title, attachments=attachments,
    )
    ansi = supports_color()
    renderer = ChatRenderer(ansi=ansi)
    unicode_ok = renderer.sym is _UNICODE_SYMBOLS
    prompt = "❯ " if unicode_ok else "> "
    cont_prompt = "… " if unicode_ok else "... "

    owns_loop = loop is None
    if loop is None:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    chat_started = time.monotonic()
    silenced: list[tuple[Any, int]] = []
    # The session came in held: from here on, every way out lets go of it.
    try:
        # Line editing and the session's own arrow-up history (see
        # _build_prompt_editor). BOTH ends must be a terminal, not just stdin:
        # with stdout redirected the editor still puts the tty in raw mode with
        # echo off and then draws into the file, so `agent-cli chat > log.txt`
        # would go silent on a terminal that no longer echoes. Redirected either
        # way, plain input() is the right reader and the echo path below is what
        # rebuilds the transcript.
        try:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        except Exception:
            interactive = False
        try:
            piped = not sys.stdin.isatty()
        except Exception:
            piped = False
        if piped:
            _skip_piped_bom()
        editor = _build_prompt_editor(
            _history_seed(ctx),
            suggest=lambda line: _completions_for(ctx, _available_skills(ctx), line),
        ) if interactive else None
        read_line = editor.read if editor else None
        read_cont = editor.read_continuation if editor else None

        # Known-good input mode, restored before every prompt: child shells
        # (terminal.execute -> MSYS bash) switch the console's INPUT mode too,
        # which kills Enter/Backspace and turns Ctrl-C into a plain character.
        input_mode = snapshot_console_input_mode()

        # Console logging would write into the live region behind its back.
        silenced = _silence_stdout_logging()

        dash = "─" if unicode_ok else "-"
        rule = dash * min(shutil.get_terminal_size((80, 20)).columns, 72)
        print(rule)
        print(f"Chat with {ctx.entry_name}   LLM: {ctx.llm_label()}   Session: {ctx.session_id}")
        print("Type /help for commands, /exit to quit. Ctrl-C cancels the running turn.")
        if ctx.attachments:
            print(f"{len(ctx.attachments)} attachment(s) go with the first message "
                  "(/attach lists them).")
        print(rule)

        # Collected once: the set of plugins cannot change while the REPL runs
        # (unlike the skill folders, which a person can edit mid-chat).
        plugin_commands = collect_plugin_commands(ctx.agent)
        ctx.plugin_commands = list(plugin_commands)

        pending: list[str] = [initial_task.strip()] if initial_task and initial_task.strip() else []
        interrupts = 0  # consecutive Ctrl-C at the prompt; two in a row exit
        while True:
            if pending:
                task = pending.pop(0)
                print(f"{prompt}{task}")  # keep the transcript complete
                # An initial_task or a line typed ahead during a turn is a
                # turn like any other, but it never passed the prompt, so the
                # editor would not have seen it.
                if editor:
                    editor.remember(task)
            else:
                restore_console_input_mode(input_mode)
                try:
                    task = _read_input(prompt, cont_prompt=cont_prompt,
                                       echo=not interactive,
                                       read_line=read_line,
                                       read_cont=read_cont)
                    # The editor runs its own asyncio.run() per prompt, and
                    # that leaves the thread with NO current event loop --
                    # measured: asyncio.get_event_loop() then raises. The REPL
                    # itself always names the loop, but a library called
                    # during the turn need not, and it used to find one.
                    asyncio.set_event_loop(loop)
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

            # Ctrl-C outside a turn: whatever runs synchronously here -- the
            # skill lookup, building a /model client, reading attachments, a
            # long /history -- is abandoned, not the chat.
            try:
                # Skills share the command namespace: anything that is not a
                # built-in is looked up as a skill, so "/writer analysiere X" runs
                # the writer skill with "analysiere X" as its arguments.
                skill_names = _available_skills(ctx)
                resolution = resolve_chat_input(task, skill_names, plugin_commands)
                command = resolution.name if resolution.kind == "command" else (
                    "unknown" if resolution.kind == "unknown" else None)
                payload = resolution.payload
                if editor and resolution.kind != "message":
                    # Not a message, so no session will hold it: the history
                    # keeps it for as long as this process runs.
                    editor.remember_command(task)

                if resolution.kind == "plugin":
                    # The plugin does the work and prints; no LLM turn, no tokens.
                    _run_plugin_command(loop, ctx, plugin_commands,
                                        resolution.name, payload)
                    continue

                if resolution.kind == "skill":
                    expanded = _expand_skill(ctx, resolution.name, payload)
                    if expanded is None:
                        continue
                    # The skill BECOMES the turn: same text an `always` skill would
                    # put in the prompt, just triggered by a person.
                    print(f"[skill: {resolution.name}]")
                    task = expanded
                    command = None

                if resolution.kind == "message":
                    # The "//" escape is resolved HERE, not left to the agent. The
                    # person typed "//compact" precisely so the model would see
                    # "/compact"; the terminal used to forward the raw line while
                    # the web surface stripped it, so the same keystrokes meant two
                    # different things depending on where they were typed.
                    task = resolution.payload

                if command == "exit":
                    break
                if command == "new":
                    print(f"New session: {_open_fresh_session(ctx, editor)}")
                    continue
                if command == "session":
                    print(f"Session: {ctx.session_id}  (user: {ctx.session_user})")
                    print(f"Resume with: {_resume_hint(ctx, ctx.session_id)}")
                    continue
                if command == "sessions":
                    _run_interruptible(loop, _list_sessions(ctx, payload), "/sessions")
                    continue
                if command == "resume":
                    previous = ctx.session_id
                    # Bare: the one this user last left. The id is only known
                    # after the listing, so the hold lives inside either way.
                    finished, resumed = _run_interruptible(
                        loop,
                        _resume_into(ctx, payload, previous) if payload
                        else _resume_last_session(ctx, previous),
                        "/resume")
                    if finished and resumed:
                        if editor:
                            editor.reseed(_history_seed(ctx))
                        print(f"Resumed session: {ctx.session_id}")
                    continue
                if command == "rename":
                    _run_interruptible(loop, _rename_current_session(ctx, payload),
                                       "/rename")
                    continue
                if command == "agent":
                    if not _switch_agent(ctx, payload):
                        continue
                    # The agent's commands change with it, and the session
                    # does too: one belongs to the agent that ran it.
                    plugin_commands = ctx.plugin_commands
                    print(f"Agent: {ctx.entry_name}   LLM: {ctx.llm_label()}")
                    print(f"New session: {_open_fresh_session(ctx, editor)}")
                    continue
                if command == "vars":
                    _run_interruptible(loop, _handle_vars(ctx, renderer, payload), "/vars")
                    continue
                if command == "model":
                    if _switch_model(ctx, payload) and not ctx.was_new_session:
                        # The record is what `--session <id>` starts on; waiting
                        # for the next turn's save lost the switch on /exit.
                        _save_now(loop, ctx)
                    continue
                if command == "tools":
                    _run_interruptible(loop, _show_tools(ctx, renderer, payload), "/tools")
                    continue
                if command == "skills":
                    _show_skills(ctx, renderer)
                    continue
                if command == "context":
                    _run_interruptible(loop, _show_context(ctx, renderer), "/context")
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
                if command == "attach":
                    _handle_attach(ctx, payload)
                    continue
                if command == "export":
                    _export_transcript(ctx, payload)
                    continue
                if command in ("undo", "retry"):
                    dropped = _drop_last_exchange(ctx)
                    if dropped is None:
                        print("Nothing to take back in this session yet.")
                        continue
                    asked = _message_text(dropped).strip()
                    print(renderer._colored(f"(dropped: {_one_line(asked, 70)})", "90"))
                    # The record has to match what the agent now holds, or the
                    # next `--session <id>` brings the dropped turn back --
                    # which is also why a session emptied by /undo is written
                    # empty (services/session_service.py, SessionTracker.emptied).
                    # A save that fails is named without guessing at the cause.
                    if not ctx.was_new_session and not _save_now(loop, ctx):
                        print(f"(the shortened session was NOT written -- "
                              f"--session {ctx.session_id} still brings the "
                              f"dropped turn back)", file=sys.stderr)
                    if command == "undo":
                        continue
                    # /retry asks the same thing again, as a turn of its own,
                    # with the parts it was sent with (an image, an audio file).
                    # The MESSAGE goes back, not its content: the agent takes a
                    # str or a ChatMessage, and the bare list of parts fell
                    # through both -- sanitize_for_llm cannot read a list and
                    # returns "", so the retry billed a turn about nothing.
                    task = dropped if getattr(dropped, "content", None) else asked
                    if not isinstance(task, str) and ctx.attachments:
                        print("(/attach stays queued: this retry sends the parts "
                              "the dropped message carried)")
                    print(f"{prompt}{_one_line(asked, 200)}")
                if command == "help":
                    print(_help_text(skill_names, plugin_commands))
                    continue
                if command == "unknown":
                    hint = suggest_command(
                        payload,
                        list(skill_names) + _plugin_spellings(plugin_commands))
                    did_you_mean = f"  Did you mean {hint}?" if hint else ""
                    print(f"Unknown command: {payload}{did_you_mean}")
                    print(f"/help lists the commands; //{payload[1:]} sends it as a message.")
                    continue

                # isinstance: a /retry hands over the parts the dropped message
                # already carried, and merging those into a second multimodal
                # message would send the text twice and the file not at all.
                if ctx.attachments and isinstance(task, str):
                    task = _task_with_attachments(ctx, task, renderer)
                    if task is None:
                        continue

                started = time.monotonic()
                result = _execute_turn(loop, ctx, task, renderer, editor)

                # Lines the user SUBMITTED during the turn but that never reached
                # the agent become the next tasks, in order -- they pressed Enter
                # on each of them. (typed_ahead used to carry only the first line;
                # the rest of the queue was silently lost.)
                queued = result.get("typed_queue") or []
                # A half-typed fragment is only shown; auto-running it would spend
                # money on something the user never sent.
                if result.get("typed_partial"):
                    print(renderer._colored(
                        f"(unsent: {result['typed_partial']})", "90"))

                # What the turn spent counts, cancelled or not: the calls before
                # the Ctrl-C were billed all the same.
                usage = result.get("usage") or {}
                _merge_totals(ctx.total_usage, usage)

                if result.get("cancelled") or result.get("interrupted"):
                    # Ctrl-C means STOP. Everything queued is dropped -- the lines
                    # from this turn AND leftovers from earlier turns still sitting
                    # in `pending`: firing a new billed turn right after Ctrl-C is
                    # the opposite of what was asked for. That holds when the
                    # answer won the race too (interrupted, not cancelled).
                    for line in (*pending, *queued):
                        print(renderer._colored(f"(dropped: {line})", "90"))
                    pending.clear()
                    queued = []
                if result.get("cancelled"):
                    print("Turn cancelled.", file=sys.stderr)
                    continue  # nothing new worth saving; next turn saves anyway

                pending.extend(queued)

                summary = result.get("summary")
                if summary:
                    _render_answer(loop, ctx, renderer, summary)
                elif not result.get("errors"):
                    print(renderer._colored("(no answer returned)", "90"))

                if ctx.show_status:
                    context_fill = result.get("context_tokens")
                    print(renderer._colored(
                        _format_usage(usage, time.monotonic() - started,
                                      renderer.sym,
                                      context=(context_fill,
                                               result.get("context_window"))
                                      if context_fill else None), "90"))

                _save_now(loop, ctx)
            except KeyboardInterrupt:
                renderer.close()
                print("\n(interrupted)", file=sys.stderr)
                # Stop means stop here too: nothing queued runs after it.
                for line in pending:
                    print(renderer._colored(f"(dropped: {line})", "90"))
                pending.clear()
    finally:
        _release_session(ctx, ctx.session_id)
        _restore_logging(silenced)
        if ctx.total_usage:
            print(renderer._colored(
                "Session total: " + _format_usage(
                    ctx.total_usage, time.monotonic() - chat_started,
                    renderer.sym),
                "90"))
        if ctx.last_saved:
            print(f"Session saved: {ctx.last_saved}", file=sys.stderr)
            print(f"Resume with: {_resume_hint(ctx, ctx.last_saved, ctx.last_saved_agent)}",
                  file=sys.stderr)
        # A BORROWED loop is not ours to tear down: the CLI's finally still
        # runs shutdown_tools/shutdown_batch_system on it after we return, and
        # close_cli_loop() at exit does the cancel/asyncgens/executor/close
        # dance exactly once. Cancelling all tasks here would kill the MCP
        # connections and the batch manager out from under those shutdowns.
        # No `return` in here: it swallowed whatever the chat raised on a
        # borrowed loop -- which is every chat the CLI starts.
        if owns_loop:
            _close_own_loop(loop)
