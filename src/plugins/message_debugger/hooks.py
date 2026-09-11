"""Message Debugger Plugin - Hooks implementation.

Captures and stores LLM conversation turns and raw API request/response logs
in a SQLite database for debugging and inspection.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.token_utils import estimate_token_count
from agent_system.llm.models import ChatMessage

if TYPE_CHECKING:
    from .database import MessageDebuggerDB

logger = logging.getLogger(__name__)

# Message fields a snapshot always keeps whole. Everything else a message
# carries is stored too, cut to a preview when long (see _compact).
_WHOLE_FIELDS = frozenset({'content', 'tool_calls'})


def _compact(value: Any, limit: int) -> Any:
    """Copy of a JSON-shaped value with every string longer than ``limit`` cut
    to a preview that names its full length; ``limit`` 0 keeps everything.

    Snapshots repeat the whole history on every call of a run, so an opaque
    blob (encrypted reasoning, base64 media) would be stored many times over.
    """
    if isinstance(value, str):
        if limit and len(value) > limit:
            return f"{value[:limit]}... [{len(value)} chars]"
        return value
    if isinstance(value, dict):
        return {key: _compact(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(item, limit) for item in value]
    return value


class MessageDebuggerPlugin(SchemaBasedPluginHook):
    """Schema-based plugin for capturing and debugging LLM messages.
    
    Hooks into:
    - pre_llm_call / post_llm_call: Capture agent-level message snapshots (turns)
    - pre_llm_request / post_llm_response: Capture raw API request/response data
    
    All data is stored in a SQLite database for persistence.
    """
    
    def __init__(
        self,
        plugin_dir: Path | str,
        db: Optional[MessageDebuggerDB] = None,
        message_history: Optional[List[Dict[str, Any]]] = None,
        mcp_config: Any = None,
    ):
        """Initialize the message debugger plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
            db: SQLite database instance for persistent storage
            message_history: Legacy list (kept for backward compat)
            mcp_config: MCP configuration
        """
        super().__init__(plugin_dir)
        
        self.db = db
        # Keep legacy list reference for backward compat
        self.message_history = message_history if message_history is not None else []
        
        # Load config
        config = self.get_config()
        if mcp_config and hasattr(mcp_config, 'config') and mcp_config.config:
            config.update(mcp_config.config)
        
        self.capture_enabled = bool(config.get('capture_enabled', True))
        self.capture_pre_llm = bool(config.get('capture_pre_llm', True))
        self.capture_post_llm = bool(config.get('capture_post_llm', True))
        self.capture_llm_requests = bool(config.get('capture_llm_requests', True))
        self.include_tool_calls = bool(config.get('include_tool_calls', True))
        self.include_token_estimates = bool(config.get('include_token_estimates', True))
        self.max_field_chars = int(config.get('max_field_chars', 500))
        self.max_history = int(config.get('max_history_entries', 100))
        self.auto_cleanup_threshold = int(config.get('auto_cleanup_threshold', 150))
        
        logger.info(
            f"MessageDebuggerPlugin initialized: db={'YES' if db else 'NO'}, "
            f"capture_enabled={self.capture_enabled}, "
            f"capture_llm_requests={self.capture_llm_requests}"
        )
    
    # ---- Agent-level hooks (turns) ----
    
    async def debugger_capture_pre_llm(self, context: HookContext) -> HookResult:
        """Capture messages before LLM call (input snapshot)."""
        if not self.capture_enabled or not self.capture_pre_llm:
            return HookResult(success=True, modified=False, context=context,
                              metadata={'reason': 'capture_disabled'})
        return await self._capture_turn(context, snapshot_type='pre_llm')
    
    async def debugger_capture_post_llm(self, context: HookContext) -> HookResult:
        """Capture LLM response after LLM call (output snapshot)."""
        if not self.capture_enabled or not self.capture_post_llm:
            return HookResult(success=True, modified=False, context=context,
                              metadata={'reason': 'capture_disabled'})
        return await self._capture_turn(context, snapshot_type='post_llm')
    
    # ---- LLM-client-level hooks (raw API) ----
    
    async def debugger_capture_pre_request(self, context: HookContext) -> HookResult:
        """Capture raw API request payload before sending to LLM provider."""
        if not self.capture_enabled or not self.capture_llm_requests:
            return HookResult(success=True, modified=False, context=context)
        
        try:
            if self.db:
                ts = context.metadata.get('timestamp_ms', time.time() * 1000)
                # Fire-and-forget: hand the json.dumps + commit to the DB's
                # background writer thread and return immediately. We must NOT
                # await it — awaiting blocks THIS agent's coroutine on the (possibly
                # multi-minute) write against a multi-GB DB, even though the event
                # loop stays free for other requests.
                self.db.submit_llm_request(
                    timestamp_ms=ts,
                    direction='request',
                    agent_name=context.agent_name or '',
                    request_id=context.request_id or '',
                    session_id=context.session_id or '',
                    provider=context.llm_provider or '',
                    model=context.llm_model or '',
                    url=context.llm_request_url or '',
                    is_streaming=context.llm_is_streaming,
                    payload=context.llm_request_payload,
                )
                logger.debug(
                    f"Captured pre_llm_request: provider={context.llm_provider}, "
                    f"model={context.llm_model}, streaming={context.llm_is_streaming}"
                )
        except Exception as e:
            logger.warning(f"Failed to capture pre_llm_request: {e}")
        
        return HookResult(success=True, modified=False, context=context)
    
    async def debugger_capture_post_response(self, context: HookContext) -> HookResult:
        """Capture raw API response data after receiving from LLM provider."""
        if not self.capture_enabled or not self.capture_llm_requests:
            return HookResult(success=True, modified=False, context=context)
        
        try:
            if self.db:
                ts = context.metadata.get('timestamp_ms', time.time() * 1000)
                # Fire-and-forget onto the DB's background writer (see pre_request).
                self.db.submit_llm_request(
                    timestamp_ms=ts,
                    direction='response',
                    agent_name=context.agent_name or '',
                    request_id=context.request_id or '',
                    session_id=context.session_id or '',
                    provider=context.llm_provider or '',
                    model=context.llm_model or '',
                    url=context.llm_request_url or '',
                    is_streaming=context.llm_is_streaming,
                    response_data=context.llm_response_data,
                    error=context.llm_error,
                    duration_ms=context.llm_duration_ms,
                    usage=context.llm_usage,
                    finish_reason=context.llm_finish_reason,
                )
                logger.debug(
                    f"Captured post_llm_response: provider={context.llm_provider}, "
                    f"model={context.llm_model}, "
                    f"duration={context.llm_duration_ms:.0f}ms" if context.llm_duration_ms else
                    f"Captured post_llm_response: provider={context.llm_provider}"
                )
        except Exception as e:
            logger.warning(f"Failed to capture post_llm_response: {e}")
        
        return HookResult(success=True, modified=False, context=context)
    
    # ---- Internal helpers ----
    
    async def _capture_turn(self, context: HookContext, snapshot_type: str) -> HookResult:
        """Capture an agent-level message snapshot (turn)."""
        try:
            # Snapshot the message list ON the event loop so the writer thread
            # iterates a private copy — immune to any concurrent mutation of the
            # live list (no "list changed size during iteration").
            messages = list(context.messages or [])
            if not messages:
                return HookResult(
                    success=True, modified=False, context=context,
                    metadata={'reason': 'no_messages', 'snapshot_type': snapshot_type}
                )

            # Snapshot the context fields we need as plain values, then hand the
            # heavy build (token estimation + json.dumps) AND the write to the DB's
            # background writer. We do NOT await it: the agent must never block on
            # a slow debug DB. Plain values (not the live context) keep the
            # deferred build race-free.
            ctx = {
                'agent_name': context.agent_name or '',
                'request_id': context.request_id or '',
                'session_id': context.session_id or '',
                'step': context.step,
                # Compacted here, on the loop: _compact copies, so the writer
                # thread never reads a dict the agent is still changing.
                'llm_response': (_compact(context.llm_response, self.max_field_chars)
                                 if snapshot_type == 'post_llm' and context.llm_response
                                 else None),
                'context_window': (context.llm.context_window
                                   if context.llm and hasattr(context.llm, 'context_window')
                                   else None),
            }
            if self.db:
                self.db.submit(lambda: self._build_and_store_turn(messages, ctx, snapshot_type))

            return HookResult(
                success=True, modified=False, context=context,
                metadata={'queued': True, 'snapshot_type': snapshot_type}
            )

        except Exception as e:
            logger.exception(f"Failed to capture {snapshot_type} turn: {e}")
            return HookResult(
                success=True, modified=False, context=context,
                metadata={'error': str(e), 'snapshot_type': snapshot_type}
            )

    def _build_and_store_turn(
        self, messages: List[Any], ctx: Dict[str, Any], snapshot_type: str
    ) -> tuple[int, int]:
        """Build the message snapshot (token estimation) and persist it to SQLite.

        Synchronous — runs on the DB's background writer thread (via db.submit),
        so the heavy per-message token estimation, json.dumps and commit never
        block the agent. ``messages`` is a snapshot list and ``ctx`` a snapshot of
        plain context values, both taken on the event loop (see _capture_turn).
        Returns (message_count, total_tokens).
        """
        message_data = []
        total_tokens = 0

        for idx, msg in enumerate(messages):
            if not isinstance(msg, ChatMessage):
                try:
                    msg = ChatMessage(**msg)
                except (TypeError, ValueError):
                    continue

            msg_tokens = 0
            if self.include_token_estimates:
                msg_tokens = estimate_token_count([msg])
                total_tokens += msg_tokens

            # Every field the message carries, not a hand-picked list: a field
            # added to ChatMessage shows up here and in the panel untouched.
            msg_info: Dict[str, Any] = {
                key: value if key in _WHOLE_FIELDS else _compact(value, self.max_field_chars)
                for key, value in msg.model_dump(mode='json', exclude_none=True).items()
            }
            if not self.include_tool_calls:
                msg_info.pop('tool_calls', None)
                msg_info.pop('tool_call_id', None)
            msg_info.update({
                'index': idx,
                'content_length': len(str(msg.content)) if msg.content else 0,
                'estimated_tokens': msg_tokens if self.include_token_estimates else None,
            })
            if self.include_tool_calls:
                msg_info['tool_call_count'] = len(msg.tool_calls or [])
                msg_info['is_tool_result'] = bool(msg.tool_call_id)

            message_data.append(msg_info)

        # The whole response, already compacted on the loop (_capture_turn).
        llm_response = ctx.get('llm_response') if snapshot_type == 'post_llm' else None

        timestamp_ms = time.time() * 1000

        if self.db:
            self.db.insert_turn(
                timestamp_ms=timestamp_ms,
                snapshot_type=snapshot_type,
                agent_name=ctx.get('agent_name', ''),
                request_id=ctx.get('request_id', ''),
                session_id=ctx.get('session_id', ''),
                step=ctx.get('step', 0),
                message_count=len(message_data),
                total_tokens=total_tokens,
                context_window=ctx.get('context_window'),
                messages=message_data,
                llm_response=llm_response,
            )

        return len(message_data), total_tokens
