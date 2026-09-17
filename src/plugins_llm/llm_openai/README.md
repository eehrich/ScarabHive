# OpenAI SDK Provider

api.openai.com through the official `openai` SDK, plus the Realtime session
adapter.

## What it provides

| Name | Kind | Module |
|---|---|---|
| `openai` | LLM | `openai_client.py` |
| `openai` | batch | see below |

```yaml
my-model:
  provider: openai
  model: gpt-5.6
  api_key: ${OPENAI_API_KEY}
```

`dependencies = ["openai>=1.37.0"]`, imported lazily in the factory.
`default_base_url` is declared in `plugin.toml` so the config resolver can
tell a base-URL-less entry apart from one that talks to a gateway.

## Where the batch backend actually lives

`provides_batch = ["openai"]` names this plugin, but the `/v1/batches`
implementation sits in `llm_openai_compat` — it is plain HTTP and never
touched the SDK. The factory here reaches it through the registry. The
naming rule is deliberate: a batch backend always carries the **same name as
the client it belongs to**, so `batch_provider: openai` pairs the SDK client
with this backend and `batch_provider: openai_httpx` pairs the httpx client
with its own. No mapping table anywhere.

## Realtime

A model whose capabilities say `default_api_type: realtime`
(gpt-realtime-2.1, gpt-realtime-2.1-mini) is served over the Realtime API (GA) instead of
`/v1/chat/completions`, which those models do not answer — `chat`,
`chat_tools` and `chat_tools_streaming` all take that path.

- One WebSocket per call, one `response.create` with `conversation: "none"`:
  the whole history goes as `input` items, a leading system message as
  `instructions`, later system messages stay items at their place. Output is
  text only: a `session.update` sets `output_modalities: ["text"]` first (the
  session default is audio). Set on the response instead, gpt-realtime-2.1
  reports `input_tokens: 0`, and the call would look free.
- Text streams as `response.output_text.delta`; a tool's name arrives only
  in `response.output_item.added`. The result — text, tool calls, usage — is
  read from `response.done`.
- A response cut at `max_output_tokens` or by the content filter keeps its
  text and reports `finish_reason` `length` / `content_filter`, as Chat
  Completions does. Any other status than `completed` is an error in the
  shape the agent server reads (`error.message`, `error.type`).
- `max_tokens` goes as `max_output_tokens`, as the entry says: the ceiling is
  the model's (gpt-realtime-2.1: 32000; the older gpt-realtime and
  gpt-realtime-mini reject more than 4096). `temperature`
  and `modalities` from the model entry do not exist in the GA API and are
  not sent (logged once per client).
- The WebSocket URL follows `base_url` (`https://…/v1` → `wss://…/v1/realtime`),
  and `ssl_verify` applies to it as to the HTTP client.
  Connecting waits up to 10 s for the handshake and 10 s for
  `session.created`; after that every event waits at most the model's
  `request_timeout` (60 s when unset). A closed or silent socket ends the
  call with an error, which also reaches the post-response hook.
- A cancel ends the call at once, while waiting too, as `CancelledError` —
  the form the agent server reports as a cancel. A request cancelled before
  it is sent is not sent.
- Images go as `input_image` — from user messages and, as on the chat path,
  a tool result's attachments as a user message right after its output
  (read and encoded off the event loop).
  Audio input is not sent, whatever `audio_input` says: an audio part of a
  user message becomes an "audio input not supported" note, and so does an
  audio attachment of a tool result (for a model without `image_input`, the
  tool note is the general one: the content cannot be shown). Usage is
  mapped to the Chat Completions shape, so the cost layer prices it — at the
  text rate (audio tokens are not reported apart). gpt-realtime-2.1 reports
  reasoning tokens; they go on as `completion_tokens_details.reasoning_tokens`.
- Known gap: no retry and no `LLMRateLimitError`. How the GA API reports a
  rate limit (HTTP 429 at the handshake, or a `failed` response) has not been
  observed yet; until then a limit is an ordinary error and the agent falls
  back to the next profile.

`realtime_session.py` holds the connection, `realtime_adapter.py` the
conversion. Checked live against gpt-realtime-mini on 2026-09-16 and
gpt-realtime-2.1 / gpt-realtime-2.1-mini on 2026-09-17.

## `provider_routing` does not apply here

The gateway `provider` object (`{order: [...], allow_fallbacks: false}`) is
OpenRouter vocabulary. This SDK path does not forward it and would drop it
silently — the config field's docstring says so, and no catalogue entry uses
this provider with routing set. Models that need routing belong on
`openai_httpx` or `openai_responses`.

## Tests

`tests/` next to the code: client behaviour, vision input, the
multimodal tool-result injection, and the Realtime path against a scripted
WebSocket (`test_llm_openai_realtime.py`).

## License

Apache-2.0 — see `LICENSE`.
