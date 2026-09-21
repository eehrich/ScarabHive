---
name: n8n-testing
description: How n8n_test_workflow runs a workflow safely -- what is pinned and what runs live, how to write trigger_input and mocks, how to read the answer, and when a workflow counts as tested. Load before the first test run.
metadata:
  version: '0.1.0'
---

# What a test run does

`n8n_test_workflow` runs the stored draft once. Nothing leaves the execution unless you ask for it and the operator allows it:

- **Triggers are always pinned** to `trigger_input` (default: one empty item, with a warning).
- **Local nodes run for real**: Set, IF, Switch, Filter, No Op, Respond to Webhook, Split Out, Aggregate, Limit, Date & Time, Sort, Merge (not Sort with custom code, not Merge in SQL mode).
- **Every other node is pinned** to `mocks[node]` -- APIs, anything with a credential, HTTP, Code, sub-workflows, AI agents. Without a mock it outputs one empty item, and every node after it computes on nothing: give mocks.
- **AI Agent and other nodes with subnodes** (model, tools, memory, the tools of a tool): the root is pinned unless the root AND every node under it may run live.
- **`live_nodes`** asks to run a node for real. Granted for an HTTP node only if its URL is a fixed, plain `http(s)://host/...` (no user info, no backslash), its host is in the operator's `allowed_hosts`, and it uses no proxy, no pagination and no credential (with a credential, the operator must also have listed its type in `live_node_types`); for any other type only if the operator listed it in `live_node_types`. A node that is not named in `live_nodes` stays pinned either way. Never live: Code (it has network access) and sub-workflows (the callee's own nodes would run unpinned; a pinned caller does not start the callee at all).

The answer says per node why it ran the way it did (`reason`), so a refused `live_nodes` entry is visible, not silent.

# Test data

Items look like `{"json": {...}}`; a plain object is wrapped for you.

| trigger | `trigger_input` |
|---|---|
| Webhook | `[{"json": {"body": {"name": "Ada"}, "headers": {}, "query": {}}}]` -- the node reads `$json.body.…` |
| Manual | `[{"json": {}}]` |
| Schedule | `[{"json": {"timestamp": "2026-01-01T08:00:00.000Z"}}]` |
| Form / Chat | the fields the trigger emits, from `n8n_get_node_types` |

A mock is what the pinned node would OUTPUT, in the shape the real service returns -- the nodes after it read those fields. Take the shape from `n8n_get_node_types` or from an earlier execution, never guess field names.

# Reading the answer

- `tested: true` only with `execution_status: success`. Anything else: NOT TESTED.
- `nodes[]`: `live` ran for real (a subnode too, when its root ran), `pinned` gave your mock, `not_reached` did not run. A mock on a subnode is ignored with a warning: mock its root.
- `not_reached` after an IF or Switch is fine when that branch was not taken -- say which branch the test took. To prove the other branch, test again with data that takes it.
- `items_out` counts every output; an IF or Switch also gets `items_per_output` (IF: `[true, false]`). `sample` is the first item of the first output that has one: check that the values are what the request wants, not just that there is output.
- Respond to Webhook: its `sample` is its INPUT, not the response body. A test proves only that the response expression did not fail -- do not claim the body's contents.
- On `execution_status: error`: `n8n_get_execution` with `nodes=[<failing node>]` and `include_data: true`, fix, test again (at most three rounds).

# Refusals

- Blocking findings on the stored workflow (see `n8n-building`) -- fix them first.
- `saveManualExecutions: false` stops test runs from being stored -- set it to `true` with `setWorkflowSettings`.
- An execution that was not stored proves nothing and is reported as an error.
