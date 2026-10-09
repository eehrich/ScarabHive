"""The run of a request: its entry (run_events) and the three phases it drives (_run_events).

run_events is how every caller runs the agent: the role gate before anything is written, the
session's metadata, the LLM the run goes out on (an override, the advanced model, the caller's), the
status forwarder and the run's context variables. _run_events registers the request, takes the
session's lock and drives Phase 1 and Phase 3 (run_phases.py) around Phase 2, the step loop
(llm_loop.py). With them: the session presence the request holds for as long as it runs, and the
request ids a run hands out.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from ....core.cancellation import get_cancellation_manager
from ....core.session_presence import SessionBusy, presence_for
from ....utils.id import short_id
from ....llm.models import ChatMessage, LLMClient
from ....llm.structured_output import ResponseFormat
from ....tools.status import (
    status_scope,
    status_bus,
    current_request_id
)
from ..components.status_forwarding import StatusEventForwarder, relay_run_event
from ..refusals import SESSION_LOCKED

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


class RunMixin:
    """The run's entry and its orchestration (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``system_config``, ``agent_config``, ``timeouts``,
    ``_session_tracker``, ``_request_manager``, ``_hook_manager``, ``_presence_holds`` and
    ``_internal_tool_counter`` with its lock.
    """

    async def run_events(
        self: Agent,
        task: Union[str, ChatMessage],
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        llm_override: Optional[LLMClient] = None,
        llm_profile_info_override: Optional[str] = None,
        use_advanced_model: bool = False,
        response_format: Optional[ResponseFormat] = None,
    ):
        """Run the agent and yield structured events for UI streaming.

        Args:
            task: Either a string task description or a ChatMessage with multimodal content
            request_id: Optional request ID for tracking
            session_id: Optional session ID for conversation history
            llm_override: Optional LLM client to use instead of self.llm (for per-request profile overrides)
            llm_profile_info_override: Optional profile info string for status display (e.g., "turbo:openai_httpx/gpt-5-nano")
            use_advanced_model: If True and llm_override not set, use best available LLM profile
            response_format: Optional structured output for this run's FINAL answer (the step without
                tool calls): sent as the provider's field on every step of the run where the step's
                LLM takes it, described in the conversation where it does not and the format allows
                the prompt fallback, refused otherwise. The final answer is validated, and sent back
                once for correction; its ``final`` event carries it unformatted (content_format "json").
        """
        if response_format is not None and not isinstance(response_format, ResponseFormat):
            # A dict would pass for "no field" at every client and then fail deep in the loop.
            raise TypeError(f"response_format must be a ResponseFormat, not {type(response_format).__name__}")

        # Generate request ID if not provided
        if request_id is None:
            request_id = short_id()

        # Role gate (metadata.min_role) -- before anything of the run is written:
        # the metadata below, the request registration, the session lock. The
        # endpoints refuse earlier with a 403; this is the backstop for every
        # path that reaches an agent without one (SAM, agent as a tool,
        # stategraph, agent-cli woken for a session). Refused the way the lock
        # refusal in _run_events is: an error, then the end.
        refusal = self._refusal_event(request_id, session_id)
        if refusal:
            yield refusal
            yield {"type": "end"}
            return

        # Track if this is a newly generated session
        was_new_session = not session_id
        
        # If no session_id provided, generate one and persist empty history
        if not session_id:
            session_id = short_id()

        # CRITICAL: Set session metadata for newly generated sessions AND ensure it exists for existing ones
        # This ensures user_id is available for tool execution even in sub-agents
        if self._session_tracker:
            existing_metadata = self._session_tracker.get_session_metadata(session_id)
            if not existing_metadata:
                # Extract user_id from the request ownership map
                # (populated by API layer / tool execution / sub_agent_manager)
                from ....core.request_context import get_request_user
                user_id = get_request_user(request_id)
                
                # Use agent's default llm_profile for metadata
                effective_llm_profile = self.agent_config.default_llm_profile if self.agent_config else "normal"
                self._session_tracker.set_session_metadata(session_id, {
                    "user_id": user_id,
                    "agent_name": self.name,
                    "llm_profile": effective_llm_profile
                })
                if was_new_session:
                    logger.debug(f"Set session metadata for new session {session_id}: user_id={user_id}")
                else:
                    logger.debug(f"Set session metadata for existing session {session_id} (was missing): user_id={user_id}")

        # Handle Union[str, ChatMessage] input
        initial_message: Optional[ChatMessage] = None
        task_text: str = ""

        if isinstance(task, ChatMessage):
            # Extract task text from ChatMessage content for logging/tracking
            initial_message = task
            if isinstance(task.content, str):
                task_text = task.content
            elif isinstance(task.content, list):
                # Extract text from content items (Pydantic models, not dicts)
                text_parts = [getattr(item, "text", "") for item in task.content if hasattr(item, "type") and getattr(item, "type") == "text"]
                task_text = " ".join(text_parts) if text_parts else "[multimodal input]"
            else:
                task_text = "[multimodal input]"
        else:
            task_text = task

        # Create suffixed request IDs for coordinator and worker so their
        # status messages can be correlated separately while still linking
        # back to the base request_id. Use the Agent's centralized counter
        # to ensure monotonic, global numbering across components.
        coordinator_request_id = await self.next_internal_tool_request_id(request_id) if request_id else None
        worker_request_id = await self.next_internal_tool_request_id(request_id) if request_id else None

        # Handle use_advanced_model if no llm_override provided
        if use_advanced_model and not llm_override:
            from agent_system.llm.factory import override_for_profile

            # Ketten-Semantik: Advanced-Modell = llm_profile_advanced[0].
            # Keine Advanced-Kette konfiguriert oder advanced == default
            # (kein echtes Upgrade) → no-op (normale Kette läuft).
            advanced_profile = self.agent_config.advanced_llm_profile if self.agent_config else None
            if advanced_profile and self.agent_config and \
                    advanced_profile == self.agent_config.default_llm_profile:
                advanced_profile = None
            if advanced_profile:
                try:
                    llm_override, llm_profile_info_override = override_for_profile(
                        self.system_config, self.agent_config, advanced_profile)
                    logger.info(f"use_advanced_model=True mapped to profile: {llm_profile_info_override}")

                except Exception as e:
                    logger.error(f"Failed to create LLM override for use_advanced_model: {e}")
                    # Continue with default LLM

        # The caller's LLM (agent_config.inherit_parent_llm): after the choices
        # made for this very run, which win over it -- an override passed in, or
        # use_advanced_model where the agent has an advanced chain to go to.
        if llm_override is None:
            from_caller = self._llm_from_caller()
            if from_caller is not None:
                llm_override, llm_profile_info_override = from_caller

        # Create and start status forwarder BEFORE entering status_scope context managers
        # This ensures the forwarder is subscribed to status_bus before any START events are generated
        # Fixes race condition where status_scope generates events before forwarder is ready
        status_forwarder = StatusEventForwarder()
        await status_forwarder.start_forwarding(request_id)

        # Wire LLM hooks to llm_override if provided (per-request LLM clients
        # won't have hooks from __init__ since they are freshly created)
        if llm_override is not None and self._hook_manager:
            self._hook_manager.wire_llm_hooks(llm_override)

        # Set per-agent app identity on override LLMs (they bypass __init__'s
        # set_app_title call since they are freshly created by CLI --llm or
        # use_advanced_model).
        if llm_override is not None and hasattr(llm_override, 'set_app_title'):
            llm_override.set_app_title(self.name)

        # The run sets its request id and its user in the context it runs in -- the
        # caller's: this generator runs in whoever iterates it. Its cleanup resets
        # them once a conversation context was built; a setup that failed before, or
        # a consumer that closed the stream early, left them to the caller. What they
        # were is what they are again when this generator ends -- and at "end", its
        # last event: a consumer that stops there (app, agent_service) never closes
        # it, and the garbage collector closes it in another task.
        from ....core.request_context import current_run_user
        request_before, user_before = current_request_id.get(), current_run_user.get()
        try:
            # Pass status_scope parameters to _run_events which will open them AFTER
            # sending the 'start' event - this ensures frontend has currentRequestId
            # before any status events arrive
            async for event in self._run_events(
                task_text,
                request_id=request_id,
                session_id=session_id,
                coordinator_request_id=coordinator_request_id,
                worker_request_id=worker_request_id,
                initial_message=initial_message,
                llm_override=llm_override,
                llm_profile_info_override=llm_profile_info_override,
                status_forwarder=status_forwarder,
                use_advanced_model=use_advanced_model,
                response_format=response_format,
            ):
                # Before the yield: the consumer of a sub-run can stop reading at its
                # end, error or cancel (sub_agent_manager does), and the event it
                # stops at would never be relayed.
                relay_run_event(status_forwarder, event, self.name)
                if event.get("type") == "end":
                    current_request_id.set(request_before)
                    current_run_user.set(user_before)
                yield event
        except GeneratorExit:
            # Generator is being closed early - clean exit without error
            raise
        finally:
            # Always remove the status_forwarder's handler from the global
            # status_bus. _finalize_request only does this when a context was
            # built (context.status_forwarder), so failure paths that return
            # before context assignment - session-lock failure, 'No LLM
            # available', any exception in _initialize_request_and_conversation
            # - would otherwise leak the handler permanently (it accumulates on
            # the shared bus and every future publish() invokes the dead
            # handler). stop_forwarding() is idempotent, so the normal-path
            # call inside _finalize_request remains harmless.
            try:
                await status_forwarder.stop_forwarding()
            except Exception as e:
                logger.debug(f"Failed to stop status_forwarder for {request_id}: {e}")
            current_request_id.set(request_before)
            current_run_user.set(user_before)

    async def _run_events(
        self: Agent,
        task: str,
        request_id: str,
        session_id: str,
        coordinator_request_id: str,
        worker_request_id: str,
        initial_message: Optional[ChatMessage] = None,
        llm_override: Optional[LLMClient] = None,
        llm_profile_info_override: Optional[str] = None,
        status_forwarder: Optional[StatusEventForwarder] = None,
        use_advanced_model: bool = False,
        response_format: Optional[ResponseFormat] = None,
    ):
        """
        Core agent execution loop - orchestrates LLM conversation with tool usage.

        Now refactored into three focused phases (see REFACTORING_PLAN.md):
        1. Initialize: _initialize_request_and_conversation() - Setup and context building
        2. Execute: _execute_llm_loop() - Main LLM interaction loop with tool execution
        3. Finalize: _finalize_request() - Cleanup and persistence

        This orchestration method is now <150 LOC, delegating complex logic to focused helpers.

        Args:
            task: Text task description (may be empty if initial_message is provided)
            request_id: Request ID for tracking and cancellation
            session_id: Session ID for conversation history persistence
            coordinator_request_id: Request ID for coordinator status scope
            worker_request_id: Request ID for worker status scope
            initial_message: Optional ChatMessage with multimodal content
            llm_override: Optional LLM client override
            llm_profile_info_override: Optional profile info for status display
            status_forwarder: Pre-created status event forwarder (created before status_scope)

        Yields:
            Dict events: start, heartbeat, thinking, status, tool_*, final, error, cancelled, end
        """
        # Initialize state variables for access in finally block
        step = 0
        context = None
        messages = None
        checkpoint_loop: Optional[asyncio.Task] = None
        results: Dict[str, Any] = {"task": task, "calls": []}

        # Register this request BEFORE emitting start event so appends work immediately
        request_entry = {
            "cancel": asyncio.Event(),
            "message_event": asyncio.Event(),
            "appended": []
        }
        self._request_manager.register_active_request(request_id, request_entry)
        self._session_tracker.register_request(request_id, session_id, request_entry)

        # Try to acquire session lock to prevent parallel requests on same session
        # This prevents race conditions when multiple browser tabs access the same session
        session_lock_timeout = self.timeouts.session_lock_timeout if self.timeouts else 5.0
        lock_acquired = await self._session_tracker.acquire_session_lock(session_id, request_id, timeout=session_lock_timeout)
        if not lock_acquired:
            # Another request is already processing this session
            is_locked, owner = self._session_tracker.check_session_locked(session_id)
            error_msg = f"Session {session_id} is currently locked by another request ({owner}). Please wait for that request to complete."
            logger.warning("Request %s failed to acquire lock for session %s (owner: %s)", 
                         request_id, session_id, owner)
            
            # CRITICAL: Clean up the request registration since we're aborting
            # Without this, the request stays registered as "active" forever
            self._request_manager.unregister_active_request(request_id)
            self._session_tracker.unregister_request(request_id)
            
            yield {"type": "error", "message": error_msg, "request_id": request_id, "error_type": SESSION_LOCKED}
            yield {"type": "end"}
            return

        # Emit start event BEFORE opening status_scope contexts
        # This ensures frontend has currentRequestId set before any status events arrive
        started = False
        try:
            yield {"type": "start", "task": task, "request_id": request_id, "session_id": session_id}
            started = True
        finally:
            if not started:
                # Closed at its first event (a client gone at once): nothing below runs, the
                # finally that ends a run included, and the session stayed held for good by a
                # run that never ran. Let go of it as a refused request does.
                self._request_manager.unregister_active_request(request_id)
                self._session_tracker.unregister_request(request_id)

        # Now open status_scope contexts - their START events will arrive AFTER the start event.
        # Entered before the block they cover, and guarded like the start event: entering one
        # publishes, and a cancel or an error there came before the try whose finally ends a run
        # -- the request stayed registered and its session held for good.
        scopes = contextlib.AsyncExitStack()
        try:
            status_coordinator = await scopes.enter_async_context(
                status_scope(status_bus, f"{self.name}_coordinator", coordinator_request_id))
            status_worker = await scopes.enter_async_context(
                status_scope(status_bus, f"{self.name}_worker", worker_request_id))
        except BaseException as error:
            self._request_manager.unregister_active_request(request_id)
            self._session_tracker.unregister_request(request_id)  # lets go of the session's lock too
            # A scope already open is told why it ends, as `async with` would tell it
            await scopes.__aexit__(type(error), error, error.__traceback__)
            raise
        async with scopes:
            try:
                # The checkpoint loop: started with the session held, first thing in the
                # try whose finally (_finalize_request) stops it -- this one, the loop this
                # run started. Started before the lock (in run_events, as it was), a
                # request refused there registered a loop of its own between two runs,
                # and the run after it went without one once that request cleaned up.
                checkpoint_loop = self._start_checkpoint_loop(session_id)

                # Session presence: held from here on, not from the first LLM
                # call -- whoever lets go of the endpoint's hold meanwhile (a
                # client that disconnects) would leave the session looking idle
                # while this run has it, and a direct message would wake a
                # second run of it.
                self._presence_hold(session_id, request_id)

                # Phase 1: Initialize request and build conversation context
                try:
                    context = await self._initialize_request_and_conversation(
                        task=task,
                        request_id=request_id,
                        session_id=session_id,
                        initial_message=initial_message,
                        llm_override=llm_override,
                        status_forwarder=status_forwarder
                    )
                except RuntimeError as e:
                    # LLM not available - emit error and end stream
                    results.setdefault("errors", []).append(str(e))
                    yield {"type": "error", "message": str(e), "request_id": request_id}
                    yield {"type": "end"}
                    return

                # Helper function to yield any pending status events from per-request forwarder
                def yield_pending_status_events():
                    if context and context.status_forwarder:
                        events = context.status_forwarder.get_pending_events()
                        for event in events:
                            yield event

                # CRITICAL: Give the forwarder task CPU time to process queued events
                # During Phase 1 (synchronous initialization), the forwarder task may not
                # have had a chance to read events from its queue. This sleep allows it to catch up.
                await asyncio.sleep(0)
                
                # CRITICAL: Yield any pending status events from Phase 1 initialization
                # This ensures START events from status_scope are delivered before LLM loop
                for status_event in yield_pending_status_events():
                    yield status_event

                # Track messages from context for updates during loop
                messages = context.messages

                # Phase 2: Execute main LLM loop with tool execution
                loop_generator = self._execute_llm_loop(
                    context=context,
                    request_id=request_id,
                    session_id=session_id,
                    status_coordinator=status_coordinator,
                    status_worker=status_worker,
                    llm_override=llm_override,
                    llm_profile_info_override=llm_profile_info_override,
                    use_advanced_model=use_advanced_model,
                    response_format=response_format,
                )

                async for event in loop_generator:
                    # Track step from events that contain step info
                    # This ensures we report accurate step count in completion message
                    if "step" in event:
                        step = event.get("step", step)

                    # Capture summary and errors from events -- before the yield:
                    # a consumer may stop reading at an error (sub_agent_manager
                    # and stategraph do), and the finalize, its status line and
                    # the session end hooks must still know of it.
                    if event.get("type") == "final" and "summary" in event:
                        results["summary"] = event["summary"]
                    elif event.get("type") == "error":
                        results.setdefault("errors", []).append(event.get("message", "Unknown error"))

                    yield event

                    # Yield any pending status events after each main event
                    # This ensures status messages are delivered in real-time, not batched at the end
                    for status_event in yield_pending_status_events():
                        yield status_event

                    # Track messages updates from context during loop execution
                    if context:
                        messages = context.messages

            except Exception as e:
                logger.exception("Agent execution failed with exception:")
                # Into the results like every error event of the loop, and before
                # the yield (a consumer may stop reading there): unrecorded, the
                # finalize reported the crashed run "completed" and the session
                # end hooks saw no error.
                results.setdefault("errors", []).append(f"Agent execution failed: {e}")
                # Whether the run had been cancelled, read before the
                # cancel_request below: it cancels the run's own token too (its
                # tools and background work stop on it), and the finalize would
                # then report every crash a consumer reads past as a cancel.
                crash_token = get_cancellation_manager().get_token(request_id)
                results["cancelled"] = bool(crash_token is not None and crash_token.is_cancelled)
                yield {"type": "error", "message": f"Agent execution failed: {e}"}
                
                # CRITICAL: Cancel all sub-requests when parent agent fails
                # This ensures sub-agents don't continue running when the parent has an error
                # Uses prefix matching: request_id "abc123" will cancel "abc123_sub_xxx" etc.
                cancellation_manager = get_cancellation_manager()
                cancelled_count = cancellation_manager.cancel_request(request_id)
                if cancelled_count:
                    logger.info(f"Cancelled {cancelled_count} sub-request(s) due to parent agent error")
            finally:
                # Phase 3: Finalize and cleanup
                # Note: This runs even if generator is closed early, but we can't yield in that case
                try:
                    await self._finalize_request(
                        request_id=request_id,
                        session_id=session_id,
                        status_coordinator=status_coordinator,
                        status_worker=status_worker,
                        context=context,
                        messages=messages if messages else (context.messages if context else None),
                        results=results,
                        step=step,
                        checkpoint_loop=checkpoint_loop,
                    )
                finally:
                    # Session presence: after the save, so input still waiting
                    # wakes the session and the woken run finds the whole
                    # conversation on disk -- and in a finally, because a
                    # cancelled save (client gone) must not leave the session
                    # looking like it still runs.
                    self._presence_release(request_id)

            # Yield final status events and end marker
            # These won't execute if generator was closed early (GeneratorExit), which is fine
            if 'yield_pending_status_events' in locals():
                for status_event in yield_pending_status_events():
                    yield status_event

            yield {"type": "end"}

    def _presence_hold(self: Agent, session_id: str, request_id: str) -> None:
        """Session presence (core/session_presence.py): the request holds its
        session for as long as it runs -- from its start, not from its first
        LLM call. A client that disconnects while the run is still setting
        itself up lets go of the endpoint's hold, and the session would look
        idle while it runs on: a direct message would wake a second run of it."""
        presence = presence_for(self.system_config)
        if presence is None or not session_id or request_id in self._presence_holds:
            return
        try:
            metadata = self._session_tracker.get_session_metadata(session_id) or {}
            user_id = metadata.get("user_id")
            if not user_id:
                from ....core.request_context import get_request_user
                user_id = get_request_user(request_id)
            held = None
            try:
                if presence.hold(session_id, user_id, self.name, run=request_id):
                    held = (presence, session_id, user_id)
            except SessionBusy as busy:
                # Forced past the refusal at the entry point. The input waiting
                # belongs to the process that holds the session.
                logger.warning("%s; this request runs it unheld", busy)
            self._presence_holds[request_id] = held
        except Exception as e:
            logger.warning("Session presence: holding %s failed: %s", session_id, e)

    def _presence_step(self: Agent, session_id: str, request_id: str) -> None:
        """Every LLM call takes the input waiting for the session -- the pre-LLM
        hooks hand it over -- as long as this request holds the session."""
        self._presence_hold(session_id, request_id)
        held = self._presence_holds.get(request_id)
        if not held:
            return
        presence, sid, user_id = held
        try:
            presence.take_pending(sid, user_id)
        except Exception as e:
            logger.warning("Session presence: step of %s failed: %s", session_id, e)

    def _presence_release(self: Agent, request_id: str) -> None:
        held = self._presence_holds.pop(request_id, None)
        if held is None:
            return
        presence, session_id, user_id = held
        try:
            presence.release(session_id, user_id)
        except Exception as e:
            logger.warning("Session presence: releasing %s failed: %s", session_id, e)

    async def next_internal_tool_request_id(self: Agent, base_request_id: str) -> str:
        """Return the next internal tool request id with a 3-digit suffix.

        This method is async and protected by an internal lock to ensure
        unique, monotonic counters for the lifetime of the Agent instance.
        """
        async with self._internal_tool_counter_lock:
            self._internal_tool_counter += 1
            return f"{base_request_id}_{self._internal_tool_counter:03d}"
