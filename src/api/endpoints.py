from fastapi import HTTPException, APIRouter
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import logging

# Create an APIRouter instead of a full FastAPI app
router = APIRouter()
logger = logging.getLogger(__name__)

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
        # This endpoint would need to be properly integrated with the app state
        # For now, return a basic structure
        return {
            "messages": [],
            "usage_stats": {},
            "message_count": 0
        }
    except Exception as e:
        logger.error(f"Error getting debug messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/debug/context-stats")
async def get_context_stats():
    """Get context management statistics."""
    try:
        # This endpoint would need to be properly integrated with the app state
        # For now, return a basic structure
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