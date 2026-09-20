# Decision models

A decision model answers **named questions** about a piece of content with a
typed value and a probability — no messages, no prose. TypeSafe calls the class
"System One" models; Jev is the first, and OpenRouter serves it behind
`POST /api/alpha/decisions`. Their own model page says the rest: *"chat
completions SDKs will not work with it"*.

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
from plugins.llm_decisions.openrouter import DecisionsClient

client = DecisionsClient(model="~typesafe/jev-latest")      # key from OPENROUTER_API_KEY
```

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
(the answer reports its own cost). `decision_models:` sits beside `models:`
the way `tts_models:` does.

Not in `llm_openrouter`: that package declares the OpenRouter SDK
(`openrouter>=1.1.108`), which pins `pydantic<2.13` for the whole application —
a dependency-free httpx client must not inherit that. Not in
`llm_openai_compat` either: the speech client lives there because that wire
*is* OpenAI's and OpenRouter adopted it verbatim; this one is OpenRouter's own
path with their provider-routing fields, so the name would be wrong.

TypeSafe serves the same models directly (`POST /v1/systemone`), and other
gateways have picked them up. When a second client is built here, what the two
share moves into a module of its own — with two implementations to generalise
from rather than one.

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

## Tests

`tests/test_openrouter.py` — the wire against a patched `httpx.AsyncClient`:
the three answer types, the malformed questions that never leave the machine,
an answer this client cannot read or does not give at all, a body that is not an
answer, the retries, the hook dispatch on every way the call can end, and the
cancels (before the call, in flight, and during the wait between two attempts).

## License

See LICENSE.
