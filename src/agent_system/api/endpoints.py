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


class PanelTab(BaseModel):
    instance_id: str
    label: str
    endpoint: str
    icon: Optional[str] = None
    plugin_name: str


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
    panel_group: Optional[str] = None
    panel_tabs: Optional[List[PanelTab]] = None


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
            "note": "Context management migrated to hook plugins (context_optimizer, context_summarizer)"
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


@router.get("/api/plugins/ui", response_model=List[PluginUIMetadata])
async def get_plugin_ui_metadata():
    """Get UI metadata for all plugins with web interfaces."""
    try:
        from agent_system.plugins.web_adapter import get_web_plugin_registry
        from agent_system.plugins.mcp_adapter import plugin_mcp_registry
        
        logger.debug("Getting plugin UI metadata...")
        
        registry = get_web_plugin_registry()
        logger.debug(f"Registry returned: {registry}")
        
        if not registry:
            logger.warning("Registry is empty or None")
            return []
        
        # Get the plugin registry to access already loaded schemas
        plugin_registry = plugin_mcp_registry
        
        ui_plugins = []
        # Track panel_groups to deduplicate buttons (one button per group)
        panel_group_added: set[str] = set()
        # Collect tabs for each panel_group
        panel_group_tabs: dict[str, list[dict]] = {}
        
        # First pass: collect all tabs for each panel_group
        for plugin_id, plugin_info in registry.items():
            try:
                plugin_server = plugin_registry.get_server(plugin_id)
                if plugin_server and plugin_server.plugin_schema:
                    schema = plugin_server.plugin_schema
                    web_ui = schema.get('web_ui') if schema else None
                    if web_ui:
                        panel_config = web_ui.get('panel', {})
                        panel_group = panel_config.get('panel_group')
                        if panel_group:
                            if panel_group not in panel_group_tabs:
                                panel_group_tabs[panel_group] = []
                            panel_group_tabs[panel_group].append({
                                "instance_id": plugin_id,
                                "label": plugin_id,
                                "endpoint": panel_config.get('endpoint', f'/plugins/{plugin_id}/'),
                                "icon": web_ui.get('button', {}).get('icon', ''),
                                "plugin_name": plugin_id
                            })
            except Exception:
                pass
        
        # Second pass: build UI metadata with tabs
        for plugin_id, plugin_info in registry.items():
            logger.debug(f"Processing plugin {plugin_id}: {plugin_info}")
            
            try:
                # Get schema from already registered plugin server instead of reloading from file
                plugin_server = plugin_registry.get_server(plugin_id)
                
                if plugin_server and plugin_server.plugin_schema:
                    schema = plugin_server.plugin_schema
                    logger.debug(f"Got schema for {plugin_id} from plugin server")
                    
                    web_ui = schema.get('web_ui') if schema else None
                    logger.debug(f"Web UI config for {plugin_id}: {web_ui}")
                    
                    if web_ui:
                        # Check button.enabled
                        button_config = web_ui.get('button', {})
                        button_enabled = button_config.get('enabled', False)
                        
                        if button_enabled:
                            panel_config = web_ui.get('panel', {})
                            
                            # Check for panel_group - only one button per group
                            panel_group = panel_config.get('panel_group')
                            if panel_group:
                                if panel_group in panel_group_added:
                                    logger.debug(f"Skipping button for {plugin_id} - panel_group {panel_group} already has button")
                                    continue
                                panel_group_added.add(panel_group)
                            
                            # Get tabs for this panel_group (if any)
                            tabs = None
                            if panel_group and panel_group in panel_group_tabs:
                                tabs = [PanelTab(**t) for t in panel_group_tabs[panel_group]]
                            
                            plugin_metadata = PluginUIMetadata(
                                id=plugin_id,
                                name=plugin_info.get('name', plugin_id),
                                enabled=True,
                                button_text=button_config.get('text', plugin_id.replace('_', ' ').title()),
                                button_icon=button_config.get('icon'),
                                panel_title=panel_config.get('title', plugin_info.get('name', plugin_id)),
                                panel_endpoint=panel_config.get('endpoint', f'/plugins/{plugin_id}/panel'),
                                panel_type='tabbed-iframe' if tabs and len(tabs) > 1 else panel_config.get('type', 'fetch'),
                                description=panel_config.get('description', plugin_info.get('description')),
                                panel_group=panel_group,
                                panel_tabs=tabs
                            )
                            ui_plugins.append(plugin_metadata)
                            logger.debug(f"Added UI plugin button: {plugin_metadata}")
                        else:
                            logger.debug(f"Plugin {plugin_id} button disabled in schema")
                    else:
                        logger.debug(f"Plugin {plugin_id} has no web_ui config")
                else:
                    logger.debug(f"No plugin server or schema found for {plugin_id}")
            except Exception as schema_error:
                logger.error(f"Error processing plugin {plugin_id}: {schema_error}")
        
        logger.debug(f"Returning {len(ui_plugins)} UI plugins: {ui_plugins}")
        return ui_plugins
    except Exception as e:
        logger.error(f"Error getting plugin UI metadata: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve plugin UI metadata: {str(e)}")
