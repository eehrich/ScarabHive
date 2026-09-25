# Decision models

A decision model answers **named questions** about a piece of content with a
typed value and a probability — no messages, no prose. TypeSafe calls the class
"System One" models; Jev is the first, and OpenRouter serves it behind
`POST /api/alpha/decisions`. Their own model page says the rest: *"chat
completions SDKs will not work with it"*. Laya (Convai Innovations, Apache-2.0
weights) is an open model for the same wire, run locally with `laya-serve`.

Nothing here can serve `chat()`, so this plugin declares no `provides` — only
`provides_decisions`. That is the same registry seam the TTS clients use, one
over: an entry under `llm_system.decision_models` names the provider, and the
registry imports this package on first use.

```python
from agent_system.config.settings import load_settings
from agent_system.llm.decisions import create_decisions_from_profile

client = create_decisions_from_profile(load_settings(), "jev")   # profile from config/
# ...or no name at all, and llm_system.default_decision_profile decides
decision = await client.decide(
    'Proposed tool call: delete_rows(table="customers", where="last_login < 2023-01-01")',
    {"safe_to_run": {"type": "noul",
                     "instructions": "Is this safe to run without a human approving it?",
                     "criteria": {"true": "Reversible or low-impact.",
                                  "false": "Destructive or irreversible."}}},
)
decision["safe_to_run"].value   # the probability the API sent -- the caller owns the threshold
decision.cost                   # what the call cost, as the API reports it
```

Without a config — a script, a test — the client takes its own arguments:

```python
from plugins.llm_decisions.system_one import SYSTEM_ONE, DecisionsClient

client = DecisionsClient(model="~typesafe/jev-latest")      # OpenRouter, key from OPENROUTER_API_KEY
laya = DecisionsClient(model="multilingual", host=SYSTEM_ONE,
                       url="http://127.0.0.1:8788/v1/systemone")
```

## Hosts: one wire, two providers

| provider | default endpoint | key (env fallback) | `session_id` sent |
|---|---|---|---|
| `openrouter_decisions` | `https://openrouter.ai/api/alpha/decisions` | `OPENROUTER_API_KEY` | yes |
| `systemone_decisions` | `https://api.typesafe.ai/v1/systemone` | `TYPESAFE_API_KEY` | no |

Both build the same client (`system_one.py`); the provider decides the `Host`:
its default endpoint, the name the hooks, the debugger and the usage tracker
book the call under, and whether OpenRouter's `session_id` goes into the body
(TypeSafe's reference lists `model`, `state` and `questions` only). A local
Laya is `systemone_decisions` with its own `url`:

```yaml
decision_models:
  laya:
    provider: systemone_decisions
    model: multilingual            # english | multilingual | typed-decisions
    url: http://127.0.0.1:8788/v1/systemone
    # api_key: ${LAYA_API_KEY}     # only if laya-serve runs with LAYA_API_KEY
```

and the server beside it (its own venv — it brings torch and transformers):

```
pip install "laya[serve]"
LAYA_HOST=127.0.0.1 LAYA_PORT=8788 laya-serve
```

What to know about `laya-serve` before relying on it (0.3.20, read in its
`serve.py` and measured):

- **Its default port is 8000** — the port of this API. Set `LAYA_PORT`. Its
  default bind is `0.0.0.0`, and without `LAYA_API_KEY` it takes any caller:
  set `LAYA_HOST=127.0.0.1` unless it is meant to be reached.
- **An unknown `model` is not refused, it is routed:** anything that is not a
  checkpoint name or alias (`laya`, `laya-multilingual`, …) makes it pick one
  by script and language. And the answer's `model` is always `laya-rl-agent`
  — which checkpoint judged stands only in its `routing` field, which this
  client does not read. Name the checkpoint in the config; a typo there is
  answered silently by another model.
- **Without an `api_key`, a local url gets no key at all** — not the
  `OPENAI_API_KEY` that `llm_common/api_keys.py` hands local OpenAI-compatible
  servers: this wire is not theirs. If laya-serve runs with `LAYA_API_KEY`, it
  answers 401 until the config names that key.
- It answers `usage` without `cost` (so does TypeSafe direct): the result's
  cost is None — unknown, not 0 — and the `decision` tool reports
  `total_cost: null` while any answer in the batch is unpriced. The usage
  tracker then prices the call from `config/llm_pricing.yaml` by the
  configured `model`: give each such model a row (a local Laya at 0,
  TypeSafe at its rate), or its spend stays unknown.
- One forward pass at a time (one worker): measured 0.3–0.6 s per call for a
  three-question form on CPU; the model card's 33 ms is a GPU figure.

## The three question types

`criteria` has a different SHAPE per type — `check_questions` refuses a
questionnaire before it is sent, naming the question. `type`, `instructions`
and (for choice and score) `criteria` are required by OpenRouter's schema; the
minimum of two options is this client's own rule, and it says so in its
docstring, because the endpoint's own limit there is untested:

| `type` | criteria | the answer's deciding field |
|---|---|---|
| `noul` | optional `{"true": …, "false": …}` | `noul` — a probability |
| `choice` | `{option: what it means}`, at least two | `choice` — the option chosen |
| `score` | `[lowest, …, highest]`, at least two | `score` — a point on that scale |

`confidence`, `probabilities` and `legend` come along when the model sends
them; the schema makes them optional, so a caller that needs them handles
`None` rather than assumes. Measured: a `noul` answer carries the probability
and nothing else, while `choice` and `score` bring all three. **The two do not
key `probabilities` the same way** — `choice` by the option (`{"read": 1,
"delete": 0}`), `score` by the POSITION as a string (`{"0": 0, "3": 0.98}`),
with `legend` mapping those positions back to the labels. And a `score` is a
point, not a rung: `2.96` on a four-step scale is a real answer.

## Where it sits, and why not elsewhere

Not under `models:` in the config: an entry there is a CHAT model — every
agent can pick it through a profile, the registry builds an `LLMClient` from
it, and the price guard demands a per-token rate this model does not have
(OpenRouter's answer reports its own cost; for hosts that do not, see
below). `decision_models:` sits beside `models:` the way `tts_models:` does.

Not in `llm_openrouter`: that package declares the OpenRouter SDK
(`openrouter>=1.1.108`), which pins `pydantic<2.13` for the whole application —
a dependency-free httpx client must not inherit that. Not in
`llm_openai_compat` either: the speech client lives there because that wire
*is* OpenAI's and OpenRouter adopted it verbatim; this wire is TypeSafe's, so
the name would be wrong.

Not a second client for TypeSafe or Laya either: the same questionnaire went
to OpenRouter and to laya-serve and the same fields came back, and TypeSafe's
reference lists them too (below). What differs is data, and it lives in `Host`.

## Measured against the live endpoint (2026-09-20)

Two calls, both 200. `~typesafe/jev-latest` is accepted and answers as
`typesafe/jev-1.13-20260917`, `provider: "TypeSafe"`; the body carries `id`,
`model`, `answers`, `provider` and `usage{input_tokens, output_tokens, cost}`
— every field this client reads, and no field it drops.

What it costs: **1.4e-05 to 1.8e-05 dollars** for a three-question form over a
one-line state. Note where that goes — 338 to 439 INPUT tokens for a shell
command of a few words: the questionnaire is most of the prompt, so the number
of questions costs more than the content being judged.

What it answered, which is the point of the thing:

| state | safe_to_run | risk (0-3) | kind |
|---|---|---|---|
| `git status` | 0.98 | 0 | read |
| `rm -rf / --no-preserve-root` | 0.01 | 2.96 | delete |

Still untested: every error path (a refused questionnaire, a 429, a broken
body) — those are pinned by unit tests against a patched transport, not by the
endpoint. `/api/alpha/` says the rest: the shape may still change, which is why
it is pinned in one file.

## The other hosts, measured (2026-09-25)

One three-type questionnaire (`rm -rf /tmp/build`: safe? read or delete? risk
0–2), sent unchanged:

- **OpenRouter `/api/v1/systemone`**: the same body as `/api/alpha/decisions`
  — `id`, `provider`, `usage.cost` included; `session_id` accepted, and
  `jev-latest` without the prefix as well. Jev is not deterministic: `safe`
  came back 0.35, 0.32, 0.35, 0.31 on four identical calls.
- **laya-serve 0.3.20** (CPU): the three types in the same fields, plus
  `answer_confidence`, `action` and `routing`; no `id`, `provider` or `cost`;
  extra request fields (`session_id`) ignored; a malformed question is a 422
  naming it. The client of this package, unchanged, read its answer.
- **TypeSafe `api.typesafe.ai`**: NOT called — no key here. Its reference
  lists the same request fields and `model`, `answers`, `usage` back, and 529
  (overloaded) as retryable, which `llm_common/http_status.py` now is.

Same questionnaire, different verdicts — Jev: safe 0.31–0.35, delete 1.0, risk
1.1; Laya english: 0.19, 0.98, 0.58; multilingual: 0.09, delete, 0.87;
typed-decisions: 0.24, delete, 1.01. A drop-in on the wire is not a drop-in
for a threshold a caller tuned on Jev.

## Tests

`tests/test_system_one.py` — the wire against a patched `httpx.AsyncClient`:
the three answer types, the malformed questions that never leave the machine,
an answer this client cannot read or does not give at all, a body that is not an
answer, the retries, the hook dispatch on every way the call can end, and the
cancels (before the call, in flight, and during the wait between two attempts);
per host, what goes out and under which name it is booked, and a recorded
laya-serve answer.

## License

See LICENSE.
