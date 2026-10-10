"""What the chat talks to: the agent, its LLM profile and thinking level, its tools and skills.

/model and /think switch the client the next turn runs on (_build_profile and
_use_profile, which /resume uses as well to put a session back on its own
LLM), /agent switches the agent and with it the session, /tools and /skills
say what that agent really has, and /context what its prompt, its tools and
the conversation make of the window. The skills are also what a /word the
REPL does not know may run (_available_skills, _expand_skill).

One module per topic of commands: repl.py's command table points each
command at its handler here (_on_<command>).
"""
from __future__ import annotations

import difflib
import logging
from typing import TYPE_CHECKING, Any, Optional

from agent_system.chat_actions import (
    context_breakdown,
    live_context_window,
    measured_context,
    one_line as _one_line,
)
from agent_system.chat_commands import group_tools_by_server, runnable_skill_names
from agent_system.plugin_commands import collect_plugin_commands
from . import context, interruptible

if TYPE_CHECKING:
    from .context import _ChatContext
    from .display import ChatRenderer
    from .repl import _Repl

logger = logging.getLogger(__name__)


def _build_profile(ctx: "_ChatContext", wanted: str,
                   params: Optional[dict] = None) -> tuple[Any, str]:
    """(client, label) for LLM profile *wanted*; raises if it cannot be built.

    The switch ``--llm`` performs, with the ``--llm-params`` of this chat
    applied to the new profile the way the command line applies them: a
    ``thinking_level=max`` typed at the start must not vanish on /model.
    Changes nothing, so an interrupt while it builds leaves the chat as it was.
    *params* instead of the chat's own: what a resumed session ran with.
    """
    from ...llm.factory import override_for_profile

    return override_for_profile(getattr(ctx.agent, "system_config", None),
                                getattr(ctx.agent, "agent_config", None),
                                wanted, (ctx.llm_params if params is None else params) or None)


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
        tracker.set_session_metadata(ctx.session_id, context._session_metadata(ctx))


def _set_thinking(ctx: "_ChatContext", payload: str) -> bool:
    """Show or change the thinking level of this chat (/think); True if it changed.

    It goes into the chat's llm_params, which every profile /model switches to
    takes along, as with --llm-params. On the agent's own profile and with no
    params left, the chat runs on the agent's own client again.
    """
    from ...llm.factory import THINKING_LEVELS

    wanted = payload.strip().lower()
    current = ctx.llm_params.get("thinking_level")
    if not wanted:
        print(f"Thinking: {current or 'default'}")
        print(f"  /think {'|'.join(THINKING_LEVELS)} sets it, /think default takes the model's own.")
        return False
    if wanted != "default" and wanted not in THINKING_LEVELS:
        print(f"Unknown thinking level: {wanted}   (one of {', '.join(THINKING_LEVELS)}, or default)")
        return False
    params = dict(ctx.llm_params)
    if wanted == "default":
        params.pop("thinking_level", None)
    else:
        params["thinking_level"] = wanted
    if params == ctx.llm_params:
        print(f"Thinking: already {current or 'default'}.")
        return False
    own = getattr(getattr(ctx.agent, "agent_config", None), "default_llm_profile", None)
    built = None
    if params or ctx.llm_profile != own:
        try:
            built = _build_profile(ctx, ctx.llm_profile, params)
        except Exception as e:
            # The old client is still good; a failed switch must not end the chat.
            logger.error("Could not set thinking level %s: %s", wanted, e, exc_info=True)
            print(f"Could not set thinking to '{wanted}': {e}")
            return False
    ctx.llm_params = params
    if built is not None:
        _use_profile(ctx, ctx.llm_profile, built)
    else:
        ctx.llm_override = None
        ctx.llm_profile_info = None
        tracker = getattr(ctx.agent, "_session_tracker", None)
        if tracker is not None:
            tracker.set_session_metadata(ctx.session_id, context._session_metadata(ctx))
    print(f"Thinking: {params.get('thinking_level') or 'default'}   (from the next message on)")
    return True


def _llm_profiles(ctx: "_ChatContext") -> dict:
    """The configured LLM profiles -- one reader for the switch and its Tab."""
    llm_system = getattr(getattr(ctx.agent, "system_config", None), "llm_system", None)
    return dict(getattr(llm_system, "profiles", None) or {})


def _switch_model(ctx: "_ChatContext", payload: str) -> bool:
    """Show or change the LLM profile this chat runs on; True if it changed.

    The next turn reads ctx.llm_override, and the choice goes into the
    session metadata so continuing the session later starts on it again
    (commands/run.py: stored_session_settings reads it back) -- the caller writes
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
    """Agents this configuration defines -- the factory's own gate, not a copy.

    Reading it a second time here is how a listing and its factory drift
    apart: /agent would offer a name that the factory then rejects.
    """
    from ...servers.agent.entry import agent_entry_names

    return agent_entry_names(getattr(ctx.agent, "system_config", None))


def _agent_for(ctx: "_ChatContext", name: str) -> Any:
    """The agent object for *name*, through the one factory
    (servers/agent/entry.py). Raises NotAnAgent.

    Not a second copy of it: that one applies the MERGED server config, and
    the copy this chat would grow instead is how an agent ends up with a
    quietly downgraded max_steps.
    """
    from ...servers.agent.entry import entry_agent

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
              f"{wanted} answers on its own profile; /model and /think switch it)")
    ctx.llm_override = None
    ctx.llm_params = {}
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
    """The registry, scanned with the roots the agent's CONFIG resolves to."""
    from agent_system.skills.registry import configured_skill_registry

    return configured_skill_registry(getattr(ctx.agent, "system_config", None))


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
    messages = context._session_messages(ctx)
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


def _on_agent(repl: _Repl, payload: str) -> None:
    ctx = repl.ctx
    if not _switch_agent(ctx, payload):
        return
    # The agent's commands change with it, and the session
    # does too: one belongs to the agent that ran it.
    repl.plugin_commands = ctx.plugin_commands
    print(f"Agent: {ctx.entry_name}   LLM: {ctx.llm_label()}")
    print(f"New session: {context._open_fresh_session(ctx, repl.editor)}")


def _on_think(repl: _Repl, payload: str) -> None:
    if _set_thinking(repl.ctx, payload) and not repl.ctx.was_new_session:
        context._save_now(repl.loop, repl.ctx)


def _on_model(repl: _Repl, payload: str) -> None:
    if _switch_model(repl.ctx, payload) and not repl.ctx.was_new_session:
        # The record is what `--session <id>` starts on; waiting
        # for the next turn's save lost the switch on /exit.
        context._save_now(repl.loop, repl.ctx)


def _on_tools(repl: _Repl, payload: str) -> None:
    interruptible._run_interruptible(repl.loop, _show_tools(repl.ctx, repl.renderer, payload),
                                     "/tools")


def _on_skills(repl: _Repl, payload: str) -> None:
    _show_skills(repl.ctx, repl.renderer)


def _on_context(repl: _Repl, payload: str) -> None:
    interruptible._run_interruptible(repl.loop, _show_context(repl.ctx, repl.renderer),
                                     "/context")
