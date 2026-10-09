"""Phase 1 and Phase 3 of a run: building its conversation, and ending it.

_initialize_request_and_conversation sets up what the step loop works on (ConversationContext): the
cancellation token, the run's context variables, the tools and their schemas, the rendered prompts,
the session's history and the opening message. _finalize_request takes it all down in the order that
keeps the session whole: the checkpoint loop stopped, late messages taken in, the final save under
the session's lock, then the session end hooks, the context variables and the final status. Phase 2,
the step loop, is llm_loop/; run.py drives the three.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ....core.cancellation import get_cancellation_manager, CancellationToken
from ....utils.json_utils import history_safe_tool_calls
from ....llm.models import ChatMessage, LLMClient
from ....llm.text_sanitizer import sanitize_for_llm
from ....tools.status import (
    StatusScope,
    current_request_id
)
from ..components.status_forwarding import StatusEventForwarder
from ..tool_schema_builder import ToolSchemaBuilder
from ..deferred_tools import DeferredTools

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """Context for agent conversation execution."""
    messages: List[ChatMessage]
    available_tools: List[str]
    tools_schema: List[Dict[str, Any]]
    tool_name_mapping: Dict[str, str]
    max_steps: int
    main_token: CancellationToken
    context_reset_token: Any  # Token for resetting contextvars
    status_forwarder: 'StatusEventForwarder'  # Per-request forwarder instance
    session_id: Optional[str] = None  # Session ID for session-scoped operations
    user_reset_token: Any = None  # Token for resetting current_run_user
    # Schemas held back until the model loads them (tools.deferred); loading
    # appends to tools_schema in place. None when the agent defers nothing.
    deferred_tools: Optional[DeferredTools] = None


class RunPhasesMixin:
    """Phase 1 and Phase 3 of a run (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``llm``, ``agent_config``, ``_session_tracker``,
    ``_session_service``, ``_request_manager``, ``_hook_manager``, ``_tool_integration_manager`` and
    ``_step_llms``.
    """

    async def _initialize_request_and_conversation(
        self: Agent,
        task: str,
        request_id: str,
        session_id: str,
        initial_message: Optional[ChatMessage] = None,
        llm_override: Optional[LLMClient] = None,
        status_forwarder: Optional[StatusEventForwarder] = None
    ) -> ConversationContext:
        """Initialize request tracking and build initial conversation context.

        Phase 1 of agent execution: Setup all state needed for the LLM loop.

        Steps:
        1. Create cancellation token
        2. Register request for cancellation/appends
        3. Set context vars
        4. Initialize session storage
        5. Use pre-created status event forwarder (passed from caller)
        6. Validate LLM availability
        7. Initialize tool integration
        8. Discover usable tools
        9. Build tool schemas (the expanded tool list the prompt renders)
        10. Render system prompts
        11. Load session history
        12. Execute session start hooks

        Args:
            task: User task description
            request_id: Unique request identifier
            session_id: Session identifier for history
            initial_message: Optional multimodal message
            llm_override: Optional LLM client override
            status_forwarder: Pre-created status event forwarder (must be started before status_scope)

        Returns:
            ConversationContext with all initialized state

        Raises:
            RuntimeError: If no LLM is available
        """
        # Determine which LLM to use
        active_llm = llm_override if llm_override is not None else self.llm

        # Create cancellation token for the main request
        cancellation_manager = get_cancellation_manager()
        main_token = cancellation_manager.create_token(request_id)
        logger.debug("Created main cancellation token for request %s", request_id)

        # Set the ContextVar so any publish_status() calls without explicit request_id
        # will inherit the current request id. Store token for reset in finally.
        try:
            context_reset_token = current_request_id.set(request_id)
        except Exception as e:
            logger.debug(f"Failed to set current_request_id context var: {e}")
            context_reset_token = None
        # Whose run this is, for the calls in it that reach the hooks without an
        # agent (a decision, TTS: llm/hook_notify.py) -- also once the request
        # that started a background sub-agent has ended and let go of its tree.
        from ....core.request_context import current_run_user
        metadata = self._session_tracker.get_session_metadata(session_id) if self._session_tracker else None
        run_user = metadata.get("user_id") if isinstance(metadata, dict) else None
        user_reset_token = current_run_user.set(run_user) if run_user else None

        # Status forwarder is passed in from caller (created before status_scope context managers)
        # This ensures forwarder is subscribed to status_bus before any START events are generated
        # Fallback to creating one here for backwards compatibility (though this defeats the purpose)
        if status_forwarder is None:
            logger.warning("status_forwarder not passed to _initialize_request_and_conversation - creating one here (may miss events)")
            status_forwarder = StatusEventForwarder()
            await status_forwarder.start_forwarding(request_id)

        # If no LLM is configured, raise error
        if active_llm is None:
            raise RuntimeError("No LLM available; agent requires an LLM to run")

        # Initialize tool integration
        await self._tool_integration_manager.setup_tool_integration()
        await self._tool_integration_manager.connect_on_demand_servers()

        # Get tools this agent can use (filtered by agent_config)
        # Returns tuple: (tools, allowed_patterns, blocked_patterns)
        usable_tools, allowed_patterns, blocked_patterns = await self.list_usable_tools()

        # Build tool schemas using ToolSchemaBuilder
        # Pass allowed_patterns and blocked_patterns so they can be applied AFTER tools are expanded
        # Before the first render: `tools` in the prompt is the EXPANDED list
        # from here on, as in every step's re-render. Rendered from the
        # server-level list, the first prompt branched differently from the
        # ones sent (the session-start hooks saw that one).
        schema_builder = ToolSchemaBuilder(
            agent_name=self.name,
            tool_integration_manager=self._tool_integration_manager,
            server_getter_func=self._get_server_from_any_registry
        )

        tools_schema, tool_name_mapping, usable_tools, display_tools = await schema_builder.build_schemas(
            usable_tools,
            allowed_patterns=allowed_patterns,
            blocked_patterns=blocked_patterns
        )
        deferred_tools = DeferredTools.split(tools_schema, tool_name_mapping, self._deferred_patterns(), self.name)

        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))

        # Centralized prompt rendering (system + optional tools) using helper.
        # Initial render with step 0 (before loop starts)
        # Pass session_id to use session-scoped template vars
        system_msg, tools_msg = self._render_prompts(usable_tools, max_steps, current_step=0, session_id=session_id)

        # Initialize conversation from persisted session history
        session_msgs = self._session_tracker.get_session_messages(session_id)

        # Create initial system messages
        messages = [ChatMessage(role="system", content=system_msg)]
        if tools_msg:
            messages.append(ChatMessage(role="system", content=tools_msg))

        # Execute session start hooks for new sessions AFTER creating system messages
        # This allows hooks to inject additional system prompts
        # A session starts once: one taken back to no messages (/undo) is not new again
        if self._session_tracker.start_session(session_id):
            modified_messages = await self._hook_manager.execute_session_start_hooks(
                session_id, request_id, messages=messages
            )
            if modified_messages is not None:
                messages = modified_messages
                logger.debug(f"Session start hooks modified messages: {len(messages)} total messages")

        # include persisted session messages
        if session_msgs:
            # Convert dicts to ChatMessage objects if needed. Persisted
            # history may predate history_safe_tool_calls (or was written by
            # an older build) -- sanitize on load, or a session poisoned by
            # invalid arguments JSON stays dead on every resume.
            for msg in session_msgs:
                if isinstance(msg, dict):
                    if msg.get("tool_calls"):
                        msg = {**msg, "tool_calls":
                               history_safe_tool_calls(msg["tool_calls"])}
                    messages.append(ChatMessage(**msg))
                else:
                    if getattr(msg, "tool_calls", None):
                        msg.tool_calls = history_safe_tool_calls(msg.tool_calls)
                    messages.append(msg)

        # add the new user input as last message
        # Use initial_message if provided (for multimodal input), otherwise create from task
        opening = initial_message or ChatMessage(
            role="user", content=sanitize_for_llm(task), timestamp=datetime.now(timezone.utc))
        # The run's own id on the first message it stores (ChatMessage.request_id): a
        # session read back tells its runs apart by it, and finds a sub-agent's run
        # under the call that started it.
        opening.request_id = request_id
        messages.append(opening)

        # Also include any appended messages already queued for this request
        messages = await self._session_tracker.drain_appended_messages(request_id, messages)

        # Track live messages for this session (request-scoped)
        self._set_live_messages(session_id, messages.copy())

        if deferred_tools is not None:
            deferred_tools.restore(messages, tools_schema)

        # Track current tool schemas per-session for token estimation by hooks.
        # The same list the run sends: a tool loaded later is counted too.
        self._set_live_tools_schema(session_id, tools_schema,
                                    held_back=deferred_tools.schemas() if deferred_tools is not None else None)

        # Return initialized context
        return ConversationContext(
            messages=messages,
            available_tools=usable_tools,  # Use usable_tools for validation (includes both server names and tool names)
            tools_schema=tools_schema,
            tool_name_mapping=tool_name_mapping,
            max_steps=max_steps,
            main_token=main_token,
            context_reset_token=context_reset_token,
            status_forwarder=status_forwarder,
            session_id=session_id,
            user_reset_token=user_reset_token,
            deferred_tools=deferred_tools,
        )

    async def _finalize_request(
        self: Agent,
        request_id: str,
        session_id: str,
        status_coordinator: StatusScope,
        status_worker: StatusScope,
        context: Optional[ConversationContext],
        messages: Optional[List[ChatMessage]],
        results: Dict[str, Any],
        step: int,
        checkpoint_loop: Optional[asyncio.Task] = None,
    ) -> None:
        """Finalize request and clean up resources.

        Phase 3 of agent execution: Cleanup and persistence.

        Steps:
        1. Execute session end hooks
        2. Unregister cancellation token
        3. Clean up request tracking
        4. Persist session messages (conversation history only)
        5. Shutdown tool integration
        6. Reset context vars
        7. Publish final status events
        8. Stop status forwarding
        9. Yield final pending status events

        Args:
            request_id: Request identifier
            session_id: Session identifier
            status_coordinator: Coordinator status scope
            status_worker: Worker status scope
            context: Conversation context (if initialized)
            messages: Final conversation messages
            results: Execution results dictionary
            step: Final step number
            checkpoint_loop: The checkpoint loop this run started (_start_checkpoint_loop)
        """
        # First, before anything that awaits: a cancellation there would leave the
        # finished run's model registered for the session's next /compact.
        self._step_llms.pop(session_id, None)

        # The session this run holds, looked up first.
        sid = self._session_tracker.get_session_for_request(request_id)

        # Stop the background checkpoint loop BEFORE the final save. The loop
        # does its own load-modify-save every ~30s; if it overlaps the final
        # save it can resume after we persist and write its older, trimmed
        # snapshot over the newer one (silent message loss). Cancelling and
        # awaiting the task here guarantees any in-flight checkpoint write has
        # completed, so the final save below writes last and wins.
        # The loop this run started and no other: a run nested in another on the
        # same session would otherwise stop that one's. And first,
        # before anything else awaits: stop_checkpoint_loop cancels it and takes
        # it out of the registry before its own first await, so no cancel landing
        # later leaves it running.
        if checkpoint_loop is not None and self._session_service:
            try:
                await self._session_service.stop_checkpoint_loop(sid or session_id, started=checkpoint_loop)
            except Exception as e:
                logger.debug(f"Failed to stop checkpoint loop for {session_id} before final save: {e}")

        # The session lock is let go of after the save below, not before it -- and
        # in the finally, whatever cuts the steps up to it short: skipped, the
        # session would stay owned by a run that is gone and refuse every later
        # request on it until restart.
        persisted = False
        cancelled = False
        try:
            # Flush injected user messages that arrived too late to be processed
            # (e.g. during the very last LLM call) into the conversation so they
            # persist with the final save instead of being dropped with the request
            # entry. They are answered by the next run on this session.
            if messages is not None:
                try:
                    messages = await self._take_in_late_messages(request_id, session_id, messages)
                except Exception as e:
                    logger.debug("Failed to flush appended messages for %s: %s", request_id, e)
            elif sid:
                # No conversation: the run failed on its way in, after its request was registered. A
                # message handed to it meanwhile went with the request entry -- answered "appended",
                # and gone. Into the session as the tracker holds it, and saved with it.
                try:
                    held = list(self._session_tracker.get_session_messages(sid))
                    taken = await self._take_in_late_messages(request_id, session_id, list(held))
                    if len(taken) > len(held):
                        messages = taken
                except Exception as e:  # noqa: BLE001 - as the flush above: nothing here may keep the lock
                    logger.debug("Failed to flush appended messages for %s: %s", request_id, e)

            # Clean up cancellation token -- read first: the session end hooks
            # are told whether the run was cancelled, and after this the token
            # is gone. A crash recorded the state from before it cancelled the
            # token itself.
            cancellation_manager = get_cancellation_manager()
            run_token = cancellation_manager.get_token(request_id)
            cancelled = (bool(results["cancelled"]) if "cancelled" in results
                         else bool(run_token is not None and run_token.is_cancelled))
            cancellation_manager.unregister_request(request_id)

            # Clean up request tracking but preserve session data
            self._request_manager.unregister_active_request(request_id)
            logger.debug("Cleaned up request tracking for %s", request_id)

            # Persist session messages and keep the request->session mapping for a while.
            # Under the session lock: let go of before this save, a request of this
            # process could open the session in between -- read it from disk, where
            # this run's last exchange was not yet -- and its run saved over it.
            if sid and messages:
                try:
                    # Check if ANY tool modified the session messages during this request
                    # Tools can call session_tracker.set_compacted_messages() to replace the history
                    compacted_msgs = self._session_tracker.get_compacted_messages(sid)

                    if compacted_msgs is not None:
                        # A tool replaced the message history - use those messages for
                        # persistence (already conversation-only, no filtering needed)
                        logger.debug(
                            f"Using {len(compacted_msgs)} tool-modified messages for session {sid} "
                            f"(request had {len(messages)} messages)"
                        )
                        self._session_tracker.set_session_messages(sid, compacted_msgs)
                        self._session_tracker.clear_compacted_messages(sid)
                        # Save to disk even if no SSE client is connected
                        # (e.g., browser disconnected during background job execution)
                        persisted = await self._save_session_to_disk(sid)
                    else:
                        # Normal case: persist the request's conversation messages
                        persisted = await self._persist_conversation(
                            sid, messages, to_disk=True, note="at end of request")

                    # Keep the request->session mapping (don't pop it immediately)
                    # This allows append requests that arrive shortly after completion to find the session
                except Exception as e:
                    logger.warning(f"Failed to persist session {sid}: {e}", exc_info=True)
        finally:
            # The lock goes once the conversation is on disk: a free lock then means
            # "saved", and the next request of this session reads it whole. In a
            # finally: a save cut short (the run cancelled) must not leave the
            # session locked for the life of the process. The session end hooks
            # after it only read the conversation.
            if sid:
                await self._session_tracker.release_session_lock(sid, request_id)
                logger.debug("Released session lock for %s (request %s)", sid, request_id)

        # Session end hooks AFTER the save, and told whether it happened. A hook
        # that counts what the request carried as delivered -- debate_forum does
        # that for direct messages -- would otherwise count it while the
        # conversation is still only in memory: a save that fails or is
        # cancelled would take the message with it and nothing would re-deliver
        # it. They read the conversation, none of them writes it, so running
        # them after the save changes nothing else.
        # How the run ended goes with it: an observer (telemetry) cannot see
        # the run's events, only the hooks.
        try:
            await self._hook_manager.execute_session_end_hooks(
                session_id, request_id, messages=messages, persisted=persisted,
                cancelled=cancelled, errors=list(results.get("errors") or []),
                completed="summary" in results,
            )
        except Exception as e:
            logger.warning(f"Session end hooks failed: {e}", exc_info=True)

        # NOTE: no MCP shutdown here. The integration is process-wide state;
        # tearing it down at the end of EVERY request broke bootstrap-only
        # processes (writer pipelines) after their first request, because
        # ToolServerIntegration.shutdown() stops all plugins but leaves
        # `initialized` True -- so the next request found a half-dead
        # integration and never re-initialized it. Shutdown belongs to the
        # process entry point (shutdown_tools), never to an agent.

        # Reset the current_request_id ContextVar so it doesn't leak to other tasks
        if context and context.context_reset_token is not None:
            try:
                current_request_id.reset(context.context_reset_token)
            except Exception:
                pass
        if context and context.user_reset_token is not None:
            from ....core.request_context import current_run_user
            try:
                current_run_user.reset(context.user_reset_token)
            except Exception:
                pass

        # Signal completion using status contexts
        # Note: step is already 1-indexed (extracted from events that use step+1)
        final_msg = "completed" if not results.get('errors') else "completed with errors"
        step_display = step if step > 0 else 1  # Ensure at least 1 step shown

        if results.get('errors'):
            await status_worker.error(final_msg, meta={"summary": results.get('summary')})
            await status_coordinator.error(f"{final_msg} ({step_display} steps)", meta={"summary": results.get('summary')})
        else:
            await status_worker.end(final_msg, meta={"summary": results.get('summary')})
            await status_coordinator.end(f"{final_msg} ({step_display} steps)", meta={"summary": results.get('summary')})

        # Give a small delay to allow final status events to be processed by the forwarding task
        await asyncio.sleep(0.01)

        # Clean up status forwarding task AFTER publishing final status
        if context and context.status_forwarder:
            await context.status_forwarder.stop_forwarding()
