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

_UNICODE_SYMBOLS = {"run": "▸", "ok": "✓", "err": "✗", "think": "✻", "cut": "…", "sep": " · "}
_ASCII_SYMBOLS = {"run": ">", "ok": "+", "err": "x", "think": "*", "cut": "...", "sep": ", "}


def _pick_symbols(out: TextIO) -> dict[str, str]:
    """Unicode symbols where the stream can encode them, ASCII otherwise."""
    encoding = getattr(out, "encoding", None) or "utf-8"
    try:
        "".join(_UNICODE_SYMBOLS.values()).encode(encoding)
        return _UNICODE_SYMBOLS
    except (UnicodeEncodeError, LookupError):
        return _ASCII_SYMBOLS


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
        single = indent + " ".join(text.split())
        limit = self._width() - 1
        if len(single) <= limit:
            return single
        cut = self.sym["cut"]
        return single[: max(limit - len(cut), 1)] + cut

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
        offset = self._total - self._lines[key] if key in self._lines else None
        if offset is not None and offset <= self._usable_height():
            self.out.write(f"\x1b[{offset}A\r\x1b[K{text}\x1b[{offset}B\r")
        else:
            self._lines[key] = self._total
            self._total += 1
            self.out.write(text + "\n")
        self.out.flush()

    def _commit_region(self) -> None:
        """Freeze the live region: lines stay as printed, tracking resets."""
        self._lines.clear()
        self._total = 0

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
        soft-wrapped line would silently shift every offset above it), and
        colour is applied per chunk AFTER slicing so no escape is ever cut.
        """
        if not self.ansi:
            self.out.write(text + "\n")
            self.out.flush()
            return
        reassert_vt()  # see _paint
        width = max(self._width() - 1, 10)
        for logical in text.splitlines() or [""]:
            for start in range(0, len(logical), width) if logical else (0,):
                chunk = logical[start:start + width]
                self.out.write(self._colored(chunk, color) if chunk else "")
                self.out.write("\n")
                self._total += 1
        self.out.flush()

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
        # Unique key per error: consecutive distinct errors must not
        # overwrite each other on one shared region line.
        self._error_seq = getattr(self, "_error_seq", 0) + 1
        self._paint(f"__error__{self._error_seq}", self._colored(self._fit(text), "31"))

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

    result: dict[str, Any] = {"summary": None, "cancelled": False, "errors": []}
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
                if assistant.get("tool_calls") and isinstance(content, str):
                    renderer.narration(content)
            elif t == "heartbeat":
                if show_status:
                    renderer.thinking_tick()
            elif t == "final":
                result["summary"] = ev.get("summary") or ""
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


def parse_chat_command(line: str) -> Optional[str]:
    """Map a /-command line to its canonical name, None for normal input."""
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    word = stripped.split()[0].lower()
    aliases = {
        "/exit": "exit", "/quit": "exit", "/q": "exit", "/bye": "exit",
        "/new": "new",
        "/session": "session",
        "/help": "help", "/?": "help",
    }
    return aliases.get(word, "unknown")


_HELP_TEXT = """\
Commands:
  /exit, /quit, /q   end the chat (Ctrl-D / Ctrl-Z+Enter work too)
  /new               start a fresh session (current one stays saved)
  /session           show the current session id
  /help              this help
  Ctrl-C             cancel the running turn (the chat keeps going)"""


class _ChatContext:
    """Everything one REPL needs, bundled so helpers stay signature-sane."""

    def __init__(self, *, agent: Any, entry_name: str, session_service: Any,
                 session_user: str, session_id: str, was_new_session: bool,
                 llm_profile: str, llm_override: Any,
                 llm_profile_info: Optional[str], show_status: bool) -> None:
        self.agent = agent
        self.entry_name = entry_name
        self.session_service = session_service
        self.session_user = session_user
        self.session_id = session_id
        self.was_new_session = was_new_session
        self.llm_profile = llm_profile
        self.llm_override = llm_override
        self.llm_profile_info = llm_profile_info
        self.show_status = show_status
        # Which session id actually reached disk (None until the first save):
        # the exit message must not claim a save that never happened.
        self.last_saved: Optional[str] = None


def _init_fresh_session(ctx: _ChatContext) -> str:
    """Create a new session id and seed the tracker like the CLI bootstrap does."""
    from ..utils.id import short_id

    new_id = short_id()
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is not None:
        tracker.set_session_messages(new_id, [])
        agent_config = getattr(ctx.agent, "agent_config", None)
        template_vars = getattr(agent_config, "template_vars", None) if agent_config else None
        if template_vars:
            tracker.set_session_template_vars(new_id, dict(template_vars))
        tracker.set_session_metadata(new_id, {
            "user_id": ctx.session_user,
            "agent_name": ctx.entry_name,
            "llm_profile": ctx.llm_profile,
        })
    return new_id


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
    print("\nCancelling turn… (Ctrl-C again to force)", file=sys.stderr)
    request_id = state.get("request_id")
    if request_id:
        # Graceful: flips the cancellation token, the agent unwinds and
        # yields its cancelled/end events through the normal path.
        try:
            loop.run_until_complete(
                asyncio.wait_for(ctx.agent.cancel_request(request_id), timeout=5)
            )
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
) -> None:
    """The chat REPL. Owns one event loop for its whole lifetime."""
    ctx = _ChatContext(
        agent=agent, entry_name=entry_name, session_service=session_service,
        session_user=session_user, session_id=session_id,
        was_new_session=was_new_session, llm_profile=llm_profile,
        llm_override=llm_override, llm_profile_info=llm_profile_info,
        show_status=show_status,
    )
    ansi = supports_color()
    renderer = ChatRenderer(ansi=ansi)
    prompt = "❯ " if renderer.sym is _UNICODE_SYMBOLS else "> "

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

    dash = "─" if renderer.sym is _UNICODE_SYMBOLS else "-"
    llm_label = ctx.llm_profile_info or ctx.llm_profile
    print(dash * min(shutil.get_terminal_size((80, 20)).columns, 72))
    print(f"Chat with {ctx.entry_name}   LLM: {llm_label}   Session: {ctx.session_id}")
    print("Type /help for commands, /exit to quit. Ctrl-C cancels the running turn.")
    print(dash * min(shutil.get_terminal_size((80, 20)).columns, 72))

    pending: Optional[str] = initial_task.strip() if initial_task else None
    try:
        while True:
            if pending is not None:
                task, pending = pending, None
                print(f"{prompt}{task}")  # keep the transcript complete
            else:
                restore_console_input_mode(input_mode)
                try:
                    task = input(prompt)
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print("\n(/exit to quit)")
                    continue
                # Piped stdin is not echoed by input(); print it so the
                # transcript still shows what was asked.
                if not sys.stdin.isatty():
                    print(task)
            task = task.strip()
            if not task:
                continue

            command = parse_chat_command(task)
            if command == "exit":
                break
            if command == "new":
                ctx.session_id = _init_fresh_session(ctx)
                ctx.was_new_session = True
                print(f"New session: {ctx.session_id}")
                continue
            if command == "session":
                print(f"Session: {ctx.session_id}  (user: {ctx.session_user})")
                continue
            if command == "help":
                print(_HELP_TEXT)
                continue
            if command == "unknown":
                print(f"Unknown command: {task.split()[0]}  (/help lists commands)")
                continue

            result = _execute_turn(loop, ctx, task, renderer)

            if result.get("cancelled"):
                print("Turn cancelled.", file=sys.stderr)
                continue  # nothing new worth saving; next turn saves anyway

            summary = result.get("summary")
            if summary:
                _render_answer(loop, ctx, renderer, summary)

            if loop.run_until_complete(_save_session(ctx)):
                ctx.last_saved = ctx.session_id
                ctx.was_new_session = False
    finally:
        if ctx.last_saved:
            print(f"Session saved: {ctx.last_saved}", file=sys.stderr)
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
