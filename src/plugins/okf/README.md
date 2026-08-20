# OKF plugin — Open Knowledge Format for ScarabHive

Read, write, validate, graph-traverse and search **OKF v0.1** knowledge bundles,
plus opt-in context injection ("wiki as context"). OKF is Google's vendor-neutral
"format, not platform": a bundle is a directory tree of markdown files; a concept
is one `.md` with YAML frontmatter whose only hard rule is a non-empty `type`.

Spec: <https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf> ·
Design: `docs/okf_support_design.md`.

## Why

Curated, git-versioned, cross-agent knowledge that grows over time — distinct
from `memory` (ephemeral per-session) and `json_store` (transient job state).
Because it is pure markdown, bundles interoperate with Google's OKF tooling and
any other consumer (verified: this plugin validates & traverses Google's real
`crypto_bitcoin` sample bundle unchanged).

## Layers

- `core.py` — the format, owned once: round-trip-safe frontmatter (via
  `ruamel.yaml`, YAML 1.2, preserves unknown keys/order/comments), conformance
  validation, bundle-relative link resolution, graph traversal, index/log.
- `server.py` — sandboxed MCP tools + the `pre_llm_call` consumer hook.

## Tools

| Tool | Purpose |
|---|---|
| `okf_validate` | Conformance gate (every concept has a non-empty `type`); broken links and a `status` outside the spec's lifecycle vocabulary → warnings |
| `okf_read_concept` | Frontmatter (all keys preserved) + body |
| `okf_write_concept` | Create/overwrite; requires `type`; preserves existing extra keys; atomic |
| `okf_list` | Concepts with type/title/description + `lifecycle` (spec §5.4: draft/stable/deprecated) |
| `okf_neighbors` | A concept's graph neighbors + broken links |
| `okf_subgraph` | BFS concepts reachable from seed(s) within N hops |
| `okf_search` | Lexical relevance ranking over a bundle (+ `lifecycle`) |
| `okf_append_log` | `log.md` entry (ISO date heading, time-stamped, newest first) |
| `okf_reindex` | Regenerate `index.md` from concept descriptions — whole bundle recursively (per-directory indexes, cross-linked); with `dir` one level of that directory |

Paths are **bundle-relative with a leading slash** (`/tables/orders.md`).

A path that does not resolve comes back with `available` (the concepts sitting in
the same directory) and, when it was close enough to be a typo, `did_you_mean` —
so a slip costs one turn instead of a guessing game.

No suggestion is made when the only difference is a **number**. `kapitel_15.md`
missing while `kapitel_16.md` exists is not a typo but a gap, and naming the
neighbour invites the agent to write the right content into the wrong document —
silently, and past every conformance check. There the listing is the answer.

## Log entries

`log.md` keeps the spec's date grouping and adds a time per entry — a single run
can write a dozen entries under one heading, and the date alone neither orders
them nor spaces them:

```markdown
## 2026-08-04
* 14:03:07 **MAP-Extraktion**: Kapitel 3 …
* 09:12:44 **Creation**: Zustandsgraph SOLL-Schicht …
```

The time stays **out of the heading** on purpose: other OKF tooling groups by
the date heading, and one heading per entry would break that for no gain.

The server stamps the current time itself — an LLM cannot read a clock, and the
date already has to be supplied. Two details follow from that:

* the time uses the agent's **configured timezone**, not UTC, because the date
  the caller passes comes from `{{ current_date }}`, which uses that same zone.
  A UTC time beside a local date disagrees by hours, and around midnight by a day;
* an entry dated in the **past** gets no time at all. Stamping the current clock
  onto a backfilled entry would not be a missing detail but a wrong one. Pass
  `time` explicitly to set one.

Entries written before this existed keep the plain `* **Action**: …` form.

## Concurrent writers

The three write tools are read-modify-write: `okf_append_log` reads the whole
`log.md` and writes it back, `okf_write_concept` merges into the existing
frontmatter, `okf_reindex` builds the index from a bundle scan. An interleaving
loses data *silently* — both callers get `ok`.

This is not hypothetical. An agent that spawns itself as a sub-agent writes the
same bundle, parallel tool calls of a single turn run as concurrent asyncio
tasks, and `agent-api`, the writer worker and a developer's CLI all share
`data/okf`. Each write therefore takes two locks:

* an **in-process lock** per bundle root, module-level so it holds even when
  parent and sub-agent hold different `OkfServer` instances. It is a
  `threading.Lock`, not an `asyncio.Lock`: the latter binds to the event loop of
  its first use and raises on the next one, and a process may run several loops
  over its lifetime while the bundle path stays the same;
* a **file lock** (`.okf.lock` in the bundle root) for other processes.

The critical section runs off the event loop (`asyncio.to_thread`), so waiting
for either guard never stalls other turns.

The guard file lives *in* the bundle on purpose: services run under different
accounts with different temp directories, so a lock outside the bundle would
silently not be the same lock. Windows removes it on release, POSIX leaves it —
add `.okf.lock` to `.gitignore` if you version your bundles. It is never read as
a concept.

If a lock cannot be taken within 30 s the tool returns an error rather than
hanging the turn.

## Config (`config/plugins.yaml`)

```yaml
okf:
  type: okf
  enabled: true
  allowed_directories:      # sandbox — tools only touch bundles under these
    - data/okf
  hook_max_concepts: 6      # consumer-hook cap
  hook_graph_depth: 1
  # hook_bundle: data/okf/<bundle>   # set to enable context injection
```

## Consumer hook (opt-in context injection)

Disabled by default. To fold a bundle into an agent's context before each LLM
call, set `hook_bundle` and enable the hook for that agent via `hooks.overrides`
in its agent config:

```yaml
hooks:
  overrides:
    okf.okf_context_injection:
      enabled: true
```

Retrieval is **dual**: the latest user message is ranked lexically against the
bundle, then the top concepts are **graph-expanded** (their linked concepts are
pulled in) so the LLM sees relationships a flat search misses.

## Retrieval note

`okf_search` and the hook rank lexically today (TF-IDF over frontmatter + body).
Embedding/semantic retrieval is a clean seam (`OkfServer._rank_lexical` is the
single ranking entry point) — deliberately not a second, conflicting ChromaDB
consumer. Graph retrieval needs no embeddings and is fully functional.
