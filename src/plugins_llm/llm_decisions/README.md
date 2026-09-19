# Decision models

A decision model answers **named questions** about a piece of content with a
typed value and a probability — no messages, no prose. TypeSafe calls the class
"System One" models; Jev is the first, and OpenRouter serves it behind
`POST /api/alpha/decisions`. Their own model page says the rest: *"chat
completions SDKs will not work with it"*.

This package is a **library**, not a provider: nothing here can serve `chat()`,
so the LLM registry skips it. A caller imports the client, the way the audio
plugins import a TTS client.

```python
from plugins_llm.llm_decisions.openrouter import DecisionsClient

client = DecisionsClient(model="~typesafe/jev-latest")      # key from OPENROUTER_API_KEY
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

## The three question types

`criteria` has a different SHAPE per type — `check_questions` refuses a
questionnaire before it is sent, naming the question. `type`, `instructions`
and (for choice and score) `criteria` are required by OpenRouter's schema; the
minimum of two options is this client's own rule, and it says so in its
docstring, because nothing here has been tried against the live endpoint:

| `type` | criteria | the answer's deciding field |
|---|---|---|
| `noul` | optional `{"true": …, "false": …}` | `noul` — a probability |
| `choice` | `{option: what it means}`, at least two | `choice` — the option chosen |
| `score` | `[lowest, …, highest]`, at least two | `score` — a point on that scale |

`confidence`, `probabilities` and `legend` come along when the model sends
them; the schema makes them optional, so a caller that needs them handles
`None` rather than assumes.

## Where it sits, and why not elsewhere

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

## Not measured yet

No call against the live endpoint has been made from this repo. The shapes come
from OpenRouter's OpenAPI document and their own worked example, and the tests
pin exactly those. The model is served by TypeSafe at $0.042 per million input
tokens, output free. `/api/alpha/` says the rest: the shape may still change,
which is why it is pinned in one file.

## Tests

`tests/test_openrouter.py` — the wire against a patched `httpx.AsyncClient`:
the three answer types, the malformed questions that never leave the machine,
an answer this client cannot read or does not give at all, a body that is not an
answer, the retries, the hook dispatch on every way the call can end, and the
cancels (before the call, in flight, and during the wait between two attempts).

## License

See LICENSE.
