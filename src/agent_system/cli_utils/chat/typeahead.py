"""What the person types while a turn runs.

The prompt is gone while the agent works, the keyboard is not: _KeyReader
reads keys without blocking (msvcrt on Windows, cbreak and select() on
POSIX), _poll_typed_input shows the line being typed below the live region
and hands every finished one to the running agent -- or to the question the
run asked -- and _stop_typing gives the terminal back and keeps a half-typed
line for the next prompt.

Its own module because it owns terminal state for the length of a turn and
has to give it back on every way out; the turn (turn.py) only starts and
stops it.
"""
from __future__ import annotations

import asyncio
import codecs
import logging
import os
import sys
from typing import TYPE_CHECKING, Optional

from agent_system.chat_commands import resolve as resolve_chat_input

if TYPE_CHECKING:
    from .context import _ChatContext
    from .display import ChatRenderer

logger = logging.getLogger(__name__)


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


async def _poll_typed_input(reader: _KeyReader, renderer: ChatRenderer,
                            ctx: "_ChatContext", state: dict) -> None:
    """Show what the user types mid-turn and hand finished lines to the agent.

    The agent drains injected messages at step boundaries, so a line sent here
    lands in the conversation at the next step -- it does not interrupt the
    running one. That is the same contract the WebUI has.
    """
    from ..questions import NOT_AN_ANSWER

    prompt = "» "
    last_shown = None
    # Whether a line is being typed, and the question shown when it was begun:
    # the one it answers -- not one that came while it was typed, unread.
    typing, begun_for = False, None
    # The question shown at the last tick: the keys of this one came after it
    # was drawn. One drawn since -- while the poller slept -- may have come
    # after the keys, so it counts from the next tick on.
    readable = None
    try:
        while reader.enabled:
            questions = state.get("questions")
            shown, readable = readable, questions.current if questions is not None else None
            for submitted in reader.poll():
                meant_for = begun_for if typing else shown   # else begun and ended this tick
                typing = False
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
                # A question of the run open: the line is its answer. Into the run it
                # would end the question unanswered (ask_user: the person wrote instead).
                taken = questions.answer(submitted, meant_for) if questions is not None else None
                if taken is not None:
                    if taken == NOT_AN_ANSWER and not reader.buffer and "\n" not in submitted:
                        reader.buffer = submitted   # back on the input line, to make it one
                        typing, begun_for = True, meant_for
                    last_shown = None
                    continue
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
            if not reader.buffer:
                typing = False
            elif not typing:
                typing, begun_for = True, shown
            if reader.buffer != last_shown:
                renderer.set_input_row(prompt + reader.buffer if reader.buffer else None)
                last_shown = reader.buffer
            if state.get("questions") is not None:
                state["questions"].refresh()
            await asyncio.sleep(_KEY_POLL_S)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Type-ahead poller stopped", exc_info=True)
    # Ended without being stopped -- the reader failed (it disables itself, or
    # raised): nothing typed reaches the run any more, so nobody can answer what
    # it asks. A question waiting ends now (nobody reads), none waits for the timeout.
    from ...core.request_context import set_run_attended
    set_run_attended(state.get("request_id") or "", False)


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
