from fastapi import HTTPException, APIRouter, Depends
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import logging

from .. import __version__
from .dependencies import get_agent_optional, get_config_optional

# Create an APIRouter instead of a full FastAPI app
router = APIRouter()
logger = logging.getLogger(__name__)


# Pydantic models for API responses
class MessageResponse(BaseModel):
    messages: List[Dict[str, Any]]
    usage_stats: Dict[str, Any]
    message_count: int


class ContextStatsResponse(BaseModel):
    context_window: Optional[int] = None
    prediction_threshold: Optional[float] = None
    summarization_threshold: Optional[float] = None
    actual_usage: Optional[Dict[str, Any]] = None
    warning_levels: Optional[Dict[str, Any]] = None


# Helper functions
# Deprecated - use dependency injection instead


def get_agent():
    """
    DEPRECATED: Get the current agent instance from the global registry.
    Use dependency injection with get_agent_optional() instead.
    """
    logger.warning("Deprecated get_agent() called - use dependency injection")
    return None


def get_config():
    """
    DEPRECATED: Get the current system configuration.
    Use dependency injection with get_config_optional() instead.
    """
    logger.warning("Deprecated get_config() called - use dependency injection")
    return None


# Existing endpoints

@router.get("/api/debug/messages", response_model=MessageResponse)
async def get_debug_messages(agent=Depends(get_agent_optional)):
    """Get current conversation messages for debugging."""
    try:
        if not agent:
            logger.warning("No agent available for debug messages")
            return {
                "messages": [],
                "usage_stats": {},
                "message_count": 0
            }

        # Get current conversation messages
        messages = []
        if hasattr(agent, 'conversation') and agent.conversation:
            messages = [
                {
                    "role": getattr(msg, 'role', 'unknown'),
                    "content": getattr(msg, 'content', ''),
                    "tool_calls": getattr(msg, 'tool_calls', None),
                    "tool_call_id": getattr(msg, 'tool_call_id', None),
                }
                for msg in agent.conversation
            ]

        # Context management stats are now handled by hook plugins
        # No centralized context_manager attribute anymore
        usage_stats = {}

        return {
            "messages": messages,
            "usage_stats": usage_stats,
            "message_count": len(messages)
        }
    except Exception as e:
        logger.error(f"Error getting debug messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/debug/context-stats")
async def get_context_stats():
    """Get context management statistics (deprecated - now handled by hook plugins)."""
    try:
        # Context management is now handled by hook plugins
        # No centralized context_manager attribute anymore
        return {
            "context_window": None,
            "prediction_threshold": None,
            "summarization_threshold": None,
            "actual_usage": None,
            "warning_levels": None,
            "note": "Context management migrated to hook plugins (context_engineer, context_summarizer)"
        }
    except Exception as e:
        logger.error(f"Error getting context stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "agent-system-api"}

@router.get("/api/version")
async def get_version(config=Depends(get_config_optional)):
    """Version information: the framework version config.yaml declares."""
    return {
        "version": config.version if config else __version__,
        "api_version": "v1",
        "service": "agent-system"
    }
