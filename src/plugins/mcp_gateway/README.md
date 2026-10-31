# MCP gateway

Hands tools of this installation to an outside CLI -- Claude Code, Antigravity -- over MCP. Nothing is open by
configuration alone: a caller in the process that starts the CLI issues a token for one run of it, names the tools
that run may call, and revokes the token when the run ends.

- **Endpoint** `/plugins/mcp_gateway/mcp` -- MCP's Streamable HTTP, served by the mcp SDK's own server (stateless,
  JSON answers). Without the run's `Authorization: Bearer <token>` the request ends at 401. A process without the
  app (agent-run, an eval run) serves it on a loopback listener of its own: `grants.address()` names where to dial.
- **Grants** `plugins.mcp_gateway.grants.issue(...)` / `revoke(token)` -- per run: the account, the tool patterns, an
  optional session, parameters fixed to one value (`pinned`), allowed values reaching into lists (`allowed`), a
  lifetime.
- **Runner** `mcp_gateway_runner` -- every call goes through this agent's `dispatch_tool_call`: its allowlist is the
  ceiling of every token, its `pre_tool_call`/`post_tool_call` hooks see every call.

Both entries ship switched off (`agents/mcp_gateway.yaml`). Enable `mcp_gateway` and `mcp_gateway_runner`, and give
the runner tools with `+` entries in its `agent_config.tools.allowed`.

The full manual -- the grant API and its errors, what a call goes through, what the CLI's model sees, the settings
and the known gaps -- is the plugin's guide, `mcp_gateway.guide`, in the Help panel.
