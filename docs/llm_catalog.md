# The model catalogue — the decisions behind it

`config/llm.yaml` and `config/llm_openrouter.yaml` are **data**: model names,
windows, knobs. What a field means is documented on the field itself in
`agent_system/config/models.py`. What is written here is what you cannot tell
from the values.

## What an endpoint speaks is stated by its entry — not by its name

The clients know no model names. Quirks of an endpoint are keys on the model, and
whoever adds a new provider sets lines instead of code. Which route evaluates
which key:

| Key | `openai_httpx` | `openai_responses` | `openrouter_sdk` | `anthropic` | `gemini*`, `openai`, `ollama` |
|---|---|---|---|---|---|
| `tool_schema_dialect` | yes | yes | yes | – | – |
| `reasoning_details_mode` | yes | yes | yes | yes | – |
| `assistant_reasoning_field` | yes | – | – | – | – |
| `thinking_request_shape` | – | – | – | yes | – |
| `stream_silence_timeout` | yes | yes | – | – | – |
| `provider_affinity_minutes` | yes | yes | yes | – | – |
| `prompt_cache_marker_style` | yes | yes | refuses `anthropic` | own path | – |
| `safety_settings` | yes | yes | refuses | – | only `gemini*` (native) |

The first six rows are the **dialect keys**: if one is set on a route that does
not evaluate it, building the client logs this ("is not wired for provider=…").
The last two rows do not have this guard.
An unknown **value** makes the build fail in all cases. Both are deliberate: being
silently ignored is the case you only notice weeks later on the invoice.

## Structured output: the route has the field, the entry says whether the model can do it

A caller that wants the final answer as JSON (`ResponseFormat`,
`agent_system/llm/structured_output.py`) gets the native field only if
**both** hold: the route has a field for it (`response_format_kinds` on the
client) and the model entry declares it.

| Route | Field | JSON schema | JSON object |
|---|---|---|---|
| `openai`, `openai_httpx` (also `ollama` in `openai_compat` mode) | `response_format` | yes | yes |
| `openai_responses`, `openrouter_sdk` | `text.format` | yes | yes |
| `anthropic` | `output_config.format` | yes | – (the Messages API has no JSON mode) |
| `gemini`, `gemini_sdk` | `responseMimeType` + `responseJsonSchema` | yes | yes (MIME type only) |
| `ollama` native | `format` | yes | yes (`"json"`) |
| Batch, Realtime | – | – | – |

- `capabilities.structured_output: true` means: the model sticks to a schema —
  **even in a request with tools**, because an agent sends the field on every
  step. Gemini before 3 rejects this combination: do not set it there.
  Over OpenRouter, each model has it individually (`supported_parameters` contains
  `structured_outputs`).
- `capabilities.json_mode` is not read. Native "any JSON object" is only given
  to a model with `structured_output: true`: nobody read the `json_mode` values in
  the catalogue files until F11 and they are unverified (Claude entries carry
  `true`, although the Anthropic route has no JSON mode at all). A model with only
  `json_mode` gets the format as a hint in the conversation, and the answer is
  validated like any other.
- The schema goes out as it passed through the strict subset
  (`agent_system/llm/schema_worker.py`: keyword whitelist, `$ref` only
  to `#/$defs/…`, annotations such as `default` removed). Gemini gets it as
  `responseJsonSchema` (JSON Schema), not through the sanitizer of the function
  declarations. Anthropic and OpenAI in `strict` mode reject schemas whose
  objects lack `additionalProperties: false` — loudly, as a 400, instead of
  silently rewriting them.
- Why the field is present on every step and not only on the last one: OpenAI
  renders the schema into the cached context, Anthropic discards the
  conversation's cache when `output_config.format` changes. Kept constant across
  the run, the prefix stays the same (measured on the payload:
  `tests/agent/test_agent_structured_output.py`); only on the last call, exactly
  that call would be a cache miss.

As of 2026-09-29, no entry in `config/llm*.yaml` declares
`structured_output`. Until an operator does, every structured call — schema or
JSON object — goes through the fallback (hint in the conversation +
validation) or fails if the caller does not allow a fallback.

## How long a stream may send only keep-alives

OpenRouter keeps a waiting stream open with comment lines, roughly every
half second. Each of them resets the `read` timeout — so it never fires there.
`stream_silence_timeout` counts only real events: if none arrives for that long,
the attempt is retried. Without an entry there is no such limit.
If a run's end event has already arrived, the answer stands, no matter how
the stream ends afterwards (keep-alives, silence, dropped connection).

Measured on 2026-09-16 (production payload, flex tier): between two events
there were at most 9.2 s, even across 38,000 tokens of hidden thinking — OpenRouter
reports an `output_item` every few seconds. The 900 s of the base are
at OpenAI's recommendation for flex (15 minutes of waiting are normal there).
DeepSeek direct gets no entry: under load the API likewise sends only
keep-alives, but closes a request itself after 10 minutes of waiting.

Do not confuse: a call that **generates** for 26 minutes (measured: a
continuity pass with 55,742 tokens) is not silent and is not
aborted. What helps there is the agent's output limit, not a timeout.

## OpenRouter: why the backends are pinned

OpenRouter's prompt cache is **backend-local**. Whoever leaves the provider choice
to price (`sort: price`) spreads the same conversation across several
backends — and with long contexts a cache miss easily costs more than the
cheaper provider saves. That is why the entries set `provider_routing.order`
and stay with the same backend.

`allow_fallbacks: false` is set where falling back would be expensive: if the
pinned provider fails, the **agent chain** (llm_profile) takes over, not a
silent 2x price jump at OpenRouter.

### Who actually delivered: `openrouter_metadata`

On OpenRouter endpoints the clients send the header
`X-OpenRouter-Metadata: enabled`. Without it **no** answer names the
backend — on the Responses route there is no other field for it. With it,
every `post_llm_response` hook gets a `routing` field:

```json
{"selected": "DeepInfra", "available": ["DeepInfra", "StreamLake"],
 "attempt": 1, "strategy": "latest", "region": "FRA"}
```

This makes it traceable for the first time whether `provider_routing.order`
held. **The client does not judge this itself:** the config names
gateway slugs, the metadata names display names, and only the gateway's
provider list can translate (`GET /api/v1/providers`, `name` → `slug`) — a
naive comparison would raise false alarms precisely at the hardest pins.
Reported, not judged.

**Provider pin:** Every assistant turn remembers as `served_by` which
backend delivered. The next request goes out with a **hard pin** on it:
`order: [this one]` plus `allow_fallbacks: false`. The slug for the display
name comes from the provider list; no provider name appears in the code.

Why hard: `allow_fallbacks: false` only keeps providers **outside** the list
out. A list with two entries rotates anyway — with DeepSeek in 34
of 38 backend changes within a run —, and an entry without any
`order` is distributed freely by the gateway: a `coder` call on 2026-09-22 ended
up on a backend with a cold cache and cost four times as much as its
neighbours. Hence an entry without a way to fall back; a model entry without
`order` is pinned the same way.

The entry still decides **which** backends are allowed at all: if the
one that delivered is not in its `order`, the entry stays unchanged.

**If the pinned backend refuses** (429, 5xx, or 404 "No endpoints found"
when it no longer carries the model), the same call goes out again immediately
— without the pin, i.e. as the model entry is configured, and without a
rate-limit wait; there the gateway may choose again. The agent type does not
remember the refused backend any further. What the backend did answer, on the
other hand (for example a 400 because of a broken reasoning item), is not a
refusal: the healing stays on the same backend, otherwise the replayed history
fails again.

**The first call of a run** has no assistant turn yet. It starts
on the backend that last served the **agent type** — regardless of which
instance, which run, which client (`agent_system/llm/backend_affinity.py`,
key agent name + model, set via `set_app_title`). That is where the
prompt shared by all runs of the type lies. This only applies within
`provider_affinity_minutes` after the type's last call (default: the model's
cache duration `prompt_cache_ttl_minutes` — Claude and Gemini 5,
GPT 30 —, without the 30; `0` = off, per model entry or via `llm_params`
per agent). After that
the cache is cold, and the configured `order` applies again. Measured on
2026-09-22 on the first calls of pipeline runs (72 h):

| Gap to the type's last call | same backend | different backend |
|---|---|---|
| < 5 min | 57.8 % from cache | 27.8 % |
| 5–30 min | 22.5 % | 9.0 % |
| 30–60 min | 1.1 % | 8.3 % |

A run's own history always beats this value: only it says
which backend can verify the replayed reasoning. If the
preferred backend refuses (429, 5xx), the gateway falls through to
the remaining `order` entries in the same request (measured: 34 of 38 changes within
a run were exactly that, the other 4 requests carried no `order`).
The memory lives in the process; after a restart the first call of
each type follows the `order` again.

**Model pin for aliases (Responses route):** A `~author/family-latest`
is resolved per request, and OpenRouter silently steps down to an older model
of the family when the newest fails (429/5xx; undocumented,
OpenRouterTeam/docs#601). Measured on a `shorts_producer` run on
2026-09-30:
- 23 of 69 calls went to 3.7 after a 504 from gemini-3.8-flash.
- The calls alternated erratically between the two models, and every change hit
  a cold cache: 20 % read instead of 74 %.
- Each of these calls had waited about 25 s for the 504 beforehand.

That is why a run stays on the model that answered its last turn.
That is stored as `served_model` in the replay block. The request names the
concrete slug, and OpenRouter can no longer step it down (`available=1`).
This applies to every `~` alias on the Responses route; DeepSeek steps down in the
same way, after 429s. The Claude aliases run over Chat Completions and are not
pinned; no step-down was observed there.

A refusal (429, 5xx, 404) sends the retry back to the alias,
as with the provider pin; the run then follows the model that answered,
until that one refuses. A run without its own history begins at the alias, i.e.
at the newest model; a resumed session stays on its model until it
refuses. The price: if the pinned model fails, the same
call waits twice, because the alias tries it once more before stepping down.
In return, a short outage passes without a model change.

The replay block of another model of the same alias counts as foreign here:
Its encrypted reasoning can only be verified by the model that wrote it.
Previously the block carried only the alias, and the check compared alias with alias.
If a block of a message is foreign, or one of its tool calls is missing from the
replay, the whole message is rebuilt.
The message validator merges consecutive turns; replayed only halfway,
the calls of the foreign half were missing, but their results
were in the request.

### `session_id`: cache locality without a hard pin

OpenRouter's prompt cache is backend-local (see above). `session_id` is the
gateway's sticky-routing key: same session → same backend.
Measured on 2026-09-01: **6/6** calls on one provider with `session_id`,
without it 6 calls were spread across **4** providers.

For this the clients send the **resolved `prompt_cache_key`** — it already
means "same stable prefix", so it is exactly the right grouping, and
needs no wiring that does not exist. Only to OpenRouter; a foreign
OpenAI endpoint rejects unknown parameters with a 400.

This does not replace `provider_routing.order`, but makes it less necessary: whoever
loosens the hard pin keeps the cache hits with `session_id` and wins
back that a failed provider no longer costs the whole model entry.

### Three fields that are available and empty for good reason

`plugins`, `prompt_cache_options` and `safety_identifier` are passed through
by the clients unchanged; none of them is set:

| Field | what it could do | why unset |
|---|---|---|
| `plugins` | `context-compression` (shorten the prompt automatically), `response-healing`, `moderation`, `file-parser`, `auto-router` | each changes what the model sees or costs — not without a measurement |
| `prompt_cache_options` | `{"mode": "explicit"}` switches off OpenAI's **own** breakpoints, so that only our markers count (GPT-5.6+) | which of the two variants caches better is unmeasured here |
| `safety_identifier` | stable pseudonym per end user; without it the request carries the **account** identity, so a policy block hits everything | a value per run would have to come from the caller, and that path does not exist yet (Responses route only) |

### No more `usage: {include: true}`

The field is deprecated at OpenRouter and has no effect — costs, cache hits
and `cost_details` arrive in every answer anyway. Cross-checked on 2026-09-01,
**streaming and non-streaming alike**: with and without the field identical
`usage` keys and the same `cost`. That is why the clients no longer send it;
adding it back gains nothing. `stream_options: {include_usage: true}`
stays, on the other hand — OpenAI direct needs that, not OpenRouter.

## DeepSeek via OpenRouter: fp8, not fp4

Price table of 2026-08-20 (input / output / cache read per 1M tokens):

| Endpoint | in | out | cache | Quantization |
|---|---|---|---|---|
| open-inference | 0.065 | 0.14 | 0.014 | fp4 |
| relace | 0.07 | 0.14 | 0.014 | fp4 |
| decart | 0.0765 | 0.153 | 0.0153 | fp4 |
| **streamlake** | 0.0784 | 0.1568 | 0.0157 | **fp8** |
| **baidu** | 0.0798 | 0.1596 | 0.0160 | **fp8** |
| **deepinfra** | 0.08 | 0.18 | 0.0160 | **fp8** ← `order[0]` |
| deepseek (direct) | 0.22 | 0.66 | 0.007 | — |

The three cheapest are **fp4-quantized**. For prose that is a quality
risk not worth ~15 % savings — that is why `order` starts
at an fp8 endpoint. Whoever wants to try fp4: put `open-inference` at position 0
and measure **on the output**, not on the invoice.

**baidu is not in the order**: it caps the output at 131072 tokens,
streamlake and deepinfra allow 384000. Three scorer agents request 262144 via
`llm_params` and would abort with a 400 at baidu (review finding B4).

`quantizations: ["fp8"]` is **enforced**, not preferred: without the field,
a busy pin falls through price-sorted — straight to the fp4 endpoints
that the table rules out for prose.

Context window: measured on the pinned endpoints (2026-08-20) — streamlake
1.024M, baidu/deepinfra 1.048M. The older or-deepseek entries declared
100k; **those** are the understatement, not the large value. A window that is too small
would make the summarizer fire ten times too early.

`openrouter-deepseek-pro` uses the alias `~deepseek/deepseek-pro-latest`
(order: alibaba, deepinfra).

## Why some entries set no `max_tokens`

The OpenRouter DeepSeek entries replace direct profiles that ran without a cap.
A hard limit there would have silently truncated long scenes and JSON — and
reasoning shares this budget. Hence: no `max_tokens`, the provider
decides. For the `-unlimited` variants `max_tokens: null` is
stated explicitly, because otherwise they would inherit it from their parent entry.

## Gemini via OpenRouter: the Responses route

The Gemini entries run via `provider: openai_responses`, not via the
Chat Completions bridge. On this route the model's output items travel
**verbatim** there and back; the bridge had to reconstruct them, which produced the
`"encrypted content ... could not be verified"` 400s.

**The `openrouter-gemini*` entries set no `safety_settings`** — the field
has no effect on this route. The same nonsense value
(`category: HARM_CATEGORY_NOT_A_REAL_THING`) is rejected on `/chat/completions` with
HTTP 400 including the enum list, and on `/responses` swallowed silently with
HTTP 200: OpenRouter drops it there before it reaches Google (measured 2026-09-01).

Nothing is missing as a result. `debug.echo_upstream_body` shows on the chat route
that without its own setting OpenRouter sets all five categories to `OFF` —
more permissive than the thresholds that used to stand here (three
at `BLOCK_ONLY_HIGH`, and `HARM_CATEGORY_CIVIC_INTEGRITY` dropped out of the
upstream body entirely). For prose our setting was the *stricter* one.

Whoever really needs their own thresholds takes the native `gemini-3-*` entries
(`provider: gemini_sdk`): they talk directly to Google, where the field takes effect.

`service_tier: flex` is Google's Flex Processing: cheaper, but a longer
queue. On a 429 on the flex tier the client drops the field once
and retries on standard.

### AI Studio before Vertex

`openrouter-gemini` asks Google AI Studio first, Vertex afterwards. Measured on
2026-10-01 with the first 30 calls of a `shorts_producer` run. They were
replayed twice per provider, each with its own nonce, so that no
pass hits another's cache:

| | Standard | Flex |
|---|---|---|
| AI Studio: from cache | 80–84 % | 82–84 % |
| AI Studio: cost | $0.25–0.27 | $0.12–0.14 |
| AI Studio: per call | 3.5 s | 4.5 s (max. 11 s) |
| Vertex: from cache | 59–71 % | 77–83 % |
| Vertex: cost | $0.32–0.40 | $0.13–0.14 |
| Vertex: per call | 7–12 s | 19 s (max. 78 s) |

The prefix was byte-identical in all cases. Vertex (on OpenRouter only its
`global` endpoint) misses its implicit cache more often and is slower. On
flex, Vertex needs 20–50 s per call; the measurement of 30.09. with "flex
14–315 s" was on Vertex. AI Studio with flex is almost as fast as with standard,
but costs only half. A real run confirms this: the same production
with flex on AI Studio cost $0.35 at an 89 % cache share.

If AI Studio fails (429/5xx), the call goes to Vertex. The pin on the provider
(`served_by`) keeps the rest of the run where it started.

## GPT-5.6 via OpenRouter

`prompt_cache_key: "auto"` is **mandatory** from GPT-5.6 on for reliable
cache matching — without a key the model practically never caches (proven 2026-07-21:
byte-identical 10k prefix, `cached_tokens=0`). The key belongs to the model, not
in the agent files; it stood there 34 times until 2026-08-22.

Reasoning round trip: The items of the reasoning models form an
encrypted chain that every turn must return in full. The
config field `reasoning_details_mode` says per model how much of it
travels back, and is evaluated on `openai_httpx`, `openai_responses`,
`openrouter_sdk` and `anthropic`. The default depends on the route:
`keep_last` on the chat route (where a thought signature only applies to the
current turn), `keep_all` where whole item chains go back verbatim.

⚠️ Whoever uses the cache of a model that compares its prefix byte for byte
(Claude) needs `keep_all` — with `keep_last` the previous round loses its
blocks, the prefix changes before the anchor, and every step rewrites the
cache instead of reading it. Measured on `book_launcher` (43 calls): 19 times
a fall back to 25,878 tokens read, never more. `openrouter-claude` therefore
sets it.

## `openrouter_sdk`: the same route via the official SDK

Since 2026-09-01 there is a second way to the same `/responses` endpoint:
`provider: openrouter_sdk` (plugin `plugins/llm_openrouter`) sends the
request via OpenRouter's official Python SDK. It is an **A/B candidate**,
not a replacement: the client inherits from the `openai_responses` client and swaps only
`_post` — payload building, cache breakpoints, parser, healing loop and hooks
are the same. What differs in a comparison is the transport.

Switching is one line; the entries inherit from `openrouter-base` anyway:

```yaml
    mein-modell:
      extends: openrouter-base
      provider: openrouter_sdk    # instead of openai_responses
```

Prerequisite: `pip install openrouter` (it is in `requirements/all.txt`).
Without the package installed, only this one entry is affected — the plugin
is only imported when a model names the provider.

**What is measured** (openrouter 1.1.108, 2026-09-01):

* The answer is read **from the raw body**, not from the typed
  result. The main reason is structural: the inherited healing loop decides
  by status code, detects body errors in an HTTP 200 and checks
  reasoning rejections against the body **text** — it needs the raw text anyway.
  In addition there is a known fragility: `usage` is `OptionalNullable`,
  and `UsageCostDetails` requires
  `upstream_inference_input_cost`/`…output_cost`. If those are missing, the
  **whole** `usage` object silently falls back to `Unset()` — tokens, `cost` and
  cache hits gone, without an error. **Not observed live**: the answers
  measured on 2026-09-01 (deepseek-v4-flash, gemini-3.5-flash-lite)
  all carried the required fields, the typed model would have parsed them
  correctly. So the raw body is a precaution, not a repair case.
* The request goes out typed and carries everything this route needs:
  `provider` routing, `reasoning`, `service_tier`, `prompt_cache_key` and the
  cache breakpoint `prompt_cache_breakpoint`.
* **Two fields it cannot do**, and therefore it refuses at build time instead of
  losing them: `safety_settings` (not an SDK parameter — this rules out the
  Gemini route) and Anthropic `cache_control` per content part
  (`prompt_cache_marker_style: anthropic`).
* The SDK adds three fields on its own: `store: false`, `stream: false`
  and `service_tier: "auto"`. The last one means: the flex drop sends the
  standard tier *explicitly*, where the httpx route omits the field.
* Price of the dependency: `pydantic<2.13`. This caps the whole application
  one minor below the current release.

## Profiles

There is **no `turbo-batch` profile** any more. `turbo` has pointed to
gemini-3.5-flash-lite via OpenRouter since 2026-08-18, and there is no
OpenRouter batch path (batch backends exist only for
gemini/openai/openai_httpx/anthropic as `batch_provider`).

`default_profile: or-deepseek-flash` (since 2026-08-20, before that `chat`):
the same model family, but via OpenRouter — `chat` pointed to the
direct API, whose account is almost empty. Applies only as the last fallback level,
when no profile can be resolved.

## What is NOT in the catalogue

* **No field explanations.** Those are on the Pydantic field in
  `agent_system/config/models.py`.
* **No change log.** Whoever wants to know when and why a value was set
  reads `git log -p config/llm*.yaml` — it is all there, and without
  clogging up the file.
