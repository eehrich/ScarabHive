"""CLI command modules."""

# `mcp` is gone: it was written against an API that had been removed long ago
# (mcp_config.servers, transport_type), had no importer, and every call would
# have died with AttributeError. The live MCP subcommands are built in
# agent_cli.py on top of MCPService/ToolService.
# `agent` followed for the same reasons: no importer, and its Agent(config)
# call never matched the real Agent.__init__ signature (nor did Agent.run()/
# Agent.stop() exist) -- every invocation would have died in the except block.
from . import plugins

__all__ = ["plugins"]
