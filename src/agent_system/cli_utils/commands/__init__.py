"""CLI command modules."""

# Only `hooks` lives here. `mcp`, `agent` and `plugins` were dead copies with
# no importer, written against APIs removed long before (server_config.servers,
# config.mcp.enabled_servers, an Agent(config) signature that never existed).
# The live plugins and mcp subcommands are built in agent_cli.py.
