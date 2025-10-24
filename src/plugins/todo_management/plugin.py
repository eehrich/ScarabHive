"""
TODO Management Plugin Factory

Exports PLUGIN_FACTORY for AgentSystem plugin discovery.
"""

from typing import Any, Dict, Optional

from plugins.todo_management.server import TodoManagementServer


def PLUGIN_FACTORY(config: Optional[Dict[str, Any]] = None) -> TodoManagementServer:
    """
    Create TodoManagementServer instance.
    
    Args:
        config: Plugin configuration (from plugins.yaml)
        
    Returns:
        TodoManagementServer instance
    """
    return TodoManagementServer(config=config)
