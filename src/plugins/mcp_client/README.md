# MCP client

Connects ScarabHive to external MCP servers -- local programs started over stdio, or remote servers over
streamable HTTP or SSE -- and hands their tools to the agents as `<server>.<tool>`, next to the local plugins'
tools. Speaks the protocol through the official `mcp` SDK.

- **External tools** -- every tool of a connected server, selected in an agent's allowlist with the dot form
  (`"+everything.*"`, `"+blender.get_scene_info"`). Images and audio in an answer become files in the data
  directory; answers and tool descriptions are capped (`max_result_chars`, `max_description_chars`), and a
  server's `tools.blocked` list keeps tools away from the model.
- **Resources and prompts** -- a server with `resources_and_prompts: true` that offers them gets
  `<server>.mcp_list_resources`, `mcp_read_resource`, `mcp_list_prompts` and `mcp_get_prompt`, reached through
  the same allowlist as its tools. A server's
  `list_changed` notification drops its cached tool list at once.
- **Questions to the person** -- a server that asks during one of its tools (MCP elicitation: a form or an
  address) gets the question into the chat, field by field; answered there like ask_user's, cancelled unasked
  when nobody watches the run (`elicitation: false` turns it off).
- **OAuth** -- `auth: {type: oauth}` for remote servers that need a login (dynamic registration or a configured
  client id); a person authorizes once with `/mcp-authorize <server>`, the tokens are kept and refreshed.
- **Tools** `mcp_client_list_servers`, `_connect`, `_authorize`, `_disconnect`, `_tools` -- see and steer the
  connections; granted with `"+mcp_client/*"`, which grants no external tool.
- **Chat commands** `/mcp`, `/mcp-connect`, `/mcp-authorize`, `/mcp-disconnect`, `/mcp-tools` -- the same five, at
  the prompt.
- **Routes** `GET /plugins/<instance>/oauth/callback` -- where the authorization server sends the browser back;
  `POST /plugins/<instance>/answer`, `GET .../pending` -- the answers to a server's questions.

Enable it in `config/plugins.yaml` (`mcp_client: {type: mcp_client, enabled: true}`, on by default) and declare the
servers in `config/mcp_servers.yaml` under `external_servers.remote_servers`.

The full manual -- naming and allowlists, what a call returns, timeouts and reconnecting, what a foreign server can
put in front of the model, the shared connections, and every setting -- is the plugin's guide, `mcp_client.guide`,
in the Help panel.
