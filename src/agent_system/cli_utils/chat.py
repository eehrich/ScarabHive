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
import logging
import os
import shutil
import sys
import time
import unicodedata
from typing import Any, Optional, TextIO

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
        offset = self._total - self._lines[key] if key in self._lines else None
        if offset is not None and offset <= self._usable_height():
            self.out.write(f"\x1b[{offset}A\r\x1b[K{text}\x1b[{offset}B\r")
        else:
            self._lines[key] = self._total
            self._total += 1
            self.out.write(text + "\n")
        self.out.flush()

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
        width = max(self._width() - 1, 10)
        for logical in text.splitlines() or [""]:
            for chunk in self._wrap(logical.expandtabs(4), width):
                self.out.write(self._colored(chunk, color) if chunk else "")
                self.out.write("\n")
                self._total += 1
        self.out.flush()

    @staticmethod
    def _wrap(line: str, width: int) -> list[str]:
        """Break `line` into chunks of at most `width` COLUMNS, on words.

        textwrap can't be used: it counts code points, so a CJK line would
        come back too wide and wrap again in the terminal -- the exact
        corruption the hard wrap exists to prevent.
        """
        if not line:
            return [""]
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
                _accumulate_usage(result["usage"], ev.get("usage"))
            elif t == "heartbeat":
                if show_status:
                    renderer.thinking_tick()
            elif t == "final":
                result["summary"] = ev.get("summary") or ""
                _accumulate_usage(result["usage"], ev.get("usage"))
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
    "/help": "help", "/?": "help",
}


def parse_chat_command(line: str) -> tuple[Optional[str], str]:
    """Split a prompt line into (command, payload).

    Only KNOWN aliases are commands. Anything else starting with "/" is a
    normal message -- "/etc/nginx/nginx.conf pruefen" is ordinary input for a
    sysadmin agent, and treating it as a typo'd command silently ate it.
    "//" is the literal escape for a message that really has to start with a
    command word.
    """
    stripped = line.strip()
    if stripped.startswith("//"):
        return None, stripped[1:]
    if not stripped.startswith("/"):
        return None, stripped
    word, _, rest = stripped.partition(" ")
    command = _COMMAND_ALIASES.get(word.lower())
    if command is None:
        return None, stripped
    return command, rest.strip()


_HELP_TEXT = '''\
Commands:
  /exit, /quit, /q   end the chat (Ctrl-D / Ctrl-Z+Enter work too)
  /new               start a fresh session (current one stays saved)
  /session           show the current session and how to resume it
  /sessions          list recent sessions
  /resume <id>       continue an earlier session
  /help              this help

Input:
  """               start/end a multi-line message (paste code between them)
  \\ at line end      continue on the next line
  //text             send a message that starts with a command word

  Ctrl-C             cancel the running turn; twice at the prompt exits'''


_FENCE = '"""'


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


def _accumulate_usage(total: dict, usage: Any) -> None:
    """Add one turn's usage into the running total (best effort)."""
    if not isinstance(usage, dict):
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value


def _format_usage(usage: dict, elapsed: float, sym: dict) -> str:
    """One dim footer line: tokens, cost, wall time."""
    def _short(n: float) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"

    parts = []
    if usage.get("prompt_tokens") or usage.get("completion_tokens"):
        up = _short(usage.get("prompt_tokens", 0))
        down = _short(usage.get("completion_tokens", 0))
        parts.append(f"{sym['up']}{up} {sym['down']}{down}")
    elif usage.get("total_tokens"):
        parts.append(f"{_short(usage['total_tokens'])} tokens")
    if usage.get("cost"):
        parts.append(f"${usage['cost']:.4f}")
    mins, secs = divmod(int(elapsed), 60)
    parts.append(f"{mins}m{secs:02d}s" if mins else f"{secs}s")
    return sym["sep"].join(parts)


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
    try:
        return loop.run_until_complete(turn)
    except KeyboardInterrupt:
        return _cancel_turn(loop, ctx, turn, state, renderer)
    except Exception as e:
        # One broken turn (LLM auth, network, agent bug) must not end the
        # whole chat: report it and hand the user the next prompt.
        logger.error("Chat turn failed: %s", e, exc_info=True)
        renderer.close()
        print(f"Turn failed: {e}", file=sys.stderr)
        return {"summary": None, "cancelled": False, "errors": [str(e)]}


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
            if command == "help":
                print(_HELP_TEXT)
                continue

            started = time.monotonic()
            result = _execute_turn(loop, ctx, task, renderer)

            if result.get("cancelled"):
                print("Turn cancelled.", file=sys.stderr)
                continue  # nothing new worth saving; next turn saves anyway

            summary = result.get("summary")
            if summary:
                _render_answer(loop, ctx, renderer, summary)
            elif not result.get("errors"):
                print(renderer._colored("(no answer returned)", "90"))

            usage = result.get("usage") or {}
            _accumulate_usage(ctx.total_usage, usage)
            if ctx.show_status:
                print(renderer._colored(
                    _format_usage(usage, time.monotonic() - started, renderer.sym), "90"))

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
                    ctx.total_usage, time.monotonic() - chat_started, renderer.sym),
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
