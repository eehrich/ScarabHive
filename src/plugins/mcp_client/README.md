# MCP client

Connects ScarabHive to external MCP servers -- local programs started over stdio, or remote servers over
streamable HTTP or SSE -- and hands their tools to the agents as `<server>.<tool>`, next to the local plugins'
tools. Speaks the protocol through the official `mcp` SDK.

- **External tools** -- every tool of a connected server, selected in an agent's allowlist with the dot form
  (`"+everything.*"`, `"+blender.get_scene_info"`). Images and audio in an answer become files in the data
  directory; answers and tool descriptions are capped (`max_result_chars`, `max_description_chars`), and a
  server's `tools.blocked` list keeps tools away from the model.
- **Tools** `mcp_client_list_servers`, `_connect`, `_disconnect`, `_tools` -- see and steer the connections; granted
  with `"+mcp_client/*"`, which grants no external tool.
- **Chat commands** `/mcp`, `/mcp-connect`, `/mcp-disconnect`, `/mcp-tools` -- the same four, at the prompt.

Enable it in `config/plugins.yaml` (`mcp_client: {type: mcp_client, enabled: true}`, on by default) and declare the
servers in `config/mcp_servers.yaml` under `external_servers.remote_servers`.

The full manual -- naming and allowlists, what a call returns, timeouts and reconnecting, what a foreign server can
put in front of the model, the shared connections, and every setting -- is the plugin's guide, `mcp_client.guide`,
in the Help panel.
