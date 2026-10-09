"""The chat REPL: read a line, run what it names, show what came back.

run_chat_loop / run_chat_turn: the REPL around the one-shot turn.
One persistent event loop for all turns (LLM/http clients pool their
connections per loop -- a loop per turn would break on turn 2), sync
input() between turns so Ctrl-C behaves natively at the prompt.

A line resolves (chat_commands.resolve) to a message, a skill, a plugin
command or a built-in command. A built-in goes through the command table
(_COMMANDS) to its handler in the module of its topic; the handler either
finishes and the prompt comes back, or -- /retry, /edit -- hands back the
task of a turn. Everything else becomes a turn (turn.py).

The REPL also knows what exists -- its commands, the agent's plugin
commands, the skills, the sessions -- so what Tab completion offers is
worked out here (_completions_for), not in the line editor.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

# The catalogue, the parser and the typo hints live in
# agent_system.chat_commands so the web UI resolves a line exactly the way the
# terminal does. Imported into this namespace because the REPL below (and its
# tests) call them by these names.
from agent_system.chat_actions import one_line as _one_line
from agent_system.chat_commands import (
    CLI as _CLI_SURFACE,
    PluginCommand,
    commands_for,
    parse_chat_command,
    resolve as resolve_chat_input,
    suggest_command,
)
from agent_system.plugin_commands import (
    collect_plugin_commands,
    help_lines as _plugin_help_lines,
    run_plugin_command,
    spellings as _plugin_spellings,
)
from ...core.session_presence import WAKE_TASK
from ...paths import user_path
from ..agent_runner import wake_message
from ..common import restore_console_input_mode, snapshot_console_input_mode, supports_color
from ..event_loop import shut_down_loop
from . import (
    agent_setup,
    context,
    display,
    interruptible,
    prompt_input,
    sessions,
    token_usage,
    transcript,
    turn,
)

if TYPE_CHECKING:
    from .context import _ChatContext
    from .display import ChatRenderer
    from .prompt_input import _PromptEditor

logger = logging.getLogger(__name__)


@dataclass
class _Repl:
    """The REPL as its command handlers see it.

    The session side is ``ctx`` (context._ChatContext); this is the terminal
    side around it -- the loop every command's work runs on, the display, the
    line editor and the prompt -- and what a line is resolved against. One
    object, so the command table hands every handler the same two arguments:
    this, and the rest of the line.
    """

    loop: asyncio.AbstractEventLoop
    ctx: _ChatContext
    renderer: ChatRenderer
    editor: Optional[_PromptEditor]
    #: A line that becomes a turn without passing the prompt (/retry, /edit)
    #: is printed behind it, to keep the transcript complete.
    prompt: str
    read_cont: Optional[Callable[[str], str]]
    ansi: bool
    interactive: bool
    #: The agent's plugin commands, collected once by the REPL; /agent
    #: replaces them with the new agent's.
    plugin_commands: Sequence[PluginCommand]
    #: The runnable skills as of the line being handled: they are folders on
    #: disk, which a person can edit mid-chat.
    skill_names: Sequence[str] = ()


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
        # Where the person stands, the same directory /attach and /export
        # resolve against. Listing the process's own would offer them the
        # project's files and then look for the accepted name somewhere else
        # -- and where both trees hold that name, silently take the wrong one.
        entries = sorted(user_path(directory or ".").iterdir())
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
    word = prompt_input._completion_word(line)
    if command == "model":
        profiles = agent_setup._llm_profiles(ctx)
        return [(name, _one_line(getattr(profiles[name], "description", "") or "", 60))
                for name in sorted(profiles)]
    if command == "agent":
        return [(name, "agent") for name in agent_setup._agent_names(ctx)]
    if command == "think":
        from ...llm.factory import THINKING_LEVELS

        return [(level, "thinking level") for level in THINKING_LEVELS] + [("default", "the model's own")]
    if command == "resume":
        # Whatever the last listing knows; /sessions and a bare /resume fill
        # it. Reading the store HERE is not possible -- the completer runs
        # inside prompt_toolkit's own loop, not the REPL's.
        return [(entry["session_id"],
                 _one_line(" ".join((entry.get("title") or "").split()), 60))
                for entry in sessions._resumable_sessions(ctx)]
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
        "",
        "Manual: /help manual -- or /help <question or topic>",
    ]
    return "\n".join(lines)


def _on_help(repl: _Repl, payload: str) -> None:
    if not payload:
        print(_help_text(repl.skill_names, repl.plugin_commands))
        return
    # The guides, browsed right here: nothing of it reaches the agent or the session.
    from ..help_viewer import open_help
    open_help(payload, getattr(repl.ctx.agent, "system_config", None),
              read=repl.read_cont or (lambda text: input(text)),
              run=lambda coro: interruptible._run_interruptible(repl.loop, coro, "/help"),
              ansi=repl.ansi, interactive=repl.interactive)


def _on_unknown(repl: _Repl, payload: str) -> None:
    """A command word nobody claims: say so, never send it as a message."""
    hint = suggest_command(
        payload,
        list(repl.skill_names) + _plugin_spellings(repl.plugin_commands))
    did_you_mean = f"  Did you mean {hint}?" if hint else ""
    print(f"Unknown command: {payload}{did_you_mean}")
    print(f"/help lists the commands; //{payload[1:]} sends it as a message.")


#: Every built-in command except /exit, which ends the loop itself, under the
#: name chat_commands resolves it to -- "unknown" for a command word nobody
#: claims. A handler gets the REPL and the rest of the line and returns None
#: when the command is done and the prompt comes back, or, for /retry and
#: /edit, the task of the turn it turns into: that then runs like a typed
#: message, attachments and all.
_COMMANDS: dict[str, Callable[[_Repl, str], Optional[Any]]] = {
    "new": sessions._on_new,
    "session": sessions._on_session,
    "sessions": sessions._on_sessions,
    "resume": sessions._on_resume,
    "title": sessions._on_title,
    "agent": agent_setup._on_agent,
    "vars": sessions._on_vars,
    "think": agent_setup._on_think,
    "model": agent_setup._on_model,
    "tools": agent_setup._on_tools,
    "skills": agent_setup._on_skills,
    "context": agent_setup._on_context,
    "costs": token_usage._on_costs,
    "history": transcript._on_history,
    "last": transcript._on_last,
    "copy": transcript._on_copy,
    "attach": sessions._on_attach,
    "export": transcript._on_export,
    "rewind": transcript._on_rewind,
    "undo": transcript._on_undo,
    "retry": transcript._on_retry,
    "edit": prompt_input._on_edit,
    "help": _on_help,
    "unknown": _on_unknown,
}


def _run_plugin_command(loop: asyncio.AbstractEventLoop, ctx: "_ChatContext",
                        commands: Sequence[PluginCommand], qualified: str,
                        payload: str) -> None:
    """Run one plugin command on the REPL's loop and print what it says."""
    match = next(c for c in commands if c.qualified == qualified)
    finished, output = interruptible._run_interruptible(loop, run_plugin_command(
        ctx.agent, match, payload,
        session_id=ctx.session_id, user_id=ctx.session_user), f"/{match.name}")
    if finished:
        print(output)


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
            from ...tools.integration import shutdown_tools
            loop.run_until_complete(shutdown_tools())
        except Exception:
            logger.debug("MCP shutdown on the chat loop failed", exc_info=True)
        shut_down_loop(loop)
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
    runtime: Any = None,
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
    ctx = context._ChatContext(
        agent=agent, entry_name=entry_name, session_service=session_service,
        session_user=session_user, session_id=session_id,
        was_new_session=was_new_session, llm_profile=llm_profile,
        llm_override=llm_override, llm_profile_info=llm_profile_info,
        show_status=show_status, session_manager=session_manager,
        template_vars=template_vars, llm_params=llm_params,
        session_title=session_title, attachments=attachments,
        runtime=runtime,
    )
    ansi = supports_color()
    renderer = display.ChatRenderer(ansi=ansi)
    unicode_ok = renderer.sym is display._UNICODE_SYMBOLS
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
            prompt_input._skip_piped_bom()
        editor = prompt_input._build_prompt_editor(
            context._history_seed(ctx),
            suggest=lambda line: _completions_for(ctx, agent_setup._available_skills(ctx), line),
            # Waiting for a line has to RUN this loop: a sub-agent started
            # with blocking=false lives on it, and between two turns nobody
            # else turns it (see _PromptEditor._ask).
            loop=loop,
        ) if interactive else None
        read_line = editor.read if editor else None
        read_cont = editor.read_continuation if editor else None

        # Known-good input mode, restored before every prompt: child shells
        # (terminal.execute -> MSYS bash) switch the console's INPUT mode too,
        # which kills Enter/Backspace and turns Ctrl-C into a plain character.
        input_mode = snapshot_console_input_mode()

        # Console logging would write into the live region behind its back.
        silenced = display._silence_stdout_logging()

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
        # What every command handler gets beside the rest of its line.
        repl = _Repl(loop=loop, ctx=ctx, renderer=renderer, editor=editor,
                     prompt=prompt, read_cont=read_cont, ansi=ansi,
                     interactive=interactive, plugin_commands=plugin_commands)

        pending: list[str] = [initial_task.strip()] if initial_task and initial_task.strip() else []
        interrupts = 0  # consecutive Ctrl-C at the prompt; two in a row exit
        while True:
            # Set only by the wake path below: what the REPL starts for a
            # background job is not a message the person is sending.
            woken = False
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
                    # The watcher may cut this read short with
                    # _WokenAtThePrompt -- see _watch_for_wake.
                    with context._watch_for_wake(ctx, editor):
                        task = prompt_input._read_input(prompt, cont_prompt=cont_prompt,
                                                        echo=not interactive,
                                                        read_line=read_line,
                                                        read_cont=read_cont)
                    # Kept for the fallback readers: input() leaves the
                    # thread alone, but a _PromptEditor without a loop runs
                    # its own asyncio.run() per prompt, and that leaves the
                    # thread with NO current event loop -- measured:
                    # asyncio.get_event_loop() then raises. The REPL names
                    # its loop everywhere, a library called during the turn
                    # need not, and it used to find one.
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
                except context._WokenAtThePrompt:
                    asyncio.set_event_loop(loop)
                    # Taken here, not left to the turn's first LLM call: a
                    # turn that never reaches one would leave the mark set and
                    # the watcher would start the next turn a tick later.
                    context._take_wake_mark(ctx)
                    task = WAKE_TASK
                    woken = True
                    print(renderer._colored(
                        "(woken: input is waiting for this session)", "90"))
                    print(f"{prompt}{task}")
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
                skill_names = agent_setup._available_skills(ctx)
                repl.skill_names = skill_names
                resolution = resolve_chat_input(task, skill_names, repl.plugin_commands)
                command = resolution.name if resolution.kind == "command" else (
                    "unknown" if resolution.kind == "unknown" else None)
                payload = resolution.payload
                if editor and resolution.kind != "message":
                    # Not a message, so no session will hold it: the history
                    # keeps it for as long as this process runs.
                    editor.remember_command(task)

                if resolution.kind == "plugin":
                    # The plugin does the work and prints; no LLM turn, no tokens.
                    _run_plugin_command(loop, ctx, repl.plugin_commands,
                                        resolution.name, payload)
                    continue

                if resolution.kind == "skill":
                    expanded = agent_setup._expand_skill(ctx, resolution.name, payload)
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
                # Every other command through its handler (_COMMANDS): done and
                # back to the prompt, or on into a turn with the task it hands
                # back. A command without a handler falls through as typed.
                handler = _COMMANDS.get(command) if command else None
                if handler is not None:
                    follow_up = handler(repl, payload)
                    if follow_up is None:
                        continue
                    task = follow_up

                # isinstance: a /retry hands over the parts the dropped message
                # already carried, and merging those into a second multimodal
                # message would send the text twice and the file not at all.
                # `woken`: files queued with /attach belong to the message
                # the person is writing, not to a turn a finished background
                # job started -- sending them here would spend the queue on
                # "You were woken because input is waiting" and empty it.
                if ctx.attachments and isinstance(task, str) and not woken:
                    task = sessions._task_with_attachments(ctx, task, renderer)
                    if task is None:
                        continue

                # Converted here and not where the wake is taken: as a string it
                # goes through the echo and the command lookup above. The role
                # is what says the run is speaking, not the person at the
                # prompt (agent_runner.wake_message).
                if woken and task == WAKE_TASK:
                    task = wake_message()

                started = time.monotonic()
                result = turn._execute_turn(loop, ctx, task, renderer, editor)

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
                token_usage._merge_totals(ctx.total_usage, usage)

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
                    # "The next turn saves anyway" held until somebody stopped
                    # a turn and then LEFT: there was no next turn, the chat
                    # had never recorded a save, and the farewell -- which
                    # names the session only when one happened -- said nothing
                    # at all. Reported from real use: "then I don't even know
                    # what the session id is". The turn is over here, nothing
                    # of it is still running, so this is an ordinary save --
                    # not for a turn refused before it ran: nothing of it to save.
                    if not result.get("refused"):
                        context._save_now(loop, ctx)
                    continue

                pending.extend(queued)

                summary = result.get("summary")
                if summary:
                    turn._render_answer(renderer, summary)
                elif not result.get("errors"):
                    print(renderer._colored("(no answer returned)", "90"))

                if ctx.show_status:
                    context_fill = result.get("context_tokens")
                    print(renderer._colored(
                        token_usage._format_usage(usage, time.monotonic() - started,
                                                  renderer.sym,
                                                  context=(context_fill,
                                                           result.get("context_window"))
                                                  if context_fill else None), "90"))

                if not result.get("refused"):  # refused before it ran: nothing of it to save
                    context._save_now(loop, ctx)
            except KeyboardInterrupt:
                renderer.close()
                print("\n(interrupted)", file=sys.stderr)
                # Stop means stop here too: nothing queued runs after it.
                for line in pending:
                    print(renderer._colored(f"(dropped: {line})", "90"))
                pending.clear()
    finally:
        context._release_session(ctx, ctx.session_id)
        display._restore_logging(silenced)
        if ctx.total_usage:
            print(renderer._colored(
                "Session total: " + token_usage._format_usage(
                    ctx.total_usage, time.monotonic() - chat_started,
                    renderer.sym),
                "90"))
        if ctx.last_saved:
            print(f"Session saved: {ctx.last_saved}", file=sys.stderr)
            print(f"Resume with: {context._resume_hint(ctx, ctx.last_saved, ctx.last_saved_agent)}",
                  file=sys.stderr)
        else:
            # The id was last seen in the header, scrolled away hours ago.
            # No claim about saving either way: the agent writes the session
            # itself when it finalises a request, so "nothing saved" would be
            # a lie in the very case this line exists for -- and withholding
            # the way back is what the person actually complained about.
            print(f"Session: {ctx.session_id}", file=sys.stderr)
            print(f"Resume with: {context._resume_hint(ctx, ctx.session_id, ctx.entry_name)}",
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
