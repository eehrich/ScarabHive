"""
PluginHook base interface and context definitions.

Provides the abstract base class for implementing hooks and defines
the context structure passed to hooks during execution.
"""

from __future__ import annotations

import logging
from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm.models import ChatMessage
    from ..servers.agent.server import Agent

logger = logging.getLogger(__name__)


class HookType(str, Enum):
    """Types of hooks available in the system."""
    PRE_LLM_CALL = "pre_llm_call"
    POST_LLM_CALL = "post_llm_call"
    PRE_TOOL_CALL = "pre_tool_call"
    POST_TOOL_CALL = "post_tool_call"
    FORMAT_OUTPUT = "format_output"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    # LLM-client-level hooks: capture exact API payloads/responses
    PRE_LLM_REQUEST = "pre_llm_request"
    POST_LLM_RESPONSE = "post_llm_response"
    # Fires DURING a streaming LLM call, every few KB of thinking. Carries no
    # messages: a deep copy of a long conversation on every tick blocks the
    # loop that is streaming (measured: 72 ms for a 4691-message session).
    LLM_PROGRESS = "llm_progress"


@dataclass
class HookContext:
    """
    Context passed to hooks during execution.
    
    Hooks receive a deep copy of this context and can modify it.
    The modified context is returned and may be used by subsequent hooks.
    
    Attributes:
        hook_type: Type of hook being executed
        request_id: Unique identifier for this request (for status messages)
        session_id: Session identifier for conversation tracking
        agent: Reference to the executing agent
        agent_name: Name of the agent (redundant but convenient)
        messages: Current conversation messages (may be None for some hooks)
        llm_response: LLM response data (for post_llm_call hooks)
        tool_call: Tool call information (for tool-related hooks)
        tool_result: Tool execution result (for post_tool_call hooks)
        output: Final output to format (for format_output hooks)
        output_format: Target format for output ('html', 'ansi', 'text', 'markdown')
        metadata: Additional hook-specific metadata
        hook_config: Per-agent custom config from hooks.overrides (auto-populated by registry)
        target_hook_name: Short hook name the registry intends to dispatch (set by registry)
        step: Current execution step number
        llm: Reference to the LLM client being used
        cancellation_token: Optional cancellation token for graceful cancellation
        llm_request_payload: Raw API request payload (for pre_llm_request hooks)
        llm_response_data: Raw API response data (for post_llm_response hooks)
        llm_provider: LLM provider name (e.g., 'openai_httpx', 'gemini', 'anthropic')
        llm_model: Model name used for the request
        llm_request_url: API endpoint URL
        llm_duration_ms: Request duration in milliseconds (for post_llm_response)
        llm_error: Error string if the request failed (for post_llm_response)
        llm_usage: Token usage data from the response (for post_llm_response)
        llm_finish_reason: Finish reason from the response (for post_llm_response)
        llm_is_streaming: Whether the request was streaming
    """
    hook_type: HookType
    request_id: str
    session_id: str
    agent: Optional[Agent] = None
    agent_name: str = ""
    messages: Optional[List[ChatMessage]] = None
    tools_schema: Optional[List[Dict[str, Any]]] = None  # per-request tool schema (token estimation)
    llm_response: Optional[Dict[str, Any]] = None
    tool_call: Optional[Dict[str, Any]] = None
    tool_result: Optional[Dict[str, Any]] = None
    output: Optional[str] = None
    output_format: str = "text"  # Target format: 'html', 'ansi', 'text', 'markdown'
    metadata: Dict[str, Any] = field(default_factory=dict)
    hook_config: Dict[str, Any] = field(default_factory=dict)
    target_hook_name: Optional[str] = None
    step: int = 0
    llm: Optional[Any] = None
    cancellation_token: Optional[Any] = None
    # LLM-client-level fields (for pre_llm_request / post_llm_response hooks)
    llm_request_payload: Optional[Dict[str, Any]] = None
    llm_response_data: Optional[Dict[str, Any]] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_request_url: Optional[str] = None
    llm_duration_ms: Optional[float] = None
    llm_error: Optional[str] = None
    llm_usage: Optional[Dict[str, Any]] = None
    llm_finish_reason: Optional[str] = None
    llm_is_streaming: bool = False
    # llm_progress: thinking of the running call so far, its length, and the
    # length at the previous tick — a hook compares both against its own
    # interval and needs no per-call state.
    reasoning_text: Optional[str] = None
    reasoning_chars: int = 0
    previous_reasoning_chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert context to dictionary for serialization."""
        return {
            "hook_type": self.hook_type.value if isinstance(self.hook_type, HookType) else self.hook_type,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "step": self.step,
            "has_messages": self.messages is not None,
            "message_count": len(self.messages) if self.messages else 0,
            "has_llm_response": self.llm_response is not None,
            "has_tool_call": self.tool_call is not None,
            "has_tool_result": self.tool_result is not None,
            "has_output": self.output is not None,
            "metadata": self.metadata,
        }


@dataclass
class HookResult:
    """
    Result returned by a hook execution.
    
    Hooks return this object to indicate success/failure and any modifications.
    
    Attributes:
        success: Whether the hook executed successfully
        modified: Whether the hook modified the context
        context: Modified context (or original if unmodified)
        error: Error message if execution failed
        metadata: Additional result metadata
    """
    success: bool
    modified: bool = False
    context: Optional[HookContext] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class PluginHook(ABC):
    """
    Abstract base class for plugin hooks.
    
    Plugins implement this interface to hook into agent lifecycle points.
    Each hook method receives a HookContext and returns a HookResult.
    
    Hooks should:
    - Execute quickly (< 1 second ideally)
    - Be idempotent when possible
    - Not modify state outside the provided context
    - Handle errors gracefully and return error HookResult
    - Use request_id from context for status messages
    
    Example:
        from agent_system.llm.message_roles import DEVELOPER

        class MyHook(PluginHook):
            def __init__(self, name: str, config: dict):
                super().__init__(name, config)
                
            async def on_pre_llm_call(self, context: HookContext) -> HookResult:
                # Modify messages before LLM call
                if context.messages:
                    # Add custom message
                    # A developer turn keeps the place it is given; a `system`
                    # message is hoisted into the prompt head by Anthropic and
                    # Gemini, where a text rebuilt per call breaks the cache.
                    context.messages.append(ChatMessage(
                        role=DEVELOPER, content="Custom prompt", injected_by=self.name))
                    return HookResult(success=True, modified=True, context=context)
                return HookResult(success=True, modified=False, context=context)
    """
    
    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the hook.
        
        Args:
            name: Unique name for this hook instance
            config: Optional configuration dictionary
        """
        self.name = name
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)
        
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """
        Called before LLM invocation.
        
        Use cases:
        - Modify messages (inject context, optimize tokens)
        - Validate message structure
        - Add custom system prompts
        - Apply content policies
        
        Args:
            context: Hook context with messages, agent, llm, step
            
        Returns:
            HookResult with success status and modified context
        """
        return HookResult(success=True, modified=False, context=context)
    
    async def on_post_llm_call(self, context: HookContext) -> HookResult:
        """
        Called after LLM response received.
        
        Use cases:
        - Modify LLM response
        - Extract metadata (token usage, latency)
        - Log response for debugging
        - Apply content filtering
        
        Args:
            context: Hook context with llm_response, messages, agent
            
        Returns:
            HookResult with success status and modified context
        """
        return HookResult(success=True, modified=False, context=context)
    
    async def on_pre_tool_call(self, context: HookContext) -> HookResult:
        """
        Called before tool execution.
        
        Use cases:
        - Modify tool parameters
        - Apply security policies
        - Validate tool inputs
        - Log tool calls
        
        Args:
            context: Hook context with tool_call, agent
            
        Returns:
            HookResult with success status and modified context
        """
        return HookResult(success=True, modified=False, context=context)
    
    async def on_post_tool_call(self, context: HookContext) -> HookResult:
        """
        Called after tool execution.
        
        Use cases:
        - Modify tool results
        - Apply transformations
        - Extract metadata
        - Log results
        
        Args:
            context: Hook context with tool_result, tool_call, agent
            
        Returns:
            HookResult with success status and modified context
        """
        return HookResult(success=True, modified=False, context=context)
    
    async def on_format_output(self, context: HookContext) -> HookResult:
        """
        Called to format final output.
        
        Use cases:
        - Convert to markdown
        - Convert to HTML
        - Apply styling
        - Add metadata
        
        Args:
            context: Hook context with output, agent
            
        Returns:
            HookResult with success status and modified context
        """
        return HookResult(success=True, modified=False, context=context)
    
    async def on_session_start(self, context: HookContext) -> HookResult:
        """
        Called when session starts.
        
        Use cases:
        - Inject initial system prompts
        - Initialize session state
        - Set up tracking
        - Apply session policies
        
        Args:
            context: Hook context with session_id, agent
            
        Returns:
            HookResult with success status and modified context
        """
        return HookResult(success=True, modified=False, context=context)
    
    async def on_session_end(self, context: HookContext) -> HookResult:
        """
        Called when session ends.
        
        Use cases:
        - Persist session state
        - Generate summaries
        - Cleanup resources
        - Log session metrics
        
        Args:
            context: Hook context with session_id, agent
            
        Returns:
            HookResult with success status and modified context
        """
        return HookResult(success=True, modified=False, context=context)
    
    async def on_llm_progress(self, context: HookContext) -> HookResult:
        """
        Called while a streaming LLM call is thinking, every few KB of thinking.

        The streaming loop awaits it, so it must return at once; anything slow
        belongs in a background task. Read-only: modifications are ignored.
        Clients that stream no thinking deltas (Gemini, batch) never fire it.

        Args:
            context: Hook context with reasoning_text, reasoning_chars,
                     previous_reasoning_chars, step, llm — no messages

        Returns:
            HookResult with success status
        """
        return HookResult(success=True, modified=False, context=context)

    async def on_pre_llm_request(self, context: HookContext) -> HookResult:
        """
        Called at the LLM client level just before sending API request.
        
        Unlike on_pre_llm_call (agent-level), this captures the exact
        serialized payload that will be sent to the API provider.
        
        Use cases:
        - Log exact API request payloads for debugging
        - Record request timing
        - Validate API payloads
        - Audit LLM API usage
        
        Args:
            context: Hook context with llm_request_payload, llm_provider,
                     llm_model, llm_request_url, llm_is_streaming
            
        Returns:
            HookResult with success status (read-only, should not modify context)
        """
        return HookResult(success=True, modified=False, context=context)
    
    async def on_post_llm_response(self, context: HookContext) -> HookResult:
        """
        Called at the LLM client level right after receiving API response.
        
        Unlike on_post_llm_call (agent-level), this captures the exact
        raw API response including errors, timing, and usage.
        
        Use cases:
        - Log exact API responses for debugging
        - Track request duration with ms precision
        - Log errors and rate limits
        - Record token usage
        - Audit LLM API costs
        
        Args:
            context: Hook context with llm_response_data, llm_provider,
                     llm_model, llm_duration_ms, llm_error, llm_usage,
                     llm_finish_reason, llm_is_streaming
            
        Returns:
            HookResult with success status (read-only, should not modify context)
        """
        return HookResult(success=True, modified=False, context=context)
    
    def get_order_spec(self) -> Dict[str, List[str]]:
        """
        Return ordering specification for this hook.
        
        Ordering spec defines before/after relationships with other hooks.
        Special values: "begin" (run first), "end" (run last)
        
        Returns:
            Dict with "before" and "after" lists of hook names
            
        Example:
            {
                "before": ["end"],  # Run before end
                "after": ["begin", "context_reducer"]  # Run after begin and context_reducer
            }
        """
        return self.config.get("order", {"before": [], "after": []})
