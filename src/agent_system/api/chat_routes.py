"""The web chat's commands: /chat/commands, /chat/resolve, /chat/vars, /chat/command,
/chat/undo, /chat/checkpoints, /chat/rewind, /chat/context, /chat/transcript and
/chat/last_answer.

The web UI resolves a typed line through the SAME catalogue and parser as
the terminal chat (agent_system.chat_commands). The browser cannot read
the skill folders, so expansion happens here -- one implementation, and
`/writer x` means the same thing on both surfaces.

A command that writes the session (/undo's cut, a rewind) holds it the way an
append does (session_writes); what it reads comes from the record, which is
what the browser shows.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from agent_system import app_state
from agent_system.api.app_context import AppContext, app_context, chat_agent, parse_json_body
from agent_system.api.session_writes import (
    drop_last_exchange_and_persist,
    file_rewinder_or_503,
    held_for_a_write,
    rewind_refusal,
    session_agent_name,
    session_held,
    session_record,
    settling_agent,
)
from agent_system.core.session_presence import presence_for

router = APIRouter()

#: Longest line /chat/resolve will look at. A chat line is a chat line; the
#: cap keeps a multi-megabyte paste from turning into CPU work on the event
#: loop before anyone has decided it is even a command.
MAX_CHAT_LINE = 100_000


async def _json_object(request: Request) -> dict:
    """The request's JSON body as an object; no body at all reads as {}."""
    body = await parse_json_body(request)
    if body is not None and not isinstance(body, dict):
        # A JSON array or bare string parses fine but has no .get -- answer
        # "bad request" rather than letting an AttributeError become a 500.
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return body or {}


def _skill_registry_for_app(ctx: AppContext):
    """Registry scanned with the roots the CONFIG resolves to."""
    from agent_system.skills.registry import configured_skill_registry

    return configured_skill_registry(ctx.live_config())


async def _chat_agent_the_caller_may_run(ctx: AppContext, request: Request, agent_name: Optional[str]):
    """chat_agent, or None for an agent behind a role gate the caller does not pass.

    Its plugin commands are derived from its tool allowlist -- what a gated
    agent reaches, which the gate keeps from the caller as /agents/{name}/tools
    does. The caller is resolved only when a gate is in play.
    """
    target = chat_agent(request, agent_name)
    if target is None or getattr(target, "min_role", None) is None or not ctx.config.auth.enabled:
        return target
    from agent_system.auth.agent_access import may_run_agent
    caller = await ctx.enforce_endpoint_security(request)
    return target if may_run_agent(target.min_role, caller, ctx.config.auth) else None


def _plugin_commands_for(agent) -> list:
    """What *agent* may run, empty for anything that cannot be asked."""
    if agent is None:
        return []
    from agent_system.plugin_commands import collect_plugin_commands
    try:
        return collect_plugin_commands(agent)
    except Exception as e:  # noqa: BLE001 - a broken plugin must not kill the chat
        logging.getLogger(__name__).warning("Could not collect plugin commands: %s", e)
        return []


@router.get("/chat/commands")
async def chat_commands(request: Request, surface: str = "web",
                        agent: Optional[str] = None, ctx: AppContext = Depends(app_context)):
    """Commands and skills this surface offers, for help and autocomplete.

    ``agent`` is not decoration: plugin commands differ per agent, so the
    browser has to say which one it is talking to -- and ask again when
    the selector changes. A caller that names none gets the entry agent's
    list, the same agent /run would have used.
    """
    from agent_system.chat_commands import commands_for, runnable_skill_names
    from agent_system.plugin_commands import spellings

    try:
        # Only what this parser can actually reach: the same filter the
        # terminal uses. A skill named "3d-print" was offered here and
        # then went to the model as a message; one named "tools" was
        # offered and ran the built-in.
        listed = {s.name: s for s in _skill_registry_for_app(ctx).list_skills()}
        skills = [
            {"name": s.name, "summary": s.description, "version": s.version,
             "kind": "skill", "display": f"/{s.name}"}
            for s in (listed[name] for name in runnable_skill_names(listed))
        ]
    except Exception as e:
        logging.getLogger(__name__).warning("Could not list skills: %s", e)
        skills = []

    plugin_commands = _plugin_commands_for(await _chat_agent_the_caller_may_run(ctx, request, agent))
    return {
        "commands": [
            {"name": c.name, "aliases": list(c.aliases), "summary": c.summary,
             "display": c.display, "kind": "command"}
            for c in commands_for(surface)
        ],
        "skills": skills,
        # The SPELLING, not just the name: a plugin command whose name a
        # built-in already owns is only reachable as "plugin:name", and
        # offering the bare one would land on the built-in instead.
        "plugin_commands": [
            {"name": c.name, "qualified": c.qualified, "spelling": spelling,
             "summary": c.summary, "argument_hint": c.argument_hint,
             "kind": "plugin",
             "display": f"/{spelling}" + (f" {c.argument_hint}" if c.argument_hint else "")}
            for spelling, c in zip(spellings(plugin_commands), plugin_commands)
        ],
    }


@router.post("/chat/resolve")
async def chat_resolve(request: Request, ctx: AppContext = Depends(app_context)):
    """Classify a typed line, expanding a skill invocation into its text.

    Returns ``kind`` (command | skill | message | unknown) plus what the
    caller needs: the command name, or the message to send to the agent.
    """
    from agent_system.chat_commands import (
        resolve as resolve_line,
        runnable_skill_names,
        suggest_command,
    )
    from agent_system.plugin_commands import spellings
    from agent_system.skills import invoke

    body = await _json_object(request)
    line = body.get("line") or ""
    if not isinstance(line, str):
        raise HTTPException(status_code=400, detail="'line' must be a string")
    if len(line) > MAX_CHAT_LINE:
        raise HTTPException(
            status_code=413,
            detail=f"Line too long ({len(line)} chars, limit {MAX_CHAT_LINE})",
        )

    try:
        registry = _skill_registry_for_app(ctx)
        # The same names the listing offers -- a typo hint pointing at a
        # skill nobody can invoke is worse than none.
        skill_names = runnable_skill_names(s.name for s in registry.list_skills())
    except Exception as e:
        logging.getLogger(__name__).warning("Could not list skills: %s", e)
        registry, skill_names = None, []

    # The agent decides which plugin commands exist at all, so an
    # unnamed one leaves "/compact" the unknown command it was before.
    plugin_commands = _plugin_commands_for(
        await _chat_agent_the_caller_may_run(ctx, request, body.get("agent_name")))

    result = resolve_line(line, skill_names, plugin_commands)
    payload = {"kind": result.kind, "name": result.name, "payload": result.payload}

    if result.kind == "skill" and registry is not None:
        skill = registry.get(result.name)
        if skill is None:
            # Vanished between listing and reading -- say so instead of
            # sending the raw "/name" to the agent as if it were a message.
            return {"kind": "unknown", "name": None, "payload": f"/{result.name}",
                    "error": f"Skill '{result.name}' is no longer available"}
        try:
            payload["text"] = invoke(skill, result.payload)
        except OSError as e:
            raise HTTPException(
                status_code=500, detail=f"Could not read skill '{result.name}': {e}"
            ) from e
    elif result.kind == "message":
        payload["text"] = result.payload
    elif result.kind == "unknown":
        # Plugin spellings compete for the typo hint too, or "/compac"
        # would be told about skills only.
        payload["suggestion"] = suggest_command(
            result.payload, list(skill_names) + spellings(plugin_commands))

    return payload


def _vars_tracker(request: Request, agent_name: Optional[str]):
    """The session tracker of the agent a chat surface is talking to.

    Template variables live on the AGENT's tracker, not in the session
    file: that is what ``server.py`` reads per turn and hands to the prompt
    strategy, and what ``session_service`` later syncs into the persisted
    ``context_vars``. Writing anywhere else would show a changed value in
    the panel while the next turn still rendered the old one.
    """
    agent = chat_agent(request, agent_name)
    return getattr(agent, "_session_tracker", None) if agent is not None else None


async def _effective_vars(tracker: Any, user_id: str, session_id: str) -> dict:
    """What the next turn will really see: persisted, then runtime on top.

    The tracker alone is not the answer on this surface. A session opened
    in the browser is not loaded into the tracker until a turn runs, so
    reading only the tracker reported "no variables" for a session whose
    file is full of them -- and, far worse since the write became a
    REPLACE, an `unset` computed from that empty base would have persisted
    an empty set and wiped the rest.

    Same merge order as GET /api/sessions/{id} (see api/session_endpoints
    _live_merge_vars), so the panel and this command cannot disagree. The
    terminal needs no such fallback: its tracker is seeded from the session
    at startup and on /resume.
    """
    merged: dict = {}
    manager = getattr(app_state.session_service, "session_manager", None)
    if manager is not None and user_id:
        try:
            stored = (await manager.load_session(user_id, session_id)) or {}
            if isinstance(stored.get("context_vars"), dict):
                merged.update(stored["context_vars"])
        except Exception:
            # No file yet, or not readable: the tracker is then the only
            # truth there is, which is the normal case for a new session.
            logging.getLogger(__name__).debug(
                "No persisted context_vars for %s", session_id, exc_info=True)
    if tracker is not None:
        merged.update(tracker.get_session_template_vars(session_id) or {})
    return merged


@router.get("/chat/vars")
async def chat_vars(request: Request, ctx: AppContext = Depends(app_context)):
    """Template variables of one session, as the next turn will see them."""
    current_user = await ctx.enforce_endpoint_security(request)
    session_id = (request.query_params.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="'session_id' is required")

    # Tracker FIRST, then the ownership check against that same tracker:
    # each agent has its own, so verifying against the default one asked
    # the wrong object and found no owner.
    tracker = _vars_tracker(request, request.query_params.get("agent_name"))
    if tracker is None:
        raise HTTPException(status_code=404, detail="No such agent")
    await ctx.verify_session_owner(session_id, current_user, tracker)
    owner = current_user.username if current_user else "anonymous"
    return {"session_id": session_id,
            "vars": await _effective_vars(tracker, owner, session_id)}


@router.post("/chat/vars")
async def chat_vars_update(request: Request, ctx: AppContext = Depends(app_context)):
    """Set, unset or clear a session's template variables.

    The BODY carries the raw rest of the ``/vars`` line, not a parsed dict:
    the grammar is then read by the same ``parse_vars`` the terminal uses,
    so ``/vars greeting="hallo welt"`` cannot come to mean two different
    things depending on which surface it was typed into.
    """
    from agent_system.chat_commands import apply_vars, parse_vars, store_vars

    current_user = await ctx.enforce_endpoint_security(request)
    body = await _json_object(request)

    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="'session_id' is required")
    payload = body.get("payload") or ""
    if not isinstance(payload, str):
        raise HTTPException(status_code=400, detail="'payload' must be a string")
    if len(payload) > MAX_CHAT_LINE:
        raise HTTPException(
            status_code=413,
            detail=f"Payload too long ({len(payload)} chars, limit {MAX_CHAT_LINE})")
    tracker = _vars_tracker(request, body.get("agent_name"))
    if tracker is None:
        raise HTTPException(status_code=404, detail="No such agent")
    await ctx.verify_session_owner(session_id, current_user, tracker)

    parsed = parse_vars(payload)
    owner = current_user.username if current_user else "anonymous"
    # The persisted set has to be in the base, or an unset computed from an
    # empty tracker would persist an empty set over a full file.
    current = await _effective_vars(tracker, owner, session_id)
    if parsed.errors:
        # Refused whole, like the terminal: half-applying a line leaves the
        # person guessing which half took.
        return {"session_id": session_id, "vars": current,
                "errors": list(parsed.errors), "changed": False}
    persisted = False
    if not parsed.is_query:
        current = apply_vars(current, parsed)
        manager = getattr(app_state.session_service, "session_manager", None)
        persisted = await store_vars(tracker, manager, owner, session_id, current)
    return {"session_id": session_id, "vars": current, "errors": [],
            "changed": not parsed.is_query, "persisted": persisted}


@router.post("/chat/command")
async def chat_command(request: Request, ctx: AppContext = Depends(app_context)):
    """Run a plugin command, the way the terminal runs it.

    The caller names a COMMAND, never a tool. It is looked up in the list
    this agent may run -- collect_plugin_commands filters by the agent's
    own dispatch predicate -- and execution goes through
    Agent.dispatch_tool_call, the same path with the same authorization,
    runtime params and status channel the terminal uses. A command the
    agent may not run does not exist here, so this is no second entry
    point beside the tools: exactly as powerful as the allowlist permits.
    """
    from agent_system.chat_commands import match_plugin_command
    from agent_system.plugin_commands import run_plugin_command

    current_user = await ctx.enforce_endpoint_security(request)
    body = await _json_object(request)

    name = body.get("name") or ""
    command_payload = body.get("payload") or ""
    if not isinstance(name, str) or not isinstance(command_payload, str):
        raise HTTPException(status_code=400,
                            detail="'name' and 'payload' must be strings")
    if len(command_payload) > MAX_CHAT_LINE:
        raise HTTPException(
            status_code=413,
            detail=f"Payload too long ({len(command_payload)} chars, "
                   f"limit {MAX_CHAT_LINE})")

    # The command runs ON a session -- compaction rewrites it -- so the
    # same ownership rule the other session-taking endpoints apply holds
    # here: without it any authenticated user could hand in a foreign
    # session id and have a tool act on it.
    session_id = body.get("session_id")
    if session_id:
        await ctx.verify_session_owner(str(session_id), current_user)

    agent = chat_agent(request, body.get("agent_name"))
    if agent is None:
        raise HTTPException(status_code=404, detail="no such agent")
    # Its tools run with this agent's authorization, so its role gate holds
    # here as it holds for /run -- answered as an agent that does not exist.
    if ctx.gate_refuses(agent, current_user):
        raise HTTPException(status_code=404, detail="no such agent")
    match = match_plugin_command(name.lstrip("/"), _plugin_commands_for(agent))
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=f"'{name}' is not a command this agent can run")

    text = await run_plugin_command(
        agent, match, command_payload,
        session_id=session_id,
        user_id=getattr(current_user, "username", None))
    return {"name": match.qualified, "text": text}


async def _session_agent_for(ctx: AppContext, request: Request, session_id: str, user_id: str,
                             agent_name: Optional[str], current_user: Any) -> Any:
    """The agent whose tracker holds the session, the owner checked against it.

    The agent the SESSION ran with, before the one the caller names:
    every agent carries its own SessionTracker, so cutting the wrong one
    leaves the exchange standing in the right one -- and its next save
    writes it back. The caller's name is the fallback for a session that
    has no record yet. A turn settling the session first, though: its
    agent holds the copy that turn puts back (settling_agent).

    The owner is checked against the tracker the caller is about to write,
    not the entry agent's: a session that is not persisted yet has no owner
    on disk, and the default tracker does not know it either -- so that
    check passes for anybody (the IDOR AppContext.verify_session_owner documents).
    """
    target_agent = settling_agent(request, session_id)
    if target_agent is None:
        ran_with = await session_agent_name(session_id, user_id)
        target_agent = chat_agent(request, ran_with or agent_name)
    if target_agent is None:
        raise HTTPException(status_code=404, detail="no such agent")
    await ctx.verify_session_owner(session_id, current_user,
                                   getattr(target_agent, "_session_tracker", None))
    return target_agent


@router.post("/chat/undo")
async def chat_undo(request: Request, force: bool = Query(default=False),
                    ctx: AppContext = Depends(app_context)):
    """Drop the last question and everything that answered it.

    `/undo` and the cut half of `/retry`. The browser reloads a session
    from disk on every message, so the record is what has to shrink --
    and the agent's copy in memory with it, or the next save would put
    the dropped turn straight back.

    The text comes back so the caller can offer it again (`/retry` puts
    it in the input). Whether it carried a file is said, not sent: the
    browser attaches from the viewer's disk, and a data URL handed back
    would be a second, silent upload.

    ``"files": true`` (`/undo files`) also puts back the files the
    exchange changed, before it is dropped; ``"overwrite": true`` puts
    back files changed outside the agent since as well. A rewind that is
    refused answers 409 with what stands in the way, and the exchange
    stays.
    """
    from agent_system.chat_actions import message_text, message_role

    current_user = await ctx.enforce_endpoint_security(request)
    user_id = current_user.username if current_user else "anonymous"
    body = await _json_object(request)

    session_id = body.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise HTTPException(status_code=400, detail="'session_id' is required")

    target_agent = await _session_agent_for(ctx, request, session_id, user_id, body.get("agent_name"),
                                            current_user)

    files = {"overwrite": body.get("overwrite") is True} if body.get("files") is True else None
    dropped, report = await drop_last_exchange_and_persist(
        target_agent, session_id, user_id, force, files=files)
    if dropped is None:
        return {"session_id": session_id, "dropped": None, "files": None}
    content = getattr(dropped, "content", None)
    # The person's words go back into the input, not what a hook wrote in
    # front of them (simple_prompt_inject task_start): sent again, the
    # hook writes it anew.
    text = message_text(dropped)
    for prefix in (getattr(dropped, "prefixed_by", None) or {}).values():
        text = text.replace(prefix, "", 1)
    return {
        "session_id": session_id,
        "dropped": {
            "role": message_role(dropped),
            "text": text,
            "had_attachments": isinstance(content, list) and len(content) > 1,
        },
        "files": report,
    }


@router.get("/chat/checkpoints")
async def chat_checkpoints(request: Request, session_id: str = Query(...),
                           agent_name: Optional[str] = Query(default=None),
                           ctx: AppContext = Depends(app_context)):
    """The file checkpoints of a session -- bare `/rewind`.

    One per turn that changed files through the agent's file tools (the
    file_checkpoints plugin records them), oldest first and numbered: the
    number is what `/rewind <n>` takes. Read off the conversation as the
    record has it, which is what the browser shows.
    """
    current_user = await ctx.enforce_endpoint_security(request)
    user_id = current_user.username if current_user else "anonymous"
    rewinder = file_rewinder_or_503()
    await _session_agent_for(ctx, request, session_id, user_id, agent_name, current_user)
    record = await session_record(session_id, user_id)
    listing = await rewinder.checkpoints(user_id=user_id, session_id=session_id,
                                         messages=record.get("messages") or [])
    return {"session_id": session_id, **listing}


@router.post("/chat/rewind")
async def chat_rewind(request: Request, force: bool = Query(default=False),
                      ctx: AppContext = Depends(app_context)):
    """Put the files back as they were before a checkpoint -- `/rewind <n>`.

    Files only: the conversation stays as it is (`/undo files` takes the
    last exchange and its files together). Body ``{"session_id",
    "checkpoint": <n from /chat/checkpoints>, "overwrite": false}``. Held
    like /undo's cut: refused while the session runs, since a run could
    write files while they are put back. 409 when files were changed
    outside the agent since (``overwrite`` puts them back anyway), 404 for
    a number that names no checkpoint.
    """
    current_user = await ctx.enforce_endpoint_security(request)
    user_id = current_user.username if current_user else "anonymous"
    body = await parse_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    session_id = body.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise HTTPException(status_code=400, detail="'session_id' is required")
    checkpoint = body.get("checkpoint")
    if isinstance(checkpoint, bool) or not isinstance(checkpoint, int) or checkpoint < 1:
        raise HTTPException(status_code=400, detail="'checkpoint' is required: a number /rewind lists")
    rewinder = file_rewinder_or_503()
    target_agent = await _session_agent_for(ctx, request, session_id, user_id, body.get("agent_name"),
                                            current_user)
    async with held_for_a_write(target_agent, session_id, user_id, force,
                                "it may write files while they are put back") as messages:
        report = await rewinder.rewind(
            user_id=user_id, session_id=session_id, messages=messages, checkpoint=checkpoint,
            registry=getattr(target_agent, "registry", None), overwrite=body.get("overwrite") is True)
    rewind_refusal(report)
    return {"session_id": session_id, **report}


@router.get("/chat/context")
async def chat_context(request: Request, session_id: str = Query(...),
                       agent_name: Optional[str] = Query(default=None),
                       ctx: AppContext = Depends(app_context)):
    """What fills the context window of a session -- `/context`.

    Two blocks that are never mixed: what the provider COUNTED on the last
    call (the usage tracker keeps it, with the window it was counted
    against), and what the conversation holds NOW, estimated per kind. A
    category worked out as "measured minus estimated" would look exact and
    carry the error of both.

    The system prompt and the tool schemas are read off the agent that
    RAN the session, because that is whose prompt and whose tools sit in
    that window -- another agent's numbers would describe a chat that
    never happened.
    """
    from agent_system.chat_actions import (
        context_breakdown, live_context_window, measured_context,
        profile_context_window)

    current_user = await ctx.enforce_endpoint_security(request)
    user_id = current_user.username if current_user else "anonymous"
    # One read: the agent it ran with, the profile its next call goes out
    # on, and the conversation itself all live in the same record.
    record = await session_record(session_id, user_id)
    ran_with = record.get("agent_name") or None
    ran_profile = record.get("llm_profile") or None
    target_agent = chat_agent(request, ran_with or agent_name)
    if target_agent is None:
        raise HTTPException(status_code=404, detail="no such agent")
    await ctx.verify_session_owner(session_id, current_user,
                                   getattr(target_agent, "_session_tracker", None))

    messages: list = record.get("messages") or []

    prompt, tools = "", []
    try:
        # With the session id: the prompt this SESSION sends, template
        # vars and all. Rendered without them it is short by the whole
        # var payload, on the one line the command exists to show.
        # And with the stored messages: the deferred tools they loaded are
        # sent again, and this process's tracker may not hold the session.
        prompt, tools = await target_agent.describe_context_inputs(session_id, messages)
    except Exception as e:  # noqa: BLE001 - a missing line, not a failed request
        logging.getLogger(__name__).warning("No context inputs for %s: %s",
                                            session_id, e)

    return {
        "session_id": session_id,
        "agent_name": getattr(target_agent, "name", ran_with or ""),
        # The window the NEXT call runs against -- of the profile the
        # SESSION is on, which is what a model picked in the panel
        # changes; the agent's own client only answers without one. The
        # measurement below carries the window IT was counted against.
        "window": (profile_context_window(target_agent, ran_profile)
                   or live_context_window(target_agent)),
        # What the provider counted, kept in its own box -- and only for a
        # session that HAS a record: the tracker is keyed by session id
        # alone, and an unpersisted id passes the ownership check.
        "last_call": measured_context(target_agent, session_id) if ran_with else {},
        "estimated": context_breakdown(messages, system_prompt=prompt, tools=tools),
    }


async def _chat_record(ctx: AppContext, request: Request, session_id: str) -> dict:
    """The stored session a chat command reads, for whoever may read it.

    By the kind of failure, not by "anything went wrong": a corrupt
    record read as "no such session" would send someone looking for a
    session id that is right there in their list.
    """
    from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError

    current_user = await ctx.enforce_endpoint_security(request)
    user_id = current_user.username if current_user else "anonymous"
    if not app_state.session_service or not app_state.session_service.session_manager:
        raise HTTPException(status_code=503, detail="No session storage")
    try:
        # A name no session can have is the caller's mistake -- load_session
        # raises the same ValueError for a corrupt FILE, which is not. And
        # before the owner lookup, which builds a path from it unchecked.
        app_state.session_service.session_manager._validate_session_id(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Not a session id: '{session_id}'")
    await ctx.verify_session_owner(session_id, current_user)
    try:
        return await app_state.session_service.session_manager.load_session(user_id, session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"No session '{session_id}'")
    except SessionPermissionError:
        raise HTTPException(status_code=403, detail=f"Not your session '{session_id}'")


@router.get("/chat/transcript")
async def chat_transcript(request: Request, session_id: str = Query(...),
                          ctx: AppContext = Depends(app_context)):
    """The conversation as markdown -- `/export`, for whoever asks.

    With the other /chat endpoints rather than under /sessions, because
    this is what a chat command produces, and the rendering is the one
    the terminal writes to a file (chat_actions.transcript_markdown).

    Rendered from the RECORD, not from a running agent's memory: the
    browser shows the record, and a transcript that disagrees with what
    is on screen is worse than none. text/markdown with a filename, so a
    browser saves it instead of painting it.
    """
    from agent_system.chat_actions import transcript_markdown

    record = await _chat_record(ctx, request, session_id)
    if not (record.get("messages") or []):
        # A file holding nothing but a heading, reported as written, is
        # what the terminal refuses too ("Nothing to export"). Reachable
        # right after /undo takes the only exchange out.
        raise HTTPException(
            status_code=409,
            detail=f"Session '{session_id}' has no messages yet")
    markdown = transcript_markdown(
        record.get("messages") or [],
        agent_name=record.get("agent_name") or "agent",
        session_id=session_id,
        llm=record.get("llm_profile") or "",
    )
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="chat-{session_id}.md"'},
    )


@router.get("/chat/last_answer")
async def chat_last_answer(request: Request, session_id: str = Query(...),
                           ctx: AppContext = Depends(app_context)):
    """`/copy`, for whoever asks: the text of the agent's last answer.

    As the model wrote it -- the reading the terminal's /copy does too
    (chat_actions.last_answer) -- and from the RECORD, like
    /chat/transcript. The clipboard is the caller's to write; "" when the
    session has no answer yet, 409 while something holds it.
    """
    from agent_system.chat_actions import last_answer

    record = await _chat_record(ctx, request, session_id)
    # A run writes checkpoints as it goes, and its answer shows before its
    # save: while one works on the session the record's last answer may be
    # an interim one. With session presence that is any process's run --
    # which presence cannot tell from an agent-cli chat that merely has the
    # session open, so the message names both; without it, only this
    # server's own runs are seen.
    config = ctx.live_config()
    held = await session_held(session_id, record.get("user_id"), config)
    if held:
        holder = ("a run, or an agent-cli chat that has it open"
                  if presence_for(config) is not None else "a run of this server")
        raise HTTPException(
            status_code=409,
            detail=f"Session {session_id} is {held} -- {holder}, so the record "
                   f"may hold an interim answer. /copy once it is free.")
    return {"text": last_answer(record.get("messages") or [])}
