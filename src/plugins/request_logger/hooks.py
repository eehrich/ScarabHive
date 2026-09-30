"""Request Logger Plugin - Schema-based hooks plugin.

Writes one log line per agent LLM call (before and after) and one at the
start of a session and at the end of every run, through the process's
normal Python logging.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Tuple

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult

logger = logging.getLogger(__name__)

#: Runs tracked at once (from their first LLM call to their session_end).
_MAX_RUNS = 1000


def _response_text(llm_response: Any) -> str:
    """The assistant text of the agent loop's LLM result ({"assistant": {...}})."""
    if isinstance(llm_response, dict):
        assistant = llm_response.get('assistant')
        if isinstance(assistant, dict):
            return str(assistant.get('content') or '')
        return str(llm_response.get('content') or '')
    return str(llm_response)


class RequestLoggerPlugin(SchemaBasedPluginHook):
    """Logs agent lifecycle events.

    Every hook gets its own copy of a fresh context, so nothing set on the
    context in log_pre_llm reaches log_post_llm. What the hooks share lives
    on the plugin, per run: ``_runs[(session_id, request_id)]``, created by
    the first LLM call of the run and removed when the run ends.
    """

    def __init__(self, plugin_dir: Path | str, server_config: Any = None):
        """Initialize the request logger plugin.

        Args:
            plugin_dir: Directory containing schema.yaml
            server_config: tool server configuration (its ``config:`` block overrides the schema defaults)
        """
        super().__init__(plugin_dir)
        self.request_count = 0
        self._runs: Dict[Tuple[Any, Any], Dict[str, Any]] = {}

        config = dict(self.get_config())
        if server_config and hasattr(server_config, 'config') and server_config.config:
            config.update(server_config.config)
        self.log_level = logging.getLevelNamesMapping().get(
            str(config.get('log_level', 'INFO')).upper(), logging.INFO)
        self.log_message_content = bool(config.get('log_message_content', True))
        self.log_timing = bool(config.get('log_timing', True))
        self.max_content_preview = min(1000, max(10, int(config.get('max_content_preview', 100))))

    def _preview(self, text: str) -> str:
        if len(text) > self.max_content_preview:
            return text[:self.max_content_preview] + "..."
        return text

    # Hook handler methods - names must match hook names in schema.yaml

    async def log_pre_llm(self, context: HookContext) -> HookResult:
        """Log LLM request before execution."""
        try:
            self.request_count += 1
            request_num = self.request_count
            now = time.time()
            key = (context.session_id, context.request_id)
            run = self._runs.get(key)
            if run is None:
                run = self._runs[key] = {'start': now, 'calls': 0}
                # A run whose session_end never fires would stay forever: keep
                # the newest _MAX_RUNS, drop the oldest first.
                while len(self._runs) > _MAX_RUNS:
                    del self._runs[next(iter(self._runs))]
            run['calls'] += 1
            run['call_start'] = now
            run['call_number'] = request_num

            msg_count = len(context.messages) if context.messages else 0
            logger.log(
                self.log_level,
                f"[RequestLogger] Request #{request_num} - Pre-LLM Call: "
                f"session={context.session_id}, messages={msg_count}, "
                f"agent={context.agent_name or 'unknown'}"
            )

            if self.log_message_content and context.messages:
                last_msg = context.messages[-1]
                logger.log(
                    self.log_level,
                    f"[RequestLogger] Last message: role={last_msg.role}, "
                    f"content={self._preview(str(last_msg.content))}"
                )

            # modified=False: nothing in the context was changed.
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'logged': True, 'request_number': request_num}
            )

        except Exception as e:
            logger.error(f"[RequestLogger] Error in log_pre_llm: {e}", exc_info=True)
            return HookResult(success=False, modified=False, context=context, error=str(e))

    async def log_post_llm(self, context: HookContext) -> HookResult:
        """Log LLM response after execution."""
        try:
            run = self._runs.get((context.session_id, context.request_id), {})
            request_num = run.get('call_number', '?')

            duration_ms = None
            if self.log_timing and run.get('call_start'):
                duration_ms = (time.time() - run['call_start']) * 1000

            log_msg = f"[RequestLogger] Request #{request_num} - Post-LLM Call"
            if duration_ms is not None:
                log_msg += f": duration={duration_ms:.2f}ms"
            if self.log_message_content and context.llm_response:
                log_msg += f", response_preview={self._preview(_response_text(context.llm_response))}"

            logger.log(self.log_level, log_msg)

            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'logged': True, 'duration_ms': duration_ms}
            )

        except Exception as e:
            logger.error(f"[RequestLogger] Error in log_post_llm: {e}", exc_info=True)
            return HookResult(success=False, modified=False, context=context, error=str(e))

    async def log_session_start(self, context: HookContext) -> HookResult:
        """Log the start of a new session (once per session)."""
        try:
            logger.log(
                self.log_level,
                f"[RequestLogger] Session Started: "
                f"session_id={context.session_id or 'unknown'}, agent={context.agent_name or 'unknown'}"
            )
            return HookResult(success=True, modified=False, context=context, metadata={'logged': True})

        except Exception as e:
            logger.error(f"[RequestLogger] Error in log_session_start: {e}", exc_info=True)
            return HookResult(success=False, modified=False, context=context, error=str(e))

    async def log_session_end(self, context: HookContext) -> HookResult:
        """Log the end of a run (the session_end hook fires after every run)."""
        try:
            run = self._runs.pop((context.session_id, context.request_id), {})

            log_msg = (f"[RequestLogger] Run Ended: session_id={context.session_id or 'unknown'}, "
                       f"llm_calls={run.get('calls', 0)}")
            duration_s = None
            if self.log_timing and run.get('start'):
                duration_s = time.time() - run['start']
                log_msg += f", duration={duration_s:.2f}s"

            logger.log(self.log_level, log_msg)

            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'logged': True, 'duration_s': duration_s}
            )

        except Exception as e:
            logger.error(f"[RequestLogger] Error in log_session_end: {e}", exc_info=True)
            return HookResult(success=False, modified=False, context=context, error=str(e))
