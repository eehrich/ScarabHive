"""How a message gets written at the chat's prompt.

The line itself (_read_input: a triple-quote fence or a trailing backslash
spans several lines), the line editor behind it (_PromptEditor on
prompt_toolkit, with the session's arrow-up history and Tab completion, or
plain input() where no console can be driven), piped stdin that starts with
a byte-order mark, and /edit, which writes the message in $EDITOR instead.

Its own module because all of it gets text from the person into the REPL
and none of it decides what the text means: the REPL resolves the line, and
Tab completion asks the REPL what exists (repl._completions_for).
"""
from __future__ import annotations

import asyncio
import codecs
import logging
import os
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

from agent_system.chat_actions import one_line as _one_line
from agent_system.chat_commands import parse_chat_command

if TYPE_CHECKING:
    from .repl import _Repl

logger = logging.getLogger(__name__)


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
                 completer: Any = None,
                 loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._prompt_session_cls = prompt_session_cls
        self._history_cls = history_cls
        self._key_bindings = key_bindings
        self._completer = completer
        #: The REPL's loop, so waiting for a line RUNS it -- see _ask.
        self._loop = loop
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
        return self._ask(self._session, prompt)

    def read_continuation(self, prompt: str) -> str:
        return self._ask(self._continuation, prompt)

    def _ask(self, session: Any, prompt: str) -> str:
        """Wait for a line ON the REPL's event loop.

        ``PromptSession.prompt()`` is synchronous: it starts a loop of its own
        and blocks this thread until Enter. Everything already running on the
        REPL's loop then stands still between two turns -- measured on
        20.09.2026, a sub-agent's one-step LLM call sat unread for four
        minutes and finished 0.3 s after somebody typed, because typing is
        what turned the loop again. `wake_when_done` could not work in the
        chat at all that way: the job that sets the wake mark was frozen, so
        the mark the prompt watches for never appeared.

        ``prompt_async`` is the same prompt as a coroutine, so waiting for a
        line drives the loop that the background work lives on.

        Without a loop it falls back to the synchronous call -- that is for a
        _PromptEditor built outside the REPL; the REPL always passes one.
        """
        if self._loop is None:
            return session.prompt(prompt)
        return self._loop.run_until_complete(session.prompt_async(prompt))

    def typed_text(self) -> str:
        """What stands in the line being written -- wherever the focus is.

        NOT ``app.current_buffer``: that one follows the FOCUS. Ctrl-R moves
        the focus to the search buffer (prompt_toolkit's ``start_search``
        focuses ``search_buffer_control``, and ``Layout.current_buffer``
        returns whatever is focused), and where the focus is on no buffer at
        all ``Application.current_buffer`` hands out an empty DUMMY buffer by
        design. Both read as "nothing typed" while the person's line sits
        untouched in the default buffer -- and the one caller of this asks in
        order NOT to throw that line away.
        """
        buffer = getattr(self._session, "default_buffer", None)
        return getattr(buffer, "text", "") or ""

    def app(self) -> Any:
        """The prompt_toolkit Application of the MAIN prompt, or None.

        Asked for per use, never held: ``reseed`` builds a new prompt session
        (and with it a new app) every time /new or /resume swaps the session
        underneath. Only the main prompt -- the continuation lines of a paste
        are content, and nothing may cut into them.
        """
        return getattr(self._session, "app", None)


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
    loop: Optional[asyncio.AbstractEventLoop] = None,
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
                             completer=_build_completer(suggest) if suggest else None,
                             loop=loop)
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


_FENCE = '"""'


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


def _editor_command() -> list[str]:
    """What ``/edit`` starts, as argv.

    $VISUAL before $EDITOR, the order every unix tool uses. The value is a
    COMMAND LINE, not a file name -- "code -w", "subl -n -w" and "vim -u NONE"
    are all normal contents -- so it is split the way a shell would.
    """
    import shlex

    raw = (os.environ.get("VISUAL") or os.environ.get("EDITOR") or "").strip()
    if not raw:
        return ["notepad"] if os.name == "nt" else ["vi"]
    if os.name != "nt":
        return shlex.split(raw)
    # Measured, because neither mode is right on its own here: posix splitting
    # eats the separators (an unquoted C:\Windows\notepad.exe comes back as
    # C:Windowsnotepad.exe), and non-posix leaves the quotes ON the token, so a
    # quoted path reaches subprocess as '"C:\Program Files\..."' and cannot be
    # opened. Split the way the backslashes survive, then take the quotes off.
    return [token[1:-1] if len(token) > 1 and token[0] == token[-1] == '"'
            else token
            for token in shlex.split(raw, posix=False)]


def _editor_needs_a_terminal() -> bool:
    """Whether /edit has to refuse: an end that is not a terminal.

    The editor inherits THIS process's stdout. With it redirected
    (``agent-cli chat > log.txt``) a full-screen editor draws its whole screen
    into the file while reading keys from the tty -- the person sees nothing
    and sits in an invisible vim. The line editor refuses the same pairing for
    the same reason (see the ``interactive`` check in run_chat_loop); this is
    the one place that hands the terminal to somebody else entirely.
    """
    try:
        return not (sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return True


def _compose_in_editor(seed: str = "",
                       loop: Optional[asyncio.AbstractEventLoop] = None) -> Optional[str]:
    """Write the next message in $EDITOR.

    Returns the text, "" for an empty file, and None when the editor could not
    run or the person backed out -- that case has said why already, and the
    caller adding "nothing sent" on top of it reads as a second failure.

    For the messages a prompt line is the wrong shape for -- a spec, a pasted
    diff with a paragraph around it, anything worth a second look before it is
    billed. The fence and the trailing backslash stay what they are: a way to
    type several lines, not a way to revise them.

    The file ends in .md because that is what the text IS, and every editor
    that highlights anything highlights that.
    """
    fd, path = tempfile.mkstemp(prefix="agent-chat-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(seed)
        argv = [*_editor_command(), path]
        try:
            # No capture: the editor IS the terminal now, and a console editor
            # with its output piped away draws into the pipe and hangs.
            #
            # On the loop, for the same reason the prompt is (_PromptEditor.
            # _ask): writing a message in vim takes minutes, and a plain
            # subprocess.run blocks this thread for all of them -- a sub-agent
            # running in the background would freeze exactly while somebody
            # composes, which is when it has the most time to finish. The
            # executor keeps subprocess.run's semantics (inherited terminal,
            # check=True) and lets run_until_complete turn the loop meanwhile.
            if loop is None:
                subprocess.run(argv, check=True)
            else:
                loop.run_until_complete(loop.run_in_executor(
                    None, lambda: subprocess.run(argv, check=True)))
        except FileNotFoundError:
            print(f"No editor: {argv[0]!r} was not found. "
                  "Set $EDITOR to the one you use.")
            return None
        except subprocess.CalledProcessError as e:
            # `vi` ending non-zero means the person aborted; sending the file
            # anyway would bill the turn they just backed out of.
            print(f"{argv[0]} ended with {e.returncode} -- nothing sent.")
            return None
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as e:
        print(f"Could not compose the message: {e}")
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            logger.debug("Could not remove %s", path, exc_info=True)
    return text.strip()


def _on_edit(repl: _Repl, payload: str) -> Optional[str]:
    """`/edit [text]`: the next message, written in $EDITOR. Returns it, or None."""
    if _editor_needs_a_terminal():
        print("/edit needs a terminal at both ends -- the "
              "editor would draw its screen into the "
              "redirect. Use \"\"\" for a multi-line message.")
        return None
    composed = _compose_in_editor(payload, loop=repl.loop)
    if composed is None:
        return None            # _compose_in_editor said why
    if not composed:
        print("Empty -- nothing sent.")
        return None
    task = composed
    # It never passed the prompt, so arrow-up would not have
    # it -- same reason an initial_task is remembered.
    if repl.editor:
        repl.editor.remember(task)
    print(f"{repl.prompt}{_one_line(task, 200)}")
    return task
