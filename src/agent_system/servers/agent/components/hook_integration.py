"""
Hook integration component for Agent lifecycle management.

Provides centralized hook execution at agent lifecycle points.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ....hooks import get_hook_registry, HookContext, HookType
from ....llm.models import ChatMessage
from .tool_execution import drop_runtime_params, tool_result_is_error

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


def _blocks(metadata: Dict[str, Any]) -> bool:
    """Whether a hook blocked the call: it set ``block`` to anything but None or
    False. An empty text blocks too -- a hook passing on a person's empty
    comment on a "no" must not let the call run."""
    block = metadata.get("block")
    return block is not None and block is not False


def _block_on_error(hook_name: str, metadata: Dict[str, Any], context: HookContext, reason: str) -> None:
    """A pre_tool_call hook registered with ``on_error: block`` failed: the call
    must not run on that account -- a policy hook that times out or crashes
    would otherwise let through what it exists to stop."""
    if metadata.get("on_error") == "block" and not _blocks(context.metadata):
        context.metadata["block"] = (
            f"The call did not run: the check '{hook_name}' that must pass first failed "
            f"({reason}). Tell the user; do not send the same call again.")


def _blocked_call_reason(block: Any, tool_name: Optional[str]) -> str:
    """What the model reads for a call a pre_tool_call hook blocked: the hook's
    own text, or a default for a hook that only said ``block: True``."""
    if isinstance(block, str) and block.strip():
        return block.strip()
    return (f"The call to '{tool_name}' was blocked before it ran; it did not "
            "execute. Do not send the same call again.")


#: System messages that are compacted CONVERSATION, not prompt. The agent's own
#: system prompt is rebuilt from config on every turn and must not be persisted;
#: these carry conversation state and would be lost for good.
#:   archived_ref  - stands where a message moved into the archive
#:   pruned_notice - the single breadcrumb naming how much left the view. It
#:                   keeps a running total, so dropping it resets the count
#:                   every turn and reports only the last round's share.
_COMPACTION_SYSTEM_TYPES = ("archived_ref", "pruned_notice")


def is_compaction_system_message(message: Any) -> bool:
    """Whether a system message carries compacted conversation and must persist.

    One definition for both places that filter system messages out of the
    conversation — the pre-LLM auto-sync and ``_persist_conversation``. Two
    hand-kept copies of a rule like this drift, and the drift is silent: the
    message simply stops coming back.
    """
    # Both shapes reach this: ChatMessage on the agent paths, plain dicts from
    # the compaction plugin's own tool path.
    content = (message.get("content") if isinstance(message, dict)
               else getattr(message, "content", None))
    if not isinstance(content, str):
        return False
    # Cheap reject before the parse — almost every message is ordinary text.
    # Deliberately NOT windowed to the first N characters: that would couple
    # correctness to JSON key order, and a single `sort_keys=True` somewhere
    # would push "type" past the window and silently drop every breadcrumb.
    if "_ref" not in content and "_notice" not in content:
        return False
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return (isinstance(parsed, dict)
            and parsed.get("type") in _COMPACTION_SYSTEM_TYPES)


class HookIntegrationManager:
    """
    Manages hook execution integration with agent lifecycle.
    
    Provides methods to execute hooks at various lifecycle points
    with proper context construction and error handling.
    """
    
    def __init__(self, agent: Agent):
        """
        Initialize hook integration manager.
        
        Args:
            agent: Agent instance this manager belongs to
        """
        self.agent = agent
        self.registry = get_hook_registry()
        self._enabled = True  # Can be disabled per-agent via config
        self._hooks_config = None  # Cached hooks config from agent_config
        
        # Load hooks config from agent if available
        if hasattr(self.agent, 'agent_config') and hasattr(self.agent.agent_config, 'hooks'):
            self._hooks_config = self.agent.agent_config.hooks
    
    def is_enabled(self) -> bool:
        """Check if hooks are globally enabled for this agent."""
        if self._hooks_config is not None:
            return self._hooks_config.enabled
        return self._enabled
    
    def is_hook_enabled(self, hook_name: str, default_enabled: bool = True) -> bool:
        """Check if a specific hook should execute for this agent.
        
        This method properly handles per-agent hook overrides:
        - If agent has explicit override: use it (ignores everything else)
        - If no override: use hook metadata default_enabled
        
        Args:
            hook_name: Full hook name (e.g., 'todo_management.inject_todo_tasks')
            default_enabled: Global enabled state from hook metadata (registry passes this)
            
        Returns:
            True if hook should execute, False otherwise
        """
        # Check for agent-specific override (highest priority)
        if self._hooks_config and hook_name in self._hooks_config.overrides:
            override = self._hooks_config.overrides[hook_name]
            if 'enabled' in override:
                # Agent has explicit override - use it regardless of global state
                return override.get('enabled', True)
        
        # No override - use global enabled state from metadata
        return default_enabled

    def user_of(self, session_id: str, request_id: str) -> Optional[str]:
        """Whose call this is: the user the session's run was opened for (the
        API and agent-cli both record it before the first step), else the user
        the request was registered under; None when neither names one."""
        tracker = getattr(self.agent, '_session_tracker', None)
        metadata = tracker.get_session_metadata(session_id) if tracker is not None and session_id else None
        user_id = metadata.get('user_id') if isinstance(metadata, dict) else None
        if user_id:
            return user_id
        from ....core.request_context import get_request_user
        return get_request_user(request_id, default=None) if request_id else None

    def wire_llm_hooks(self, llm_client: Any) -> None:
        """Wire LLM-client-level hooks to the given LLM client.
        
        Sets up callback functions on the LLM client that fire
        PRE_LLM_REQUEST and POST_LLM_RESPONSE hooks through the registry.
        This captures the exact API payloads sent/received at the transport level.
        
        Args:
            llm_client: An LLMClient instance to wire hooks into
        """
        if not self.is_enabled():
            return
        if llm_client is None:
            return
        if not hasattr(llm_client, 'set_llm_hooks'):
            logger.debug(f"LLM client {type(llm_client).__name__} does not support hooks")
            return
        
        agent_ref = self.agent
        manager = self
        registry = self.registry
        hook_filter = self.is_hook_enabled

        def _session_of(request_id: str) -> str:
            # The agent already maps every running request to its session.
            tracker = getattr(agent_ref, '_session_tracker', None)
            if tracker is None or not request_id:
                return ''
            return tracker.get_session_for_request(request_id) or ''

        async def _on_pre_request(info: Dict[str, Any]) -> None:
            """Callback invoked by LLM client before API request."""
            try:
                # Get current request_id from context var
                from ....tools.status import current_request_id
                req_id = current_request_id.get('') or ''
                context = HookContext(
                    hook_type=HookType.PRE_LLM_REQUEST,
                    request_id=req_id,
                    session_id=_session_of(req_id),
                    user_id=manager.user_of(_session_of(req_id), req_id),
                    agent=agent_ref,
                    agent_name=agent_ref.name if agent_ref else '',
                    llm_request_payload=info.get("payload"),
                    llm_provider=info.get("provider"),
                    llm_model=info.get("model"),
                    llm_request_url=info.get("url"),
                    llm_is_streaming=info.get("is_streaming", False),
                    metadata={"timestamp_ms": info.get("timestamp_ms", time.time() * 1000)},
                )
                await registry.execute_hooks(
                    HookType.PRE_LLM_REQUEST, context, hook_filter=hook_filter
                )
            except Exception as e:
                logger.debug(f"pre_llm_request hook error: {e}")
        
        async def _on_post_response(info: Dict[str, Any]) -> None:
            """Callback invoked by LLM client after API response."""
            try:
                from ....tools.status import current_request_id
                req_id = current_request_id.get('') or ''
                context = HookContext(
                    hook_type=HookType.POST_LLM_RESPONSE,
                    request_id=req_id,
                    session_id=_session_of(req_id),
                    user_id=manager.user_of(_session_of(req_id), req_id),
                    agent=agent_ref,
                    agent_name=agent_ref.name if agent_ref else '',
                    llm_response_data=info.get("response_data"),
                    llm_provider=info.get("provider"),
                    llm_model=info.get("model"),
                    llm_request_url=info.get("url"),
                    llm_duration_ms=info.get("duration_ms"),
                    llm_error=info.get("error"),
                    llm_usage=info.get("usage"),
                    llm_finish_reason=info.get("finish_reason"),
                    llm_is_streaming=info.get("is_streaming", False),
                    metadata={
                        "timestamp_ms": info.get("timestamp_ms", time.time() * 1000),
                        # The backend a gateway routed to (OpenRouter), as the
                        # client read it: streamed answers have no response_data.
                        "served_by": (info.get("routing") or {}).get("selected"),
                    },
                )
                await registry.execute_hooks(
                    HookType.POST_LLM_RESPONSE, context, hook_filter=hook_filter
                )
            except Exception as e:
                logger.debug(f"post_llm_response hook error: {e}")
        
        llm_client.set_llm_hooks(
            on_pre_request=_on_pre_request,
            on_post_response=_on_post_response,
        )
        logger.debug(f"Wired LLM hooks to {type(llm_client).__name__}")
    
    async def execute_pre_llm_hooks(
        self,
        messages: List[ChatMessage],
        step: int,
        request_id: str,
        session_id: str,
        llm: Optional[Any] = None,
        cancellation_token: Optional[Any] = None
    ) -> List[ChatMessage]:
        """
        Execute pre-LLM hooks.
        
        Args:
            messages: Current conversation messages
            step: Current execution step
            request_id: Request identifier
            session_id: Session identifier
            llm: LLM client instance
            cancellation_token: Optional cancellation token for graceful cancellation
            
        Returns:
            Potentially modified messages list
        """
        if not self.is_enabled():
            return messages
        
        # Provide the per-session tool schema via the context so token-estimating
        # hooks don't have to read the agent's (shared, racy) _current_tools_schema.
        tools_schema = None
        if hasattr(self.agent, "get_live_tools_schema"):
            tools_schema = self.agent.get_live_tools_schema(session_id)

        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id=request_id,
            session_id=session_id,
            user_id=self.user_of(session_id, request_id),
            agent=self.agent,
            agent_name=self.agent.name,
            messages=messages,
            tools_schema=tools_schema,
            step=step,
            llm=llm,
            cancellation_token=cancellation_token,
        )

        modified_context = await self.registry.execute_hooks(
            HookType.PRE_LLM_CALL,
            context,
            hook_filter=self.is_hook_enabled
        )
        
        # Return modified messages if hooks changed them
        if modified_context.messages is not None:
            result_messages = modified_context.messages
            
            # AUTO-SYNC: If hooks modified the messages, automatically update session tracker
            # This eliminates the need for plugins to call set_compacted_messages() manually.
            # The session tracker is updated so:
            # 1. Subsequent hooks in the same pre_llm_call chain see the updated messages
            # 2. The modified messages are persisted at end of request
            # 3. Tool execution and other components see the compacted state
            if result_messages is not messages:  # Identity check - different list means modified
                self._auto_sync_session_messages(session_id, result_messages)
            
            return result_messages
        return messages
    
    def _auto_sync_session_messages(
        self, 
        session_id: str, 
        messages: List[ChatMessage]
    ) -> None:
        """Automatically sync modified messages to session tracker.
        
        Called when pre_llm hooks modify the message list. This ensures:
        - Modified messages are persisted at end of request
        - Subsequent components see the updated state
        - Plugins don't need to manually call set_compacted_messages()
        
        Args:
            session_id: Session to update
            messages: The modified messages (may include system messages)
        """
        if not hasattr(self.agent, '_session_tracker') or not self.agent._session_tracker:
            logger.debug(
                f"[HookIntegration] No session tracker available, skipping auto-sync "
                f"for session {session_id}"
            )
            return

        # Filter out system messages - session tracker stores conversation only.
        # System messages are prepended fresh each turn from agent config; the
        # ones that are actually compacted conversation must survive.
        conversation_msgs = [
            msg for msg in messages
            if msg.role != 'system' or is_compaction_system_message(msg)
        ]
        
        # Use set_compacted_messages() which signals to _finalize_request 
        # that hooks modified the conversation and this version should be persisted
        self.agent._session_tracker.set_compacted_messages(session_id, conversation_msgs)
        
        logger.debug(
            f"[HookIntegration] Auto-synced {len(conversation_msgs)} messages to session "
            f"{session_id} (hooks modified conversation)"
        )
    
    async def execute_post_llm_hooks(
        self,
        messages: List[ChatMessage],
        llm_response: Dict[str, Any],
        step: int,
        request_id: str,
        session_id: str,
        llm: Optional[Any] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Execute post-LLM hooks.
        
        Args:
            messages: Current conversation messages
            llm_response: LLM response dictionary
            step: Current execution step
            request_id: Request identifier
            session_id: Session identifier
            llm: LLM client instance
            
        Returns:
            Tuple of (potentially modified LLM response, metadata dict)
            Metadata may include 'content_format' if hooks modified the output format
        """
        if not self.is_enabled():
            return llm_response, {}

        # Same per-session schema as the PRE_LLM_CALL path: token-estimating
        # post hooks (context_usage_tracker) read this field too.
        tools_schema = None
        if hasattr(self.agent, "get_live_tools_schema"):
            tools_schema = self.agent.get_live_tools_schema(session_id)

        context = HookContext(
            hook_type=HookType.POST_LLM_CALL,
            request_id=request_id,
            session_id=session_id,
            user_id=self.user_of(session_id, request_id),
            agent=self.agent,
            agent_name=self.agent.name,
            messages=messages,
            llm_response=llm_response,
            tools_schema=tools_schema,
            step=step,
            llm=llm,
        )
        
        modified_context = await self.registry.execute_hooks(
            HookType.POST_LLM_CALL, 
            context,
            hook_filter=self.is_hook_enabled
        )
        
        # Return modified LLM response and metadata if hooks changed it
        if modified_context.llm_response is not None:
            return modified_context.llm_response, modified_context.metadata
        return llm_response, {}
    
    def wants_hooks(self, hook_type: HookType) -> bool:
        """Whether any hook of this type would run for this agent.

        Asked before building a context the hooks would need, so an agent no
        hook of the type runs for pays nothing: no thinking collected for
        llm_progress, no tool call or result copied for the tool hooks.
        """
        if not self.is_enabled():
            return False
        for name, _hook, _order, metadata in self.registry._hooks.get(hook_type, ()):
            if self.is_hook_enabled(name, metadata.get("enabled", True)):
                return True
        return False

    def wants_llm_progress(self) -> bool:
        """Whether any llm_progress hook would run for this agent (checked
        before a streaming call)."""
        return self.wants_hooks(HookType.LLM_PROGRESS)

    async def execute_llm_progress_hooks(
        self,
        reasoning_text: str,
        reasoning_chars: int,
        previous_reasoning_chars: int,
        step: int,
        request_id: str,
        session_id: str,
        llm: Optional[Any] = None
    ) -> None:
        """Execute llm_progress hooks from inside a streaming call.

        Deliberately without messages (see HookType.LLM_PROGRESS) and without
        a return value: a hook cannot change a call that is already running.
        """
        if not self.is_enabled():
            return
        context = HookContext(
            hook_type=HookType.LLM_PROGRESS,
            request_id=request_id,
            session_id=session_id,
            user_id=self.user_of(session_id, request_id),
            agent=self.agent,
            agent_name=self.agent.name,
            step=step,
            llm=llm,
            reasoning_text=reasoning_text,
            reasoning_chars=reasoning_chars,
            previous_reasoning_chars=previous_reasoning_chars,
        )
        await self.registry.execute_hooks(
            HookType.LLM_PROGRESS,
            context,
            hook_filter=self.is_hook_enabled
        )

    async def execute_pre_tool_hooks(
        self,
        tool_call: Dict[str, Any],
        step: int,
        request_id: str,
        session_id: str,
        cancellation_token: Optional[Any] = None,
    ) -> Tuple[Any, Optional[str]]:
        """
        Execute pre-tool hooks for one tool call, before it runs.

        Callers ask ``wants_hooks(HookType.PRE_TOOL_CALL)`` first.

        Args:
            tool_call: ``{"id", "name", "server", "arguments", "source"}`` --
                the provider's call id (None outside the model's loop), the
                tool name the model called, the server that runs it (for an
                external MCP tool ``"<server>.<tool>"``), the arguments as they
                were sent, and who made the call ("model", or the in-process
                caller acting for it, e.g. "tool_script")
            step: Current execution step (0 outside the model's loop)
            request_id: Request identifier
            session_id: Session identifier
            cancellation_token: The run's token -- a hook that waits (for a
                person, say) stops waiting when the run is cancelled

        Returns:
            (arguments, block). ``arguments``: what the call runs with -- a
            hook changes them by writing ``tool_call["arguments"]`` (a dict)
            and returning ``modified=True``; the name and id are read-only.
            ``block``: the text the model reads in place of a result when a
            hook blocked the call with ``metadata={"block": "<what to do>"}``
            (``True`` gets a default text), else None. The first hook that
            blocks ends the chain: a later one neither runs nor lifts it.
        """
        arguments = tool_call.get("arguments")
        name = tool_call.get("name")
        if not self.is_enabled():
            return arguments, None

        tools_schema = None
        if hasattr(self.agent, "get_live_tools_schema"):
            tools_schema = self.agent.get_live_tools_schema(session_id)

        context = HookContext(
            hook_type=HookType.PRE_TOOL_CALL,
            request_id=request_id,
            session_id=session_id,
            user_id=self.user_of(session_id, request_id),
            agent=self.agent,
            agent_name=self.agent.name,
            tool_call=tool_call,
            tools_schema=tools_schema,
            step=step,
            cancellation_token=cancellation_token,
        )

        modified_context = await self.registry.execute_hooks(
            HookType.PRE_TOOL_CALL,
            context,
            hook_filter=self.is_hook_enabled,
            stop_when=lambda ctx: _blocks(ctx.metadata),
            on_failure=_block_on_error,
        )

        if _blocks(modified_context.metadata):
            return arguments, _blocked_call_reason(modified_context.metadata["block"], name)

        changed = (modified_context.tool_call or {}).get("arguments", arguments)
        if changed is arguments:
            return arguments, None
        if not isinstance(changed, dict):
            logger.warning(
                "pre_tool_call hooks left arguments of type %s for %s; "
                "the call runs with the arguments it was sent with",
                type(changed).__name__, name)
            return arguments, None
        # A hook passes values on; the runtime params stay the framework's.
        changed, dropped = drop_runtime_params(changed)
        if dropped:
            logger.warning("Dropping runtime param(s) %s a pre_tool_call hook put into %s", dropped, name)
        return changed, None

    async def execute_post_tool_hooks(
        self,
        tool_call: Dict[str, Any],
        tool_result: Any,
        step: int,
        request_id: str,
        session_id: str,
        cancellation_token: Optional[Any] = None,
    ) -> Any:
        """
        Execute post-tool hooks for one call that ran.

        Callers ask ``wants_hooks(HookType.POST_TOOL_CALL)`` first.

        Args:
            tool_call: The call as it ran (see execute_pre_tool_hooks)
            tool_result: What the caller reads: in the model's loop the
                content of the result message, decoded -- the tool's result,
                or the error the framework put in its place (failed,
                cancelled); for tool_script the tool's return value
            step: Current execution step (0 outside the model's loop)
            request_id: Request identifier
            session_id: Session identifier
            cancellation_token: The run's token

        Returns:
            The result the caller reads. The hooks see
            ``context.tool_result = {"result": <value>, "is_error": <bool>}``;
            a hook changes the result by writing ``tool_result["result"]`` and
            returning ``modified=True`` -- ``is_error`` is read-only.
        """
        if not self.is_enabled():
            return tool_result

        context = HookContext(
            hook_type=HookType.POST_TOOL_CALL,
            request_id=request_id,
            session_id=session_id,
            user_id=self.user_of(session_id, request_id),
            agent=self.agent,
            agent_name=self.agent.name,
            tool_call=tool_call,
            tool_result={"result": tool_result, "is_error": tool_result_is_error(tool_result)},
            step=step,
            cancellation_token=cancellation_token,
        )

        modified_context = await self.registry.execute_hooks(
            HookType.POST_TOOL_CALL,
            context,
            hook_filter=self.is_hook_enabled
        )

        if modified_context is context or not isinstance(modified_context.tool_result, dict):
            return tool_result
        return modified_context.tool_result.get("result", tool_result)
    
    async def execute_format_output_hooks(
        self,
        output: str,
        request_id: str,
        session_id: str,
        output_format: str = "html"
    ) -> tuple[str, str]:
        """
        Execute format-output hooks.
        
        Args:
            output: Final output string
            request_id: Request identifier
            session_id: Session identifier
            output_format: Target format ('html', 'ansi', 'text', 'markdown')
            
        Returns:
            Tuple of (formatted_output, content_format)
            content_format indicates actual format of returned content
        """
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"execute_format_output_hooks called: output_format={output_format}, output_length={len(output)}")
        
        if not self.is_enabled():
            return output, 'text'
        
        context = HookContext(
            hook_type=HookType.FORMAT_OUTPUT,
            request_id=request_id,
            session_id=session_id,
            user_id=self.user_of(session_id, request_id),
            agent=self.agent,
            agent_name=self.agent.name,
            output=output,
            output_format=output_format,
        )
        
        logger.info(f"HookContext created: output_format={context.output_format}")
        
        modified_context = await self.registry.execute_hooks(
            HookType.FORMAT_OUTPUT, 
            context,
            hook_filter=self.is_hook_enabled
        )
        
        # Check if any hook indicated HTML format in metadata
        content_format = modified_context.metadata.get('content_format', 'text')
        
        # Return modified output if hooks changed it
        if modified_context.output is not None:
            return modified_context.output, content_format
        return output, content_format
    
    async def execute_session_start_hooks(
        self,
        session_id: str,
        request_id: str,
        messages: Optional[List[ChatMessage]] = None
    ) -> Optional[List[ChatMessage]]:
        """
        Execute session-start hooks.
        
        Args:
            session_id: Session identifier
            request_id: Request identifier
            messages: Optional initial messages
            
        Returns:
            Potentially modified messages (for system prompt injection)
        """
        if not self.is_enabled():
            return messages
        
        context = HookContext(
            hook_type=HookType.SESSION_START,
            request_id=request_id,
            session_id=session_id,
            user_id=self.user_of(session_id, request_id),
            agent=self.agent,
            agent_name=self.agent.name,
            messages=messages,
        )
        
        modified_context = await self.registry.execute_hooks(
            HookType.SESSION_START, 
            context,
            hook_filter=self.is_hook_enabled
        )
        
        # Return modified messages if hooks changed them
        return modified_context.messages
    
    async def execute_session_end_hooks(
        self,
        session_id: str,
        request_id: str,
        messages: Optional[List[ChatMessage]] = None,
        persisted: bool = False
    ) -> None:
        """
        Execute session-end hooks.
        
        Args:
            session_id: Session identifier
            request_id: Request identifier
            messages: Optional final messages
            persisted: Whether this request's conversation reached the session
                file. A hook that counts what the request carried as delivered
                may only do so when it did -- what an unsaved run was told is
                gone with it. False unless the caller knows better.
        """
        if not self.is_enabled():
            return
        
        context = HookContext(
            hook_type=HookType.SESSION_END,
            request_id=request_id,
            session_id=session_id,
            user_id=self.user_of(session_id, request_id),
            agent=self.agent,
            agent_name=self.agent.name,
            messages=messages,
            metadata={"persisted": persisted},
        )
        
        await self.registry.execute_hooks(
            HookType.SESSION_END, 
            context,
            hook_filter=self.is_hook_enabled
        )
