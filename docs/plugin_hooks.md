# Plugin Hook System

The verified quick reference is the Claude skill `.claude/skills/plugin-authoring/` (`references/hooks.md`).

**Status:** Production Ready
**Version:** 1.0
**Last Updated:** 2025-10-14

## Table of Contents

1. [Overview](#overview)
2. [Hook Types](#hook-types)
3. [PluginHook Interface](#pluginhook-interface)
4. [Hook Ordering](#hook-ordering)
5. [Schema-Based Hooks](#schema-based-hooks)
6. [Configuration](#configuration)
7. [Best Practices](#best-practices)
   - [Cache Safety](#cache-safety)
8. [Examples](#examples)
9. [Troubleshooting](#troubleshooting)

## Overview

The Plugin Hook System provides lifecycle interception points for extending agent behavior without modifying core code. Hooks allow plugins to observe and modify agent operations at key points in the execution flow.

### Key Features

- **9 Hook Types**: session start/end, pre/post LLM call, LLM progress, pre LLM request/post LLM response (client level), pre/post tool call
- **Flexible Ordering**: Named dependencies with topological sorting
- **Schema-Based Pattern**: Declarative hook definitions in YAML
- **Error Isolation**: Hook failures don't crash the agent
- **Global Configuration**: Override hook behavior per agent/environment
- **Metadata Tracking**: Detailed execution statistics and audit trails

### Architecture

```
Agent Execution Flow
  ↓
  SESSION_START hooks (new sessions only)
  ↓
  ┌─ PRE_LLM_CALL hooks (every step)
  │    ↓
  │  LLM call ── PRE_LLM_REQUEST / LLM_PROGRESS / POST_LLM_RESPONSE (client level)
  │    ↓
  │  POST_LLM_CALL hooks
  │    ↓
  └─ Tool execution, per call: PRE_TOOL_CALL → tool → POST_TOOL_CALL
  ↓
  Session saved
  ↓
  SESSION_END hooks
```

### What Takes Effect

The registry hands every hook a **deep copy** of the context (`messages`,
`llm_response`, `metadata`, ...; `agent`, `llm`, `tools_schema` and the
cancellation token by reference).

- Changes are only taken when the hook returns `success=True, modified=True`
  with the changed `context`; otherwise they are discarded.
- `result.metadata` is merged into the context metadata even with
  `modified=False`.
- `success=False` → neither context nor metadata is taken.

### Use Cases

- **Context Management**: Optimize message history before LLM calls
- **Validation**: Validate message format and content
- **Logging**: Audit agent behavior and track metrics
- **Transformation**: Modify inputs/outputs dynamically
- **Security**: Sanitize content, enforce policies
- **Monitoring**: Track performance, costs, usage patterns

## Hook Types

### PRE_LLM_CALL

**Trigger:** Every step before the LLM call, including the final call after `max_steps`
**Use Cases:** Context optimization, prompt injection, validation
**Can Modify:** `messages` — the changed list is sent as-is and synced to the session tracker.
The context also carries `tools_schema`, `llm`, `step` and `cancellation_token`.

```python
async def on_pre_llm_call(self, context: HookContext) -> HookResult:
    """Executed before LLM call.

    Common use cases:
    - Optimize context (remove duplicates, truncate)
    - Inject system prompts
    - Validate message structure
    - Add custom metadata
    """
```

**Example Plugins:**
- `context_engineer`: Compacts the context in layers (offloads and archives tool results)
- `context_summarizer`: Intelligently summarizes older messages
- `message_validator`: Validates message format

### POST_LLM_CALL

**Trigger:** After receiving the LLM response
**Use Cases:** Response validation, logging, statistics, auto-continuation
**Can Modify:** Only `llm_response["assistant"]["content"]` and
`llm_response["assistant"]["tool_calls"]` are read back. Metadata keys the
loop reads: `content_format`, `continue`, `continue_message`,
`continue_injected_by`, `continuation_count`, `continuation_reason`.
A hook that sets `continue` should also set `continue_injected_by`, so it can
count its own nudges; without one the loop marks the nudge `post_llm_call_hook`
(see [Cache Safety](#cache-safety)).

```python
async def on_post_llm_call(self, context: HookContext) -> HookResult:
    """Executed after LLM call.

    Common use cases:
    - Log response and timing
    - Validate response format
    - Extract structured data
    - Update statistics
    """
```

**Example Plugins:**
- `request_logger`: Logs timing and response preview

### LLM_PROGRESS

**Trigger:** During a streaming LLM call, every 2000 characters of reasoning
(`_REASONING_PROGRESS_TICK` in `servers/agent/mixins/llm_loop/llm_call.py`)
**Use Cases:** Observing a call that is stuck thinking
**Can Modify:** nothing — the call is already running

```python
async def on_llm_progress(self, context: HookContext) -> HookResult:
    """context.reasoning_text: the reasoning of this call so far,
    context.reasoning_chars / previous_reasoning_chars: length now and at the
    previous tick. NO messages."""
```

- **The stream waits for the hook** — it must return immediately; anything
  slow belongs in a background task. Errors in the hook do not abort the
  call.
- **No `messages`:** the registry's deep copy on every tick blocks the
  streaming loop (measured: 72 ms with 4691 messages). A hook that needs the
  task remembers it in a `pre_llm_call` hook.
- **Your own interval without state:** due when
  `reasoning_chars // n > previous_reasoning_chars // n`.
- **Blind** where no `thinking_delta` flows: Gemini (thoughts arrive as
  `content_delta`), batch, non-streaming clients.

**Example Plugins:**
- `agent_watchdog`: `observe_reasoning`

### PRE_TOOL_CALL

**Trigger:** before every tool call of the model, individually per call, and
before every call of a `tool_script` script (see [Which Calls Count](#which-calls-count))
**Use Cases:** Approvals (ask/auto/off, allow/deny — #091, built as the plugin
[`tool_approval`](../src/plugins/tool_approval/README.md)), checking or
correcting arguments, logging
**Can Modify:** `tool_call["arguments"]`; the hook can also block the call

`context.tool_call`:

```python
{
    "id": "call_abc",               # call ID of the provider; None for tool_script
    "name": "file_ops_read_file",   # the tool, as the model called it
    "server": "file_ops",           # the server that executes it ("<server>.<tool>" for external MCP tools)
    "arguments": {"path": "..."},   # the arguments, as they were sent
    "source": "model",              # "model" or "tool_script"
}
```

Plus `session_id`, `request_id`, `user_id`, `agent`, `step`, `tools_schema` and
`cancellation_token`, the run's token. A hook that waits (for a person) stops
waiting as soon as the run is cancelled.

**Blocking:** `HookResult(success=True, metadata={"block": "<what the model should do>"})`.
The call does not run, and instead of a result the model reads:

```json
{"status": "error", "error": "<text of the hook>", "type": "ToolCallBlocked"}
```

- `block: True` without text gets a default text. The text is written for the
  model, so it says what to do instead.
- **The first hook that blocks ends the chain.** Later hooks no longer run for
  this call, so they cannot lift the block either. A hook that asks
  therefore never asks about a call another hook has already
  blocked.
- The run continues: a blocked call is an error result, not an
  exception. The UI receives a `tool_error` event with `"blocked": true` and
  shows it as its own line in the step's status (a blocked call opens
  no status scope and sends no `tool_call`).
- Under `tool_script` the block becomes a `ToolDispatchError`, which arrives in
  the script as `ToolCallError` and can be caught there.

**Changing arguments:** The hook writes `context.tool_call["arguments"] = {...}`
(a dict) and returns `modified=True`. Nobody reads back `name`, `server` and
`id`. Runtime parameters (`_session_id`, `_user_id`, …, `request_id`)
are removed from the changed arguments, because only the framework sets them.

**The history keeps what the model sent.** The assistant turn containing the
call is not rewritten (cache prefix, see [Cache Safety](#cache-safety)),
but the tool runs with the changed arguments. If the model needs to know about
the change, a `post_tool_call` hook tells it in the result.

**Order:** With parallel calls the pre-hooks run one after another, in the
order of the calls, before the first one starts. A hook that asks a person
therefore asks one question after the other, and a block leaves the calls
next to it untouched. After that the calls that are not blocked run in parallel. While
a hook waits, the run's status events keep flowing into the stream: a
question the hook asks via `StatusScope` is seen by the person while it
waits for their answer.

**Errors:** A hook that raises, runs into the timeout, returns an invalid result
or reports `success=False` is skipped (registry rule, see
[Error Handling](#error-handling)), and the call runs. A policy hook for which
this must not happen sets `on_error: block` in `schema.yaml`: then each
of these errors blocks the call, including a timeout that the operator has
shortened via `hooks.overrides`. The model reads which check
failed. `on_error` applies only to `pre_tool_call`; a different
spelling or a different hook type is reported by the validator as an error and by the
registration as a warning. If the hook machinery itself fails (not a
single hook), the call does not run either.

```yaml
hooks:
  - name: check_policy
    type: pre_tool_call
    timeout: 120          # a follow-up question may take this long
    on_error: block       # timeout or crash block the call
```

**Asking a person.** Asking happens only where someone can answer:
`status_forwarding.attended_stream_of(context.request_id)` names the stream
of a running run that a person follows and that the status lines of the
call reach (the run itself or one above it, for example for a sub-agent).
A run counts as followed if its client says so at start
(`"attended": true` on `POST /events`, form field `attended` on `/run` with
files; `request_context.set_run_attended`), as long as it is running and a
logged-in person started it (or auth is off). The web chat does this, and
`agent-cli chat` for a turn whose status it shows in colour and whose keys it
reads (not with `--no-status`, `--color never`/`text`, `NO_COLOR`,
`TERM=dumb`, or input that is not a terminal). Once the stream is over, for example for an asynchronous sub-agent after the
end of its caller, or if nobody reads the run's job any more (tab
closed), nobody is asked any more. A sub-run asks in the stream of the run above
it; a call inside a `tool_script` script never asks. Everything else (openai_api, agent-run, a one-shot
agent-cli, a run woken in a process of its own, JSON `/run`) is unattended, and the
hook decides without asking. The question itself is a status line under its
own child ID with `meta.tool_approval`. The chat draws buttons for it, and
the last line of the series (end/error) removes them again. The machinery
for this is shared by `tool_approval`, the tool `ask_user` and stategraph's wait questions: open questions,
status line, waiting for the answer, timeout, cancellation and "nobody reads any more" in
`agent_system/core/run_questions.py` (`QuestionBroker`, `put_to_person`), the
answer route including "who may answer" in `agent_system/api/question_routes.py`,
the answer box in the chat in `syncQuestionActions` and `questionBox` (`static/js/chat_module.js`).
Every kind of question describes itself in one form any client can draw
(`Question.form`: prompt, detail, warning, choices, multi_select, text) and
takes an answer in one form any client can send (`QuestionBroker.take`: the
values picked and the text); `agent-cli chat` prints every kind from the form
and answers through `run_questions.answer_question` in its own process
(`cli_utils/questions.py`); the web chat draws every kind from the same form
(`questionBox`) and posts that answer to the kind's route (`question_router`).
A new kind needs no client code; one without `form` or `take` fails when it
asks (`QuestionBroker.open_question`).

**Pitfalls for policy and approval hooks:**

- The block belongs in `HookResult.metadata`. A `block` that is only in
  `context.metadata` and returns with `modified=False` is discarded by the
  registry, and the call runs.
- A hook that runs after the approval hook can still change the arguments,
  and nobody checks them afterwards. The approval hook therefore belongs at the end of the
  chain (`order: {after: [...]}` on the categories that change arguments).
  A `pre_tool_call` hook that changes arguments carries
  `category: tool_arguments` for this; `tool_approval` sits behind all of these with
  `after: ["begin", "tool_arguments"]`.
- Change the context, do not rebuild it: a newly built `HookContext` without
  `tool_call` or `cancellation_token` makes the hooks after it fail.
- An empty block text (`block: ""`) blocks as well; only `None` or `False`
  let the call through.
- If the run is cancelled while a hook waits, the call reports itself as
  cancelled, not as blocked, even if the hook fails in the process. It does not run
  and does not reach a post-hook. Under `tool_script` the script
  then aborts without being able to catch the cancellation.
- Blocked calls do not count towards the error streak that opens an
  escalation window (`auto_escalate_on_stuck`): a step whose calls were all blocked
  leaves the streak as it is. If the model sends a
  blocked call again verbatim, however, the loop detector sees a
  loop in it and intervenes as with any other, because the model is then
  actually stuck.

```python
async def on_pre_tool_call(self, context: HookContext) -> HookResult:
    call = context.tool_call
    if call["name"] == "terminal_exec" and "rm -rf" in call["arguments"].get("command", ""):
        return HookResult(success=True, metadata={
            "block": "Recursive deletes are not allowed here. Ask the user what to remove."})
    return HookResult(success=True, context=context)
```

### POST_TOOL_CALL

**Trigger:** after every call that the pre-hooks let through, even if
it failed or was cancelled. It comes before the result goes into the
history. With parallel calls the post-hooks run one after another in
call order, once all are finished.
**Use Cases:** Shortening, redacting or supplementing results; logging
**Can Modify:** `tool_result["result"]`

`context.tool_result = {"result": <value>, "is_error": <bool>, "started_at": <float|None>, "finished_at": <float|None>}`:

- `result` is what the caller reads. In the agent loop this is the content
  of the tool message, decoded: the result of the tool or the error the
  framework put in its place. Under `tool_script` it is the
  return value of the tool; if the tool raises, the framework turns that, as in the
  loop, into an error result (`{"status": "error", ...}`) that the post-hooks
  see before the script receives it as `ToolCallError`.
- `is_error` follows the convention (`{"status": "error"}` or a bare
  `{"error": ...}`) and is not read back.
- `started_at` / `finished_at` (`time.time()`) say when the call itself ran.
  The hook's own clock is no use for this: with parallel calls the
  post-hooks only run once all are finished, so it would see the end of the
  slowest call for every call. Neither is read back.
- `context.tool_call` is the call as it ran, that is, with the arguments after
  the pre-hooks.

To change the result, the hook writes `context.tool_result["result"]` and
returns `modified=True`. The change ends up in the history and thus in the
session file. The live event `tool_result` and the run's `calls` list
continue to show what the tool actually returned. Multimodal attachments
(`_multimodal_content`) are not visible to a post-hook.

A hook that fails or returns a result that cannot be written as JSON
changes nothing: the result stays as the tool delivered it.

```python
async def on_post_tool_call(self, context: HookContext) -> HookResult:
    result = context.tool_result["result"]
    if isinstance(result, dict) and "api_key" in result:
        context.tool_result["result"] = {**result, "api_key": "<redacted>"}
        return HookResult(success=True, modified=True, context=context)
    return HookResult(success=True, context=context)
```

### Which Calls Count

| Path | Tool hooks |
|---|---|
| Tool calls of the model in the agent loop, including parallel ones and external MCP tools | yes, `source: "model"` |
| Calls of a `tool_script` script (`Agent.dispatch_tool_call(..., hook_source="tool_script")`) | yes, `source: "tool_script"`, `id: None`, `step: 0`. No hook sees the secrets from `inject_params`: they are only added after the pre-hooks |
| Sub-agents | The spawn is a call of the parent agent and runs through its hooks. The sub-agent's calls run through its own loop, with its hook configuration. A hook that wants to pass on the rules of the parent run finds it via the request ID (`<run>_003_sub_…` extends the caller's ID); `tool_approval` does this and treats a sub-agent without the hook as a separate case |
| Slash commands, web buttons, `tool_preload`, stategraph activities, `Agent.call_tool` | no, because the caller is a person or the framework, not the model |
| Calls that the framework rejects beforehand (broken JSON, unknown tool) | no, because they never run |
| blocked calls | only the pre-hooks up to the block, no post-hook |
| Calls of a cancelled run that never started | none (or only the pre-hooks that ran before the cancellation), no post-hook |

**Cost:** If no hook of the type runs for an agent, because none is registered
or all are off for it, no context is built and nothing is copied
(`HookIntegrationManager.wants_hooks`). Otherwise the registry copies, per hook, the
call or the result (deep copy, like `messages` for `pre_llm_call`).

**Limits:**

- Under `tool_script`, its `per_call_timeout` also limits the hooks of a
  call. A hook there receives the `cancellation_token` from the script. A hook
  should not wait for a person inside a script: the script as a
  whole was the model's call and has already gone through the hooks.
  `tool_approval` therefore never asks inside it, but only blocks what deny rules
  or a spawn without approvals do not allow. It never approves a script itself
  "for the session": each one is asked about with its code.

### SESSION_START

**Trigger:** When a new session begins (not for resumed sessions)
**Use Cases:** Initialization, logging, setup
**Can Modify:** `messages` — the returned list replaces the session's message list

```python
async def on_session_start(self, context: HookContext) -> HookResult:
    """Executed at session start.

    Common use cases:
    - Initialize session tracking
    - Log session start
    - Set up session state
    - Load session context
    """
```

**Example Plugins:**
- `request_logger`: Logs the start of a new session

### SESSION_END

**Trigger:** When the request ends
**Use Cases:** Cleanup, statistics, logging
**Can Modify:** nothing — the return value is ignored

**When:** after the request's conversation has been written to the session
file, not before. `context.metadata["persisted"]` says whether that write
happened -- a hook that counts what the request carried as done (debate_forum
marks direct messages delivered there) may only do so when it did: persisting
never raises, so a failed or cancelled save is silent otherwise.

How the run ended is also in the metadata, because a hook sees
none of its events: `cancelled` (the run's cancellation token was
cancelled; a crash does not count, although it cancels the token
afterwards), `errors` (the run's error messages, including a crash,
recorded before they are emitted, so a consumer that stops at the
error does not lose them) and `completed` (whether the run reached a
final answer). The `otel` plugin ends its run span
afterwards.

```python
async def on_session_end(self, context: HookContext) -> HookResult:
    """Executed at session end.

    Common use cases:
    - Clean up resources
    - Log session summary
    - Save session statistics
    - Final reporting
    """
```

**Example Plugins:**
- `request_logger`: Logs the end of each run with its LLM call count and duration

### PRE_LLM_REQUEST / POST_LLM_RESPONSE

**Trigger:** Inside the LLM client, around the raw API request
**Use Cases:** Capturing exact payloads, responses, usage
**Can Modify:** nothing — read-only; errors in these hooks are swallowed

Fields: `llm_request_payload`, `llm_response_data`, `llm_provider`,
`llm_model`, `llm_request_url`, `llm_duration_ms`, `llm_error`, `llm_usage`,
`llm_finish_reason`, `llm_is_streaming`.

## PluginHook Interface

### Base Class

All hook plugins inherit from `PluginHook` or `SchemaBasedPluginHook`:

```python
from agent_system.hooks import PluginHook, HookContext, HookResult

class MyPlugin(PluginHook):
    def __init__(self, name: str, config: dict = None):
        super().__init__(name, config or {})
        # Plugin initialization

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        # Hook implementation
        return HookResult(
            success=True,
            modified=False,
            context=context
        )
```

### HookContext

Container for hook execution context:

```python
@dataclass
class HookContext:
    hook_type: HookType
    request_id: str
    session_id: str
    agent: Optional[Agent] = None
    agent_name: str = ""
    messages: Optional[List[ChatMessage]] = None          # ChatMessage objects, not dicts
    tools_schema: Optional[List[Dict[str, Any]]] = None   # per-request tool schema
    llm_response: Optional[Dict[str, Any]] = None         # post_llm_call: {"assistant": {...}}
    tool_call: Optional[Dict[str, Any]] = None            # pre/post_tool_call: id, name, server, arguments, source
    tool_result: Optional[Dict[str, Any]] = None          # post_tool_call: {"result", "is_error", "started_at", "finished_at"}
    metadata: Dict[str, Any] = field(default_factory=dict)
    hook_config: Dict[str, Any] = field(default_factory=dict)  # per-agent override keys
    target_hook_name: Optional[str] = None                # short hook name being dispatched
    step: int = 0
    llm: Optional[Any] = None
    cancellation_token: Optional[Any] = None
    user_id: Optional[str] = None  # the user the run belongs to
    # llm_request_payload, llm_response_data, llm_provider, llm_model,
    # llm_request_url, llm_duration_ms, llm_error, llm_usage,
    # llm_finish_reason, llm_is_streaming: pre_llm_request / post_llm_response
    # reasoning_text, reasoning_chars, previous_reasoning_chars: llm_progress
```

### HookResult

Return value from hook execution:

```python
@dataclass
class HookResult:
    success: bool                                          # Hook executed successfully
    modified: bool = False                                 # Context was modified
    context: Optional[HookContext] = None                  # Modified (or original) context
    error: Optional[str] = None                            # Error message if failed
    metadata: Dict[str, Any] = field(default_factory=dict) # Merged into context.metadata
```

**Important:**
- Set `modified=True` if you changed the context — otherwise the change is discarded
- Return modified context in `context` field
- Use `metadata` for signals and statistics; it is merged even with `modified=False`
- Set `success=False` and provide `error` on failure — nothing is taken then

## Hook Ordering

### Named Dependencies

Hooks specify dependencies in `order`:

```yaml
hooks:
  - name: my_hook
    type: pre_llm_call
    category: prompt_injection
    order:
      after: ["begin", "context_engineer.engineer_context"]  # full name
      before: ["context_summarization", "end"]               # category or virtual node
```

A reference resolves only to:

- a **full hook name** (`<server instance name>.<hook name>`),
- a **category** (all registered hooks with that `category`),
- `begin` / `end`: virtual nodes at the start/end of each type.

A short hook name (`after: ["engineer_context"]`) or a reference to a hook
that is not registered is **silently ignored** (debug log only).

### Topological Sort

The `HookRegistry` sorts with Kahn's algorithm on every execution:

1. Resolve references and build the dependency graph
2. Sort topologically; ties are broken alphabetically by full name
3. On a circular dependency: an error is logged and the hooks run in
   registration order — nothing is raised

### Example Ordering

```yaml
# config/plugins.yaml
hooks:
  enabled: true
  overrides:
    request_logger.log_pre_llm:
      order:
        after: ["begin"]
        before: ["context_summarizer.summarize_context"]

    context_summarizer.summarize_context:
      order:
        after: ["request_logger.log_pre_llm"]
        before: ["message_validator.validate_messages"]

    message_validator.validate_messages:
      order:
        after: ["context_summarizer.summarize_context"]
        before: ["end"]
```

**Execution Order:**
1. `request_logger.log_pre_llm`
2. `context_summarizer.summarize_context`
3. `message_validator.validate_messages`

## Schema-Based Hooks

Recommended pattern for new hook plugins.

### Directory Structure

```
src/plugins/my_plugin/
├── plugin.toml      # Manifest (entrypoint = "plugin:PLUGIN_FACTORY")
├── plugin.py        # Factory function
├── hooks.py         # Hook implementation
├── schema.yaml      # Hook definitions + config
└── README.md        # Documentation
```

### schema.yaml

```yaml
# Hook definitions
hooks:
  - name: my_hook_handler        # Must match method name exactly (handler:/priority: are ignored)
    type: pre_llm_call
    enabled: true
    timeout: 30.0                # default 30.0
    category: prompt_injection   # optional; order may reference categories
    description: "What this hook does"
    order:
      after: ["begin"]
      before: ["end"]

# Configuration schema
config:
  max_items:
    type: integer
    default: 100
    description: "Maximum items to process"
    minimum: 1
    maximum: 1000

  enable_feature:
    type: boolean
    default: true
    description: "Enable special feature"
```


### Instance Default via `hook_config`

The schema speaks for the plugin type. When the same plugin runs as several
instances (e.g. `context_summarizer` and `research_context_summarizer`), an
instance's server config sets its own registration default:

```yaml
# config/plugins.yaml or an included plugin config
servers:
  research_context_summarizer:
    type: context_summarizer
    hook_config:
      enabled: false   # this instance starts OFF; agents switch it on
                       # via hooks.overrides (exact <instance>.<hook> key)
```

It can **only disable**: `enabled: false` switches this instance's hooks off;
`enabled: true` does NOT raise a schema default (raising a default is the
operator's move — global `hooks.overrides`). Precedence at registration:
schema entry < instance `hook_config.enabled: false` < global
`hooks.overrides` < global master switch `hooks.enabled: false`. At runtime
the agent override (exact key) wins over all of these, see
[Agent-Level Config](#agent-level-config).

A hook is registered only if the plugin's `schema.yaml` has a `hooks:` key and
the instance is enabled in `config/plugins.yaml`.

### hooks.py

```python
from pathlib import Path
from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult

class MyPlugin(SchemaBasedPluginHook):
    def __init__(self, plugin_dir: Path | str, server_config=None):
        super().__init__(plugin_dir)

        # get_config() holds only the schema.yaml defaults: {key: default}
        config = dict(self.get_config())
        # Merging the instance config from plugins.yaml is the plugin's job
        if server_config is not None and getattr(server_config, 'config', None):
            config.update(server_config.config)
        self.max_items = config.get('max_items', 100)

    # Handler name MUST match hook name in schema.yaml
    async def my_hook_handler(self, context: HookContext) -> HookResult:
        # Implementation
        return HookResult(
            success=True,
            modified=False,
            context=context
        )
```

### plugin.py

```python
from pathlib import Path
from .hooks import MyPlugin

# Called as factory(name, system_config, server_config) — a zero-argument factory raises TypeError
def PLUGIN_FACTORY(name=None, system_config=None, server_config=None) -> MyPlugin:
    return MyPlugin(Path(__file__).parent, server_config)
```

### Convention

**Hook name = Method name** (no `handler` field needed)

```yaml
# schema.yaml
hooks:
  - name: optimize_context    # Method: async def optimize_context(...)
  - name: validate_messages   # Method: async def validate_messages(...)
```

## Configuration

### Hook Naming Convention

**CRITICAL:** Hooks are registered and referenced using the **full name** format:
`<server instance name>.<hook name>` — the instance key under `servers:` in
`config/plugins.yaml`, not the plugin type or folder name.

**Examples:**
- `todo.inject_todo_tasks`
- `context_engineer.engineer_context`

**Why This Matters:**
- Enables multiple plugins (and instances of one plugin) to have hooks with the same base name
- Required for global and per-agent hook overrides and for `order` references
- The registration log shows the exact name: `Registered hook '<full name>' ...`

**Convention Breakdown:**
```yaml
# In plugin schema.yaml (just the hook name)
hooks:
  - name: inject_todo_tasks
    type: pre_llm_call
    enabled: false

# In registry (full name with instance prefix)
# Registered as: todo.inject_todo_tasks

# In agent config overrides (full name required)
agent_config:
  hooks:
    overrides:
      todo.inject_todo_tasks:
        enabled: true
```

### Plugin-Level Config

Defined in plugin's `schema.yaml`:

```yaml
config:
  param1:
    type: string
    default: "value"
    description: "Parameter description"
```

### Global Overrides

Override in `config/plugins.yaml`:

```yaml
hooks:
  enabled: true                   # false disables all hooks at registration
  overrides:
    instance_name.hook_name:      # or just instance_name for all its hooks
      enabled: false              # Disable specific hook
      timeout: 60.0               # Override timeout
      order:
        after: ["other_instance.other_hook"]  # Override order (full names/categories)
```

`hooks.default_timeout` is the timeout of every hook whose schema sets no
`timeout`. Only the field names of an override are validated; a key that matches
no registered hook (or hook-owning instance) has no effect, and startup logs a
warning for it.

A global override knows exactly these three keys (`enabled`, `timeout`,
`order`) — any other key (a typo such as `timout`) makes `load_settings`
fail instead of silently having no effect. An empty key (all
lines below it commented out) means "nothing set". Plugin-specific
parameters belong in the plugin's hook metadata or the agent overrides
(`agent_config.hooks.overrides`), not here.

The `hooks:` section is merged when the config is loaded, like `plugins:`
(`AgentSystemConfig.hooks`) — from `config/plugins.yaml` or
any other file loaded via `includes`, namely from the config the
process runs with (`--config`, `AGENT_CONFIG_PATH`), independent of the
working directory.

### Agent-Level Config

Per-agent hook configuration with overrides:

```yaml
# config/plugins.yaml
agents:
  meta_agent:
    agent_config:
      hooks:
        enabled: true   # false disables all hooks for this agent
        overrides:
          # Enable globally disabled hook for this agent
          todo.inject_todo_tasks:
            enabled: true
            # Any other key arrives as context.hook_config; it only has an
            # effect if the hook reads it (todo does not)

          # Disable globally enabled hook for this agent
          context_engineer.engineer_context:
            enabled: false

  simple_agent:
    agent_config:
      hooks:
        enabled: true
        overrides: {}  # Uses all global defaults
```

**Override Logic:**
1. **Agent `hooks.enabled: false`** → no hook runs for this agent
2. **Hook has agent override with `enabled`** → Use override value (ignores registration state)
3. **No agent override** → Use the registration state (schema, instance `hook_config`, global overrides)
4. **Hook globally disabled + agent enables** → Hook executes for this agent only
5. **Hook globally enabled + agent disables** → Hook skipped for this agent only

⚠️ Agent override keys must be the **full** hook name: a short name, a type
name, an instance name or a typo has no effect. Startup logs a warning for every
such key once all hooks are registered. `timeout` and `order` in agent overrides
are ignored.

**Example Scenario:**

```yaml
# schema.yaml (global)
hooks:
  - name: inject_todo_tasks
    enabled: false  # Disabled by default

# config/plugins.yaml
meta_agent:
  hooks:
    overrides:
      todo.inject_todo_tasks:
        enabled: true  # Enabled ONLY for meta_agent

sysadmin_agent:
  hooks:
    overrides: {}  # No override, uses global (disabled)
```

**Result:**
- ✅ `meta_agent`: Hook executes (override enabled)
- ❌ `sysadmin_agent`: Hook skipped (global disabled, no override)

## Best Practices

### Performance

1. **Keep Hooks Fast**: Target <100ms execution time
2. **Async Operations**: Use `await` for I/O operations
3. **Avoid Blocking**: Don't use `time.sleep()`, use `asyncio.sleep()`
4. **Cache Results**: Store expensive computations
5. **Limit Processing**: Process only what's necessary

### Error Handling

1. **Always Return HookResult**: Even on error
2. **Set success=False on Failure**: Provide error message
3. **Don't Raise Exceptions**: Hook system handles errors
4. **Log Errors**: Use `logger.error()` with traceback
5. **Graceful Degradation**: Return original context on error

How the registry handles failures:

- **Timeout** (per hook, `asyncio.wait_for`): error log, the hook's changes are
  discarded, the chain continues. The hook task is cancelled — side effects may
  be half done.
- **Exception, invalid result, missing method**: logged, the hook counts as
  failed, the chain continues.
- Exception: a `pre_tool_call` hook with `on_error: block` blocks the call on each
  of these errors, and the chain ends there (see
  [PRE_TOOL_CALL](#pre_tool_call)).
- If the whole chain fails, the agent loop continues with the unchanged
  messages — a broken hook only shows up in the log.

### Cache Safety

Providers cache the request prefix. **Every LLM request must be a prefix of
the next one**; a hook that breaks this pays for the whole context again on
every step.

- Nothing ticking (clock, step counter) in the system prompt or at the front of
  the history — the prompt is re-rendered every step.
- Don't rewrite earlier messages; append new content at the end. That holds
  for your own previous block too: find it by `ChatMessage.injected_by`,
  compare its text, and if it says the same thing write nothing at all. If it
  differs, append the new state behind it and leave the old one standing --
  replacing it changes the prefix the provider has already cached.
- A state block is `developer`, not `system`: Anthropic and Gemini have no
  system role inside a history and hoist such a message into the prompt head.
- **Every `role: user` message a hook or the loop inserts carries
  `injected_by`.** `None` means "written by a person"; context_engineer, OKF,
  tool_preload and agent_continuation rely on it to find the last human
  message. A `post_llm_call` hook that sets `continue` should set
  `continue_injected_by` so it can count its own nudges; without one the loop
  marks the nudge `post_llm_call_hook`.

Guards: `tests/agent/test_agent_step_budget_note.py`,
`tests/config/test_prompts_have_no_ticking_clock.py`.

### Testing

1. **Test Each Hook**: Separate test for each hook type
2. **Test Edge Cases**: Empty inputs, large inputs, invalid data
3. **Test Ordering**: Verify hooks run in correct order
4. **Test Timeouts**: Ensure hooks respect timeout limits
5. **Test Failures**: Verify error handling and isolation

### Code Quality

1. **Type Hints**: Full type annotations
2. **Docstrings**: Document purpose, args, returns
3. **Clear Names**: Descriptive hook and method names
4. **Single Responsibility**: Each hook does one thing
5. **Configuration**: Make behavior configurable

## Examples

See [Plugin Examples](../src/plugins/) for complete implementations:

- **[context_engineer](../src/plugins/context_engineer/)**: Layered context compaction, preserves the prompt cache
- **[context_summarizer](../src/plugins/context_summarizer/)**: Intelligent LLM-based summarization
- **[message_validator](../src/plugins/message_validator/)**: Message format validation
- **[request_logger](../src/plugins/request_logger/)**: Request/response logging

## Troubleshooting

### Hook Not Executing

1. Check that hook is enabled in configuration
2. Verify the hook is registered (`GET /hooks` API endpoint, or `Registered hook` in the log)
3. Check the call is one the hook type sees at all (tool hooks: [Which Calls Count](#which-calls-count))
4. Check `schema.yaml` has a `hooks:` key and the method name equals the hook name
5. Review log files for errors

### Circular Dependency

```
ERROR: Circular dependency in hooks for pre_llm_call: Circular dependency detected in hooks: [...]
```

Nothing is raised; the hooks of that type run in registration order.

**Solution:** Review `order` specifications in hooks, remove cycles

### Hook Timeout

```
ERROR: Hook 'my_instance.my_hook' timed out after 30.0s
```

**Solutions:**
- Increase timeout in configuration
- Optimize hook implementation
- Move expensive operations to background task

### Context Not Modified

**Issue:** Changes to context don't persist

**Solution:** Ensure you return modified context and set `modified=True`:

```python
result = HookResult(
    success=True,
    modified=True,          # Must be True
    context=modified_context  # Return modified context
)
```

### Order Not Respected

**Issue:** Hooks execute in wrong order

**Solution:**
- Use full hook names or categories in `order` — short names are silently ignored
- Check for conflicting order constraints (`Hook ordering conflict` warning)
- Inspect registered hooks via `GET /hooks`; the executed order is logged at
  debug level (`Executing N hooks for pre_llm_call: [...]`)

### Agent Override Not Working

**Issue:** Per-agent hook override has no effect

**Common Causes & Solutions:**

1. **Incorrect Hook Name Format**
   ```yaml
   # ❌ WRONG - Missing instance prefix
   hooks:
     overrides:
       inject_todo_tasks:
         enabled: true

   # ✅ CORRECT - Full <instance>.<hook> format
   hooks:
     overrides:
       todo.inject_todo_tasks:
         enabled: true
   ```

2. **Hook Not Registered**
   - The plugin's `schema.yaml` needs a `hooks:` key and the instance must be enabled in `config/plugins.yaml`
   - The `type` in `plugin.toml` is not used for hook registration

3. **Missing or Wrong PLUGIN_FACTORY**
   - Ensure `plugin.py` exports `PLUGIN_FACTORY`
   - It is called as `PLUGIN_FACTORY(name, system_config, server_config)`

**Debug Steps:**
```bash
# Check hook registration (shows the exact full name to use)
grep "Registered hook" logs/cli.log | grep inject_todo_tasks
```

---

**Related Documentation:**
- [Plugin Authoring Guide](./plugin_authoring.md)
- [Configuration Reference](./configuration.md)
- [App Architecture](./_arch_app_architecture.md)
