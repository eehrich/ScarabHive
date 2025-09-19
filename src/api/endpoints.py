from fastapi import HTTPException, APIRouter
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import logging

# Create an APIRouter instead of a full FastAPI app
router = APIRouter()
logger = logging.getLogger(__name__)

def get_agent():
    """Get the current agent instance from the global registry."""
    try:
        from ..agent_system.agent.interface_api import _app_registry
        if _app_registry and hasattr(_app_registry, 'get'):
            return _app_registry.get('agent')
    except ImportError:
        pass
    return None

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

@router.get("/api/debug/messages", response_model=MessageResponse)
async def get_debug_messages():
    """Get current conversation messages for debugging."""
    try:
        agent = get_agent()
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

        # Get context usage stats
        usage_stats = {}
        if hasattr(agent, 'context_manager') and agent.context_manager:
            usage_stats = agent.context_manager.get_usage_stats()
            # Add predicted tokens for current conversation
            if agent.conversation:
                predicted_tokens = agent.context_manager.estimate_token_count(agent.conversation)
                usage_stats['predicted_tokens'] = predicted_tokens

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
    """Get context management statistics."""
    try:
        agent = get_agent()
        if not agent:
            logger.warning("No agent available for context stats")
            return {
                "context_window": None,
                "prediction_threshold": None,
                "summarization_threshold": None,
                "actual_usage": None,
                "warning_levels": None
            }

        # Get context stats from agent's context manager
        if hasattr(agent, 'context_manager') and agent.context_manager:
            stats = agent.context_manager.get_usage_stats()
            return stats
        else:
            return {
                "context_window": None,
                "prediction_threshold": None,
                "summarization_threshold": None,
                "actual_usage": None,
                "warning_levels": None
            }
    except Exception as e:
        logger.error(f"Error getting context stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/agents/stats")
async def get_agent_stats():
    """Get per-agent context tracking statistics."""
    try:
        from agent_system.context.agent_tracker import get_all_agent_stats
        stats = get_all_agent_stats()
        return {
            "agent_count": len(stats),
            "agents": stats
        }
    except Exception as e:
        logger.error(f"Error getting agent stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "agent-system-api"}

@router.get("/api/version")
async def get_version():
    """Get API version information."""
    return {
        "version": "1.0.0",
        "api_version": "v1",
        "service": "agent-system"
    }