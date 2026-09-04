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
import json
import logging
import random
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from agent_system.llm.models import ChatMessage, LLMClient, LLMRateLimitError, LLMQuotaExhaustedError
from agent_system.llm.retry_utils import parse_retry_delay, is_rate_limit_error
from agent_system.llm.tls import httpx_verify

from . import anthropic_utils
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
        self.reasoning_details_mode = reasoning_details_mode
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

    #: Model families where ``thinking={"type":"enabled","budget_tokens":N}`` is
    #: REJECTED with HTTP 400 — adaptive thinking is the only "on" mode there.
    #: Claude documents this for Fable/Mythos 5, Opus 4.7+ and Sonnet 5; our own
    #: config/llm.yaml carries the same note per model. Prefix matching is safe:
    #: "claude-sonnet-5" does not match "claude-sonnet-4-5-…".
    _ADAPTIVE_ONLY_THINKING = (
        "claude-fable-", "claude-mythos-",
        "claude-opus-5", "claude-opus-4-7", "claude-opus-4-8",
        "claude-sonnet-5",
    )

    def _build_thinking_param(self) -> Dict[str, Any]:
        """Thinking config for this model.

        Newer models take only ``{"type": "adaptive"}`` and 400 on a fixed
        budget; older ones still require ``budget_tokens``. Sending the wrong
        shape fails the request outright, so pick by model rather than always
        using the legacy form.

        ``thinking_budget`` stays a config field for the legacy models (and as a
        soft-cap indicator elsewhere) but must NOT be sent to adaptive-only ones.
        """
        if self.model.startswith(self._ADAPTIVE_ONLY_THINKING):
            if self.thinking_budget:
                logger.debug(
                    "thinking_budget=%s ignored: %s accepts adaptive thinking only",
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

    def _replayable_thinking(self, msg: ChatMessage) -> List[Dict[str, Any]]:
        """This message's thinking blocks, if they may be replayed to THIS model.

        Signatures are model-bound. Replaying them to a different model does not
        error — the blocks are ignored but still billed as input — so a fallback
        chain that switched models would silently pay for dead weight.
        """
        blocks = getattr(msg, "thinking_blocks", None)
        if not blocks:
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

        for msg in messages:
            role = msg.role

            # Extract system message(s) - Anthropic only supports a single system param
            if role == "system":
                text = msg.content if isinstance(msg.content, str) else msg.get_text_content()
                if system_prompt:
                    system_prompt += "\n" + text
                else:
                    system_prompt = text
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
                content_blocks.extend(self._replayable_thinking(msg))

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
                    self._replayable_thinking(msg)
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

        return system_prompt, converted_messages

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
        """Clean JSON schema for Anthropic compatibility."""
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
        
        result = {
            "prompt_tokens": getattr(usage, "input_tokens", 0),
            "completion_tokens": getattr(usage, "output_tokens", 0),
            "total_tokens": getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0),
        }
        
        # Extract cached tokens if present
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0)
        
        if cache_read or cache_creation:
            result["prompt_tokens_details"] = {
                "cached_tokens": cache_read,
                "cache_creation_tokens": cache_creation
            }
        
        return result

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

    async def chat(self, messages: List[ChatMessage], cancellation_token=None) -> str:
        """Simple chat without tools - returns text response."""
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
        
        response = await self._client.messages.create(**request_kwargs)
        
        # Extract text content
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        
        return "".join(text_parts)

    async def chat_tools(
        self,
        messages: List[ChatMessage],
        tools: List[Dict],
        cancellation_token=None,
        status_scope=None
    ) -> Dict[str, Any]:
        """Chat with tools - non-streaming."""
        result = {}
        async for chunk in self.chat_tools_streaming(messages, tools, cancellation_token, status_scope):
            if chunk.get("type") == "final":
                result = chunk
        
        return result

    async def chat_tools_streaming(
        self,
        messages: List[ChatMessage],
        tools: List[Dict],
        cancellation_token=None,
        status_scope=None
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
        
        # Accumulators
        accumulated_content: List[str] = []
        accumulated_thinking: List[str] = []
        # Verbatim thinking/redacted_thinking blocks incl. signatures — see
        # _serialize_thinking_blocks. Reset per attempt with the others below.
        thinking_blocks: List[Dict[str, Any]] = []
        accumulated_tool_calls: Dict[str, Dict[str, Any]] = {}
        accumulated_usage: Optional[Dict[str, Any]] = None
        current_tool_call_id: Optional[str] = None
        current_tool_name: Optional[str] = None
        current_tool_input: str = ""
        
        # Notify pre-request hook (LLM-client level)
        import time as _time
        await self._notify_pre_request({
            "provider": "anthropic",
            "model": self.model,
            "url": str(getattr(self._client, '_base_url', 'https://api.anthropic.com')),
            "payload": request_kwargs,
            "is_streaming": True,
            "timestamp_ms": _time.time() * 1000,
        })
        
        _request_start = _time.time()
        last_exception = None
        _effective_max = max(self.max_retries, self.rate_limit_max_retries)
        for attempt in range(_effective_max + 1):
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled before attempt")
            
            try:
                logger.debug(f"[Anthropic] Starting streaming request to {self.model} (attempt {attempt + 1})")
                
                async with self._client.messages.stream(**request_kwargs) as stream:
                    async for event in stream:
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
                                yield {
                                    "type": "content_delta",
                                    "delta": text,
                                    "accumulated": all_text
                                }
                            
                            elif delta_type == "thinking_delta":
                                thinking_text = getattr(delta, "thinking", "")
                                accumulated_thinking.append(thinking_text)
                                
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
                                # Parse accumulated tool input
                                try:
                                    args = json.loads(current_tool_input) if current_tool_input else {}
                                except json.JSONDecodeError:
                                    repaired = repair_json(current_tool_input)
                                    args = repaired if repaired is not None and isinstance(repaired, dict) else {}
                                
                                tool_call = {
                                    "id": current_tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": current_tool_name,
                                        "arguments": json.dumps(args)
                                    }
                                }
                                accumulated_tool_calls[current_tool_call_id] = tool_call
                                
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
                    # The accumulated final message is the ONLY place the thinking
                    # blocks' signatures exist — the stream deltas carry text only
                    # (and with display="omitted", the default on Opus 5 / Sonnet 5
                    # / Fable 5, not even that). Capture them verbatim here.
                    if final_message:
                        thinking_blocks = self._serialize_thinking_blocks(final_message)
                
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
                
                # Include thinking in final result if present
                if accumulated_thinking:
                    final_result["thinking"] = "".join(accumulated_thinking)
                
                logger.debug(
                    f"[Anthropic] Streaming complete: {len(accumulated_content)} content parts, "
                    f"{len(accumulated_tool_calls)} tool calls"
                )
                
                # Notify post-response hook
                _duration_ms = (_time.time() - _request_start) * 1000
                await self._notify_post_response({
                    "provider": "anthropic", "model": self.model,
                    "url": str(getattr(self._client, '_base_url', 'https://api.anthropic.com')),
                    "is_streaming": True, "duration_ms": _duration_ms,
                    "usage": accumulated_usage,
                    "timestamp_ms": _time.time() * 1000,
                })
                
                yield {"type": "final", **final_result}
                return  # Success
                
            except asyncio.CancelledError:
                logger.info("[Anthropic] Request cancelled by user")
                raise
            
            except Exception as e:
                last_exception = e
                error_str = str(e)
                
                # Check for rate limit errors
                is_rate_limit = (
                    is_rate_limit_error(e) or
                    "rate_limit" in error_str.lower() or
                    "429" in error_str
                )
                
                if is_rate_limit:
                    parsed_delay = parse_retry_delay(error_str)
                    base_wait = parsed_delay if parsed_delay else 60.0
                    jitter = base_wait * random.uniform(0.0, 0.5)
                    wait_time = base_wait + jitter
                    
                    if attempt < self.rate_limit_max_retries:
                        await report_status(f"Rate limit, waiting {wait_time:.0f}s, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model}")
                        logger.warning(
                            f"[Anthropic] Rate limit hit. Waiting {wait_time:.1f}s before retry "
                            f"(attempt {attempt + 1}/{self.rate_limit_max_retries})"
                        )
                        await self._notify_retry("anthropic", self.model, "", True, f"Rate limit (429): {error_str[:200]}", attempt, self.rate_limit_max_retries + 1)
                        await self._cancellable_sleep(wait_time, cancellation_token)
                        # Reset accumulators
                        accumulated_content = []
                        accumulated_thinking = []
                        thinking_blocks = []
                        accumulated_tool_calls = {}
                        accumulated_usage = None
                        continue
                    
                    # Exhausted retries
                    await report_status(f"Rate limit exceeded after {self.rate_limit_max_retries + 1} attempts: {self.model}")
                    if "quota" in error_str.lower() or "exhausted" in error_str.lower():
                        raise LLMQuotaExhaustedError(
                            f"Quota exhausted: {error_str}",
                            provider="anthropic", model=self.model, retry_after=wait_time
                        )
                    raise LLMRateLimitError(
                        f"Rate limit exceeded: {error_str}",
                        provider="anthropic", model=self.model, retry_after=wait_time
                    )
                
                # Check for overloaded errors
                if "overloaded" in error_str.lower() and attempt < self.max_retries:
                    wait_time = 2.0 * (2 ** attempt)  # 2s, 4s, 8s
                    await report_status(f"Service overloaded, retry {attempt + 1}/{self.max_retries} in {wait_time:.0f}s: {self.model}")
                    logger.warning(
                        f"[Anthropic] Service overloaded. Waiting {wait_time:.1f}s "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                    await self._notify_retry("anthropic", self.model, "", True, f"Service overloaded: {error_str[:200]}", attempt, self.max_retries + 1)
                    await self._cancellable_sleep(wait_time, cancellation_token)
                    # Reset accumulators
                    accumulated_content = []
                    accumulated_thinking = []
                    thinking_blocks = []
                    accumulated_tool_calls = {}
                    accumulated_usage = None
                    continue
                
                # Non-recoverable error
                await report_status(f"Request failed: {self.model}")
                logger.error(f"[Anthropic] Error: {error_str}")
                raise
        
        # Should not reach here, but raise last exception if we do
        if last_exception:
            raise last_exception

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
