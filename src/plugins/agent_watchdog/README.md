# agent_watchdog

Watches a running agent. Every *n* steps a separate, configurable model reads a
bounded excerpt of the run and answers one question: **was a decision made or
an open question closed?** The verdict — `continue`, `steer` or `abort` — is
written to a log and shown as a status line.

**Stage 1 of `docs/agent_watchdog_konzept.md`: it never intervenes.** No
message is injected, nothing is aborted. This stage exists to find out whether
the verdicts are good enough to act on.

## How it works

- Hook `observe_step` on `post_llm_call`. At a due step it snapshots the
  excerpt and returns at once; the judge runs in a background task. The agent
  loop awaits every post-LLM hook, so a judge inside the hook would add its
  whole latency to the watched step.
- Due steps: `first_check_step`, then every `every_n_steps`. Not time — the same
  agent with the same thinking volume takes 3.5 or 12.5 minutes depending on
  the provider.
- At most one check per run at a time; a due step while the previous check is
  still thinking is skipped.
- The excerpt (`window.py`): the user messages (newest first into the
  `task_chars` budget, shown oldest first), the end of the system prompt, the
  most recent thinking, and the recent tool calls as **status only** — name,
  argument fingerprint, `ok`/`empty`/`error`/`pending`, result size. Never the
  arguments: for a writing tool they are the whole file.
  - Not just the first user message: measured live, a follow-up request then
    showed the judge the previous assignment, and it called a healthy run
    "drifted".
  - `empty` is structural: a JSON result whose top-level lists are all empty
    (a search answering `"status": "success"` with no hits) — measured live,
    such searches used to read as `ok`.
- **Inside a call** (`observe_reasoning`, hook type `llm_progress`): a single
  call that thinks for a long time has no step boundary to check at. Every
  `every_n_reasoning_chars` characters of that call's thinking, the same
  judge gets the same excerpt, with the running call's thinking as
  `recent_thinking` and `call_in_progress: true`. The stream context carries
  no messages, so `remember_task` (`pre_llm_call`) builds the excerpt of the
  call about to stream and keeps only that — both hooks have to be on. Characters, not tokens:
  token counts arrive only with the end of the call. Blind on clients that
  stream no thinking deltas (Gemini, batch). Measured live with
  `or-deepseek-flash`: checks at 4 024 and 6 024 characters, 2.5–3 s each.
- Every log line carries `trigger`: `step` or `reasoning` (then with
  `reasoning_chars`).
- On shutdown (`stop_plugin`) running checks are awaited up to
  `judge_timeout_seconds`, and any still running is logged as
  `cancelled_at_shutdown`. Measured live: the CLI exited 0.35 s after starting
  its last check, which then left no line at all.
- Fail-open: unparseable answer, unknown verdict, missing or non-verbatim
  `evidence`, `steer`/`abort` without `message`, timeout, provider error, no
  judge client — all read as `continue`, and the log names which one.

## Configuration

`config/plugins.yaml`:

```yaml
agent_watchdog:
  type: agent_watchdog
  enabled: true
  config:
    llm_profile: "or-deepseek-flash"
```

Measured with that profile: 0.00012 $ and 2–12 s per check.

Per agent, in its `hooks.overrides` (the hook is off unless an agent turns it on):

```yaml
hooks:
  overrides:
    agent_watchdog.observe_step:
      enabled: true
      first_check_step: 10
      every_n_steps: 10
      judge_prompt: "config/agents/prompts/my_agent_watchdog.md"
    # checks inside one long call — both hooks
    agent_watchdog.remember_task:
      enabled: true
    agent_watchdog.observe_reasoning:
      enabled: true
      every_n_reasoning_chars: 20000
```

Settings are read per hook, each from its own override. For checks inside a
call, `remember_task` builds the excerpt before the call (`task_chars`,
`spec_chars`, `max_tool_calls`); `observe_reasoning` takes the rest
(`every_n_reasoning_chars`, `reasoning_chars`, `judge_prompt`,
`judge_timeout_seconds`).

Keys (defaults in `schema.yaml`): `llm_profile`, `judge_prompt`,
`first_check_step`, `every_n_steps`, `every_n_reasoning_chars`, `task_chars`, `spec_chars`,
`reasoning_chars`, `max_tool_calls`, `judge_timeout_seconds`, `log_path`. Per
agent: all except `llm_profile` and `log_path`. A value that is not a positive
integer falls back to the plugin value with a warning.

`judge_prompt` is a path relative to the project root; empty means the built-in
`prompts/judge.md`. A custom prompt must keep the output contract (the JSON
fields and verbatim `evidence`), otherwise every check is logged as fail-open.
An unreadable file falls back to the built-in prompt; the log line then carries
`judge_prompt` and `judge_prompt_error`.

## The log

`data/agent_watchdog/verdicts.jsonl`, one line per check (timestamps UTC):
`request_id`, `session_id`, `agent`, `observed_model`, `step`, `trigger`,
`reasoning_chars` (reasoning checks), `judge_prompt` (when configured),
`task_preview` (first 200 characters of the newest user message), `verdict`,
`reason`, `evidence`, `message`, `judge_model`, `usage`, `finish_reason`,
`latency_ms`, and — when the answer was not used as given — `fail_open` plus
the first 2000 characters of `raw`.

The judge's calls bypass the hook pipeline, so they appear in no
`llm_requests` row and do not shift the watched agent's numbers. `usage` in
this log is the only record of what the judge costs.

## Model Experience

### What the model sees

The **watched** agent sees nothing from this plugin.

The **judge** receives `prompts/judge.md` as system prompt and the excerpt as a
JSON user message with the keys `user_messages`, `expected_result`, `recent_thinking`,
`recent_tool_calls`, and `call_in_progress` for checks inside a call. It has no
tools.

### Token and cache effect

None for the watched agent: its context is not touched. The judge costs one
call per check, bounded by the excerpt limits (by default about 15 KB of text
plus the prompt), independent of how long the watched run is.

### Known gaps

- **No intervention.** Stage 2 (inject `message`) and stage 3 (abort and
  re-instruct) are separate steps in the concept, each with its own guards.
- **`expected_result` is the tail of the system prompt**, not a located
  "deliverable" section. Output contracts usually close a prompt; whether that
  holds is what the log will show.
- **Refusals are not counted separately.** A provider refusal lands as
  `fail_open: judge_error` with the error text; a per-agent refusal rate has to
  be computed from the log.
- **A prompt file is read once per process** (the built-in one at start, a
  configured one at its first check). Editing it needs a restart —
  deliberately, so a save mid-run cannot change the verdicts of a run already
  being watched.
- **Not combined with `agent_continuation` yet.** Both on the same agent is
  harmless while this plugin only logs; it becomes a decision at stage 2.
