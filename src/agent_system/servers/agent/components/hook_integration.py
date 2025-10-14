"""
Hook integration component for Agent lifecycle management.

Provides centralized hook execution at agent lifecycle points.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ....hooks import get_hook_registry, HookContext, HookType
from ....llm.models import ChatMessage

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


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
    
    def is_enabled(self) -> bool:
        """Check if hooks are enabled for this agent."""
        # Check agent config for hook enablement
        if hasattr(self.agent, 'agent_config') and hasattr(self.agent.agent_config, 'hooks'):
            return getattr(self.agent.agent_config.hooks, 'enabled', True)
        return self._enabled
    
    async def execute_pre_llm_hooks(
        self,
        messages: List[ChatMessage],
        step: int,
        request_id: str,
        session_id: str,
        llm: Optional[Any] = None
    ) -> List[ChatMessage]:
        """
        Execute pre-LLM hooks.
        
        Args:
            messages: Current conversation messages
            step: Current execution step
            request_id: Request identifier
            session_id: Session identifier
            llm: LLM client instance
            
        Returns:
            Potentially modified messages list
        """
        if not self.is_enabled():
            return messages
        
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id=request_id,
            session_id=session_id,
            agent=self.agent,
            agent_name=self.agent.name,
            messages=messages,
            step=step,
            llm=llm,
        )
        
        modified_context = await self.registry.execute_hooks(HookType.PRE_LLM_CALL, context)
        
        # Return modified messages if hooks changed them
        if modified_context.messages is not None:
            return modified_context.messages
        return messages
    
    async def execute_post_llm_hooks(
        self,
        messages: List[ChatMessage],
        llm_response: Dict[str, Any],
        step: int,
        request_id: str,
        session_id: str,
        llm: Optional[Any] = None
    ) -> Dict[str, Any]:
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
            Potentially modified LLM response
        """
        if not self.is_enabled():
            return llm_response
        
        context = HookContext(
            hook_type=HookType.POST_LLM_CALL,
            request_id=request_id,
            session_id=session_id,
            agent=self.agent,
            agent_name=self.agent.name,
            messages=messages,
            llm_response=llm_response,
            step=step,
            llm=llm,
        )
        
        modified_context = await self.registry.execute_hooks(HookType.POST_LLM_CALL, context)
        
        # Return modified LLM response if hooks changed it
        if modified_context.llm_response is not None:
            return modified_context.llm_response
        return llm_response
    
    async def execute_pre_tool_hooks(
        self,
        tool_call: Dict[str, Any],
        step: int,
        request_id: str,
        session_id: str
    ) -> Dict[str, Any]:
        """
        Execute pre-tool hooks.
        
        Args:
            tool_call: Tool call information
            step: Current execution step
            request_id: Request identifier
            session_id: Session identifier
            
        Returns:
            Potentially modified tool call
        """
        if not self.is_enabled():
            return tool_call
        
        context = HookContext(
            hook_type=HookType.PRE_TOOL_CALL,
            request_id=request_id,
            session_id=session_id,
            agent=self.agent,
            agent_name=self.agent.name,
            tool_call=tool_call,
            step=step,
        )
        
        modified_context = await self.registry.execute_hooks(HookType.PRE_TOOL_CALL, context)
        
        # Return modified tool call if hooks changed it
        if modified_context.tool_call is not None:
            return modified_context.tool_call
        return tool_call
    
    async def execute_post_tool_hooks(
        self,
        tool_call: Dict[str, Any],
        tool_result: Dict[str, Any],
        step: int,
        request_id: str,
        session_id: str
    ) -> Dict[str, Any]:
        """
        Execute post-tool hooks.
        
        Args:
            tool_call: Tool call information
            tool_result: Tool execution result
            step: Current execution step
            request_id: Request identifier
            session_id: Session identifier
            
        Returns:
            Potentially modified tool result
        """
        if not self.is_enabled():
            return tool_result
        
        context = HookContext(
            hook_type=HookType.POST_TOOL_CALL,
            request_id=request_id,
            session_id=session_id,
            agent=self.agent,
            agent_name=self.agent.name,
            tool_call=tool_call,
            tool_result=tool_result,
            step=step,
        )
        
        modified_context = await self.registry.execute_hooks(HookType.POST_TOOL_CALL, context)
        
        # Return modified tool result if hooks changed it
        if modified_context.tool_result is not None:
            return modified_context.tool_result
        return tool_result
    
    async def execute_format_output_hooks(
        self,
        output: str,
        request_id: str,
        session_id: str
    ) -> str:
        """
        Execute format-output hooks.
        
        Args:
            output: Final output string
            request_id: Request identifier
            session_id: Session identifier
            
        Returns:
            Potentially formatted output
        """
        if not self.is_enabled():
            return output
        
        context = HookContext(
            hook_type=HookType.FORMAT_OUTPUT,
            request_id=request_id,
            session_id=session_id,
            agent=self.agent,
            agent_name=self.agent.name,
            output=output,
        )
        
        modified_context = await self.registry.execute_hooks(HookType.FORMAT_OUTPUT, context)
        
        # Return modified output if hooks changed it
        if modified_context.output is not None:
            return modified_context.output
        return output
    
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
            agent=self.agent,
            agent_name=self.agent.name,
            messages=messages,
        )
        
        modified_context = await self.registry.execute_hooks(HookType.SESSION_START, context)
        
        # Return modified messages if hooks changed them
        return modified_context.messages
    
    async def execute_session_end_hooks(
        self,
        session_id: str,
        request_id: str,
        messages: Optional[List[ChatMessage]] = None
    ) -> None:
        """
        Execute session-end hooks.
        
        Args:
            session_id: Session identifier
            request_id: Request identifier
            messages: Optional final messages
        """
        if not self.is_enabled():
            return
        
        context = HookContext(
            hook_type=HookType.SESSION_END,
            request_id=request_id,
            session_id=session_id,
            agent=self.agent,
            agent_name=self.agent.name,
            messages=messages,
        )
        
        await self.registry.execute_hooks(HookType.SESSION_END, context)
