# Cognitive Stack

Working memory as a stack. An agent pushes what it is about to interrupt,
works on the interruption, and pops back — instead of holding the nesting in
prose and losing it three turns later. A `pre_llm_call` hook re-injects the top
frames as a turn at the end of the history, so the stack stays visible even after the
conversation has been compacted.

## What it provides

`type = ["tool-server", "hooks"]`, no pip dependencies.

| Surface | Name | Purpose |
|---|---|---|
| Tool | `cognitive_stack` | one tool, five operations |
| Hook | `inject_stack_context` (`pre_llm_call`, off by default) | top N frames appended as a `developer` turn, only when they changed |

The tool is named after the **instance**, not after an action:
`SchemaBasedToolMixin` routes a tool whose name equals the server name to
`execute()`, which dispatches on the `operation` argument. `op` is accepted as
an alias — models abbreviate.

| Operation | Effect |
|---|---|
| `push_batch` | append frames (`context` required, `data` optional) |
| `pop_batch` | remove and return the top `count` frames |
| `peek` | read the top N without removing |
| `list` | all frames |
| `clear` | reset |

## Session resolution

Agents never have to carry a stack id. `_session_id` — injected by the
framework on every tool call — is mapped to a stack on first use, and every
later call resolves through that mapping. `stack_id` stays available for the
rare case of several stacks in one session.

## Why `clear` refuses sometimes

The server is a **singleton shared by all sessions and users**. A `clear` with
no resolvable stack therefore has two very different meanings:

* called with a `_session_id` (i.e. from an LLM tool call) → refused. Falling
  through to a global wipe would let one agent's output destroy every other
  session's stacks.
* called without any session context (maintenance/CLI) → global clear-all,
  which is not reachable from a tool call.

## Storage

In memory only. Stacks are lost on restart, and a stack untouched for
`session_ttl_seconds` (default 3600) is dropped — but the sweep runs lazily
inside `_get_or_create_stack`, which only `push_batch` reaches. A session that
only reads never expires anything, and an idle server keeps its stacks until
somebody pushes again.

`max_depth` guards against a recursion that pushes forever — 20 by default in
code and schema, raised to 50 in `config/plugins.yaml`.

## Configuration

```yaml
cognitive_stack:
  type: cognitive_stack
  enabled: true
  max_depth: 50
  max_frames_in_prompt: 3   # how many frames the hook injects
```

`max_frames_in_prompt` is the knob that matters for cost: the hook fires before
*every* LLM call, so each frame is paid for on every turn.

## Tests

`tests/test_plugin_cognitive_stack.py`.

## License

Apache-2.0 — see `LICENSE`.
