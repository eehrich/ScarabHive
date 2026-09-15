# Hooks

Enum `HookType` in `src/agent_system/hooks/plugin_hook.py`; called through the
`HookIntegrationManager` (`servers/agent/components/hook_integration.py`) and the
step loop in `servers/agent/server.py`.

## Types — when they fire, what takes effect

| Type | Fires | What takes effect |
|---|---|---|
| `session_start` | new session only, before history and user input | `messages` (in practice: extra system messages at the front) |
| `pre_llm_call` | every step before the LLM call (including the final call after max_steps) | `messages` — the changed list is sent as-is and mirrored into the session; receives `tools_schema`, `llm`, `step`, `cancellation_token` |
| `llm_progress` | while streaming, every 2000 chars of thinking | nothing |
| `post_llm_call` | after the assistant message is appended | only `assistant.content`/`tool_calls`; metadata `continue`, `continue_message`, `continue_injected_by`, `content_format` |
| `format_output` | display (HTML) | display only, never history |
| `session_end` | after saving | nothing; `metadata["persisted"]` |
| `pre_llm_request` / `post_llm_response` | at client level | read-only, errors swallowed |
| `pre_tool_call` / `post_tool_call` | **never fire** — no callers; registration logs a warning | — |

The context is **a deep copy per hook** (`messages`, `llm_response`, `metadata`);
`agent`, `llm`, `tools_schema`, the token are passed by reference.
- Changes without `modified=True` → discarded.
- `result.metadata` is merged **even with `modified=False`**.
- `success=False` → neither context nor metadata is taken.

## Minimal hook plugin

`plugin.toml`: `entrypoint = "plugin:PLUGIN_FACTORY"`, `type = ["hooks"]`, `category = "hooks"`.

```yaml
# schema.yaml
hooks:
  - name: inject_note          # == method name (handler:/priority: are ignored)
    type: pre_llm_call
    enabled: false             # usually off; agents switch it on
    timeout: 5.0               # default 30
    category: prompt_injection # order may reference categories
    order: {after: ["begin"], before: ["end"]}
config:
  note: {type: string, default: ""}
```

```python
# hooks.py
from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.models import ChatMessage

MARK = "my_hook"

class MyHook(SchemaBasedPluginHook):
    def __init__(self, plugin_dir, mcp_config=None):
        super().__init__(plugin_dir)                  # schema.yaml defaults only
        cfg = dict(self.get_config())
        if mcp_config is not None and getattr(mcp_config, "config", None):
            cfg.update(mcp_config.config)             # merging plugins.yaml config is the plugin's job
        self.note = cfg.get("note", "")

    async def inject_note(self, context: HookContext) -> HookResult:
        note = context.hook_config.get("note", self.note)   # per-agent keys
        if not note or context.messages is None:
            return HookResult(success=True, modified=False, context=context)
        msgs = [m for m in context.messages if m.injected_by != MARK]   # drop our previous insert
        i = next((k for k, m in enumerate(msgs) if m.role != "system"), len(msgs))
        msgs.insert(i, ChatMessage(role="system", content=note, injected_by=MARK))
        context.messages = msgs
        return HookResult(success=True, modified=True, context=context)
```

```python
# plugin.py — the factory gets (name, system_config, mcp_config); zero-arg → TypeError
from pathlib import Path
from .hooks import MyHook

def PLUGIN_FACTORY(name=None, system_config=None, mcp_config=None):
    return MyHook(Path(__file__).parent, mcp_config)
```

Registered only if `schema.yaml` has a `hooks:` key **and** the instance is
`enabled: true` in `plugins.yaml`.

**Hybrid (tool + hook):** a `hooks_plugin` attribute on the server, or duck typing —
the server defines `on_pre_llm_call` itself (`okf/server.py`). Careful: with two hooks
of the same type, `on_*` runs once per registered name.

## Names, ordering, configuration

- **Full name = `<server instance name>.<hook name>`** — not the plugin type, not the folder.
- `order` resolves only **full names, categories, `begin`/`end`**. A short name
  (`after: ["optimize_context"]`) is silently ignored. Ties sort alphabetically.
  A cycle → error log, registration order, no exception.
- Precedence at registration: schema.yaml < instance `hook_config.enabled: false`
  (can only switch off) < global `hooks.overrides` < `hooks.enabled: false`.
  Global override keys: `<instance>.<hook>` or `<instance>` for all of its hooks.
  A key that matches no registered hook has no effect; startup logs a warning for it.
- Per agent (`agent_config.hooks`):
  - `enabled: false` switches off all hooks of the agent
  - `overrides["<full name>"].enabled` beats the default
  - all other keys arrive as `context.hook_config`
- ⚠️ **Agent override keys must be the full name.** Short name, type name, instance
  name, typo → no effect; startup logs a warning per key (after all hooks are
  registered). `timeout` and `order` have no effect there.
- `hooks.default_timeout` is the timeout for hooks whose schema sets none.

## Errors and timeout

- Timeout per hook (`asyncio.wait_for`): error log, changes discarded, the chain
  continues. The task is cancelled — side effects may be half done.
- Exception, invalid result, missing method: logged, the hook counts as failed, the
  chain continues. If the chain fails, the loop continues with the unchanged
  messages — **a broken hook only shows up in the log.**

## Cache safety

Providers cache the request prefix. **Every request must be a prefix of the next.**
A hook that breaks this pays for the whole context again on every step.

- Nothing ticking (clock, step counter, "19 s ago") in the system prompt or at the
  front of history. The prompt is **re-rendered every step**.
  Guard: `tests/config/test_prompts_have_no_ticking_clock.py` (Jinja AST).
- Don't rewrite earlier messages; append new content at the end. Whoever inserts at
  the front (OKF seed, `simple_prompt_inject` with `before_last_user`) breaks the cache
  deliberately — state it in the README under "Token and cache effect".
- Find your own insert by `injected_by` and replace it, don't duplicate it.
- Guard for the loop: `tests/agent/test_agent_step_budget_note.py`
  (`test_every_request_is_a_prefix_of_the_next`) — the pattern for your own hook tests.
- Token counts: `max(provider count, estimate)` (`agent_system.llm.token_utils`).
  An under-count heals after one call, an over-count persists.

## `injected_by`

`ChatMessage.injected_by: Optional[str]` — clients strip it before sending.

- **Every inserted `role: user` message is marked.** `None` means "written by a
  person"; context_engineer (turn age, trigger), OKF, tool_preload, agent_continuation
  find "the last human message" by it.
- A `post_llm_call` hook that sets `continue` also sets `continue_injected_by`, so it
  can count its own nudges; without one the loop marks the nudge `post_llm_call_hook`.
- Mark system messages a hook inserts as well (so it can replace them).
