"""POST /run: a run answered as one JSON result -- or, carrying files, streamed as SSE.

The text-only run waits for its result (``collect_final_result``) and is
mirrored into a background job so a page can follow it; a run with uploads
saves them to a temporary directory, builds the multimodal message (after the
check whether the model takes it) and streams its events inline, without a job.
What it shares with /events -- the request id check, the agent and LLM choice,
opening, naming and saving the session -- is in run_start; holding the session
in session_writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import types
from typing import Any, Optional

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from starlette.datastructures import UploadFile  # Use starlette's UploadFile for isinstance checks

from agent_system import app_state
from agent_system.api.app_context import AppContext, app_context, parse_json_body
from agent_system.api.run_start import (
    asks_a_person,
    carry_title,
    get_agent_with_overrides,
    llm_params_from,
    open_session_for_run,
    refused_before_the_run,
    save_after_run,
    set_run_metadata,
    sse_response,
    validate_client_request_id,
)
from agent_system.api.session_writes import claim_session, hold_fresh_session, let_go
from agent_system.core.request_context import (
    register_request_user,
    release_request_user_tree,
    release_run_attended,
    set_run_attended,
)
from agent_system.services.background_job_manager import DuplicateRequestIdError, get_background_job_manager
from agent_system.utils.id import short_id

logger = logging.getLogger(__name__)

router = APIRouter()


async def _mirror_run_as_job(request_id: str, user_id: str, agent_name: str,
                             session_id: Optional[str], llm_profile: Optional[str], llm_params: dict):
    """A BackgroundJob that shows a run somebody else collects, or None.

    POST /run answers its caller from collect_final_result and made no job, so
    nothing could follow such a run: not the chat (it attaches to a session's
    job), not a second page. The job takes the events as they pass (``put``);
    the caller's answer does not go through it.

    A job running under the id already is refused with a 409, as /events does:
    validate_client_request_id cannot rule out a request that got past it at
    the same time, and two agents under one id cannot be told apart.
    """
    feed: asyncio.Queue = asyncio.Queue()

    async def relay():
        while (event := await feed.get()) is not None:
            yield event

    try:
        await get_background_job_manager().create_job(
            request_id=request_id, user_id=user_id, agent_name=agent_name,
            session_id=session_id, agent_runner=relay, llm_profile=llm_profile,
            llm_params=llm_params, mirror=True)
    except DuplicateRequestIdError:
        logger.warning("[RUN] refused duplicate request_id=%s -- a job is already running under it", request_id)
        raise HTTPException(status_code=409, detail="request_id is already running")

    return types.SimpleNamespace(put=feed.put_nowait, close=lambda: feed.put_nowait(None))


def _remove_uploads(temp_files: list, temp_dir: Any) -> None:
    """Delete the uploads of a /run and their temporary directory (None: not made yet)."""
    for temp_file in temp_files:
        try:
            temp_file.unlink()
        except Exception as e:
            logger.warning("Failed to delete temp file %s: %s", temp_file, e)
    if temp_dir is not None:
        try:
            temp_dir.rmdir()
        except Exception as e:
            logger.warning("Failed to delete temp dir %s: %s", temp_dir, e)


@router.post("/run")
async def run(
    request: Request,
    traceparent: Optional[str] = Header(default=None),
    session_id: Optional[str] = Query(default=None),
    agent_name: Optional[str] = Query(default=None),
    llm_profile: Optional[str] = Query(default=None),
    session_title: Optional[str] = Query(default=None),
    force: bool = Query(default=False),
    ctx: AppContext = Depends(app_context),
):
    """Run agent with optional multimodal input (text + images).

    This handler accepts either:
    - multipart/form-data with fields 'task' and repeated 'files' entries
      (and 'attended': the client shows the run's questions to the person
      who started it -- the answer streams back, as on /events), or
    - application/json with {"task": "..."}, or
    - query param ?task=... (fallback used by some clients)

    Query parameters:
    - session_id: Optional session ID for conversation continuity
    - agent_name: Optional agent to use instead of default
    - llm_profile: Optional LLM profile override (turbo, normal, think, etc.)
    - session_title: Optional title for the session, written with its first save

    Security:
    - Requires authentication when auth.enabled=true
    - Validates LLM request permissions
    - Enforces session ownership
    """
    logger = logging.getLogger(__name__)
    request_id = short_id()

    # ========================================
    # SECURITY: Enforce endpoint authentication
    # ========================================
    current_user = await ctx.enforce_endpoint_security(request)

    # SECURITY: Validate LLM access (this is an LLM-consuming endpoint)
    ctx.validate_llm_access(current_user, is_llm_request=True)

    # Determine user_id for session management
    user_id = current_user.username if current_user else "anonymous"

    # Try to parse task and files from the request in a flexible way
    task = None
    upload_files: list[UploadFile] = []
    content_type = request.headers.get('content-type', '')
    logger.debug("/run content-type: %s", content_type)

    # Optional client-supplied request id — collected from body/form/
    # query below, validated + applied after parsing.
    client_request_id: Optional[str] = None
    raw_llm_params: Any = None  # JSON body: an object; multipart: its JSON text
    # The client shows the run's questions to the person who started it
    # (form field ``attended``; the chat sends it with files). Only a run
    # streamed back to that client can be: the text-only run is not.
    attended = False

    # JSON body: {"task": "...", "session_id": "...", "agent_name": "...", "llm_profile": "...", "request_id": "..."}
    if content_type.startswith('application/json'):
        body = await parse_json_body(request)
        logger.debug("/run parsed JSON body: %s", body)
        if isinstance(body, dict):
            task = body.get('task')
            # Allow overrides from JSON body
            if not session_id and 'session_id' in body:
                session_id = body.get('session_id')
            if not agent_name and 'agent_name' in body:
                agent_name = body.get('agent_name')
            if not llm_profile and 'llm_profile' in body:
                llm_profile = body.get('llm_profile')
            raw_llm_params = body.get('llm_params')
            if not session_title and 'session_title' in body:
                session_title = body.get('session_title')
            if 'request_id' in body:
                client_request_id = body.get('request_id')
            # Session presence: run a session another process holds anyway
            force = force or bool(body.get('force'))

    # multipart/form-data: parse form and files
    elif content_type.startswith('multipart/form-data'):
        try:
            form = await request.form()
        except Exception:
            # Malformed multipart body is a client error, not a 500
            raise HTTPException(
                status_code=400,
                detail="Invalid multipart form data: could not be parsed"
            )
        try:
            logger.debug("/run parsed form keys: %s", list(form.keys()))
        except Exception as e:
            logger.debug("/run parsed form (unable to list keys): %s", e)
        # Extract task field
        if 'task' in form:
            task = form['task']
        # Allow overrides from form data
        if not session_id and 'session_id' in form:
            session_id = form.get('session_id')
        if not agent_name and 'agent_name' in form:
            agent_name = form.get('agent_name')
        if not llm_profile and 'llm_profile' in form:
            llm_profile = form.get('llm_profile')
        raw_llm_params = form.get('llm_params')
        if not session_title and 'session_title' in form:
            session_title = form.get('session_title')
        if 'request_id' in form:
            client_request_id = form.get('request_id')
        force = force or str(form.get('force') or "").lower() in ("1", "true", "yes")
        attended = str(form.get('attended') or "").lower() in ("1", "true", "yes")
        # Collect UploadFile instances - use getlist() for repeated fields
        if hasattr(form, 'getlist'):
            files_list = form.getlist('files')
        else:
            files_list = [form.get('files')] if 'files' in form else []

        for file_val in files_list:
            if file_val and isinstance(file_val, UploadFile):
                upload_files.append(file_val)
        logger.debug("/run collected upload_files count=%d", len(upload_files))

    # Fallback: query param
    if not task:
        query_task = request.query_params.get('task')
        if query_task:
            task = query_task
    if not client_request_id:
        client_request_id = request.query_params.get('request_id')

    # Client-supplied request_id (writer-jobs worker et al.): lets the
    # caller key this run under an id IT already persisted, so its
    # later ``/api/requests/{rid}/status`` probes and
    # ``/api/requests/{rid}/cancel`` propagation actually match this
    # run. Without this, /run minted an id the caller never learns
    # (the response body carries no request_id), so writer-side
    # reconcile probes were guaranteed misses — reported
    # ``unknown/no_active_run`` for live runs (→ resume double-run)
    # and cancel propagation no-opped while the agent kept burning
    # tokens.
    #
    # Guards (400 format; 409 already active, or in line with a live run's
    # id): see validate_client_request_id, shared with /events.
    if client_request_id:
        request_id = await validate_client_request_id(client_request_id)

    logger.info("/run invoked, task=%s, files=%d, request_id=%s, session_id=%s, agent=%s, llm_profile=%s, user=%s",
               task, len(upload_files), request_id, session_id, agent_name or "default",
               llm_profile or "default", user_id)

    # Get agent with LLM override
    llm_params = llm_params_from(raw_llm_params)
    # what the caller chose for the model: the session's record keeps it (SessionService.save_session)
    llm_choice = {"profile": llm_profile, "params": llm_params}
    selected_agent, llm_override, llm_profile_info = get_agent_with_overrides(
        ctx, agent_name, llm_profile, requester=current_user, llm_params=llm_params)

    # A run without a session creates one, as with files and on /events. The text-only run goes through
    # collect_final_result, which takes a missing id for a stateless call: a throwaway session, never saved.
    # Its caller is headless (the writer's dispatches) and never learns the id: once saved, the session
    # leaves the agent's tracker, which keeps what it holds for the life of the process.
    made_session = False
    if not session_id and not upload_files and task:
        session_id, made_session = short_id(), True

    session_exists = await open_session_for_run(selected_agent, user_id, session_id, llm_profile, llm_choice)
    if session_exists:
        session_title = None  # it names a session the run creates, not one it continues

    from agent_system.servers.agent.result_utils import collect_final_result

    # If no uploaded files, treat as text-only
    if not upload_files:
        if not task:
            raise HTTPException(status_code=400, detail="Missing 'task' in request")

        # Mirrored into a job, so a page can follow the run (see _mirror_run_as_job).
        # First: a second run under a running id is refused before it takes the
        # id's ownership or the session.
        try:
            mirror = await _mirror_run_as_job(
                request_id, user_id, selected_agent.name, session_id,
                llm_profile or selected_agent.agent_config.default_llm_profile, llm_params)
        except HTTPException:
            if made_session:  # opened already, and nothing below lets go of it
                selected_agent._session_tracker.discard_session(session_id)
            raise
        held = None
        try:
            # Register request ownership for status stream security -- AFTER
            # all validations and inside the try whose finally releases it.
            # Registered earlier, every 4xx above leaked the entry.
            register_request_user(request_id, user_id)
            # Session presence (core/session_presence/): held through the save
            # after the run, so no woken run has its turn overwritten.
            refusal, held = await claim_session(selected_agent, session_id, user_id, force)
            if refusal:
                raise HTTPException(status_code=409, detail=refusal)
            refused = []

            def on_event(event: dict) -> Any:
                carry_title(selected_agent, event, session_title)
                if refused_before_the_run(event):
                    refused.append(event)
                return mirror.put(event)

            # Pass LLM override to collect_final_result
            result = await collect_final_result(
                selected_agent, task,
                request_id=request_id,
                session_id=session_id,
                llm_override=llm_override,
                llm_profile_info_override=llm_profile_info,
                on_event=on_event,
            )

            # Save session after execution (if session_id was provided or created) --
            # not one the run was refused, nor one somebody holds after it
            # (after_run): another run of this process, or an append saving it.
            if session_id and app_state.session_service and not refused:
                await save_after_run(selected_agent, user_id, session_id, llm_profile,
                                     not session_exists, llm_choice)

            return result
        finally:
            mirror.close()
            let_go(selected_agent, held, user_id)
            if made_session:
                selected_agent._session_tracker.discard_session(session_id)
            # Cleanup: release request + derived sub-request ids (tool
            # suffixes, sub-agents) from the ownership map
            release_request_user_tree(request_id)

    # Process uploaded files for multimodal input
    from agent_system.utils.multimodal_processor import (
        AttachmentRejected, detect_file_type, message_with_attachments)
    import tempfile
    from pathlib import Path

    # Categorize uploaded files by type
    image_paths = []
    audio_paths = []
    text_paths = []
    temp_files = []
    temp_dir = None
    # Once the SSE generator is returned, ITS finally owns the cleanup.
    # Until then every error path (400 capability check, write failure,
    # processing error) must clean up here — see the outer finally.
    stream_owns_cleanup = False

    # Ownership registration inside the try/finally pairing (see the
    # text-only branch for the rationale).
    register_request_user(request_id, user_id)

    try:
        temp_dir = Path(tempfile.mkdtemp())

        temp_dir_resolved = temp_dir.resolve()
        for upload_file in upload_files:
            # SECURITY: the client-supplied filename must NOT be trusted.
            # Path's `/` drops the left side if the right is absolute and
            # honors '../' segments, so a raw join allows arbitrary-path
            # writes (RCE / config overwrite). Take only the basename and
            # verify the result stays inside temp_dir.
            safe_name = Path(upload_file.filename or "").name
            if not safe_name:
                logger.warning("Skipping upload with empty/unsafe filename: %r", upload_file.filename)
                continue
            temp_path = temp_dir / safe_name
            if not temp_path.resolve().is_relative_to(temp_dir_resolved):
                logger.warning("Skipping upload that escapes temp dir: %r", upload_file.filename)
                continue
            with open(temp_path, 'wb') as f:
                content = await upload_file.read()
                f.write(content)
            temp_files.append(temp_path)

            # Categorize by file type
            file_type = detect_file_type(temp_path)
            if file_type == 'image':
                image_paths.append(str(temp_path))
            elif file_type == 'audio':
                audio_paths.append(str(temp_path))
            elif file_type == 'text':
                text_paths.append(str(temp_path))
            else:
                logger.warning("Unsupported file type for %s, skipping", upload_file.filename)

            logger.debug("Saved uploaded file %s (%d bytes) -> %s [%s]",
                       upload_file.filename, len(content), temp_path, file_type)

        if not task and not (image_paths or audio_paths or text_paths):
            raise HTTPException(status_code=400, detail="Missing 'task' in request")
        # The check against the model this run uses, then the build: the
        # step every entry point that attaches media shares.
        try:
            multimodal_msg = message_with_attachments(
                task, {"image": image_paths, "audio": audio_paths, "text": text_paths},
                llm_override, selected_agent)
        except AttachmentRejected as e:
            # A file that failed to process carries its cause; that one may
            # be a server fault (a codec, a bug) and keeps its traceback.
            logger.warning("Attachments refused: %s", e, exc_info=e.__cause__ is not None)
            raise HTTPException(status_code=400, detail=str(e))

        # Stream events for multimodal message (same as /events endpoint)
        async def event_stream():
            # Initial keep-alive line
            yield ":ok\n\n"

            # Track if this is a new session
            was_new_session = (session_id is None) or (not session_exists)
            actual_session_id = session_id
            refused = False  # the run was refused at the agent's session lock
            # Session presence (core/session_presence/): held before the
            # run through the save after it; a session this run creates
            # comes with the start event.
            refusal, held = await claim_session(
                selected_agent, actual_session_id, user_id, force)
            if refusal:
                yield "event: error\n"
                yield f"data: {json.dumps({'type': 'error', 'message': refusal}, ensure_ascii=False)}\n\n"
                return

            # tool_approval may ask the person reading this stream (released below)
            set_run_attended(request_id, asks_a_person(attended, current_user, ctx.live_config().auth.enabled))
            try:
                async for event in selected_agent.run_events(multimodal_msg, request_id=request_id, session_id=actual_session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info):
                    event_type = event.get("type")
                    refused = refused or refused_before_the_run(event)

                    # Capture session_id from start event (created on first call)
                    if event_type == "start" and event.get("session_id"):
                        actual_session_id = event["session_id"]
                        if not held:
                            held = hold_fresh_session(
                                selected_agent, actual_session_id, user_id)
                        carry_title(selected_agent, event, session_title)

                    # CRITICAL: Always set/update session metadata (even for existing sessions)
                    # This ensures user_id is available for tool execution AND respects llm_profile overrides
                    if was_new_session or event_type == "start":
                        set_run_metadata(selected_agent, actual_session_id, user_id, llm_profile, llm_choice)
                    # Ensure proper JSON serialization
                    if hasattr(event, 'to_dict'):
                        payload = event.to_dict()
                    else:
                        payload = event

                    yield f"event: {event_type}\n"
                    yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

                    if event_type == "end":
                        break

            except Exception as e:
                logger.exception("Error streaming multimodal events: %s", e)
                error_event = {"type": "error", "message": str(e)}
                yield "event: error\n"
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
            finally:
                try:
                    # Save session after completion -- not one the run was refused, nor
                    # one somebody holds after it (after_run): another run of this
                    # process, an append saving it, or this run itself, not done (a
                    # stream left at a yield) -- its own end saves it. Shielded: a
                    # client that leaves cancels the stream's whole scope, the run in
                    # it and its last save too, and here every await was cancelled
                    # again -- nothing saved, and the steps below skipped.
                    if app_state.session_service and actual_session_id and not refused:
                        # Use actual agent name and effective llm_profile (respecting overrides)
                        with anyio.CancelScope(shield=True):
                            await save_after_run(selected_agent, user_id, actual_session_id, llm_profile,
                                                 was_new_session, llm_choice)
                finally:
                    # Whatever became of the save: skipped, the session stayed held
                    # for the life of the process (presence refuses every later run).
                    let_go(selected_agent, held, user_id)
                    # Cleanup: release request + derived sub-request ids
                    release_request_user_tree(request_id)
                    release_run_attended(request_id)

                    # Cleanup temp files after streaming completes
                    _remove_uploads(temp_files, temp_dir)

        stream_owns_cleanup = True
        return sse_response(event_stream(), media_type="text/event-stream")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error in /run: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if not stream_owns_cleanup:
            # The generator was never handed to the client — uploads and
            # the ownership entry would leak on this error path.
            release_request_user_tree(request_id)
            _remove_uploads(temp_files, temp_dir)
