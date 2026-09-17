# MCP Client Plugin

Connects to external MCP servers and hands their tools to the agents. Speaks
the current protocol through the official `mcp` SDK.

This used to be part of the core, welded into `ToolServerIntegration` next to the
plugin bootstrap, which has nothing to do with it. It is a plugin now: the core
asks *whoever* provides external tools, and this is the one that answers.

## What agents see

Nothing changes for them. A foreign tool is still called `server.tool` —
`context7.resolve-library-id`, not `resolve_library_id`. That dot is not
decoration: the tool-permission layer matches patterns against it, and the
runtime uses it to decide whether a call goes to a plugin or out to a server.
Handing these tools out under flat names would quietly widen what an agent is
allowed to call.

So an agent opts in with a dotted pattern, not the slashed one local plugins
use:

```yaml
tools:
  allowed:
    - "everything.*"        # every tool of the 'everything' server
    - "context7.resolve-library-id"   # or one by name
```

`mcp_client/*` is something else entirely: that grants the four management
tools below (connect, disconnect, list), not any foreign tool. And an empty
result is worth reading twice — if the server failed to connect, its tools were
never discovered, and *every* pattern matches nothing. Check
`External MCP servers: N connected` in the log before suspecting the pattern.

### The CLI needed one event loop for this to work

`agent-cli run` used to hand agents no foreign tools at all, and the reason was
structural rather than a misconfiguration. A connection counts as live while
its task runs (`ServerConnection.connected` is `self._task is not None and not
self._task.done()`), but `agent_cli.py` drove bootstrap and the agent run
through **separate `asyncio.run()` calls**, and `asyncio.run` closes its loop on
return. Every connection task was therefore already done by the time the agent
asked:

```
INFO  External MCP servers: 2 connected, 0 failed
PROBE conns=['everything','everything_local'] connected=[]
WARNING Agent ... allow list patterns produced an empty tool set
```

Under uvicorn one loop spans everything, which is why the same agent and server
always worked over `/run` — and why `agent-cli mcp test` looked fine too: it
opens a loop of its own and uses it immediately.

Fixed 2026-09-02: the CLI runs every step on one persistent loop
(`agent_cli.run_async`, torn down once at exit). Both routes now hand over the
same 13 tools, with the server's own wording in the log
(`External tool everything_local.get-sum returned: The sum of 19 and 23 is 42.`).
`tests/cli/test_cli_event_loop.py` pins the invariant.

The chat REPL had the same problem one layer up: it built a **private** loop
for its turns, so the bootstrap connections were parked on a loop that never
ran again — `connected` stayed True (the task was parked, not done) and every
call would have run into the submit timeout instead of failing fast. Chat now
borrows the CLI's shared loop (`run_chat_loop(loop=...)`) and leaves its
teardown to the owner; standalone use without `loop` keeps the private-loop
behaviour. Verified in a piped chat session: the tool call completes in the
turn, with the server's wording in the transcript.

## Configuration

Servers are configured in `config/mcp_servers.yaml`, unchanged:

```yaml
external_servers:
  remote_servers:
    context7:
      url: "https://server.example.com/mcp"
      transport: streaming        # see below
      enabled: true
      description: "Remote Context7 MCP"
      auth:
        type: bearer              # none | bearer | api_key | basic
        bearer_token: "${SOME_TOKEN}"
      tools:
        blocked: [dangerous_tool] # marked, not hidden — see below
```

The plugin itself is enabled in `config/plugins.yaml`. Leave it enabled even
when no external server is: it must be findable, otherwise an outage looks
exactly like "nothing is configured".

### Transports

| Config value | Goes to | Notes |
|---|---|---|
| `streaming`, `streamable_http`, `streamable-http`, `smithery` | `streamablehttp_client` | The usual case |
| `http` | `streamablehttp_client` | Used to mean bare JSON-RPC POSTs; no current server answers those (measured: HTTP 406), and those endpoints speak streamable HTTP today |
| `sse`, `http_sse`, `http+sse` | `sse_client` | The older HTTP+SSE transport |
| `stdio`, `local` | `stdio_client` | Needs `command` (and optionally `args`, `env`) instead of `url` |

### `initialization_options` is ignored

The previous client sent this as an `initializationOptions` field on
`initialize`. No such field exists in the protocol and servers ignored it. The
key is still accepted in the config, but connecting **warns** that it is not
sent. Put credentials in `auth:` or in the URL.

## Blocked tools are marked, not removed

A blocked tool still appears in the listing with `blocked: true`, and calling
it raises `PermissionError`. Dropping it from the listing instead would be
indistinguishable from the server not offering it, and the UI could no longer
show what is blocked.

## Tools

| Tool | Purpose |
|---|---|
| `mcp_client_list_servers` | Configured servers with connection state, protocol version, transport |
| `mcp_client_connect` | Connect or reconnect one server (full handshake, reports server info) |
| `mcp_client_disconnect` | Close one server's connection |
| `mcp_client_tools` | Tools of the connected servers; `force_refresh` bypasses the cache |

## Image and audio results become files, not text

A tool result's `image`/`audio` content blocks are written to
`data/media/external_mcp/<server>/` and returned as the house contract
(`_multimodal_content`, path-based) that the agent turns into a real
image part for the model. Before 2026-09-02 such a block either vanished
(the first text block won) or its base64 landed inside the JSON payload —
one blender `get_viewport_screenshot` pushed a live session past the
model's input limit. Text blocks next to the media survive as `message`.

Video has no standard MCP content block; a server shipping video does it
as a blob resource, which this path does not unpack (named gap). The
dump directory is not cleaned up automatically — screenshots are small,
but a long-running host may want a sweep eventually.

## One connection, one task

Worth knowing before changing `connection.py`: the SDK's transports and
`ClientSession` are `anyio` context managers, and anyio refuses to let a cancel
scope be exited by a task other than the one that entered it. Closing a session
from a request handler therefore raises

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

which is what the previous dict-of-clients design did. So each connection owns
one task; that task opens the transport, opens the session and then serves
commands from a queue. Every public method just puts work on the queue, which
makes the object safe to use — and to close — from anywhere. `stop()` posts a
sentinel so the owning task unwinds its own scopes.

## Tests

`tests/` drives a **real** MCP server (`tests/probe_server.py`, started over
stdio) rather than mocks. That is deliberate: the implementation this replaced
had a green test suite for years while being unable to talk to any current
server at all — it never sent `notifications/initialized` and announced
protocol `2024-11-05`. Mocks would have kept saying yes.

```bash
.venv/Scripts/python.exe -m pytest src/plugins/mcp_client/tests/ -q
```
