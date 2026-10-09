"""
API Dependencies

Provides FastAPI dependency injection for common resources.
Replaces global registry anti-pattern (Issue #13).
"""

from __future__ import annotations

from typing import Any, Optional
from fastapi import Request, HTTPException
import logging

from agent_system.config.models import AgentSystemConfig

logger = logging.getLogger(__name__)


def _state_or_503(request: Request, attr: str, logged_as: str, detail_as: str) -> Any:
    """``request.app.state.<attr>``, or a 503 while the app has not set it.

    The shape of every required dependency below. ``logged_as`` names the
    resource in the warning ("... not available in application state"),
    ``detail_as`` in the response ("... not initialized").
    """
    value = getattr(request.app.state, attr, None)
    if not value:
        logger.warning(f"{logged_as} not available in application state")
        raise HTTPException(
            status_code=503,
            detail=f"{detail_as} not initialized"
        )
    return value


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
    return _state_or_503(request, 'agent', "Agent", "Agent")


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
    return _state_or_503(request, 'config', "Config", "Configuration")


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
    return _state_or_503(request, 'session_manager', "Session manager", "Session manager")


async def get_tool_registry(request: Request):
    """
    Get the tool registry from application state.
    
    Args:
        request: FastAPI request containing app state
        
    Returns:
        tool registry instance or None if not available
    """
    return getattr(request.app.state, 'tool_registry', None)
