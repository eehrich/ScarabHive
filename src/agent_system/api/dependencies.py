"""
API Dependencies

Provides FastAPI dependency injection for common resources.
Replaces global registry anti-pattern (Issue #13).
"""

from __future__ import annotations

from typing import Optional
from fastapi import Request, HTTPException
import logging

from agent_system.config.models import AgentSystemConfig

logger = logging.getLogger(__name__)


async def get_agent(request: Request):
    """
    Get the agent instance from application state.
    
    Args:
        request: FastAPI request containing app state
        
    Returns:
        Agent instance
        
    Raises:
        HTTPException: If agent not initialized
    """
    agent = getattr(request.app.state, 'agent', None)
    if not agent:
        logger.warning("Agent not available in application state")
        raise HTTPException(
            status_code=503, 
            detail="Agent not initialized"
        )
    return agent


async def get_agent_optional(request: Request):
    """
    Get the agent instance from application state (optional).
    
    Args:
        request: FastAPI request containing app state
        
    Returns:
        Agent instance or None if not available
    """
    return getattr(request.app.state, 'agent', None)


async def get_config(request: Request) -> AgentSystemConfig:
    """
    Get the system configuration from application state.
    
    Args:
        request: FastAPI request containing app state
        
    Returns:
        System configuration
        
    Raises:
        HTTPException: If config not available
    """
    config = getattr(request.app.state, 'config', None)
    if not config:
        logger.warning("Config not available in application state")
        raise HTTPException(
            status_code=503,
            detail="Configuration not initialized"
        )
    return config


async def get_config_optional(request: Request) -> Optional[AgentSystemConfig]:
    """
    Get the system configuration from application state (optional).
    
    Args:
        request: FastAPI request containing app state
        
    Returns:
        System configuration or None if not available
    """
    return getattr(request.app.state, 'config', None)


async def get_session_manager(request: Request):
    """
    Get the session manager from application state.
    
    Args:
        request: FastAPI request containing app state
        
    Returns:
        SessionManager instance
        
    Raises:
        HTTPException: If session manager not initialized
    """
    manager = getattr(request.app.state, 'session_manager', None)
    if not manager:
        logger.warning("Session manager not available in application state")
        raise HTTPException(
            status_code=503,
            detail="Session manager not initialized"
        )
    return manager


async def get_mcp_registry(request: Request):
    """
    Get the MCP registry from application state.
    
    Args:
        request: FastAPI request containing app state
        
    Returns:
        MCP registry instance or None if not available
    """
    return getattr(request.app.state, 'mcp_registry', None)
