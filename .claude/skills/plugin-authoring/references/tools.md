# Implementing tools

Plugins that follow the current pattern: `src/plugins/json_store`, `sqlite_query`,
`tool_script`, `todo`.

## Server

```python
import asyncio
import logging
from agent_system.tools.schema_based import SchemaBasedToolServer

logger = logging.getLogger(__name__)   # there is no self.logger

class MyServer(SchemaBasedToolServer):
    def __init__(self, name, system_config, server_config):
        super().__init__(name, system_config, server_config)   # schema renders lazily on first get_tools()
        self.max_rows = int(getattr(server_config, "max_rows", 200))

    async def query(self, params: dict) -> dict:            # tool "{name}_query"
        status = params.get("_status")
        token = params.get("_cancellation_token")
        sql = params.get("sql")
        if not sql:
            if status: await status.error("sql parameter is required")
            return {"status": "error", "error": "sql parameter is required"}
        if token and token.is_cancelled:
            return {"error": "Tool 'query' was cancelled.", "cancelled": True, "forced": False}
        rows = await asyncio.to_thread(self._run, sql)      # never block the event loop
        rows = rows[: self.max_rows]
        if status: await status.end(f"Query returned {len(rows)} row(s)")
        return {"status": "success", "rows": rows}
```

A broken `schema.yaml` does not fail in `__init__` — only on the first `get_tools()`.
Test that path.

## schema.yaml

```yaml
tools:
  - type: function
    function:
      name: "{{ name }}_query"
      description: |
        What it does, when to use it, what it returns, how to recover from errors.
      parameters:
        type: object
        properties:
          sql: {type: string, description: "..."}
        required: ["sql"]
        additionalProperties: false     # INSIDE parameters
commands: []    # optional, slash commands
web_ui: {}      # optional, see skill panel-authoring
```

- The file is **rendered as Jinja first**, then parsed as YAML. The only default
  variable is `{{ name }}` = instance name. Overriding `get_template_vars()` replaces
  the dict, so put `"name": self.name` back in. Forgotten variables render
  **silently as an empty string**.
- **Routing** (`tools/schema_mixin.py`): tool `{name}_x` → method `x`; tool exactly
  `{name}` → `execute`; otherwise the method of the same name. Sync and async both work.
- **Always prefix `{{ name }}_`** — the model's tool name is the rendered
  `function.name`, and two equal names collide silently. Only `[a-zA-Z0-9_-]`.
- **Parameter names never start with `_` and are never `request_id` / `requestId`** —
  model-supplied values for those are dropped (the framework sets them).
- Descriptions are the contract with the model: what, when, return value, write
  protection, recovery (example `json_store/schema.yaml`). Overridable per instance
  via `self_tool_descriptions` (server level).

## Handler contract

- Signature `async def tool(self, params: dict) -> dict`.
- Injected by the framework: `_status`, `_request_id`, `_session_id`, `_user_id`,
  `_agent_name`, `_agent`, `_cancellation_token` (**only with a request id** — absent
  in tests, CLI and `dispatch_tool_call`).
- The return value reaches the model as `json.dumps(result, ensure_ascii=False, default=str)`.
- **No argument validation by the framework** — only malformed JSON is rejected.
  Check required fields, enums and ranges yourself.

| Case | Shape |
|---|---|
| success | `{"status": "success", ...}` |
| error | `{"status": "error", "error": "<actionable text>"}` |
| cancelled | `{"error": "Tool 'x' was cancelled.", "cancelled": True, "forced": bool}` |

- The error net in `call_with_status` (`tools/base.py`) recognises `status == "error"`,
  `error` without `status`, and `success: False` with `error`. **Not recognised**
  (shows "completed"): `{"status": "failed"}`, `{"success": False}` without `error`.
- Raising an exception also works (the model gets `{"error": str(e)}`), but a text
  the model can act on is better (`sqlite_query`: `did_you_mean` + table list).
- **Don't raise `CancellationError` from a tool** — like any exception it becomes
  `{"error": str(e)}`, not the cancelled shape. Return the cancelled dict.

## Asking the person watching the run

Don't build a second question box: `ask_user` is the tool for it, and a tool or
hook that must ask a person itself uses the shared pieces -- a
`core.run_questions.QuestionBroker` subclass for the questions,
`put_to_person(broker, question, scope, meta_key=..., ...)` for the status row and
the wait (answer, `TIMEOUT`, `CANCELLED`, `GONE`, or what your `interrupt=` check returns), `api.question_routes.question_router`
for `/answer` + `/pending` with the owner-or-admin rule. Ask only where
`is_read(request_id, grace)` is true; the chat draws a box only for the meta keys
`syncQuestionActions` knows. Worked examples: `src/plugins/ask_user`, `src/plugins/tool_approval`.

## Multimodal

```python
return {"status": "success",
        "_multimodal_content": [{"type": "image", "path": str(png),
                                 "mime_type": "image/png", "description": "..."}]}
```

Removed from the dict and attached to the ChatMessage (`MultimodalToolContent`,
`llm/models.py`). Gemini gets it natively, OpenAI/Anthropic as a synthetic user
message. Plugin path only, not for external MCP tools.

## Status

`StatusScope` has exactly `progress(msg, meta=None)`, `end(msg, meta=None)`,
`error(msg, meta=None)`. **`complete`, `info`, `warning` do not exist.**

- Steps via `progress`, exactly one closing `end` or `error`.
- The end line names the result (count, ids, titles), ≤140 chars — the WebUI shows
  only that line.
- `tests/plugins/test_status_end_lines.py`:
  - driven: real calls, exactly one END/ERROR, line ≤140, mentions values from the
    result, not just filler words + tool name
  - AST over `src/plugins`: every `.end("…")`/`.error("…")` with a constant text that
    says nothing once plugin/function words are removed fails (including
    `logger.error("completed")`)
  - tools the driven layer can't reach are listed with a reason in `UNCOVERED`; stale
    entries fail the test

## Cancellation

`agent_system.core.cancellation.CancellationToken`: `is_cancelled`, `is_forced`,
`add_cleanup_callback`, `await cleanup()`.

- The token can be `None` — always `if token and token.is_cancelled`.
- Check before starting and between chunks/hops (`tool_script/server.py`).
- Blocking work goes to `asyncio.to_thread`. After `tool_cleanup_timeout` (30 s) the
  manager cancels the task hard.
- Needed for long loops, network calls, subprocesses; unnecessary for short tools.

## Result size

The only cap: context_engineer Pre-Layer T (`tool_result_max_window_share`, default
0.25 of the window) — and only for agents running that hook. So bound results in the
tool itself (`max_rows`, `max_result_chars`) and say how to get more (paging, `offset`).

## Slash commands

```yaml
commands:
  - name: compact
    description: "Compact the context now"
    tool: "{{ name }}_compact"
    argument: focus          # optional; never "_…", request_id, session_id, agent_name
    argument_hint: "<topic>"
```

Runs through `Agent.dispatch_tool_call` without an LLM turn, with allowlist, runtime
params and status. Tool not allowed → command hidden. Precedence: built-ins → plugin
→ skills; on a name clash `/<plugin>:name`. Test:
`tests/pluginsystem/test_plugin_command_declarations.py`.
