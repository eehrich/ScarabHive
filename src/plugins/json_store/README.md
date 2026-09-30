# JSON Store

JSON working documents for agents. An agent that builds a large JSON result over many steps keeps it on the server
instead of re-typing it: the plugin checks and repairs what it is sent, merges pieces in code, and every read
returns valid JSON. Agents share documents through a namespace, each document can only be changed by the session
that created it, recent changes can be undone, and documents are saved to disk so an aborted run can continue.

- **Tool** `<instance>_manage_json` -- one tool with the operations `write`, `read`, `merge`, `merge_doc`,
  `set_value`, `delete_keys`, `delete_doc`, `undo`, `list`, `outline` and `stats`. Optional key models fix the
  allowed keys and value types per document.
- No hooks, no panel.

Declare an instance in an agent's YAML (`wf_json: {type: json_store, enabled: true}`, settings under `config:`)
and allow `wf_json/*` in that agent's tool list.

The full manual -- every operation, parameter and answer, paths and merging, namespaces and ownership, key models,
where the data lives and the server settings -- is the plugin's guide, `json_store.guide`, in the Help panel.
