# Tool Script Plugin

Scripted tool chains: the agent submits ONE small Python script that
orchestrates its other tools server-side via `call_tool(name, **params)`.
Intermediate results never enter the LLM context — only the script and its
final `result` cost tokens. Kills the re-typing token tax AND the re-typing
corruption class (values shortened, `\n` un-escaped, commas dropped).

Design: [docs/tool_script_plugin_design.md](../../../docs/tool_script_plugin_design.md)
(concept v2, panel-reviewed).

## Tool

One tool: `<instance>_run_script(script, timeout?, dry_run?)`.

```python
# example script — v6 ritual step, 4 agent turns collapsed into 1 call:
merge = call_tool("v6_json_manage_json", operation="merge_doc",
                  source="doc_a1b2", doc="synopsis", namespace="624")
delta = call_tool("v6_json_manage_json", operation="read",
                  doc="doc_a1b2", namespace="624")
call_tool("debate_forum_post_message", channel_id=90140,
          content="### 2.2 World\n```json\n" + delta["json"] + "\n```")
result = {"merged_keys": merge["merged_keys"]}
```

## Security model

- **Dispatch** goes through `Agent.dispatch_tool_call` (core): same server
  resolution, same `tools.allowed` AND `tools.blocked` semantics as schema
  build (shared matcher + parity test). What the LLM cannot see, the script
  cannot call — in both directions.
- **Tool hooks:** every call passes the agent's `pre_tool_call` /
  `post_tool_call` hooks like the model's own calls (`source: "tool_script"`,
  `docs/plugin_hooks.md`). A call a hook blocks raises `ToolCallError` with the
  hook's text; `per_call_timeout` bounds the hooks of a call too. The
  `inject_params` values are merged after the hooks — no hook sees a secret.
  A tool that raises comes back as an error result (it passes the post hooks)
  and raises `ToolCallError` like any error result.
- Runtime params (`_session_id`, `_agent`, ...) are injected AFTER validating
  that script params are plain data — scripts cannot forge context.
- Tool RESULTS are sanitized through a JSON round-trip — live objects can
  never leak into script variables.
- Sandbox = `script_interpreter`'s SafeExecutor (no imports, no attribute
  tricks, loop guards, output caps); fresh executor per call.
- No recursion: `*_run_script` tools are never callable from scripts.
- Caps: `max_tool_calls`, `per_call_timeout`, `max_call_result_bytes`,
  `max_result_chars`; one running script per session.

## Error contract

- Params are validated against the target tool's JSON schema at the call
  site — typos fail immediately with the valid parameter list.
- A tool result `{"status": "error"}` raises `ToolCallError` (catchable
  in-script with `try/except ToolCallError`).
- On failure the report contains: failing line, every executed call with
  ok/error + duration, a `committed_side_effects` summary and a size-capped
  variables snapshot — the agent continues AFTER the last committed call
  instead of redoing the chain. Executed side effects are never rolled back.

## Configuration

```yaml
pipe:
  type: tool_script
  enabled: true
  config:
    timeout: 120                  # overall budget (s), enforced between hops
    per_call_timeout: 60
    max_tool_calls: 20
    max_call_result_bytes: 524288
    max_result_chars: 20000
    max_output_length: 4000
    allowed_tools: []             # optional fnmatch narrowing (never widens)
    blocked_tools: []
    inject_params: {}             # optional: {tool-pattern: {param: value}}
```

### Param injection (`inject_params`)

Server-side constants merged into matching `call_tool` params — for secrets
(`write_key`, tokens) that must never flow through the LLM (LLM-typed values
are transposition-prone). Config values ALWAYS override script-provided
values, and injection runs BEFORE schema validation so scripts may omit the
parameter entirely. Patterns use fnmatch like `allowed_tools`.

```yaml
    inject_params:
      "writer_content_story": { write_key: "..." }
      "writer_issues_op": { write_key: "..." }
```

Give the agent the tool via its normal allowlist (`- "pipe/*"`). Everything a
script may call must ALSO be in the agent's `tools.allowed` — the plugin never
widens permissions.

## Notes

- External tools (dotted names) are not callable from scripts (v1).
- The overall `timeout` cannot interrupt an in-flight inner call; worst-case
  wall clock is bounded by `max_tool_calls × per_call_timeout`.
- Cancellation is honored between hops (long-running inner tools that check
  the token themselves abort sooner).
