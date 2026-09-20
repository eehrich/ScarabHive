# Agent Continuation

Hook-only plugin. It reads every text-only LLM response and decides whether the
agent is finished or has just filed a status report. On "status report" it sets
`metadata["continue"] = True`, and the core loop injects a continuation message
instead of returning — which is what makes multi-step autonomous runs possible
without the agent stopping after "I will now do X".

## What it provides

| Hook | Type | Default |
|---|---|---|
| `evaluate_completion` | `post_llm_call` | `enabled: false` |

`type = ["hooks"]`, no tools, no pip dependencies.

## What it never touches

Responses **with** `tool_calls` and empty responses are skipped immediately —
the loop continues on its own there, so evaluating them would only cost time
and risk a double continuation. Only a text-only answer is a candidate for
"final".

The hook never modifies the response content: `HookResult(modified=False)` even
when it decides to continue. It communicates purely through `metadata`.

## Four strategies

* **`rules`** — deterministic, no LLM cost. The default.
* **`llm`** — a lightweight profile classifies the answer as `FINAL` or
  `CONTINUE`.
* **`decision`** — a decision model answers ONE named question with a
  probability. See below.
* **`hybrid`** — rules first; `hybrid_fallback` (`llm` by default) decides only
  when no rule matched.

Every failure path resolves to FINAL: no evaluator, no prompt or question, an
exception during the call. Stopping is the safe error, continuing costs tokens
in a loop. The one exception is a user cancel — `CancelledError` is a
`BaseException` and travels on, so a stop stays a stop.

## `decision`: ask one question, get a probability

A decision model (`llm_system.decision_models`, see
`src/plugins_llm/llm_decisions`) does not write prose. It takes the answer plus
one named question and returns how likely it is that the answer is *final* —
which is exactly the judgement this hook needs, and the reason it is worth a
second kind of model here.

What that buys over `llm`: **the number**. `FINAL` from a chat model says
nothing about how close the call was; `0.83` and `0.51` are both "final" and
only one of them deserves to be trusted. The probability is in the log line and
in `metadata["continuation_reason"]`, so a run that continues too eagerly can
be tuned instead of guessed at.

```yaml
hooks:
  overrides:
    agent_continuation.evaluate_completion:
      enabled: true
      strategy: decision
      # every key below is optional — these are the defaults
      decision_profile: ""            # "" = llm_system.default_decision_profile
      decision_question: "Is this the agent's FINAL answer to its task?"
      decision_final_means: "A finished result: the work is done and this reports the outcome."
      decision_continue_means: "An intermediate status report, a plan, or an announcement of what the agent is about to do next."
      decision_threshold: 0.5         # continue while p(final) is BELOW this
```

`decision_threshold` is where a run says how sure it wants to be. `0.5` means
"whichever the model thinks is more likely" — the honest default. Raise it to
keep working unless the model is quite sure; lower it to stop more easily. It
is a **probability**, not a percentage: anything outside 0.0–1.0 is refused
with a warning and the level above decides, because `70` would put every
answer below the line and pay for a continuation on each one until the budget
runs out.

One divergence from `strategy: llm` worth knowing: `network.ssl_verify: false`
does not reach this client. The seam that builds it takes the model entry
alone, so a decision call always verifies — like the TTS and batch clients,
and unlike the chat evaluator next to it. Behind a MITM proxy this strategy
therefore fails every call (and, per the contract above, answers FINAL every
time).

**Measured** against `~typesafe/jev-latest` on 2026-09-20, with exactly the
defaults above:

| agent response | p(final) | at 0.5 |
|---|---|---|
| "I will now search the codebase for the failing test." | 0.02 | continue |
| "Here is my plan: 1) read the config, 2) patch the loader…" | 0.03 | continue |
| "Which of the two databases should I migrate first?" | 0.05 | continue |
| "The answer is 42." | 0.77 | final |
| "Fixed. All 42 tests pass and I committed as a1b2c3d." | 0.83 | final |

Two things to take from that table. The separation is wide — no run needs to
sit near the threshold. And the third row is a policy question, not a bug: an
agent that asks the user something is not delivering a finished result, so with
these criteria it gets continued. If a run should stop and let the human
answer, say so in `decision_final_means` ("…or a question the agent cannot
resolve on its own").

### Where that cost shows up

In the live total, since `b9222431`. It did not at first: `context_usage_tracker`
hooks `post_llm_call`, which the agent loop fires, while the decisions client
fires `PRE_LLM_REQUEST` / `POST_LLM_RESPONSE` — so the spend reached only the
message debugger, whose rows a retention setting prunes and a switch can turn
off. This hook was the first caller that made that cost money per agent step,
and the tracker now carries a second hook for calls that belong to no agent.

Its guard is worth knowing if you ever read that code: `post_llm_response`
fires for chat as well, so anything counted there without checking
`context.agent is None` would book every chat call at least twice — it is a
transport-level hook, so a retried or failed-over call fires it more than once
per agent step.

**TTS spend is still outside the table**, and for a reason rather than an
oversight: `notify_tts_response` reports audio seconds and bytes, not tokens,
so there is nothing to normalise into a row. A row that pretended otherwise
would be worse than the gap. Note which guard actually keeps it out, because
it is not the obvious one: TTS sets `agent=None` too, so it passes the agent
check and is stopped by the empty-usage check one line further down. Tighten
that check and audio starts being booked as tokens.

### What it costs and how long it takes

Measured with this hook's own payload — one question, the criteria above,
`~typesafe/jev-latest`, 2026-09-20:

| judged response | input tokens | cost | round trip |
|---|---|---|---|
| one line | 349 | 1.5e-05 $ | 0.56 s |
| 2900 characters | 1070 | 4.5e-05 $ | 0.33 s |

The question and its criteria are ~350 tokens before the answer is even added,
so a short response is mostly question — and a long one is mostly response,
three times the price. That is what the 3000-character cut in the code is for.

The round trip is well inside the hook's 30 s ceiling (`schema.yaml`), but the
client's own retry budget is not: `request_timeout: 60` with `max_retries: 2`
can reach ~186 s, and the registry cancels the hook at 30. So against a
sick endpoint the first attempt is what counts, the retries configured on the
decision model never run from here, and the result is FINAL — the safe
direction, but do not expect the retries to fire.

`hybrid` with `hybrid_fallback: decision` is the cheap arrangement — free rules
first, the paid judge only when they do not match:

```yaml
      strategy: hybrid
      hybrid_fallback: decision
      rules:
        - type: keyword_continue
          keywords: ["I will now", "next I"]
```

## The rule engine

Keyword rules are collected across **all** rule blocks before a decision is
made, and `keyword_continue` beats `keyword_final` when both match the same
text. That is deliberate: rule order must not decide the outcome, and stopping
too early is worse than one extra turn.

Structural rules short-circuit as soon as they fire, because they are
unambiguous:

| Rule | Fires when |
|---|---|
| `keyword_continue` / `keyword_final` | substring match, or regex with `regex: true` |
| `min_length` | response shorter than `min_chars` |
| `step_check` | `context.step` below `min_steps` |

An invalid regex is logged and skipped, not raised.

When nothing matched, `default` decides — `"final"` unless the agent sets
`"continue"`. The "nothing matched" signal is also what `hybrid` uses to hand
over to the LLM, so a `default: continue` in hybrid mode still reaches the LLM.

## Configuration

Two places, and the agent-level one wins:

1. the agent's own `hooks.overrides."agent_continuation.evaluate_completion"` —
   preferred, because the rules stay with the agent they describe;
2. `agent_rules[<agent_name>]` in `config/plugins.yaml`;
3. otherwise the global defaults.

```yaml
# in an agent's config/agents/*.yaml
hooks:
  overrides:
    agent_continuation.evaluate_completion:
      enabled: true
      strategy: rules
      default: final
      continue_message: "Keep going — next step."
      rules:
        - type: keyword_continue
          keywords: ["I will now", "next I"]
```

The hook itself does no agent gating: the registry only fires it for agents
that opted in, so an `if agent_name in ...` check here would be a second,
divergent allowlist.

## Scripted follow-ups

Agents often deliver a sloppy first result and find their own mistakes when
asked to look again. `followups` automates that ask: a list of fixed user
messages, sent one per final answer, in order.

```yaml
agent_continuation.evaluate_completion:
  enabled: true
  followups:
    - "Review your result once more against the source. Fix what is wrong."
    - "Now return the complete final result in the required format."
```

```
user → tools → final → follow-up 1 → tools → final → follow-up 2 → … → final
```

- **The last final answer is the result.** The caller gets only that one, so
  for an agent with an output format the last follow-up has to ask for the
  **complete** result again — a "fixed two things" answer replaces the report.
- Follow-ups come **after** the strategy: an answer the rules read as a status
  report gets `continue_message` first; only a final answer gets the next
  follow-up.
- **No state.** Sent follow-ups are the user messages marked
  `injected_by: agent_continuation.followup` since the last message a person
  wrote. Every new request — also a pipeline continuing a sub-agent — starts
  the list again.
- `followups_on_continue: false` sends the list only on the **first** request
  of a session. A request that continues a session (an earlier assistant
  answer precedes it) gets none — a pipeline can then continue the instance
  for a narrow question ("which beat does this belong to?") without the
  follow-up replacing that answer. A new session still gets the list. Only a
  real boolean counts (a quoted `"false"` logs a warning and leaves the
  follow-ups on), and the evidence is the history the hook sees: once a
  summarizer or pruning has replaced the earlier answers, a continued request
  looks like a first one.
- Follow-ups count against `max_continuations`, and none is sent once it is
  reached: the ceiling on a history that lost its markers.
- Each round costs steps; `max_steps` must leave room for them.

## Budget

`max_continuations` (default 10) is counted per `request_id` and the counter is
dropped as soon as a final answer arrives. It is a safety valve against a loop
that keeps talking itself into another round.

It follows the same resolution as every other key: the agent's own
`max_continuations` wins, the value from `plugins.yaml` is the fallback. A
value that is not a positive number falls back too and says so in the log — a
budget of 0 through a typo would silently disable the hook, which is never what
someone writing this key means.

## Tests

`tests/test_plugin_agent_continuation.py`.

## License

Apache-2.0 — see `LICENSE`.
