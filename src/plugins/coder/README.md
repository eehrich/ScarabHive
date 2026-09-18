# coder plugin — a coding agent with a harness

A coding agent is only as good as what surrounds it: what it knows before it
starts, what it delegates, and what has to be true before it may call the work
done. That surround is the harness, and this plugin is it.

**This plugin ships no tools.** It has no `plugin.py` and no `server.py` — it
is configuration: four agents, their prompts, and two skill bundles. Plugin
discovery skips a directory without an entrypoint file, so nothing has to be
registered for that to work (same shape as `writer_publish`).

## What you get

| Agent | Role | Writes? |
|---|---|---|
| `coder` | Owns the change: plans, edits, runs tests, gets reviewed | **yes** — the only one |
| `coder_explorer` | Read-only finder: "where is X, who calls Y" | no (enforced) |
| `coder_reviewer` | Attacks the finished change | no (enforced) |
| `coder_tester` | Runs suites, reports failures small, mutation probe | no edit tools |

Plus the instances they run on, in [agents/tools.yaml](agents/tools.yaml):
`coder_fs` (sandboxed read/write), `coder_fs_ro` (the same tree, read-only),
`coder_shell`, and `coder_sam` (the sub-agent manager).

Try it:

```bash
.venv/Scripts/agent-cli.exe run --agent coder "..."
.venv/Scripts/agent-cli.exe chat --agent coder
```

## The one knob you will want

`allowed_directories` on **both** `coder_fs` and `coder_fs_ro` in
`agents/tools.yaml`. It decides which tree the harness may touch at all;
the default is `data/workspace/` plus the plugin's own `skills/`.

Keep the two lists identical. A reviewer that cannot read what the coder just
wrote reviews nothing, and it will not say so — it will review what it can
reach.

## Why the roles are separate agents

Not for tidiness. Each one buys something a prompt section cannot:

**Context economy.** Answering "who calls `build_client`?" means sweeping
thirty files. The coder needs the three-line answer, not the thirty files, and
it still needs its window for the actual change. The explorer and the tester
exist to spend *their* context so the coder keeps its own.

**A fresh pair of eyes.** The coder knows what it meant, and that knowledge is
exactly what hides the bug. The reviewer starts with no conversation history
and sees only the code.

**A guarantee instead of a request.** "Whoever reviews, reviews read-only" is
a rule that a prompt can only ask for. Here `coder_fs_ro` sets
`read_only: true`, and `file_ops` then does not even render its write tools
into the schema — plus the server rejects a write a second time if one is
called anyway. The reviewer cannot quietly turn into a second author.

Nesting is capped at 2 and none of the three roles owns a sub-agent manager,
so a review cannot start a review.

## Knowledge lives in skills, not in the prompts

Two new bundles under [skills/](skills/), reachable because
`config/config.yaml` lists `src/plugins*/*/skills` as a skill root:

- **`coding-harness`** — the seven-step loop, when to delegate, what must be
  true before "done", and what to carry into the next session. Always in the
  coder's prompt.
- **`adversarial-review`** — how to review so the findings are real. Always in
  the reviewer's prompt, and the coder pulls it on demand before reviewing its
  own fix round.

Three bundles that already existed under `skills/coding/` are referenced
rather than restated: `tdd`, `diagnosing-bugs`, `codebase-design`. The
existing `code-review` skill is deliberately **not** wired in — its process
assumes another tool's sub-agent mechanics, so it would send the agent looking
for tools it does not have.

### Prompts carry no *per-call* variables

Deliberate, and worth keeping that way. The system prompt is the cached
prefix; a `{{ current_step }}` in it changes on every single call and
invalidates the cache for everything after it. Whatever varies per turn
belongs in an injection hook, not in the template. Skill bodies follow the
same rule.

Static template variables are fine and there is one: `{{ okf_bundle }}`, so
the bundle path is written once and cannot drift from what the injection hook
reads. It is the same string on every call, so it costs the cache nothing.

### The completion gate sits at the tail, on purpose

First live run: the agent built the thing, ran a syntax check *and* a real
three-case test of the only non-trivial function — then reported done without
a `coder_reviewer` round, which its prompt lists as a hard condition.

Measured cause, not guessed: the gate sat at **27%** of a 15k-character system
prompt with 11k characters after it, and the last thing the model read was a
skill index. Nothing enforced it; recency worked against it.

Two changes, both structural rather than louder wording:

- The report contract moved to the **end of the `coding-harness` skill** — now
  at 93%, the last body text before the on-demand index.
- Skipping the review is **allowed and must be stated**. Every report closes
  with `Ran:` / `Check:` / `Reviewed:`, and `Reviewed: skipped — <reason>` is
  a legitimate answer. An absolute rule that is obviously silly for a 60-line
  toy gets broken, and a broken rule teaches that gates are negotiable; a rule
  that permits the skip but forbids the silence can be followed every time.

If you want it absolute instead, delete the `— or ... skipped` clause in both
the prompt and the skill. The visibility contract stays either way.

### Injection hooks that churn the prefix are off

The same rule, one level up. `todo.inject_todo_tasks`,
`sequential_thinking.inject_active_sessions` and
`coder_sam.inject_sub_agent_context` each insert a system message directly
behind the first one — inside the cached prefix — and each carries content
that changes **mid-turn**: a task is updated, a thought is added, a sub-agent
finishes. So the prefix dies exactly while the agent is working hardest, and
every later call in that turn pays full price.

Nothing is lost by turning them off. All three inject the agent's *own tool
calls*, which are in the transcript already, and each tool is in its allowlist
to re-read on demand (`todo` with `operation: list`, `coder_sam_manage_sub_agent`
with `list`). The three sub-agent names are in the prompt anyway, so the
injected "available agents" line duplicated something the cached prefix
already carried for free.

`coder_okf.okf_context_injection` stays **on** despite injecting at the same
place, and the difference is measurable: it keys on the last *user* message,
so its block is byte-identical across every LLM call within a turn and only
moves when the user speaks — which changes the prefix anyway. The one
exception is the agent writing a concept mid-turn, a deliberate and rare act.

A test holds this line, because flipping one back on has no visible symptom —
just a quietly larger bill.

## Persistent knowledge — an OKF bundle

The coder keeps what it learns in `data/okf/coder`: an OKF bundle, which is a
directory of markdown concepts with YAML frontmatter and links between them.
Plain files, git-versionable, readable without any tooling.

`coder_okf.okf_context_injection` folds the part of it that the *current turn
is about* into the prompt — the lexical hit plus its linked neighbours, capped
at ten concepts (`hook_max_concepts`).

That cap is a token ceiling, and it is worth knowing how it is spent: the hook
injects each concept's **full body**, hard-truncated at 1500 characters, and
that 1500 is hardcoded in the plugin. Ten concepts is therefore at most ~15000
characters, roughly 3.7k tokens — affordable against a 200k window.

The number that actually bites is the per-concept one. Measured across the
bundles already in `data/okf/`, **55% of concepts are long enough to be cut**
mid-sentence, and nothing warns the reader that it happened. Raising the count
does not help a concept that is being halved; writing shorter ones does. The
`coding-harness` skill therefore tells the agent to keep a concept under ~1500
characters and split rather than sprawl. That is the reason for a graph rather than a notes file: the
bundle can grow well past what would fit in a prompt, and the agent still
starts each turn with the relevant slice of it. Everything else it fetches
itself with `coder_okf_search` / `_read_concept` / `_neighbors`, and writes
with `_write_concept` / `_append_log`.

**It gets its own OKF instance, not the shared `okf` server.** The shared one
is sandboxed to all of `data/okf`, so any agent using it can write into every
other agent's bundle — including the sysadmin agent's `infra` runbooks, which
are injected into *that* agent's prompt. `coder_okf` is rooted at the one
bundle instead, so the boundary is enforced rather than requested. Measured:
a write to `data/okf/infra` comes back `bundle 'data/okf/infra' is outside the
allowed OKF directories`.

Two silent failure modes are covered by tests, because neither announces
itself: a `hook_bundle` outside the instance's sandbox makes the hook return
without injecting anything (indistinguishable from "nothing learned yet"), and
a sandbox as wide as the shared one re-opens the cross-bundle write.

The bundle directory does not need to exist — it is created on the first
write, and until then the hook simply does nothing. `data/okf/` is gitignored,
so this knowledge is local state.

What belongs in it, and what does not, is spelled out in the `coding-harness`
skill: measured facts, traps, conventions and decisions **with their reasons**
— never a second copy of what the code already says, because the copy is the
one that goes stale and it will be believed.

The agent may also write to `src/plugins/coder/skills/` — it can correct the
discipline it works by. Everything else about this plugin stays out of reach:
an agent that can rewrite its own tool allowlist does not have one.

Not wired up, deliberately: read-only bundle access for the reviewer. It would
let a review check a change against a recorded convention, and `okf` supports
`read_only: true` for exactly that. It needs one more instance, and it is only
worth it once the bundle actually holds conventions — add it then.

## Semantic search, and the one index behind it

`semantic_search` is on for all four agents since 18.09.2026. It was blocked
before, for a good reason that no longer holds: with embeddings off every call
answered `SemanticSearchDisabled` and cost the agent a turn.

What changed is what the index stores. One document per **symbol** — a
function, a class, a heading section — so a hit is `file:line` with a
signature, not a file you then have to read. Measured on the coder tree:
3.295 files, 51.730 documents, a first build of seven minutes in the
background, and incremental passes in 0.4 s afterwards. The
build state is written next to the vectors, so a restart does not pay for it
again.

**One index, two sandboxes.** `coder_fs` and `coder_fs_ro` see the same tree,
so indexing it twice would be the same seven minutes twice — and because a
full rebuild clears its collection first, the two would also take turns
emptying each other's index. Both therefore name the same
`collection_name: file_ops_coder_tree`, and only `coder_fs_ro` has
`enable_indexing: true`. The read-only twin builds, the read-write one reads.

`grep_search` stays the first choice whenever the agent knows the word the code
uses. This tool is for when it does not.

## Where the shell is honest about its limits

`coder_shell` is **not** confined by the kernel. `terminal`'s `sandbox.mode`
needs bubblewrap, and on Windows a confining mode makes every command fail
with `SANDBOX_UNAVAILABLE` — so it is left at the default. The blacklist there
(`rm -rf /`, `git push`, `git reset --hard`, …) is a speed bump against typos
and nothing more: a pattern list cannot bound what `bash -c` can do.

The enforced boundary in this harness is `coder_fs`, not the shell. Read that
as: do not hand `coder_shell` to an agent you would not trust with the whole
machine.

It starts in `data/workspace` (`platform.initial_cwd`) so tests run where the
code lives. One consequence is worth knowing: with the default sandbox,
`data/workspace/` is *inside* this repository and *ignored* by it, so `git`
from there resolves to the AgentSystem repo — it would show the agent other
people's uncommitted changes and none of its own. The prompt therefore tells
the agent to check `git rev-parse --show-toplevel` before trusting git at all.
Make the workspace project its own repository and git works normally; the
blacklist blocks `push`, `reset --hard`, `checkout --` and `clean` either way.

## What this borrows, and from where

Surveyed while building it — Claude Code, OpenAI Codex CLI, Hermes Agent
(Nous Research), plus Gemini CLI, OpenHands, Amp, Aider and Factory Droid:

| Taken | From |
|---|---|
| Read-only explorer sub-agent with its own context | Claude Code `Explore`, Amp sub-agents, Hermes `delegate_tool` |
| Skills with progressive disclosure (index → body → reference file) | Agent Skills standard; our `skills` plugin already matches it |
| "At most one task in progress at a time" | Codex `update_plan` |
| Resolve ambiguity by reading the code before asking | Codex Plan Mode, phase 1 |
| Copy files before an irreversible sweep | Gemini CLI checkpointing, in the cheap form |
| Durable facts always in context, procedures loaded on demand | Hermes' split between `MEMORY.md` and skills |
| A byte-stable system prompt for cache reasons | Hermes freezes its memory snapshot at session start for the same reason |

Two places where this harness deliberately goes further than what those
systems ship:

- **The reviewer must try to refute its own finding** before reporting it.
  None of the surveyed harnesses require this; roughly a third of findings do
  not survive the attempt, and "fixing" those damages working code.
- **The review round is a precondition for "done"**, not a command the user
  has to remember. Every surveyed system runs its reviewer only when asked.

## Not built, and why

- **A hard plan gate.** Codex blocks edits until a plan is approved. The
  `task_switch` plugin could gate a phase transition the same way here. The
  task list plus "understand first" covers most of it; add the gate if plans
  turn out to get skipped in practice.
- **A repo map** (Aider builds one with tree-sitter and PageRank). The
  explorer answers the same questions on demand. This would be a plugin, not
  a config change.
- **Automatic lesson extraction.** See above — deliberate beats automatic.

## Adding another agent here

One file per agent under `agents/`, its prompt under `agents/prompts/`, and
`system_template: "./prompts/<name>.md"` — that path resolves next to the YAML,
so nothing is hard-coded to a repo layout. A new sub-agent must be listed in
`coder_sam.allowed_agents` or it cannot be spawned;
`tests/config/test_config_no_silent_drift.py::test_every_allowed_sub_agent_exists`
fails on a name that does not resolve, so the typo surfaces at test time
instead of at runtime.

## Tests

```bash
.venv/Scripts/python.exe -m pytest src/plugins/coder/tests -q
```

[tests/test_coder_harness.py](tests/test_coder_harness.py) guards the
properties that are specific to this plugin and would otherwise break in
silence — the read-only instance offering no write tools, both sandboxes
covering the same tree, every tool named in a prompt existing and being
visible, `semantic_search` reachable and backed by an index, and the
prompts staying free of per-call template variables.

Each of those was checked by mutation — break the config, watch the right test
go red, restore:

| Mutation | Test that caught it |
|---|---|
| `read_only: false` on `coder_fs_ro` | write tools reappear in the schema |
| dropped one directory from the read-only sandbox | sandboxes-cover-the-same-tree |
| `enable_indexing` on both twins | the-two-sandboxes-share-one-index-and-one-builder |
| `{{ current_step }}` added to a prompt | no-per-call-template-variables |
| prompt naming `coder_fs_ro_manage` | prompt-names-unusable-tools |
| `coder_fs/*` given to the explorer | read-only-agents-have-no-writable-tools |
| `coder_okf` sandboxed to all of `data/okf` | bundle-sandbox-is-not-the-shared-one |
| `hook_bundle` pointed at another agent's bundle | injected-bundle-resolves-inside-that-sandbox |
| `okf_bundle` removed from `template_vars` | prompt-tells-the-agent-which-bundle |
| prompt naming `coder_okf_recall` | prompt-names-unusable-tools |
| a churning injection hook switched back on | no-hook-rewrites-the-cached-prefix |

The general config guards cover the rest and are not restated here — a bogus
entry in `coder_sam.allowed_agents` fails
`tests/config/test_config_no_silent_drift.py::test_every_allowed_sub_agent_exists`,
and a bogus `llm_profile` fails `test_every_profile_reference_resolves`. Both
were confirmed by mutation too.

## License

Apache-2.0 — see `LICENSE`.
