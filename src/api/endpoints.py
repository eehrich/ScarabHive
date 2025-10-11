from fastapi import HTTPException, APIRouter
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import logging

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


class ConfigAgentInfo(BaseModel):
    """Information about a configuration-based agent"""
    name: str
    enabled: bool
    description: Optional[str] = None
    base_type: str
    llm_profile: str
    max_steps: int
    system_template: Optional[str] = None
    has_inline_prompt: bool
    tools: Optional[Dict[str, List[str]]] = None
    context_management: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any]


class ConfigAgentListResponse(BaseModel):
    """List of configuration-based agents"""
    total: int
    enabled: int
    disabled: int
    agents: List[Dict[str, Any]]


class ValidationResult(BaseModel):
    """Validation result for config agents"""
    agent_name: str
    valid: bool
    errors: List[str]


class ValidationResponse(BaseModel):
    """Response for config agents validation"""
    total: int
    passed: int
    failed: int
    results: List[ValidationResult]


# Helper functions
def get_agent():
    """Get the current agent instance from the global registry."""
    try:
        from ..agent_system.agent.interface_api import _app_registry
        if _app_registry and hasattr(_app_registry, 'get'):
            return _app_registry.get('agent')
    except ImportError:
        pass
    return None


def get_config():
    """Get the current system configuration."""
    try:
        from agent_system.config.settings import load_settings
        return load_settings()
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return None


# Existing endpoints

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
                        
                        if web_ui:
                            # Check button.enabled
                            button_config = web_ui.get('button', {})
                            button_enabled = button_config.get('enabled', False)
                            
                            if button_enabled:
                                panel_config = web_ui.get('panel', {})
                                
                                plugin_metadata = PluginUIMetadata(
                                    id=plugin_id,
                                    name=plugin_info.get('name', plugin_id),
                                    enabled=True,
                                    button_text=button_config.get('text', plugin_id.replace('_', ' ').title()),
                                    button_icon=button_config.get('icon'),
                                    panel_title=panel_config.get('title', plugin_info.get('name', plugin_id)),
                                    panel_endpoint=panel_config.get('endpoint', f'/plugins/{plugin_id}/panel'),
                                    panel_type=panel_config.get('type', 'fetch'),
                                    description=panel_config.get('description', plugin_info.get('description'))
                                )
                                ui_plugins.append(plugin_metadata)
                                logger.info(f"Added UI plugin button: {plugin_metadata}")
                            else:
                                logger.info(f"Plugin {plugin_id} button disabled in schema")
                        else:
                            logger.info(f"Plugin {plugin_id} has no web_ui config")
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


# Configuration-Based Agents Endpoints (Epic 0043)

@router.get("/api/config-agents", response_model=ConfigAgentListResponse)
async def list_config_agents():
    """List all configuration-based agents."""
    try:
        config = get_config()
        if not config or not config.mcp_system:
            return ConfigAgentListResponse(total=0, enabled=0, disabled=0, agents=[])
        
        from agent_system.plugins.config_agent_discovery import list_config_agents as list_agents
        agents_list = list_agents(config.mcp_system)
        
        enabled_count = sum(1 for a in agents_list if a.get('enabled', False))
        disabled_count = len(agents_list) - enabled_count
        
        return ConfigAgentListResponse(
            total=len(agents_list),
            enabled=enabled_count,
            disabled=disabled_count,
            agents=agents_list
        )
    except Exception as e:
        logger.error(f"Error listing config agents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/config-agents/{agent_name}", response_model=ConfigAgentInfo)
async def get_config_agent_details(agent_name: str):
    """Get detailed information about a specific configuration-based agent."""
    try:
        config = get_config()
        if not config or not config.mcp_system:
            raise HTTPException(status_code=404, detail="Configuration not found")
        
        from agent_system.plugins.config_agent_discovery import get_config_agent_info
        
        try:
            info = get_config_agent_info(agent_name, config.mcp_system)
            return ConfigAgentInfo(**info)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"Config agent '{agent_name}' not found"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting config agent details: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/config-agents/validate/all", response_model=ValidationResponse)
async def validate_all_config_agents():
    """Validate all configuration-based agents."""
    try:
        config = get_config()
        if not config or not config.mcp_system:
            return ValidationResponse(total=0, passed=0, failed=0, results=[])
        
        from agent_system.plugins.config_agent_validation import validate_all_config_agents as validate_all
        
        # Get LLM profiles for validation
        llm_profiles = list(config.llm_system.profiles.keys()) if config.llm_system else None
        
        validation_results = validate_all(config.mcp_system, llm_profiles)
        
        results = [
            ValidationResult(
                agent_name=name,
                valid=len(errors) == 0,
                errors=errors
            )
            for name, errors in validation_results.items()
        ]
        
        passed = sum(1 for r in results if r.valid)
        failed = len(results) - passed
        
        return ValidationResponse(
            total=len(results),
            passed=passed,
            failed=failed,
            results=results
        )
    except Exception as e:
        logger.error(f"Error validating config agents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/config-agents/validate/{agent_name}", response_model=ValidationResult)
async def validate_config_agent(agent_name: str):
    """Validate a specific configuration-based agent."""
    try:
        config = get_config()
        if not config or not config.mcp_system:
            raise HTTPException(status_code=404, detail="Configuration not found")
        
        if not config.mcp_system.config_agents or agent_name not in config.mcp_system.config_agents:
            raise HTTPException(
                status_code=404,
                detail=f"Config agent '{agent_name}' not found"
            )
        
        from agent_system.plugins.config_agent_validation import validate_config_agent as validate_agent
        
        # Get LLM profiles for validation
        llm_profiles = list(config.llm_system.profiles.keys()) if config.llm_system else None
        
        definition = config.mcp_system.config_agents[agent_name]
        errors = validate_agent(agent_name, definition, llm_profiles)
        
        return ValidationResult(
            agent_name=agent_name,
            valid=len(errors) == 0,
            errors=errors
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validating config agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))
