"""CLI command modules."""

# `mcp` is gone: it was written against an API that had been removed long ago
# (mcp_config.servers, transport_type), had no importer, and every call would
# have died with AttributeError. The live MCP subcommands are built in
# agent_cli.py on top of MCPService/ToolService.
from . import agent, plugins

__all__ = ["agent", "plugins"]
