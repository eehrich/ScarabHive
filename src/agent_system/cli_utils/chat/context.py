"""The chat's session side: the context a REPL carries, and the session it holds.

_ChatContext bundles what one REPL knows about the conversation it runs: the
agent, the session, the LLM it answers on, what is queued for the next
message. Around it lives what acts on that session as a whole rather than
for one command: its messages and the prompt history they seed, the
metadata the chat records for it, moving onto a fresh one, saving it, and
session presence (core/session_presence/) -- holding the open session,
letting go of one left behind, claiming a turn, and the wake watch at the
prompt.

Its own module because the commands, the turn and the loop all work on this
one session, and none of them should have to know how it is held or written.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Optional, Sequence

from agent_system.chat_actions import message_text as _message_text
from agent_system.chat_commands import PluginCommand, needs_escape as _needs_escape
from ...core.session_presence import SessionBusy, presence_for
from . import interruptible

if TYPE_CHECKING:
    from .prompt_input import _PromptEditor

logger = logging.getLogger(__name__)


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
                 attachments: Sequence[str] = (),
                 runtime: Any = None) -> None:
        self.agent = agent
        # The Runtime this process bootstrapped: which agents are meant for
        # chat (/sessions). Held here, not read from Runtime.last_started --
        # an in-process pipeline that bootstraps its own would swap that.
        self.runtime = runtime
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


def _session_messages(ctx: "_ChatContext") -> list:
    tracker = getattr(ctx.agent, "_session_tracker", None)
    if tracker is None:
        return []
    try:
        return list(tracker.get_session_messages(ctx.session_id) or [])
    except Exception:
        logger.debug("Could not read session messages", exc_info=True)
        return []


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


def _llm_choice(ctx: "_ChatContext") -> dict:
    """What this chat chose for its model (SessionService.save_session): a profile other than the
    agent's own, and the params. An override on the agent's own primary carries params only."""
    own = getattr(getattr(ctx.agent, "agent_config", None), "default_llm_profile", None)
    picked = ctx.llm_profile if ctx.llm_override is not None and ctx.llm_profile != own else None
    return {"profile": picked, "params": dict(ctx.llm_params)}


def _session_metadata(ctx: "_ChatContext") -> dict:
    """What the tracker holds for this chat's session: the turn loop reads it for tool context, and the
    agent's own saves write llm_profile and the choice from it -- the record a later resume reads."""
    return {"user_id": ctx.session_user, "agent_name": ctx.entry_name,
            "llm_profile": ctx.llm_profile, "llm_choice": _llm_choice(ctx)}


def _report_what_stays_behind(ctx: "_ChatContext", previous: str) -> None:
    """Say what the session being left takes with it, and what waits here.

    Both ways out of a session pass here (`/new`, `/agent` and `/resume`): a
    title typed with `/title` before the first message has no record to go
    into and dies with the session -- and losing it without a word looks like
    a bug. Queued attachments do NOT die; they are simply easy to forget
    once the chat says "New session".

    What it does NOT say is whether that title reached the disk. A /title of
    a session that HAS a record writes it and keeps ctx.session_title only so
    a later save cannot put the old name back -- "nothing written yet" was a
    plain lie about that session.
    """
    if ctx.session_title:
        # Nothing about the session gone to: /resume goes to one with a title of its own.
        print(f"(the title '{ctx.session_title}' stays with {previous})")
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
    from ...utils.id import short_id

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
        tracker.set_session_metadata(new_id, _session_metadata(ctx))
    return new_id


def _hold_session(ctx: "_ChatContext", session_id: str) -> bool:
    """Session presence (core/session_presence/): chat holds the session it
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


def _claim_turn(ctx: "_ChatContext", request_id: str) -> Optional[Any]:
    """The turn is the session's last run from its start (core/session_presence/),
    not only once its run takes the session: a stop noted in between (_cancel_turn)
    would otherwise be the previous turn's business. Nested in the chat's own hold;
    the run's hold under the same id nests in this one."""
    presence = presence_for(getattr(ctx.agent, "system_config", None))
    if presence is None:
        return None
    try:
        return presence if presence.hold(ctx.session_id, ctx.session_user, ctx.entry_name,
                                         run=request_id) else None
    except SessionBusy:
        return None   # another process has it; the chat's own hold said so already


class _WokenAtThePrompt(Exception):
    """A wake-up arrived while the REPL sat at the prompt, doing nothing."""


#: How often the prompt asks whether input is waiting. A stat() per tick.
_WAKE_POLL_S = 0.5


def _take_wake_mark(ctx: "_ChatContext") -> None:
    """Clear the wake mark BEFORE the turn the REPL starts for it.

    ``_presence_step`` clears it on the turn's first LLM call anyway
    (servers/agent/mixins/run.py), so this is not what makes the mark go away --
    it is what keeps a turn that never GETS to an LLM call (a config error, a
    refused hold) from leaving the mark set: the watcher would see it again a
    tick later and start another billed turn, and another. And it stamps the
    wake: a ringer that still rings for news this turn is told about stops
    (session_presence.take_for_wake) instead of starting a turn per ring.
    """
    presence = presence_for(getattr(ctx.agent, "system_config", None))
    if presence is None:
        return
    try:
        presence.take_for_wake(ctx.session_id, ctx.session_user)
    except OSError:
        logger.debug("Could not take the wake mark", exc_info=True)


@contextmanager
def _watch_for_wake(ctx: "_ChatContext",
                    editor: Optional["_PromptEditor"]) -> Iterator[None]:
    """Cut into the waiting prompt when input arrives for this session.

    A held session is told about waiting input by a mark on disk, and the mark
    is picked up on a step of the session -- ``_presence_step`` does it on
    every LLM call. The chat holds its session across the WHOLE repl, so
    between turns no call runs: a sub-agent finished with ``wake_when_done``
    would sit there unmentioned until the person happened to type something.
    So the prompt itself asks, and starts a turn the way a typed line would.

    Only with the line editor: the fallback reader is ``input()``, which no
    thread can interrupt -- and that path is the redirected/piped one, where
    nobody is sitting in front of the prompt to be woken anyway.

    The cut is refused while anything is typed (``_PromptEditor.typed_text``,
    which asks the DEFAULT buffer and not the focused one -- see there):
    ``exit()`` discards the buffer, and losing a half-written message to a
    background job is a worse trade than a wake-up that waits for the next
    tick -- by which time their own line has started a turn that takes the
    mark itself.
    """
    presence = presence_for(getattr(ctx.agent, "system_config", None))
    if presence is None or editor is None:
        yield
        return

    stop = threading.Event()

    def _watch() -> None:
        while not stop.wait(_WAKE_POLL_S):
            try:
                if not presence.pending(ctx.session_id, ctx.session_user):
                    continue
                app = editor.app()
                loop = getattr(app, "loop", None)
                if loop is None or not getattr(app, "is_running", False):
                    continue
                loop.call_soon_threadsafe(_cut_in, app)
            except Exception:
                # A watcher is never a reason for the chat to end: whatever
                # goes wrong here costs a wake-up, not the session.
                logger.debug("Wake watcher tick failed", exc_info=True)

    def _cut_in(app: Any) -> None:
        # On the prompt's own loop now, so the buffer and the future cannot
        # change underneath: they typed while the tick was in flight, or the
        # prompt has already returned by itself.
        try:
            if editor.typed_text().strip():
                return
            if app.future is None or app.future.done():
                return
            app.exit(exception=_WokenAtThePrompt)
        except Exception:
            logger.debug("Could not cut into the prompt", exc_info=True)

    watcher = threading.Thread(target=_watch, name="chat-wake-watch", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join(timeout=_WAKE_POLL_S * 2)


def _resume_hint(ctx: "_ChatContext", session_id: str,
                 agent_name: Optional[str] = None) -> str:
    """The exact command that brings this session back."""
    parts = ["agent-cli chat", f"--session {session_id}",
             f"--agent {agent_name or ctx.entry_name}"]
    if ctx.session_user != "cli_user":
        parts.append(f"--session-user {ctx.session_user}")
    return " ".join(parts)


def _finish_save(loop: asyncio.AbstractEventLoop, task: "asyncio.Task") -> Any:
    """Wait out a save a Ctrl-C interrupted; a second one abandons it."""
    print("\n(finishing the save -- Ctrl-C again to abandon it)", file=sys.stderr)
    try:
        return loop.run_until_complete(task)
    except KeyboardInterrupt:
        if interruptible._through_before_the_interrupt(task):
            return task.result()
        interruptible._drain(loop, task, "the save")
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
            if not interruptible._through_before_the_interrupt(task):
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
            llm_choice=_llm_choice(ctx),
        ))
    except Exception as e:
        logger.error("Failed to save chat session: %s", e, exc_info=True)
        print(f"Warning: failed to save session: {e}", file=sys.stderr)
        return False
