"""Context Engineer Plugin - Advanced context management for AI agents.

This plugin provides:
- Tool Result Clearing: Replace old tool outputs with compact references
- Variable Substitution: Replace large content blocks with $VAR_N references
- Core Memory: Compact facts store always in context
- Archival Memory: Full conversation history with semantic search
- Layered Compaction Strategy: Progressive compression based on reversibility
"""

from plugins.context_engineer.plugin import (
    PLUGIN_FACTORY,
    ContextEngineerHybridPlugin,
)

__all__ = ["PLUGIN_FACTORY", "ContextEngineerHybridPlugin"]
