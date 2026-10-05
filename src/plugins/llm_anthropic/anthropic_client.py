"""
Anthropic Claude Client - Native SDK Implementation

Native implementation using the official anthropic SDK with full feature support:
- Full streaming support with tool calls
- Extended thinking (Claude reasoning)
- Prompt caching for cost optimization
- Image/vision support
- Native error handling

Key features over OpenAI-compatibility layer:
- Prompt caching (70-80% cost savings on repeated prompts)
- Extended thinking with full reasoning output
- Native beta features via custom headers
- Better error messages and handling
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from agent_system.llm.message_roles import (
    DEVELOPER, NOTE_CLOSE, NOTE_OPEN, USER, developer_turn, resolve_rung,
)
from agent_system.llm.models import (
    ChatMessage, LLMClient, LLMConnectionError, LLMQuotaExhaustedError, LLMRateLimitError,
    LLMServerError,
)
from agent_system.llm.structured_output import JSON_SCHEMA, ResponseFormat
from agent_system.llm.retry_utils import parse_retry_delay
from agent_system.llm.tls import httpx_verify

from . import anthropic_utils
from plugins.llm_common import cancellation
from plugins.llm_common.model_dialects import (
    reasoning_replay_flags,
    resolve_reasoning_details_mode,
    resolve_thinking_request_shape,
)
from agent_system.llm.cache_key import (
    CACHE_BP_SENTINEL,
    anthropic_cache_conversation,
    cap_cache_control,
    mark_conversation_tail,
    mark_last_text_block,
    mark_last_tool,
    messages_have_history,
    strip_cache_breakpoints,
)
from agent_system.utils.json_utils import repair_json

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://api.anthropic.com"
#: Longest retry-after (seconds) this client sleeps itself; a longer one goes to the server.
_MAX_RATE_LIMIT_WAIT = 60.0


def _ending_error(error: BaseException) -> str:
    """The post_llm_response error text for a request that ended by *error*."""
    if isinstance(error, asyncio.CancelledError):
        return str(error) or "cancelled"
    if isinstance(error, GeneratorExit):
        return "stream abandoned by the caller"
    return str(error) or type(error).__name__


def _error_type(error: BaseException) -> Optional[str]:
    """The API's own error type (``overloaded_error``, ...) from the body the SDK kept."""
    body = getattr(error, "body", None)
    detail = body.get("error") if isinstance(body, dict) else None
    return detail.get("type") if isinstance(detail, dict) else None


def _error_kind(error: BaseException) -> Optional[str]:
    """rate_limit, server, connection or refused -- None for anything that is no API failure.

    Decided by status and error type, not by the message: an error event inside a
    stream arrives with the stream's status 200 and names its kind only in the body.
    """
    import anthropic

    status = getattr(error, "status_code", None) or 0
    kind = _error_type(error)
    if status == 429 or kind == "rate_limit_error":
        return "rate_limit"
    if status >= 500 or kind in ("overloaded_error", "api_error"):
        return "server"
    if isinstance(error, (anthropic.APIConnectionError, httpx.TransportError)):
        return "connection"
    if isinstance(error, anthropic.APIStatusError):
        return "refused"
    return None


def _error_message(error: BaseException) -> str:
    body = getattr(error, "body", None)
    detail = body.get("error") if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("message"):
        return str(detail["message"])
    return str(error)


def _retry_after(error: BaseException) -> Optional[float]:
    """The wait the API asked for: its retry-after header, else one named in the message."""
    response = getattr(error, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    try:
        seconds = float(header) if header else parse_retry_delay(str(error))
    except ValueError:
        seconds = parse_retry_delay(str(error))
    # inf, nan or a negative value is no wait at all -- treated as not asked
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


def _typed_error(error: BaseException, kind: Optional[str], model: str,
                 retry_after: Optional[float] = None) -> BaseException:
    """The error the agent server falls back on for *error* -- or *error* itself.

    The SDK's own exceptions are none of the types the server's profile fallback
    catches, so a refused, overloaded or unreachable model ended the run where
    the chain had a fallback ready.
    """
    status = getattr(error, "status_code", None)
    message = _error_message(error)
    if kind == "rate_limit":
        lowered = message.lower()
        cls = (LLMQuotaExhaustedError if "quota" in lowered or "exhausted" in lowered
               else LLMRateLimitError)
        return cls(f"Rate limit: {message}", provider="anthropic", model=model,
                   retry_after=retry_after)
    if kind == "server":
        # 529 for an overload that came as an error event inside a stream (status 200)
        code = status if status and status >= 500 else (529 if _error_type(error) == "overloaded_error" else 500)
        return LLMServerError(f"HTTP {code}: {message}", provider="anthropic", model=model,
                              status_code=code)
    if kind == "connection":
        return LLMConnectionError(f"Network/protocol error: {type(error).__name__}: {message}",
                                  provider="anthropic", model=model)
    if kind == "refused":
        return httpx.HTTPStatusError(f"HTTP {status}: {message}", request=error.request,
                                     response=error.response)
    return error


async def _next_event(events: Any, cancelled: Optional["asyncio.Future"]) -> Any:
    """The stream's next event, or CancelledError as soon as the run is cancelled.

    Checking the token between events is not enough: the SDK swallows the API's
    keep-alive pings, and a model thinking with display omitted sends nothing
    for minutes -- billed all the while a cancel waits for the next event.
    """
    if cancelled is None:
        return await events.__anext__()
    step = asyncio.ensure_future(events.__anext__())
    try:
        await asyncio.wait({step, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        if step.done():
            return step.result()
    finally:
        if not step.done():
            step.cancel()
            with contextlib.suppress(BaseException):
                await step
    raise asyncio.CancelledError("Request cancelled during streaming")


class AnthropicAsyncClient(LLMClient):
    """Anthropic Claude client using official SDK.
    
    Full-featured client with:
    - Streaming support for text and tool calls
    - Extended thinking integration
    - Prompt caching for cost savings
    - Vision/image support
    - Token tracking
    - Retry logic with exponential backoff
    
    Usage:
        client = AnthropicAsyncClient(
            model="claude-sonnet-4-20250514",
            api_key="sk-ant-...",
            context_window=200000,
        )
        
        # Streaming with tools
        async for chunk in client.chat_tools_streaming(messages, tools):
            if chunk["type"] == "content_delta":
                print(chunk["delta"], end="")
    """

    #: Structured outputs travel as ``output_config.format`` (a JSON schema; the Messages
    #: API has no schema-less JSON mode, so json_object is not offered here). The API takes
    #: it beside tools; changing it invalidates the conversation's prompt cache, which is why
    #: an agent sends it on every step of a run, not only on the last.
    response_format_kinds = (JSON_SCHEMA,)

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        context_window: int = 200000,
        request_timeout: int = 180,
        max_retries: int = 3,
        rate_limit_max_retries: int = 2,
        max_tokens: int = 8192,
        include_thinking: bool = False,
        thinking_budget: Optional[int] = None,
        enable_prompt_caching: bool = True,
        prompt_cache_mode: Optional[str] = None,
        reasoning_details_mode: Optional[str] = None,
        thinking_request_shape: Optional[str] = None,
        capabilities=None,
        ssl_verify: Optional[bool] = None,
        **extra_params
    ):
        """Initialize Anthropic client.
        
        Args:
            model: Model name (e.g., "claude-sonnet-4-20250514", "claude-opus-4-20250514")
            api_key: Anthropic API key
            base_url: Optional custom base URL
            context_window: Maximum context window size
            request_timeout: Request timeout in seconds
            max_retries: Maximum retry attempts
            max_tokens: Maximum tokens to generate
            include_thinking: Enable extended thinking output
            thinking_budget: Token budget for thinking (requires include_thinking=True)
            enable_prompt_caching: Enable prompt caching for cost savings
            ssl_verify: False disables certificate verification (TLS proxies);
                None keeps the SDK default.
            **extra_params: Additional parameters (temperature, top_p, etc.)
        """
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise RuntimeError("anthropic package required for AnthropicAsyncClient. Install with: pip install anthropic") from e
        
        self.model = model
        self.model_name = model  # For token tracking compatibility
        self.capabilities = capabilities
        self.api_key = api_key
        self.base_url = base_url
        self.context_window = context_window
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.rate_limit_max_retries = rate_limit_max_retries
        self.max_tokens = max_tokens
        self.include_thinking = include_thinking
        self.thinking_budget = thinking_budget
        self.enable_prompt_caching = enable_prompt_caching
        # Anthropic-Cache-Policy (geteilt mit dem OpenRouter-httpx-Pfad):
        # steuert ob der wachsende Konversations-Tail zusaetzlich zu System/Tools
        # als cache_control-Breakpoint markiert wird (Multi-Turn).
        self.prompt_cache_mode = prompt_cache_mode
        # Governs the thinking-block replay below. keep_all is what this
        # client has always done and what a tool round trip needs: the blocks
        # of the latest assistant turn must come back complete, and the
        # earlier ones keep the cached prefix byte-identical.
        self.reasoning_details_mode = resolve_reasoning_details_mode(
            reasoning_details_mode, model=model, default="keep_all")
        # "budget" or "adaptive" — declared per model entry, see
        # _build_thinking_param. Unknown value: fail here, not on the wire.
        self.thinking_request_shape = resolve_thinking_request_shape(
            thinking_request_shape, model=model)
        # The API refuses budget_tokens >= max_tokens on every call; say so once, here.
        if include_thinking and self.thinking_request_shape == "budget":
            budget = thinking_budget or 8192
            if budget >= max_tokens:
                raise ValueError(
                    f"model {model}: thinking budget {budget} must be below max_tokens "
                    f"{max_tokens} (set max_tokens or thinking_budget in the entry)")
        self.extra_params = extra_params
        
        # Initialize the official client
        client_kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "timeout": request_timeout,
            "max_retries": 0,  # We handle retries ourselves
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        if ssl_verify is False:
            # network.ssl_verify: false reaches every other TLS provider —
            # this one used to accept the flag and ignore it, so behind a
            # TLS-intercepting proxy Anthropic alone kept failing on the
            # certificate with nothing pointing at the setting.
            import httpx as _httpx
            client_kwargs["http_client"] = _httpx.AsyncClient(
                verify=httpx_verify(False), timeout=request_timeout)

        self._client = AsyncAnthropic(**client_kwargs)
        
        logger.info(
            f"Initialized AnthropicAsyncClient with model={model} "
            f"context_window={context_window} "
            f"include_thinking={include_thinking} "
            f"thinking_budget={thinking_budget} "
            f"enable_prompt_caching={enable_prompt_caching}"
        )

    def _build_thinking_param(self) -> Dict[str, Any]:
        """Thinking config for this model, per its declared request shape.

        Newer models take only ``{"type": "adaptive"}`` and 400 on a fixed
        budget; older ones still require ``budget_tokens``. Sending the wrong
        shape fails the request outright, and the model NAME is not allowed to
        decide it: the entry declares ``thinking_request_shape``, so the next
        family is a config line instead of a table in here.

        ``thinking_budget`` stays a config field for the budget models (and as a
        soft-cap indicator elsewhere) but must NOT be sent to adaptive ones.
        """
        if self.thinking_request_shape == "adaptive":
            if self.thinking_budget:
                logger.debug(
                    "thinking_budget=%s ignored: %s is declared "
                    "thinking_request_shape=adaptive",
                    self.thinking_budget, self.model,
                )
            return {"type": "adaptive"}
        return {"type": "enabled", "budget_tokens": self.thinking_budget or 8192}

    #: Both thinking block types. `redacted_thinking` carries encrypted
    #: reasoning and is just as mandatory on replay as a plain `thinking` block.
    _THINKING_BLOCK_TYPES = ("thinking", "redacted_thinking")

    @staticmethod
    def _serialize_thinking_blocks(message: Any) -> List[Dict[str, Any]]:
        """Verbatim copy of every thinking/redacted_thinking block, in order.

        Two filters that look reasonable and are NOT allowed here:

        * dropping blocks whose ``thinking`` text is empty — with
          ``display="omitted"`` (the default on Opus 5 / Sonnet 5 / Fable 5) the
          text is always empty while the *signature* still carries the encrypted
          reasoning;
        * keeping only ``type == "thinking"`` — that silently drops
          ``redacted_thinking``.

        Either one turns a complete echo into a PARTIAL one, which Anthropic
        rejects with 400 ("...blocks in the latest assistant message cannot be
        modified"). Copy everything, in order, unchanged.
        """
        blocks: List[Dict[str, Any]] = []
        for block in getattr(message, "content", None) or []:
            if getattr(block, "type", None) not in AnthropicAsyncClient._THINKING_BLOCK_TYPES:
                continue
            dump = getattr(block, "model_dump", None)
            if callable(dump):
                blocks.append(dump(mode="json", exclude_none=True))
            elif isinstance(block, dict):
                blocks.append(dict(block))
        return blocks

    def _replayable_thinking(self, msg: ChatMessage,
                             allowed: bool = True) -> List[Dict[str, Any]]:
        """This message's thinking blocks, if they may be replayed to THIS model.

        Signatures are model-bound. Replaying them to a different model does not
        error — the blocks are ignored but still billed as input — so a fallback
        chain that switched models would silently pay for dead weight.

        ``allowed`` is this turn's verdict from ``reasoning_details_mode``
        (keep_all / keep_last / strip): thinking blocks ARE this route's
        reasoning round trip — it carries them on ``ChatMessage.thinking_blocks``
        instead of ``reasoning_details``, so the declared key governs them.
        """
        blocks = getattr(msg, "thinking_blocks", None)
        if not blocks or not allowed:
            return []
        origin = getattr(msg, "thinking_model", None)
        if origin and origin != self.model:
            logger.debug(
                "Dropping %d thinking block(s) from %s (current model: %s)",
                len(blocks), origin, self.model,
            )
            return []
        return [dict(b) for b in blocks if isinstance(b, dict)]

    def _convert_messages(
        self, messages: List[ChatMessage]
    ) -> tuple[Optional[str | List[Dict[str, Any]]], List[Dict[str, Any]]]:
        """Convert ChatMessage list to Anthropic format.
        
        Returns:
            (system_prompt, messages_list)
            system_prompt is a string or list of content blocks (with cache_control)
        """
        system_prompt: Optional[str] = None
        converted_messages: List[Dict[str, Any]] = []
        may_replay = reasoning_replay_flags(messages, self.reasoning_details_mode)

        for index, msg in enumerate(messages):
            role = msg.role

            # Extract system message(s) - Anthropic only supports a single system param
            if role == "system":
                text = msg.content if isinstance(msg.content, str) else msg.get_text_content()
                if system_prompt:
                    system_prompt += "\n" + text
                else:
                    system_prompt = text
                continue
            
            # A developer note: the RUN talking to the model mid-conversation.
            # The Messages API has no role for it -- "there is no `system` role
            # for input messages" -- so it rides the lowest rung: a user turn in
            # <developer_note> tags. The top-level system parameter is the wrong
            # home for it, and not because of style: a note hoisted up there
            # reads as if it had held since the first turn, and the whole reason
            # to send it mid-run is that it did not.
            if role == DEVELOPER:
                # Built as `developer` and lowered at the END of this function,
                # after the cache markers: while it says `developer` they look
                # past it, and a note must never carry a breakpoint -- its text
                # is rewritten every call, so the marked prefix would differ
                # every turn and the cache would never hit again.
                if isinstance(msg.content, list):
                    converted_messages.append({
                        "role": DEVELOPER,
                        "content": anthropic_utils.normalize_content_list(msg.content)})
                else:
                    converted_messages.append({
                        "role": DEVELOPER,
                        "content": msg.content if isinstance(msg.content, str)
                        else msg.get_text_content()})
                continue

            # Map roles
            if role == "assistant":
                anthropic_role = "assistant"
            else:
                anthropic_role = "user"
            
            # Handle tool results
            if role == "tool":
                # Tool results need to be converted to tool_result content blocks
                tool_result_content = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id or "",
                    "content": msg.content if isinstance(msg.content, str) else msg.get_text_content()
                }
                converted_messages.append({
                    "role": "user",
                    "content": [tool_result_content]
                })
                
                # Inject multimodal content as separate user message if present
                if msg.multimodal_content:
                    from agent_system.utils.multimodal_tool_content import (
                        create_anthropic_multimodal_injection,
                        check_vision_support
                    )
                    # Anthropic Claude models generally support vision, so an
                    # unset capabilities means "allow" here. getattr because
                    # test doubles and partially built clients turn up on this
                    # path — the factory does wire capabilities through.
                    _caps = getattr(self, "capabilities", None)
                    supports_vision = check_vision_support(_caps) if _caps else True
                    injection = create_anthropic_multimodal_injection(
                        msg, supports_vision=supports_vision, model_name=self.model
                    )
                    if injection:
                        converted_messages.append(injection)
                
                continue
            
            # Handle assistant messages with tool calls
            if role == "assistant" and msg.tool_calls:
                content_blocks: List[Dict[str, Any]] = []

                # Thinking blocks FIRST — that is the order the model emitted
                # them in ([thinking, text, tool_use]), and the order the
                # "must match what the model generated" check validates against.
                # Required when returning tool results: the blocks have to come
                # back complete and unmodified or the turn is rejected.
                content_blocks.extend(
                    self._replayable_thinking(msg, may_replay[index]))

                # Add text content if present
                text_content = msg.content if isinstance(msg.content, str) else msg.get_text_content()
                if text_content:
                    content_blocks.append({"type": "text", "text": text_content})
                
                # Add tool_use blocks
                for tool_call in msg.tool_calls:
                    func = tool_call.get("function", {})
                    args_str = func.get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except json.JSONDecodeError:
                        repaired = repair_json(args_str)
                        args = repaired if repaired is not None and isinstance(repaired, dict) else {}
                    
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tool_call.get("id", f"call_{uuid.uuid4().hex[:16]}"),
                        "name": func.get("name", ""),
                        "input": args
                    })
                
                converted_messages.append({
                    "role": "assistant",
                    "content": content_blocks
                })
                continue
            
            # Handle multimodal content
            if isinstance(msg.content, list):
                content_blocks = anthropic_utils.normalize_content_list(msg.content)
                
                converted_messages.append({
                    "role": anthropic_role,
                    "content": content_blocks
                })
            else:
                # Simple text content. An assistant turn that carried thinking
                # blocks keeps them here too, so the echo stays complete across
                # the whole history — but only alongside real text: blocks with
                # no content would produce an empty assistant message.
                replay = (
                    self._replayable_thinking(msg, may_replay[index])
                    if anthropic_role == "assistant" else []
                )
                if replay and isinstance(msg.content, str) and msg.content:
                    converted_messages.append({
                        "role": anthropic_role,
                        "content": replay + [{"type": "text", "text": msg.content}],
                    })
                else:
                    converted_messages.append({
                        "role": anthropic_role,
                        "content": msg.content or ""
                    })
        
        # Cache-Breakpoint-Sentinels strippen (Sicherheitsnetz, auf dem
        # KONVERTIERTEN Output — Session-Messages bleiben unangetastet):
        # der native Anthropic-Pfad nutzt cache_control (nicht die OpenAI-
        # Sentinel-Breakpoints); ein Sentinel-Marker darf das Modell nie
        # erreichen (s. cache_key.py).
        if isinstance(system_prompt, str) and CACHE_BP_SENTINEL in system_prompt:
            system_prompt = strip_cache_breakpoints(system_prompt)
        for cm in converted_messages:
            content = cm.get("content")
            if isinstance(content, str) and CACHE_BP_SENTINEL in content:
                cm["content"] = strip_cache_breakpoints(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    # text-Bloecke tragen "text", tool_result-Bloecke "content"
                    for key in ("text", "content"):
                        val = block.get(key)
                        if isinstance(val, str) and CACHE_BP_SENTINEL in val:
                            block[key] = strip_cache_breakpoints(val)

        # Prompt caching (geteilte Anthropic-Policy, s. cache_key.py):
        # System-Prefix als cache_control-Breakpoint markieren; bei Multi-Turn
        # zusaetzlich den wachsenden Konversations-Tail (frueher fehlte das im
        # nativen Pfad — dadurch cachte provider=anthropic den Verlauf NICHT,
        # or-claude-sonnet via OpenRouter aber schon → jetzt konsistent).
        if self.enable_prompt_caching and system_prompt:
            if isinstance(system_prompt, str):
                system_prompt = [{"type": "text", "text": system_prompt}]
            mark_last_text_block(system_prompt)
            # multi_turn ab Runde 1; auto/None nur bei echter Historie. System +
            # Tool + Tail bleiben <= 4 Bloecke; der Cap am Assemblierungspunkt
            # (chat / chat_tools_streaming) erzwingt das harte Limit ohnehin.
            if anthropic_cache_conversation(
                self.prompt_cache_mode, messages_have_history(converted_messages)
            ):
                mark_conversation_tail(converted_messages)

        # AFTER the markers, never before: this format has no `developer` role
        # at all ("there is no `system` role for input messages" either), so a
        # note becomes a tagged user turn -- but only once nothing is left that
        # would put a cache breakpoint on it.
        self._lower_developer_messages(converted_messages)
        return system_prompt, converted_messages

    def _lower_developer_messages(self, converted_messages: List[Dict[str, Any]]) -> None:
        """Rewrite developer notes to the rung the Messages API permits.

        resolve_rung for the warning as much as for the answer: the ceiling
        here is `user`, so a model entry claiming `developer` can only be a
        mistake, and ignoring it in silence is how a mistake survives.
        """
        # getattr twice: test doubles and partially built clients reach this
        # path without a `capabilities` attribute at all -- the same reason the
        # vision check a few lines up spells it out.
        rung = resolve_rung(
            getattr(getattr(self, "capabilities", None), "developer_role", None),
            ceiling=USER, default=USER, route="the Anthropic Messages API")
        for message in converted_messages:
            if message.get("role") != DEVELOPER:
                continue
            message["role"] = rung
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = [{"type": "text", "text": NOTE_OPEN}, *content,
                                      {"type": "text", "text": NOTE_CLOSE}]
            else:
                _role, message["content"] = developer_turn(
                    content if isinstance(content, str) else "", rung)

    def _convert_tools(self, tools: List[Dict]) -> List[Dict[str, Any]]:
        """Convert OpenAI tool schema to Anthropic format."""
        anthropic_tools = []
        
        for tool in tools:
            if tool.get("type") != "function":
                continue
            
            func = tool.get("function", {})
            params = func.get("parameters", {})
            
            # Clean schema for Anthropic (similar to Gemini)
            input_schema = self._clean_schema(params)
            
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": input_schema
            })
        
        # Prompt caching: letzte Tool-Definition markieren (geteilte Policy)
        if self.enable_prompt_caching:
            mark_last_tool(anthropic_tools)

        return anthropic_tools

    def _clean_schema(self, schema: Dict) -> Dict:
        """Clean JSON schema for Anthropic compatibility.

        NOT llm_common's ``sanitize_schema_for_gemini``, although it looks like
        a fourth copy of it. Measured over the same four schemas (a pydantic
        tool signature, a nested array of objects, enum/const, a oneOf union):
        0 of 4 came out the same. Anthropic's ``input_schema`` IS JSON Schema
        and keeps ``minimum``, ``maximum``, ``pattern``, ``format``, ``const``
        and ``oneOf``; the Gemini dialect deletes all of them and appends their
        values to the description as prose. Unifying would hand Claude tools
        whose constraints are only a hint — a loss, for no bug.
        """
        if not schema:
            return {"type": "object", "properties": {}}
        
        cleaned = {}
        
        for key, value in schema.items():
            # Skip unsupported fields
            if key in ("additionalProperties", "examples", "$schema", "$id"):
                continue
            
            if key == "properties" and isinstance(value, dict):
                cleaned["properties"] = {
                    k: self._clean_schema(v) for k, v in value.items()
                }
            elif key == "items" and isinstance(value, dict):
                cleaned["items"] = self._clean_schema(value)
            elif isinstance(value, dict):
                cleaned[key] = self._clean_schema(value)
            else:
                cleaned[key] = value
        
        return cleaned

    def _extract_usage(self, usage) -> Dict[str, Any]:
        """Extract usage info in OpenAI-compatible format."""
        if not usage:
            return {}
        return anthropic_utils.usage_to_openai(usage)

    def _cap_anthropic_cache(
        self,
        system_prompt: Optional[str | List[Dict[str, Any]]],
        converted_messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Defense-in-depth: max. 4 cache_control-Bloecke ueber System + Tools +
        Messages (Anthropic-Prefix-Reihenfolge). No-op ohne Caching. Geteilte
        Policy (cache_key.cap_cache_control)."""
        if not self.enable_prompt_caching:
            return
        cap_cache_control([
            tools or [],
            system_prompt if isinstance(system_prompt, list) else [],
            converted_messages,
        ])

    @staticmethod
    def _output_config(response_format: ResponseFormat) -> Dict[str, Any]:
        """``output_config`` for a structured answer. The schema goes as the caller wrote it:
        the API refuses one it cannot enforce (an object without ``additionalProperties:
        false``, say) with a 400 -- loud, where rewriting it would quietly change the contract."""
        return {"format": {"type": "json_schema", "schema": response_format.schema}}

    async def chat(self, messages: List[ChatMessage], cancellation_token=None, *,
                   response_format: Optional[ResponseFormat] = None) -> str:
        """Simple chat without tools - returns text response."""
        self._require_response_format(response_format)
        system_prompt, converted_messages = self._convert_messages(messages)
        self._cap_anthropic_cache(system_prompt, converted_messages)

        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": converted_messages,
            "max_tokens": self.max_tokens,
        }

        if system_prompt:
            request_kwargs["system"] = system_prompt

        # Add extra params (temperature, top_p, etc.)
        for key in ("temperature", "top_p", "top_k"):
            if key in self.extra_params:
                request_kwargs[key] = self.extra_params[key]
        if response_format is not None:
            request_kwargs["output_config"] = self._output_config(response_format)

        # Reported like chat_tools: callers of chat() (llm_router, summaries)
        # were invisible to the message debugger and the cost figures.
        url = str(getattr(self._client, "_base_url", _DEFAULT_URL))
        await self._notify_pre_request({
            "provider": "anthropic", "model": self.model, "url": url,
            "payload": request_kwargs, "is_streaming": False,
            "timestamp_ms": time.time() * 1000,
        })
        started = time.time()
        ended = False

        async def report_end(**info: Any) -> None:
            nonlocal ended
            ended = True
            await self._notify_post_response({
                "provider": "anthropic", "model": self.model, "url": url,
                "is_streaming": False, "duration_ms": (time.time() - started) * 1000,
                "timestamp_ms": time.time() * 1000, **info,
            })

        try:
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")
            try:
                if cancellation_token:
                    # The token has to be watched while the call runs; without it a
                    # cancelled request kept billing until the answer arrived.
                    call = asyncio.create_task(self._client.messages.create(**request_kwargs))
                    response = await cancellation.await_call(call, cancellation_token)
                else:
                    response = await self._client.messages.create(**request_kwargs)
            except Exception as e:
                typed = _typed_error(e, _error_kind(e), self.model, _retry_after(e))
                if typed is e:
                    raise
                raise typed from e

            text = "".join(block.text for block in response.content if hasattr(block, "text"))
            await report_end(
                usage=self._extract_usage(getattr(response, "usage", None)) or None,
                finish_reason=anthropic_utils.finish_reason(getattr(response, "stop_reason", None)))
            return text
        except BaseException as error:
            # One report per request: whatever ended it without one travels on unchanged.
            if not ended:
                await report_end(error=_ending_error(error))
            raise

    async def chat_tools(
        self,
        messages: List[ChatMessage],
        tools: List[Dict],
        cancellation_token=None,
        status_scope=None,
        *,
        response_format: Optional[ResponseFormat] = None,
    ) -> Dict[str, Any]:
        """Chat with tools - non-streaming."""
        result = {}
        async for chunk in self.chat_tools_streaming(messages, tools, cancellation_token, status_scope,
                                                     response_format=response_format):
            if chunk.get("type") == "final":
                result = chunk

        return result

    async def chat_tools_streaming(
        self,
        messages: List[ChatMessage],
        tools: List[Dict],
        cancellation_token=None,
        status_scope=None,
        *,
        response_format: Optional[ResponseFormat] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream chat with tools.
        
        Yields events in format compatible with other LLM clients:
        - content_delta: Text content chunks with delta and accumulated
        - tool_call_delta: Tool call information
        - thinking_delta: Extended thinking output (if enabled)
        - final: Final accumulated result
        """
        # Status reporting helper
        async def report_status(message: str) -> None:
            if status_scope is None:
                return
            try:
                await status_scope.progress(message)
            except Exception as e:
                logger.debug(f"Failed to report LLM status: {e}")
        
        self._require_response_format(response_format)
        system_prompt, converted_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools) if tools else []
        self._cap_anthropic_cache(system_prompt, converted_messages, anthropic_tools)

        # Build request kwargs
        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": converted_messages,
            "max_tokens": self.max_tokens,
        }
        
        if system_prompt:
            request_kwargs["system"] = system_prompt
        
        if anthropic_tools:
            request_kwargs["tools"] = anthropic_tools
        
        # Add extra params. With extended/adaptive thinking active, Anthropic
        # rejects sampling modifications (temperature/top_p/top_k) with 400 —
        # drop them instead of forwarding. NOTE: gated on include_thinking,
        # so an adaptive-only model configured with include_thoughts=false
        # (thinks server-side anyway) and the no-tools chat() path are NOT
        # covered — both pre-existing, no such config exists today.
        for key in ("temperature", "top_p", "top_k", "stop_sequences"):
            if key in self.extra_params:
                if self.include_thinking and key != "stop_sequences":
                    logger.debug(
                        "%s=%s dropped (extended thinking active, model=%s)",
                        key, self.extra_params[key], self.model,
                    )
                    continue
                request_kwargs[key] = self.extra_params[key]

        # Handle extended thinking
        if self.include_thinking:
            request_kwargs["thinking"] = self._build_thinking_param()

        if response_format is not None:
            request_kwargs["output_config"] = self._output_config(response_format)
        
        # Notify pre-request hook (LLM-client level)
        url = str(getattr(self._client, "_base_url", _DEFAULT_URL))
        await self._notify_pre_request({
            "provider": "anthropic",
            "model": self.model,
            "url": url,
            "payload": request_kwargs,
            "is_streaming": True,
            "timestamp_ms": time.time() * 1000,
        })

        _request_start = time.time()
        # Every way this request ends reaches post_llm_response exactly once --
        # an answer, a refusal after the retries, a cancel, an abandoned stream.
        ended = False
        # Whether the caller has seen a delta of an attempt that did not finish:
        # a retry starts from scratch, so the caller is told to drop what it has.
        yielded_delta = False

        async def report_end(**info: Any) -> None:
            nonlocal ended
            ended = True
            await self._notify_post_response({
                "provider": "anthropic", "model": self.model, "url": url,
                "is_streaming": True, "duration_ms": (time.time() - _request_start) * 1000,
                "timestamp_ms": time.time() * 1000, **info,
            })

        wait_for_cancel = getattr(cancellation_token, "wait_for_cancellation", None)
        cancelled = asyncio.ensure_future(wait_for_cancel()) if callable(wait_for_cancel) else None
        _effective_max = max(self.max_retries, self.rate_limit_max_retries)
        try:
            for attempt in range(_effective_max + 1):
                if cancellation_token and cancellation_token.is_cancelled:
                    raise asyncio.CancelledError("Request cancelled by user")
                if yielded_delta:
                    yielded_delta = False
                    yield {"type": "stream_restart"}

                # Accumulators, fresh for every attempt: a retry starts from scratch.
                accumulated_content: List[str] = []
                accumulated_thinking: List[str] = []
                # Verbatim thinking/redacted_thinking blocks incl. signatures — see
                # _serialize_thinking_blocks.
                thinking_blocks: List[Dict[str, Any]] = []
                accumulated_tool_calls: Dict[str, Dict[str, Any]] = {}
                accumulated_usage: Optional[Dict[str, Any]] = None
                current_tool_call_id: Optional[str] = None
                current_tool_name: Optional[str] = None
                current_tool_input: str = ""
                stop: Optional[str] = None

                try:
                    logger.debug(f"[Anthropic] Starting streaming request to {self.model} (attempt {attempt + 1})")

                    async with self._client.messages.stream(**request_kwargs) as stream:
                        events = stream.__aiter__()
                        while True:
                            try:
                                event = await _next_event(events, cancelled)
                            except StopAsyncIteration:
                                break
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise asyncio.CancelledError("Request cancelled during streaming")

                            event_type = event.type if hasattr(event, "type") else str(type(event).__name__)

                            # Handle content block start
                            if event_type == "content_block_start":
                                block = event.content_block
                                block_type = getattr(block, "type", None)

                                if block_type == "tool_use":
                                    current_tool_call_id = getattr(block, "id", f"call_{uuid.uuid4().hex[:16]}")
                                    current_tool_name = getattr(block, "name", "")
                                    current_tool_input = ""
                                    logger.debug(f"[Anthropic] Tool call started: {current_tool_name}")

                            # Handle content block delta
                            elif event_type == "content_block_delta":
                                delta = event.delta
                                delta_type = getattr(delta, "type", None)

                                if delta_type == "text_delta":
                                    text = getattr(delta, "text", "")
                                    accumulated_content.append(text)

                                    all_text = "".join(accumulated_thinking) + "".join(accumulated_content)
                                    yielded_delta = True
                                    yield {
                                        "type": "content_delta",
                                        "delta": text,
                                        "accumulated": all_text
                                    }

                                elif delta_type == "thinking_delta":
                                    thinking_text = getattr(delta, "thinking", "")
                                    accumulated_thinking.append(thinking_text)

                                    yielded_delta = True
                                    yield {
                                        "type": "thinking_delta",
                                        "delta": thinking_text,
                                        "accumulated": "".join(accumulated_thinking)
                                    }

                                elif delta_type == "input_json_delta":
                                    partial_json = getattr(delta, "partial_json", "")
                                    current_tool_input += partial_json

                            # Handle content block stop
                            elif event_type == "content_block_stop":
                                if current_tool_call_id and current_tool_name:
                                    # The input as the model sent it. Not repaired here:
                                    # tool_execution rejects malformed arguments and the
                                    # model sends the call again -- a repaired guess ran as
                                    # if it were the call, and input nothing could repair
                                    # ran with {} (2026-09-11).

                                    tool_call = {
                                        "id": current_tool_call_id,
                                        "type": "function",
                                        "function": {
                                            "name": current_tool_name,
                                            "arguments": current_tool_input or "{}"
                                        }
                                    }
                                    accumulated_tool_calls[current_tool_call_id] = tool_call

                                    yielded_delta = True
                                    yield {
                                        "type": "tool_call_delta",
                                        "index": len(accumulated_tool_calls) - 1,
                                        "delta": {"function": {"name": current_tool_name}},
                                        "accumulated": tool_call
                                    }

                                    logger.debug(f"[Anthropic] Tool call complete: {current_tool_name}")
                                    current_tool_call_id = None
                                    current_tool_name = None
                                    current_tool_input = ""

                            # Handle message delta (usage info)
                            elif event_type == "message_delta":
                                usage = getattr(event, "usage", None)
                                if usage:
                                    accumulated_usage = self._extract_usage(usage)

                        # Get final message for complete usage info
                        final_message = await stream.get_final_message()
                        if final_message and final_message.usage:
                            accumulated_usage = self._extract_usage(final_message.usage)
                        # why the answer ended: a cut (max_tokens) or a refusal must reach the loop's guards
                        stop = anthropic_utils.finish_reason(getattr(final_message, "stop_reason", None))
                        # The accumulated final message is the ONLY place the thinking
                        # blocks' signatures exist — the stream deltas carry text only
                        # (and with display="omitted", the default on Opus 5 / Sonnet 5
                        # / Fable 5, not even that). Capture them verbatim here.
                        if final_message:
                            thinking_blocks = self._serialize_thinking_blocks(final_message)

                except asyncio.CancelledError:
                    logger.info("[Anthropic] Request cancelled by user")
                    raise

                except Exception as e:
                    error_str = str(e)
                    kind = _error_kind(e)

                    if kind == "rate_limit":
                        asked = _retry_after(e)
                        base_wait = 60.0 if asked is None else asked
                        wait_time = base_wait + base_wait * random.uniform(0.0, 0.5)
                        # A longer wait is not slept here: while this call sleeps, the
                        # server's fallback chain cannot run. It blocks the model for
                        # retry_after and moves on instead.
                        if asked is not None and asked > _MAX_RATE_LIMIT_WAIT:
                            wait_time = asked
                        elif attempt < self.rate_limit_max_retries:
                            await report_status(f"Rate limit, waiting {wait_time:.0f}s, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model}")
                            logger.warning(
                                f"[Anthropic] Rate limit hit. Waiting {wait_time:.1f}s before retry "
                                f"(attempt {attempt + 1}/{self.rate_limit_max_retries})"
                            )
                            await self._notify_retry("anthropic", self.model, url, True, f"Rate limit (429): {error_str[:200]}", attempt, self.rate_limit_max_retries + 1)
                            await self._cancellable_sleep(wait_time, cancellation_token)
                            continue
                        await report_status(f"Rate limit exceeded after {attempt + 1} attempts: {self.model}")
                        raise _typed_error(e, kind, self.model, wait_time) from e

                    # Overloaded, other 5xx, an error event mid-stream, a dropped or
                    # timed-out connection: retried from scratch with backoff.
                    if kind in ("server", "connection") and attempt < self.max_retries:
                        wait_time = 2.0 * (2 ** attempt)  # 2s, 4s, 8s
                        label = "Service overloaded" if "overloaded" in error_str.lower() else (
                            "Server error" if kind == "server" else "Connection error")
                        await report_status(f"{label}, retry {attempt + 1}/{self.max_retries} in {wait_time:.0f}s: {self.model}")
                        logger.warning(
                            f"[Anthropic] {label}. Waiting {wait_time:.1f}s "
                            f"(attempt {attempt + 1}/{self.max_retries + 1})"
                        )
                        await self._notify_retry("anthropic", self.model, url, True, f"{label}: {error_str[:200]}", attempt, self.max_retries + 1)
                        await self._cancellable_sleep(wait_time, cancellation_token)
                        continue

                    # Non-recoverable error
                    await report_status(f"Request failed: {self.model}")
                    logger.error(f"[Anthropic] Error: {error_str}")
                    typed = _typed_error(e, kind, self.model)
                    if typed is e:
                        raise
                    raise typed from e

                # Build final result
                assistant: Dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(accumulated_content)
                }

                if accumulated_tool_calls:
                    assistant["tool_calls"] = list(accumulated_tool_calls.values())

                # Inside `assistant` because the agent loop only reads
                # llm_out["assistant"] when building the ChatMessage.
                if thinking_blocks:
                    assistant["thinking_blocks"] = thinking_blocks
                    assistant["thinking_model"] = self.model

                final_result: Dict[str, Any] = {"assistant": assistant}
                if accumulated_usage:
                    final_result["usage"] = accumulated_usage
                if stop:
                    final_result["finish_reason"] = stop

                # Include thinking in final result if present
                if accumulated_thinking:
                    final_result["thinking"] = "".join(accumulated_thinking)

                logger.debug(
                    f"[Anthropic] Streaming complete: {len(accumulated_content)} content parts, "
                    f"{len(accumulated_tool_calls)} tool calls"
                )

                await report_end(usage=accumulated_usage, finish_reason=stop)
                yield {"type": "final", **final_result}
                return  # Success
        except BaseException as error:
            # Whatever ends the request without a report yet -- a cancel during
            # a retry wait, a caller abandoning the stream (GeneratorExit; awaiting
            # is allowed while it closes), an unexpected exception -- is reported
            # here, once, and travels on unchanged.
            if not ended:
                await report_end(error=_ending_error(error))
            raise
        finally:
            if cancelled is not None:
                cancelled.cancel()

    def supports_streaming(self) -> bool:
        """Streaming unless the model's capabilities explicitly disable it."""
        if self.capabilities is not None:
            if isinstance(self.capabilities, dict):
                return self.capabilities.get("streaming", True)
            if hasattr(self.capabilities, "streaming"):
                return self.capabilities.streaming
        return True

    async def close(self) -> None:
        """Close the client."""
        await self._client.close()
