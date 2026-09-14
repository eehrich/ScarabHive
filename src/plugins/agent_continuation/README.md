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

## Three strategies

* **`rules`** — deterministic, no LLM cost. The default.
* **`llm`** — a lightweight profile classifies the answer as `FINAL` or
  `CONTINUE`.
* **`hybrid`** — rules first; the LLM decides only when no rule matched.

Every LLM failure path resolves to FINAL: no evaluator LLM, no `llm_prompt`, an
exception during the call. Stopping is the safe error, continuing costs tokens
in a loop.

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
