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
| `write`       | `doc?`, `data`\|`json_text`, `if_exists?` | create a doc — **omit `doc`** to get a fresh collision-free id back (best for parallel writers); `if_exists` (`error` default = catch collisions, `replace` = overwrite on purpose) applies only to a named doc |
| `read`        | `doc`, `path?`                  | canonical JSON (whole or sub-path) |
| `merge`       | `doc`, `data`\|`json_text`, `array_mode?` | deep-merge (dicts recurse, scalars overwrite, arrays replace/concat) |
| `merge_doc`   | `doc` (target), `source`, `array_mode?` | deep-merge one stored doc into another, in code (no JSON re-typing) |
| `set_value`   | `doc`, `path`, `value`          | set one value, creates intermediate objects |
| `delete_keys` | `doc`, `paths[]`                | delete paths |
| `delete_doc`  | `doc`                           | drop document |
| `list`        | –                               | existing docs |
| `outline`     | `doc`, `depth?`                 | structure without values (cheap inspection) |

Paths use dot notation with `[i]` for arrays: `key_characters.Nora.age`,
`milestones[2].label`.

## Configuration

```yaml
my_json:
  type: json_store
  enabled: true
  config:
    session_scoped: true      # docs isolated per agent session (default);
                              # calls without a session id share "global"
    max_docs: 50
    max_doc_bytes: 2097152
    namespace_ttl_hours: 48   # evict idle session namespaces (0 = never)
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

- Documents are in-memory working state, not durable storage. Idle session
  namespaces are evicted after `namespace_ttl_hours` (default 48h).
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
