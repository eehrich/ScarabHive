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
| `okf_validate` | Conformance gate (every concept has a non-empty `type`); broken links → warnings |
| `okf_read_concept` | Frontmatter (all keys preserved) + body |
| `okf_write_concept` | Create/overwrite; requires `type`; preserves existing extra keys; atomic |
| `okf_list` | Concepts with type/title/description (progressive disclosure) |
| `okf_neighbors` | A concept's graph neighbors + broken links |
| `okf_subgraph` | BFS concepts reachable from seed(s) within N hops |
| `okf_search` | Lexical relevance ranking over a bundle |
| `okf_append_log` | `log.md` entry (ISO date, newest first) |
| `okf_reindex` | Regenerate `index.md` from concept descriptions |

Paths are **bundle-relative with a leading slash** (`/tables/orders.md`).

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
