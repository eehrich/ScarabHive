# otel

OpenTelemetry traces for agent runs, LLM calls and tool calls — and, on
request, the two GenAI client metrics. A hooks-only plugin: it observes, it
never changes a message, a call or a result, and it never fails a run.
**Off by default.**

## Enabling

```yaml
# config/plugins.yaml
plugins:
  servers:
    otel:
      type: otel
      enabled: true
      config:
        exporter: otlp            # otlp | console | none
        # endpoint: "http://localhost:4317"
```

With no `endpoint`, the exporter reads the standard variables
(`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_ENDPOINT`, default
`localhost:4317`); `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`,
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_TRACES_SAMPLER` work as usual. A new
instance needs a restart. Once enabled, the hooks run for every agent; one
agent opts out per hook:

```yaml
agent_config:
  hooks:
    overrides:
      otel.record_llm_call: {enabled: false}
```

For a first look without a collector: `exporter: console` prints every span to
stdout.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `exporter` | `otlp` | `otlp`, `console` (stdout, for debugging) or `none` (spans built, nothing exported) |
| `protocol` | `""` | `grpc` or `http/protobuf`. Empty: `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` / `OTEL_EXPORTER_OTLP_PROTOCOL`, else `grpc`. `http/protobuf` needs `opentelemetry-exporter-otlp-proto-http`, which is not a dependency — without it the plugin logs one ERROR naming the package and exports nothing |
| `endpoint` | `""` | OTLP endpoint; empty = the environment's |
| `headers` | `{}` | Extra OTLP headers, e.g. `authorization: "${OTEL_TOKEN}"` |
| `insecure` | `null` | gRPC only: plaintext instead of TLS; empty = decided by the endpoint and `OTEL_EXPORTER_OTLP_TRACES_INSECURE` |
| `service_name` | `""` | `service.name`; empty = `OTEL_SERVICE_NAME`, then `OTEL_RESOURCE_ATTRIBUTES`, else `scarabhive` |
| `capture_content` | `false` | Export the run's input and final answer, tool arguments and tool results (see Privacy) |
| `content_max_chars` | `2000` | Cap per content attribute, truncation mark included |
| `capture_user_id` | `false` | Put the run's user id on every span (`user.id`) |
| `max_open_runs` | `1000` | Run spans held open at most; beyond it the oldest ends as `evicted` |
| `max_pending_tool_calls` | `4096` | Tool calls waiting for their post hook, at most; beyond it the oldest is reported as `unknown` |
| `idle_timeout_seconds` | `21600` | A run with no event (of its own or of a run nested in it) for this long ends as `expired`; a waiting call whose run is gone is reported as `unknown` after it |
| `metrics` | `false` | Also export `gen_ai.client.token.usage` and `gen_ai.client.operation.duration` |
| `metrics_export_interval_seconds` | `60` | Metric export interval |
| `shutdown_timeout_seconds` | `5` | How long stopping waits for the last export |

A value of the wrong type falls back to its default with a warning — a typo
never switches content capture on.

## Span model

Names and attributes follow the OpenTelemetry GenAI semantic conventions. The
constants in `opentelemetry-semantic-conventions` are marked deprecated (the
GenAI conventions moved to their own repository), so the plugin spells the
names out.

```
invoke_agent <agent>            one per run (request id)
├── chat <model>                one per LLM request the client reports, retries included
├── execute_tool <tool>         one per tool call
└── invoke_agent <sub-agent>    a run whose request id extends this one's
```

| Span | Kind | Attributes |
|---|---|---|
| `invoke_agent` | INTERNAL | `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name`, `gen_ai.conversation.id` (the conversation: for an agent called as a tool, its caller's -- the top of the chain) + `session.id` (the session the run ran on), `scarabhive.request_id`, `scarabhive.run.outcome` (`completed`, `error`, `cancelled`, `incomplete`, `expired`, `evicted`, `shutdown`), `scarabhive.run.persisted`, `user.id` if configured |
| `chat` | CLIENT | `gen_ai.operation.name=chat`, `gen_ai.provider.name` (as the client names itself, e.g. `openai_httpx`), `gen_ai.request.model`, `gen_ai.request.stream`, `gen_ai.usage.input_tokens` (cache reads included), `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_creation.input_tokens`, `gen_ai.response.finish_reasons`, `server.address`/`server.port` (host only — a URL may carry a key), `error.type` (the HTTP status the error names, else `_OTHER`), `scarabhive.llm.retry` on an attempt the client retried |
| `execute_tool` | INTERNAL | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type=function`, `scarabhive.tool.server`, `scarabhive.tool.source` (`model` or `tool_script`), `scarabhive.tool.outcome` (`ok`, `error`, `cancelled`, `blocked`, `unknown`), `error.type` |

Every span also carries the agent name, session and request id. A failed run,
LLM request or tool call has status ERROR.

- **Run span**: opened by the run's first LLM or tool event, ended at
  `session_end`, which tells the outcome (`metadata`: `cancelled`, `errors`,
  `completed`, `persisted`). A run no event opened (it failed on its way in)
  still gets a span at `session_end`, timed from its opening message.
- **LLM span**: built from `post_llm_response`, ending when the client
  reported the call (`timestamp_ms`) and starting its reported duration
  earlier. LLM calls a tool or hook makes inside a run (the
  run's request id is in the context) are children of the run.
- **Tool span**: built at `post_tool_call`, timed by the call's own start and
  end (`tool_result["started_at"/"finished_at"]` — the post hooks run once
  every call of the step is done, so the hook's own clock would give every
  parallel call the duration of the slowest).
- **Calls without a post hook** — blocked by a `pre_tool_call` hook, cancelled
  before they ran, or lost with a crashed run — are remembered from
  `pre_tool_call` and resolved at `session_end` from their result message
  (`blocked`, `cancelled`, else `unknown`), with no duration. A call blocked by
  a hook that ran *before* this plugin's pre hook is found at `session_end` by
  its blocked result.
- **Nesting by request id**: tool-specific, sub-agent and tool_script request
  ids extend their run's (`<id>_007`, `<id>_007_sub_…`, `<id>_007_ts01`); a
  sub-agent run becomes a child of its parent run, a tool_script call a tool
  span of the run. An event of a nested run keeps the runs above it alive: a
  parent waiting in the tool call that runs its sub-agent does not expire
  while the sub-agent works, nor does a waiting call of a run still alive.
- **A reused request id** (a job dispatched again under its id) gets a run
  span of its own: the id's cancellation token is a new run's then. Late
  events of the ended run (a session_end hook calling a model) stay with it.
  A run whose end never reached the plugin ends as `evicted` when a new run
  takes its id.
- **Calls that share a key** (a backend that sends no call ids) are matched
  to their post hooks in call order.

## Privacy

Without `capture_content`, no prompt, answer, tool argument or tool result
leaves the process — neither as an attribute nor as a status text. Error
texts can quote content (a provider echoing the request, a validation error
quoting its input), so they stay in too: a failed tool call's status says
`tool call error`, a failed LLM request `LLM request failed (HTTP 503)` (only
the status the text names), a failed run `run reported 1 error(s)`. What is
exported regardless: names (agent, tool, model, provider), ids (session,
request, tool call), token counts, timings and the LLM host.

With `capture_content: true`: `gen_ai.input.messages` (the message that opened
the run) and `gen_ai.output.messages` (its last answer) on the run span,
`gen_ai.tool.call.arguments` and `gen_ai.tool.call.result` on the tool span,
and the error texts as the status (LLM and run errors capped at 256
characters). Each content attribute is capped at `content_max_chars`, marked
`…[truncated]`; nested values are shortened before encoding, so a huge result
costs no more than a small one.

The user id is exported only with `capture_user_id: true`.

## Export and global state

- A `BatchSpanProcessor` exports on its own thread; no hook waits for an
  export. An exporter that fails or hangs costs spans, never a run.
- `stop_plugin` ends what is still open (outcome `shutdown`), then flushes and
  shuts down on a daemon thread, waiting at most `shutdown_timeout_seconds`.
- **The plugin never installs a global TracerProvider** (or MeterProvider).
  If the process already has one — an application embedding ScarabHive with
  its own tracing — the plugin uses it, and its own exporter settings do not
  apply (logged at INFO); stopping then only flushes. Otherwise it builds a
  private provider. Why not install it globally: `set_tracer_provider` works
  once per process and cannot be undone, so a plugin stopped and started
  again would find its own dead provider, and it would take the decision away
  from an embedding application that sets its provider later.

## Cost

Four hooks, none of them `pre_llm_call`, `post_llm_call` or
`pre_llm_request`: the registry deep-copies the whole conversation (or the
request payload) for each hook of those types on every step. What the plugin
uses copies the call's arguments and result (tool hooks), the client's
response data (`post_llm_response`) and, once per run, the conversation
(`session_end`). Registering a `pre_tool_call` hook makes the loop build a hook
context for every tool call of every agent.

## Model Experience

### What the model sees

Nothing. The plugin has no tools, injects no message and changes no call or
result; every hook returns unmodified.

### Token and cache effect

None. No request changes, so every request stays a prefix of the next.

### Known gaps

- **Content of LLM requests is not captured**, even with `capture_content`:
  only `pre_llm_request` carries the request, and that hook costs a deep copy
  of the whole payload on every call. The run's input and final answer are
  captured instead.
- **An LLM call cut off before the client reports it** (the run cancelled
  mid-stream, a client that reports only success) leaves no `chat` span; the
  run span ends as `cancelled`/`error` anyway.
- **A tool_script call blocked by a hook** is reported as `unknown`, not
  `blocked`: the block reaches the script as an exception and leaves no
  result message to tell it by. The same for a model call sent without a call
  id: its blocked result carries a made-up id and cannot be matched to it.
- **A run whose span expired or was evicted** loses its outcome: its late
  session_end has no open span to end. Defaults (6 h without an event, 1000
  open runs) keep this to runs that hang.
- **Calls the framework rejects before any hook** (malformed arguments, an
  unknown tool) get no span; the model reads their error in its history.
- **The run span starts at the run's first LLM or tool event**, not at the
  request: prompt rendering before the first call is not in it.
- **Sub-agent runs nest under their parent run, not under the tool call that
  started them**: the tool span is built when the call ends, after the
  sub-agent's run span began.
