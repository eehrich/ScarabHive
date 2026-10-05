# otel

OpenTelemetry traces for agent runs, LLM requests and tool calls -- one span each, nested as the calls were made,
named after the GenAI semantic conventions -- and, on request, the two GenAI client metrics. It only watches: no
tools, no changed message, call or result, and it never fails a run. Off by default; prompts, answers, tool
arguments and results stay in the process unless `capture_content` is on.

- **Hooks** `record_llm_call`, `open_tool_call`, `close_tool_call`, `close_run` -- build the `chat`, `execute_tool`
  and `invoke_agent` spans; on for every agent once the instance is enabled.
- **Tools / panel** -- none.

Enable it in `config/plugins.yaml` (`otel: {type: otel, enabled: true}`, exporter and endpoint in its `config:`
block, or the standard `OTEL_EXPORTER_OTLP_*` variables); `exporter: console` prints spans to stdout.

The full manual -- the span model and every attribute, privacy, the hooks, the settings and how export and
shutdown work -- is the plugin's guide, `otel.guide`, in the Help panel.
