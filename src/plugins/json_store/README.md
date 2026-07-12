# JSON Store Plugin

Validated JSON working documents for agents. Agents that assemble large JSON
across multiple steps corrupt it when they re-type it through their own token
stream (shortened values, `\n` turned into raw newlines, lost commas). This
plugin moves those operations into code:

- Documents live server-side as **Python data structures** — never as text.
- Every input is **parsed = validated**; ` ```json ` fences are stripped and
  broken JSON is auto-repaired via `json_utils.repair_json` (raw newlines in
  strings, missing commas, truncation, ...). Unrecoverable input is rejected
  with a precise error; applied repairs are reported in the result.
- **merge** deep-merges in code — the agent passes only the delta.
- **read** returns guaranteed-valid canonical JSON.

## Tool

One tool, `<instance>_manage_json`, dispatched via `operation`
(keeps the tool list small, same pattern as `sub_agent_manager`):

| operation     | params                          | effect |
|---------------|---------------------------------|--------|
| `write`       | `doc?`, `data`\|`json_text`, `if_exists?`, `write_access?` | create a doc — **omit `doc`** to get a fresh collision-free id back (best for parallel writers); `if_exists` (`error` default = catch collisions, `replace` = overwrite on purpose) applies only to a named doc; `write_access` (`owner` default, see below) |
| `read`        | `doc`, `path?`                  | canonical JSON (whole or sub-path) |
| `merge`       | `doc`, `data`\|`json_text`, `array_mode?` | deep-merge (dicts recurse, scalars overwrite, arrays replace/concat) |
| `merge_doc`   | `doc` (target), `source`, `array_mode?` | deep-merge one stored doc into another, in code (no JSON re-typing) |
| `set_value`   | `doc`, `path`, `value`          | set one value, creates intermediate objects |
| `delete_keys` | `doc`, `paths[]`                | delete paths |
| `delete_doc`  | `doc`                           | drop document |
| `undo`        | `doc`                           | revert your LAST change (repeatable; recover from a mistake without re-typing the old JSON) |
| `list`        | –                               | existing docs |
| `outline`     | `doc`, `depth?`                 | structure without values (cheap inspection) |

Paths use dot notation with `[i]` for arrays: `key_characters.Nora.age`,
`milestones[2].label`.

## Undo

Every mutation snapshots the document first, so an agent that made a mistake
(merged the wrong delta, deleted too much, overwrote a field) can revert it
with `undo` instead of reconstructing the previous JSON by hand — which would
mean re-typing JSON through the LLM, the exact corruption this plugin exists to
avoid.

- The interface is **single-step and parameter-free**: `undo` reverts the LAST
  mutation on `doc`. An LLM never has to count how many operations to reverse.
- **Repeatable**: call it again to step further back, up to `undo_depth`
  (default 5) snapshots.
- Undoing the operation that *created* a document deletes it again; `undo` after
  a `delete_doc` recreates the document.
- Owner-guarded like any mutation (only the owner may undo; a deleted doc can
  only be undone by its original owner).
- The result reports what was undone and how many undos remain
  (`{"undone": "merge_doc", "snapshots_left": 1}`).
- History is per-namespace and in-memory. It is bounded three ways: `undo_depth`
  snapshots per doc, at most `2 × max_docs` tracked docs (deleted docs keep their
  history so a deletion can be undone, but oldest/dead ones are pruned first),
  and full eviction with the namespace TTL. No-op mutations don't consume depth.
  `undo_depth: 0` disables it. Not a redo/version-control system — a short
  safety net for immediate mistakes.

## Write protection (owner)

Agents share a namespace to collaborate, so a confused reader can silently
clobber someone else's document. Observed live: three panel writers merged into
and deleted keys from the coordinator's `synopsis`, shrinking it each round.

Therefore **a document belongs to the session that created it**:

- Everyone sharing the namespace may **read** every document.
- Only the owner may `write` / `merge` / `merge_doc` (target) / `set_value` /
  `delete_keys` / `delete_doc` it. `merge_doc`'s **source** is read-only, so an
  owner can merge someone else's document into their own.
- Pass `write_access: "shared"` when creating a document to opt out, or set
  `default_write_access: shared` in the config for the old behaviour.
- `list` reports `writable` per document.

Ownership is bound to the runtime-injected `_session_id`: not a token the model
has to carry (nothing to leak into a forum post or to forget), and
model-supplied `_`-prefixed arguments are stripped before dispatch, so an LLM
cannot impersonate another agent.

The denial message tells the agent what to do instead — write your own document
(omit `doc` for a fresh id) and let the owner merge it.

### Limits (by design)

- **A namespace is a shared capability, not a boundary.** Anyone you hand a
  `namespace` to can read every document in it and can create (squat) any
  not-yet-existing name. Ownership only stops *modifying an existing* document.
  So share a namespace only with cooperating agents of one workflow — which is
  exactly how it is used (a run's GROUP_ID).
- **No cross-session takeover.** Ownership is bound to the session id, which
  does not survive a re-run. If a *new* session reuses a live namespace (e.g. a
  coordinator restarted with the SAME group id while the old docs are still
  within `namespace_ttl_hours`), it cannot reset/delete the previous run's
  owned documents — a full `write(if_exists="replace")` is refused too. The
  default flow avoids this (each run creates a fresh group id → fresh
  namespace); an orchestrator that deliberately reuses a namespace across runs
  should use `default_write_access: shared` for it (with persistence, the
  memory TTL no longer frees a namespace — its files outlive it until
  `file_retention_hours`).
  Role-based takeover is deliberately NOT offered: a panel runs several
  sub-agents of the same role in one namespace, so it would reopen exactly the
  cross-writer clobbering this protects against.

## Configuration

```yaml
my_json:
  type: json_store
  enabled: true
  config:
    session_scoped: true      # docs isolated per agent session (default);
                              # calls without a session id share "global"
    default_write_access: owner   # 'shared' restores the pre-0.5 free-for-all
    max_docs: 50
    max_doc_bytes: 2097152
    namespace_ttl_hours: 48   # evict idle namespaces from MEMORY (0 = never);
                              # persisted files stay and reload on next access
    persist: true             # per-namespace disk persistence (default on):
                              # <storage_path>/<server>/<namespace>/<doc>.json,
                              # written atomically on every mutation, incl. the
                              # owning session — survives CLI abort + continue
                              # and server restarts. Undo history stays in RAM.
    storage_path: data/json_store
    file_retention_hours: 336 # delete idle namespace FILES (startup sweep, 0 = never)
    # Optional wrong->right remaps for semantic renames (applied before fuzzy).
    key_aliases:
      synopsis: synopsis_text
    # Optional: allowed keys per document. Unknown keys are auto-remapped to the
    # closest allowed key (via key_aliases or a confident fuzzy match like
    # 'genere'->'genre') and reported in the result's 'remapped'; keys that can't
    # be mapped are rejected with a "did you mean 'X'?" hint. A top-level "*" is a
    # default model for any doc without its own (validates unnamed/auto-id docs).
    # Inside a model: "*" = any key (dynamic names), {} = free subtree, a STRING
    # node = a leaf type (string|number|integer|boolean|array|object|any; wrong
    # type rejected, null always ok), a one-element LIST [elem] = an array whose
    # elements match elem.
    key_models:
      "*":                         # one schema for every doc in this store
        synopsis_text: string      # plain string, not an array (null ok)
        themes: array
        genre: {}                  # any type
        milestones:
          - { label: string, setting: string }   # array of these objects
        key_characters:
          "*": { age: {}, role: string, arc: {} }
```

Failed checks (unmappable keys, size limit, invalid JSON) never partially apply —
the stored document stays untouched. Exception: `delete_keys` applies each path
independently and reports unknown ones in `missing`.

## Notes

- Documents are working state held in memory AND persisted per namespace to
  disk (like the todo/memory plugins), so a CLI abort + `continue` or a server
  restart does not lose them; ownership survives too because session ids are
  restored on continue. `namespace_ttl_hours` (default 48h) only bounds MEMORY
  — evicted namespaces reload lazily from disk; files are removed by the
  startup sweep after `file_retention_hours` (default 14 days).
- The plugin instance is a process-wide singleton, so passing the same
  `namespace` from different agents/sessions (e.g. a coordinator and its
  sub-agents) shares one document — no need to pass the JSON between them.
- **Parallel writers:** a writer calls `write` **without `doc`** and gets a
  fresh collision-free id back in the result's `doc`; it returns that id to its
  coordinator/moderator, who then `merge_doc`s it. N writers (e.g. a panel) run
  concurrently in one namespace without clobbering each other — no name is
  pre-assigned, so the `error` default never needs to be defeated with `replace`.
- Key models are primarily a KEY whitelist; values are free unless a key's model
  node is a **string type name** (`string`, `array`, ...) — which enforces the
  value's JSON type at that leaf (null always passes) — or a **one-element list**
  `[elem]` — which asserts an array and models each element.
