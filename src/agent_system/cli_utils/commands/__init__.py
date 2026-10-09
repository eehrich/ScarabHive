"""agent-cli's commands, one module per command family; agent_cli dispatches.

run.py (with one_shot.py) is `run` and `chat`; plugins.py, mcp.py, hooks.py
and reload.py are the commands that inspect or post without an agent.
"""

# Earlier `mcp`, `agent` and `plugins` modules here were dead copies with no
# importer, written against APIs removed long before (server_config.servers,
# config.mcp.enabled_servers, an Agent(config) signature that never existed).
# The live commands moved here from agent_cli.py's one long _main().
