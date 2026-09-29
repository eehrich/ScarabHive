# Vendored editor addon

`godot_mcp/` is the Godot editor addon from
https://github.com/satelliteoflove/godot-mcp, copied verbatim (MIT, see LICENSE).

| | |
|---|---|
| Addon version | 4.1.11 (`godot_mcp/plugin.cfg`) |
| Upstream commit | `028b68d51b2e9801d6f186b2f0b28a3e259c9b9e` |
| Requires | Godot 4.5+ |
| Vendored | 2026-09-04 |

The plugin's `setup` tool copies this directory into a project's
`addons/godot_mcp` and enables it. Only the addon is vendored; the upstream
Node MCP server is not used -- `server.py` speaks the addon's WebSocket
protocol directly.

To update: replace `godot_mcp/` with the upstream directory
`godot/addons/godot_mcp` at the new commit, update this table, and run the
plugin tests plus one `setup` against a scratch project.
