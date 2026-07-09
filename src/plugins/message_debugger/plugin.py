"""Message Debugger Plugin - Factory and exports.

Hybrid plugin: Schema-based hooks + Web UI for viewing captured message snapshots.
Hook implementations are in hooks.py, web endpoints in web_endpoints.py.
Data stored in SQLite via database.py.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Dict, Any, TYPE_CHECKING

from agent_system.plugins.web_base import SchemaBasedPluginWebInterface
from .database import MessageDebuggerDB
from .hooks import MessageDebuggerPlugin
from .web_endpoints import MessageDebuggerWebFactory

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class MessageDebuggerHybridPlugin(SchemaBasedPluginWebInterface):
    """Hybrid plugin that provides both hooks and web capabilities.
    
    Uses SQLite for persistent storage of:
    - Agent-level message turns (pre/post LLM call)
    - Raw LLM API requests and responses
    """
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        """Initialize with standard hybrid plugin signature."""
        # Initialize base class (loads schema automatically)
        super().__init__(name, system_config, mcp_config)
        
        plugin_dir = Path(__file__).parent
        
        # Determine DB path from config
        config = {}
        if mcp_config and hasattr(mcp_config, 'config') and mcp_config.config:
            config = mcp_config.config
        
        db_path = config.get('db_path', None)
        if not db_path:
            # Default: data/message_debugger/debugger.db
            data_dir = Path('data') / 'message_debugger'
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(data_dir / 'debugger.db')
        else:
            # Ensure parent directory exists
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Create SQLite database with a hard size cap (auto-retention). Writes go
        # through a background thread (see MessageDebuggerDB) so capture never
        # blocks the agent; queue_max bounds that queue's memory.
        max_size_mb = config.get('max_db_size_mb', 5120)  # default 5 GB
        queue_max = config.get('capture_queue_max', 2000)
        self._db = MessageDebuggerDB(db_path, max_size_mb=max_size_mb, queue_max=queue_max)
        logger.info(
            f"MessageDebugger DB initialized at: {db_path} "
            f"(cap {max_size_mb} MB, write-queue {queue_max})"
        )
        
        # Legacy list kept for backward compat (not actively used)
        self._message_history: List[Dict[str, Any]] = []
        
        # Create hooks plugin with DB and config
        self.hooks_plugin = MessageDebuggerPlugin(
            plugin_dir, db=self._db,
            message_history=self._message_history,
            mcp_config=mcp_config,
        )
        
        # Create web UI factory with DB for queries
        self.web_factory = MessageDebuggerWebFactory(
            db=self._db, name=name, server=self,
        )
    
    # Hook interface - delegate to hooks plugin
    def get_hooks(self):
        """Return hooks from the hooks plugin."""
        return self.hooks_plugin.get_hooks()
    
    async def execute_hook(self, hook_type, context):
        """Execute hook - delegate to hooks plugin."""
        return await self.hooks_plugin.execute_hook(hook_type, context)
    
    # Web interface - delegate to web factory
    def get_web_router(self):
        """Return FastAPI router for web UI."""
        return self.web_factory.get_web_router()


PLUGIN_FACTORY = MessageDebuggerHybridPlugin
