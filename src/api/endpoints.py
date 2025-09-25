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

class PluginUIMetadata(BaseModel):
    id: str
    name: str
    enabled: bool
    button_text: str
    button_icon: Optional[str] = None
    panel_title: str
    panel_endpoint: str
    panel_type: str
    description: Optional[str] = None

@router.get("/api/plugins/ui", response_model=List[PluginUIMetadata])
async def get_plugin_ui_metadata():
    """Get UI metadata for all plugins with web interfaces."""
    try:
        from agent_system.plugins.web_adapter import get_web_plugin_registry
        import yaml
        from pathlib import Path
        
        logger.info("Getting plugin UI metadata...")
        
        registry = get_web_plugin_registry()
        logger.info(f"Registry returned: {registry}")
        
        if not registry:
            logger.warning("Registry is empty or None")
            return []
        
        ui_plugins = []
        for plugin_id, plugin_info in registry.items():
            logger.info(f"Processing plugin {plugin_id}: {plugin_info}")
            
            # Check if plugin has web UI configuration
            schema_path = plugin_info.get('schema_path')
            logger.info(f"Schema path for {plugin_id}: {schema_path}")
            
            if schema_path:
                schema_file = Path(schema_path)
                logger.info(f"Checking schema file: {schema_file} (exists: {schema_file.exists()})")
                
                if schema_file.exists():
                    try:
                        with open(schema_file, 'r') as f:
                            schema = yaml.safe_load(f)
                        logger.info(f"Loaded schema for {plugin_id}: {schema}")
                        
                        web_ui = schema.get('web_ui') if schema else None
                        logger.info(f"Web UI config for {plugin_id}: {web_ui}")
                        
                        if web_ui and web_ui.get('enabled', False):
                            plugin_metadata = PluginUIMetadata(
                                id=plugin_id,
                                name=plugin_info.get('name', plugin_id),
                                enabled=True,
                                button_text=web_ui.get('button_text', plugin_id.replace('_', ' ').title()),
                                button_icon=web_ui.get('button_icon'),
                                panel_title=web_ui.get('panel_title', plugin_info.get('name', plugin_id)),
                                panel_endpoint=web_ui.get('panel_endpoint', f'/plugins/{plugin_id}/panel'),
                                panel_type=web_ui.get('panel_type', 'fetch'),
                                description=web_ui.get('description', plugin_info.get('description'))
                            )
                            ui_plugins.append(plugin_metadata)
                            logger.info(f"Added UI plugin: {plugin_metadata}")
                        else:
                            logger.info(f"Plugin {plugin_id} web UI not enabled or missing")
                    except Exception as schema_error:
                        logger.error(f"Error parsing schema for {plugin_id}: {schema_error}")
                else:
                    logger.warning(f"Schema file does not exist: {schema_file}")
            else:
                logger.info(f"No schema path for plugin {plugin_id}")
        
        logger.info(f"Returning {len(ui_plugins)} UI plugins: {ui_plugins}")
        return ui_plugins
    except Exception as e:
        logger.error(f"Error getting plugin UI metadata: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve plugin UI metadata: {str(e)}")