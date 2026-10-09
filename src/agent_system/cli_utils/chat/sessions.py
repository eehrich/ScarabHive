"""The chat's session commands: /new, /session, /sessions, /resume, /title, /vars, /attach.

/sessions, a bare /resume and the completion behind /resume <Tab> read one
listing of this user's sessions (_load_recent_sessions). /resume takes a
session over -- held before it is loaded, put back on its own LLM, or
refused when another agent ran it (_resume_session). /title names the open
session, /vars changes its template variables, /attach queues files that
_task_with_attachments sends with the next message.

One module per topic of commands: repl.py's command table points each
command at its handler here (_on_<command>); what they do to the session as
a whole -- holding, saving, starting a fresh one -- is context.py's.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from agent_system.chat_actions import one_line as _one_line
from agent_system.chat_commands import apply_vars, parse_vars, store_vars
from ..attachments import sort_attachments
from ..session_defaults import load_session_llm_params, session_defaults
from ..session_listing import (DEFAULT_LIMIT, in_chat_selector, newest_of, parse_listing,
                               print_sessions)
from . import agent_setup, context, interruptible

if TYPE_CHECKING:
    from .context import _ChatContext
    from .display import ChatRenderer
    from .repl import _Repl

logger = logging.getLogger(__name__)


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


async def _named_session(ctx: _ChatContext, typed: str) -> str:
    """The session id *typed* names -- a person may type the title they gave
    the session with /title, since ids are machine-made and cannot be renamed.
    """
    if ctx.session_manager is None:
        return typed
    others: list = []
    named = await ctx.session_manager.resolve_session_ref(
        ctx.session_user, typed, others=others)
    if named and named != typed:
        print(f"Session '{typed}': {named}{newest_of(others, '/sessions all')}")
        return named
    return typed


async def _resume_into(ctx: _ChatContext, session_id: str, previous: str) -> bool:
    """Take *session_id* over, and let go of whichever session is left behind.

    The hold comes BEFORE the load -- a session another process is running
    must not be pulled out from under it -- and the release is in a
    ``finally``: a Ctrl-C lands inside the load, and a hold taken there and
    never given back locks the session for the rest of the process.

    A title is turned into its id before anything is held: holding the words
    typed left the session itself unlocked, skipped the busy check, and kept
    a lock file named after the title until the process ended.
    """
    session_id = await _named_session(ctx, session_id)
    if not context._hold_session(ctx, session_id):
        return False  # another process runs it: the chat stays where it is
    switched = False
    try:
        switched = await _resume_session(ctx, session_id)
    finally:
        context._release_session(ctx, previous if switched else session_id)
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


async def _set_session_title(ctx: _ChatContext, title: str) -> bool:
    """Give the open session a title, the one `/sessions` shows -- bare, say it."""
    if not title:
        # The title waiting for the first save, else the one on disk:
        # ctx.session_title is dropped once a save has written it.
        current = ctx.session_title
        if current is None and not ctx.was_new_session and ctx.session_manager is not None:
            try:
                record = await ctx.session_manager.load_session(ctx.session_user, ctx.session_id)
                current = (record or {}).get("title")
            except Exception:  # noqa: BLE001 -- a missing line, not the end of the chat
                logger.debug("No title for %s", ctx.session_id, exc_info=True)
        print(f"Title: {' '.join(current.split())}" if current
              else "This session has no title yet.")
        print("Usage: /title <text>   (/resume and --session take it)")
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
    """Show this user's own sessions -- `/sessions [count|all]`, 0 for no limit."""
    limit, everything, complaint = parse_listing(payload, DEFAULT_LIMIT)
    if complaint:
        # Same voice as /history next door: a discarded argument that still
        # prints a plausible listing is indistinguishable from a honoured one.
        print(f"Usage: /sessions [count|all]   (got: {complaint})")
        return
    # What it printed is what the completion and a bare /resume read -- taken
    # from the listing it already did, not from a second walk of the index.
    listed = await print_sessions(
        ctx.session_manager, ctx.session_user,
        limit=limit,
        current_session_id=ctx.session_id,
        more_hint="/sessions <count>, /sessions 0 for no limit",
        footer="Use /resume <id or title> to continue one.",
        # Which agents are meant for chat -- and this chat's own, whatever it is.
        shown=None if everything else in_chat_selector(ctx.runtime, keep=(ctx.entry_name,)),
        everything_hint="/sessions all",
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
        print(f"Continue it with: {context._resume_hint(ctx, session_id, stored_agent)}")
        return False
    # Built before ANYTHING happens: nothing is loaded and nothing switches
    # until the session's own LLM stands. Running it on this chat's profile
    # instead would look harmless and then write that profile over the
    # session's record on the next save -- a failed switch destroying the
    # very choice it was trying to honour.
    stored_params = await load_session_llm_params(ctx.session_manager, ctx.session_user, session_id)
    if stored_params is None:
        # none stored: --llm-params typed for this chat stay, as they do when it starts on a session
        stored_params = dict(ctx.llm_params)
    own = getattr(getattr(ctx.agent, "agent_config", None), "default_llm_profile", None)
    # Nothing stored: nobody picked one for that session (or llm.yaml retired it), so it runs on the
    # agent's own, as --session <id> starts it. Staying on this chat's pick wrote it into that record
    # as the session's own on the next save.
    target = stored_llm or own or ctx.llm_profile
    back_to_own = target == own and not stored_params
    built = None
    if not back_to_own and (target != ctx.llm_profile or stored_params != ctx.llm_params):
        try:
            built = agent_setup._build_profile(ctx, target, stored_params)
        except Exception as e:
            logger.error("Could not switch to the session's profile %s: %s",
                         target, e, exc_info=True)
            print(f"Session '{session_id}' runs on LLM '{target}', which "
                  f"cannot be started here: {e}")
            # stored params go with any --llm (agent_cli), so a profile alone does not get past them
            print(f"Continue it with: {context._resume_hint(ctx, session_id)} --llm <profile>"
                  + (" --llm-params thinking_level=<level>" if stored_params else ""))
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
    # from a /title it never got to write. Named, then dropped.
    context._report_what_stays_behind(ctx, ctx.session_id)
    ctx.session_id = session_id
    ctx.was_new_session = False
    ctx.session_title = None
    left_a_pick = back_to_own and ctx.llm_override is not None
    if back_to_own:
        ctx.llm_override = None
        ctx.llm_profile = own
        ctx.llm_profile_info = None
        ctx.llm_params = {}
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is not None:
        # The turn loop reads metadata for tool context; without this the
        # resumed session would still carry the previous one's values.
        tracker.set_session_metadata(session_id, context._session_metadata(ctx))
    print(f"({count} messages restored)")
    if left_a_pick:
        print(f"LLM: {own}   (the agent's own -- the session has no pick)")
    if built is not None:
        ctx.llm_params = stored_params
        agent_setup._use_profile(ctx, target, built)
        print(f"LLM: {ctx.llm_profile_info}   (the session's own)")
    return True


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
    from ...utils.multimodal_processor import detect_file_type

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
    from ...utils.multimodal_processor import AttachmentRejected, message_with_attachments

    # /attach already refused what cannot be sent, so a problem here means the
    # file changed under us since it was queued -- say which one, keep the rest.
    kinds, problems = sort_attachments(ctx.attachments)
    for problem in problems:
        print(f"Not sent: {problem}")
    if problems:
        return None

    # The per-request override wins over the agent's default -- one rule for
    # the HTTP API, the chat and both command-line entry points.
    try:
        message = message_with_attachments(task, kinds, ctx.llm_override, ctx.agent)
    except AttachmentRejected as e:
        print(f"Not sent: {e}")
        print(renderer._colored(f"(kept text: {task})", "90"))
        return None
    except Exception as e:
        print(f"Attachment failed, nothing sent: {e}")
        return None
    print(renderer._colored(
        f"(sending with {len(ctx.attachments)} attachment(s))", "90"))
    ctx.attachments.clear()
    return message


def _on_new(repl: _Repl, payload: str) -> None:
    print(f"New session: {context._open_fresh_session(repl.ctx, repl.editor)}")


def _on_session(repl: _Repl, payload: str) -> None:
    ctx = repl.ctx
    print(f"Session: {ctx.session_id}  (user: {ctx.session_user})")
    print(f"Resume with: {context._resume_hint(ctx, ctx.session_id)}")


def _on_sessions(repl: _Repl, payload: str) -> None:
    interruptible._run_interruptible(repl.loop, _list_sessions(repl.ctx, payload), "/sessions")


def _on_resume(repl: _Repl, payload: str) -> None:
    ctx = repl.ctx
    previous = ctx.session_id
    # Bare: the one this user last left. The id is only known
    # after the listing, so the hold lives inside either way.
    finished, resumed = interruptible._run_interruptible(
        repl.loop,
        _resume_into(ctx, payload, previous) if payload
        else _resume_last_session(ctx, previous),
        "/resume")
    if finished and resumed:
        if repl.editor:
            repl.editor.reseed(context._history_seed(ctx))
        print(f"Resumed session: {ctx.session_id}")


def _on_title(repl: _Repl, payload: str) -> None:
    interruptible._run_interruptible(repl.loop, _set_session_title(repl.ctx, payload),
                                     "/title")


def _on_vars(repl: _Repl, payload: str) -> None:
    interruptible._run_interruptible(repl.loop, _handle_vars(repl.ctx, repl.renderer, payload),
                                     "/vars")


def _on_attach(repl: _Repl, payload: str) -> None:
    _handle_attach(repl.ctx, payload)
