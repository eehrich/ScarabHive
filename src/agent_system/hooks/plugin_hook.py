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
        step: Current execution step number
        llm: Reference to the LLM client being used
    """
    hook_type: HookType
    request_id: str
    session_id: str
    agent: Optional[Agent] = None
    agent_name: str = ""
    messages: Optional[List[ChatMessage]] = None
    llm_response: Optional[Dict[str, Any]] = None
    tool_call: Optional[Dict[str, Any]] = None
    tool_result: Optional[Dict[str, Any]] = None
    output: Optional[str] = None
    output_format: str = "text"  # Target format: 'html', 'ansi', 'text', 'markdown'
    metadata: Dict[str, Any] = field(default_factory=dict)
    step: int = 0
    llm: Optional[Any] = None
    
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
        class MyHook(PluginHook):
            def __init__(self, name: str, config: dict):
                super().__init__(name, config)
                
            async def on_pre_llm_call(self, context: HookContext) -> HookResult:
                # Modify messages before LLM call
                if context.messages:
                    # Add custom message
                    context.messages.append(ChatMessage(role="system", content="Custom prompt"))
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
