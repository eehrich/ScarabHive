"""The conversation read back, taken back and handed on.

/history and /last read the session's messages back (tool calls and results
compact or in full), /undo and /retry cut the last exchange off -- /undo
files and /rewind put back the files it changed too -- and /export and /copy
hand the text to a file or the clipboard.

One module per topic of commands: repl.py's command table points each
command at its handler here (_on_<command>).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any, Optional

from agent_system.chat_actions import (
    last_answer,
    message_text,
    one_line,
    split_off_last_exchange,
    starts_a_turn,
    tool_call_summary,
    transcript_markdown,
)
from agent_system.chat_commands import parse_undo
from ...paths import user_path
from . import context, interruptible

if TYPE_CHECKING:
    from .context import _ChatContext
    from .display import ChatRenderer
    from .repl import _Repl

logger = logging.getLogger(__name__)


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


#: One reader for both shapes of a message, shared with the web surface.
_message_text = message_text


#: /history and /last cut at the same place /undo does.
_is_real_turn = starts_a_turn


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

    messages = context._session_messages(ctx)
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
    messages = context._session_messages(ctx)
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
    kept, dropped = split_off_last_exchange(context._session_messages(ctx))
    if dropped is None:
        return None
    try:
        tracker.set_session_messages(ctx.session_id, kept)
    except Exception as e:
        logger.error("Could not drop the last exchange: %s", e, exc_info=True)
        print(f"Could not drop the last exchange: {e}")
        return None
    return dropped


def _file_rewinder_or_say() -> Any:
    from agent_system.file_rewind import file_rewinder

    rewinder = file_rewinder()
    if rewinder is None:
        print("File checkpoints are off -- the file_checkpoints plugin is not loaded.")
    return rewinder


def _rewind_last_turn(loop: asyncio.AbstractEventLoop, ctx: "_ChatContext", overwrite: bool) -> bool:
    """`/undo files`: put back the files the last exchange changed, BEFORE it is
    dropped. False when the exchange has to stay -- nothing to take back, no
    rewinder, or a rewind that was refused or only partly went through (what
    it says is printed): the person looks, and tries again."""
    from agent_system.file_rewind import NOTHING, REWOUND

    messages = context._session_messages(ctx)
    if split_off_last_exchange(messages)[1] is None:
        print("Nothing to take back in this session yet.")
        return False
    rewinder = _file_rewinder_or_say()
    if rewinder is None:
        return False
    report = interruptible._run_to_the_end(loop, rewinder.rewind(
        user_id=ctx.session_user, session_id=ctx.session_id, messages=messages, checkpoint=None,
        registry=getattr(ctx.agent, "registry", None), overwrite=overwrite))
    print(report["text"])
    if report["status"] not in (REWOUND, NOTHING):
        print("(the exchange stays -- /undo without 'files' drops it and leaves the files)")
        return False
    return True


def _rewind_command(loop: asyncio.AbstractEventLoop, ctx: "_ChatContext", payload: str) -> None:
    """`/rewind` lists the file checkpoints, `/rewind <n> [overwrite]` puts the
    files back as they were before checkpoint n. The conversation stays."""
    request = parse_undo(payload, rewind=True)
    # force is the browser's word for a lock a crashed process left; this chat holds its session itself.
    if request.errors or request.force:
        print("Usage: /rewind lists the checkpoints, /rewind <n> puts the files back as they were "
              "before checkpoint n, /rewind <n> overwrite also the files changed outside the agent.")
        return
    rewinder = _file_rewinder_or_say()
    if rewinder is None:
        return
    if request.checkpoint is None:
        finished, listing = interruptible._run_interruptible(loop, rewinder.checkpoints(
            user_id=ctx.session_user, session_id=ctx.session_id, messages=context._session_messages(ctx)),
            "/rewind")
        if finished:
            print(listing["text"])
        return
    report = interruptible._run_to_the_end(loop, rewinder.rewind(
        user_id=ctx.session_user, session_id=ctx.session_id, messages=context._session_messages(ctx),
        checkpoint=request.checkpoint, registry=getattr(ctx.agent, "registry", None),
        overwrite=request.overwrite))
    print(report["text"])


def _export_transcript(ctx: "_ChatContext", payload: str) -> None:
    """Write the conversation to a markdown file.

    An existing file is never overwritten: the obvious name (`/export`
    without a path) is the same for every export of one session, and losing
    yesterday's transcript to today's would be silent.
    """
    messages = context._session_messages(ctx)
    if not messages:
        print("Nothing to export -- this session has no messages yet.")
        return
    # Written where the person stands, not where the process runs -- the chat
    # runs from the project since enter_project(). user_path also holds the ~
    # handling this used to do itself: `/export ~$notes.md`, the lock file Word
    # leaves beside a document, RAISED out of pathlib and took the chat down.
    path = user_path(payload.strip() or f"chat-{ctx.session_id}.md")
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


def _clipboard_writers() -> list[list[str]]:
    """The clipboard commands to try, best first, for this platform."""
    if os.name == "nt":
        return [["clip"]]
    if sys.platform == "darwin":
        return [["pbcopy"]]
    return [["wl-copy"], ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"]]


def _copy_to_clipboard(text: str) -> Optional[str]:
    """Put *text* on the system clipboard. Returns the error, or None on success.

    Bytes, not a str through ``input=``: `clip` on Windows reads its stdin in
    the console codepage and turns every umlaut in an answer into a question
    mark -- measured. It does understand UTF-16LE, which is what it gets here.
    """
    encoding = "utf-16-le" if os.name == "nt" else "utf-8"
    payload = text.encode(encoding, errors="replace")
    writers = _clipboard_writers()
    refused = []
    for argv in writers:
        try:
            subprocess.run(argv, input=payload, check=True)
            return None
        except FileNotFoundError:
            continue
        except (subprocess.CalledProcessError, OSError) as e:
            # Installed but not usable is not the end of the list: wl-copy is
            # on plenty of X11 machines and exits non-zero there, and giving
            # up on it would skip the xclip that would have taken the text.
            refused.append(f"{argv[0]}: {e}")
    if refused:
        return "; ".join(refused)
    return ("no clipboard tool found -- tried "
            + ", ".join(argv[0] for argv in writers))


def _copy_last_answer(ctx: "_ChatContext") -> None:
    """``/copy``: the agent's last answer onto the clipboard, as it was written.

    The TEXT of the message, not what the terminal made of it: the live region
    wraps to the window and shortens tool lines, so copying from the scrollback
    gives back a hard-wrapped, truncated version of what the model said.
    """
    text = last_answer(context._session_messages(ctx))
    if not text:
        print("No answer to copy yet.")
        return
    error = _copy_to_clipboard(text)
    if error:
        print(f"Could not copy: {error}")
    else:
        lines = text.count("\n") + 1
        print(f"Copied the last answer ({len(text)} chars, {lines} line(s)).")


def _take_back(repl: _Repl, command: str, payload: str) -> Optional[Any]:
    """`/undo` and `/retry`: drop the last exchange, and for `/retry` ask it again.

    Returns the task the retry runs as a turn of its own, or None when the
    command is done -- an undo, or a take-back that did not happen.
    """
    ctx, renderer = repl.ctx, repl.renderer
    undo = parse_undo(payload)
    if undo.errors or undo.force:
        print(f"/{command} takes 'files' and 'overwrite' (with files): "
              f"/{command} files puts back the files the exchange changed too.")
        return None
    if undo.files and not _rewind_last_turn(repl.loop, ctx, undo.overwrite):
        return None
    dropped = _drop_last_exchange(ctx)
    if dropped is None:
        print("Nothing to take back in this session yet.")
        return None
    asked = _message_text(dropped).strip()
    print(renderer._colored(f"(dropped: {_one_line(asked, 70)})", "90"))
    # The record has to match what the agent now holds, or the
    # next `--session <id>` brings the dropped turn back --
    # which is also why a session emptied by /undo is written
    # empty (services/session_service.py, SessionTracker.emptied).
    # A save that fails is named without guessing at the cause.
    if not ctx.was_new_session and not context._save_now(repl.loop, ctx):
        print(f"(the shortened session was NOT written -- "
              f"--session {ctx.session_id} still brings the "
              f"dropped turn back)", file=sys.stderr)
    if command == "undo":
        return None
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
    print(f"{repl.prompt}{_one_line(asked, 200)}")
    return task


def _on_undo(repl: _Repl, payload: str) -> None:
    _take_back(repl, "undo", payload)


def _on_retry(repl: _Repl, payload: str) -> Optional[Any]:
    return _take_back(repl, "retry", payload)


def _on_rewind(repl: _Repl, payload: str) -> None:
    _rewind_command(repl.loop, repl.ctx, payload)


def _on_history(repl: _Repl, payload: str) -> None:
    _show_history(repl.ctx, repl.renderer, payload)


def _on_last(repl: _Repl, payload: str) -> None:
    _show_last(repl.ctx, repl.renderer)


def _on_export(repl: _Repl, payload: str) -> None:
    _export_transcript(repl.ctx, payload)


def _on_copy(repl: _Repl, payload: str) -> None:
    _copy_last_answer(repl.ctx)
