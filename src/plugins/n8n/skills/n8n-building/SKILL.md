---
name: n8n-building
description: How to write an n8n workflow as Workflow SDK code that n8n accepts and that runs -- the code shape, versions, credentials, expressions, and what each finding code of the n8n tools means. Load before writing the first workflow.
metadata:
  version: '0.1.0'
---

# The code shape

Workflows are SDK code, not JSON. The authoritative reference is `n8n_get_sdk_reference` -- read `import` and `patterns` first, `expressions` when you reference data, `rules` before you finish.

```javascript
import { workflow, node, trigger, expr, newCredential } from '@n8n/workflow-sdk';

const hook = trigger({ type: 'n8n-nodes-base.webhook', version: 2.1,
  config: { name: 'Hook', parameters: { httpMethod: 'POST', path: 'order-created' } } });
const shape = node({ type: 'n8n-nodes-base.set', version: 3.4,
  config: { name: 'Shape', parameters: { assignments: { assignments: [
    { id: 'a1', name: 'greeting', value: expr('Hello {{ $json.body.name }}'), type: 'string' } ] } } } });

export default workflow('order-created', 'Order created').add(hook).to(shape);
```

- `type` and `version` come from `n8n_search_nodes` / `n8n_get_node_types`. A version n8n does not know passes n8n's own validators and fails later -- the tools name it as `UNKNOWN_TYPE_VERSION`.
- Parameter names and shapes come from `n8n_get_node_types` for the exact resource and operation. Never from memory.
- Expressions: `expr('... {{ $json.field }} ...')`. Variables live INSIDE `{{ }}`. Every `{{` needs its `}}` -- an open one evaluates to null silently.
- Data from a node further up: `$('Node Name').item.json.field` inside `{{ }}`, not `$json`.
- A node that should run once for many items: `executeOnce: true` in its `config`.
- Node names are how you address nodes later (`update_workflow`, `mocks`, `live_nodes`): make them short and unique.

# Credentials

- An existing one: `n8n_list_credentials`, then reference its id exactly as listed.
- One the user still has to create: `credentials: { slackApi: newCredential('Slack Bot') }`. It stores no credential on the node; put it in `todo_for_user`.
- Never a made-up id. A credential id n8n does not know is stored unchecked and fails at runtime -- the tools reject it (`CREDENTIAL_UNKNOWN_ID`, `CREDENTIAL_TYPE_MISMATCH`).
- n8n may attach an existing credential by itself; `n8n_create_workflow` reports that as `auto_assigned_credentials`. Mention it in the handover.

# Changing a workflow

`n8n_update_workflow` with atomic operations, e.g.

```json
[{"type": "updateNodeParameters", "nodeName": "Shape", "parameters": {"assignments": {...}}},
 {"type": "setNodeParameter", "nodeName": "Hook", "path": "/path", "value": "orders"},
 {"type": "addNode", "node": {"name": "Filter", "type": "n8n-nodes-base.filter", "typeVersion": 2.2, "parameters": {}}},
 {"type": "addConnection", "source": "Shape", "target": "Filter"}]
```

The fields of the other operations:

| operation | fields |
|---|---|
| `setNodeCredential` | `nodeName`, `credentialKey` (the credential type, e.g. `slackApi`), `credentialId`, `credentialName` |
| `addTags` / `removeTags` | `names` |
| `setNodeSettings` / `setWorkflowSettings` | `settings` |
| `setNodeDisabled` | `nodeName`, `disabled` |
| `renameNode` | `oldName`, `newName` |
| `addConnection` / `removeConnection` | `source`, `target`, optional `sourceIndex`, `targetIndex`, `connectionType` (default `main`) |

No operation changes a node's type or version: `removeNode`, `addNode` with the right `typeVersion`, then `addConnection` again. Never create a second workflow instead -- you cannot delete the first.

Pass `expected_version_id` from your last read. Refused by policy: removing the tag `scarabhive`, adding a blocked node type, a credential that does not exist, and `saveManualExecutions: false` (a test must leave a stored execution).

# What the findings mean

| code | level | means | do |
|---|---|---|---|
| `BLOCKED_NODE` | error | shell, file, legacy code or instance-admin node -- not allowed | use a dedicated node |
| `REVIEW_NODE` | warning | HTTP, Code, FTP, sub-workflow, MCP client: a human looks before publishing | name it in the handover |
| `UNKNOWN_TYPE_VERSION` | error | n8n does not know this type/version | take the version `get_node_types` knows |
| `WEBHOOK_PATH_EMPTY` | error | webhook without a path answers 404 | set a path |
| `RESPOND_NODE_MISSING` | error | `responseMode: responseNode` without a Respond to Webhook after it -- green in a test, HTTP 500 in production | add the node or use `lastNode` |
| `EXPRESSION_UNBALANCED` | error | `{{` without `}}` | close it |
| `EXECUTE_WORKFLOW_SOURCE` | error | sub-workflow not from the database with a fixed id | store it, reference its id |
| `CREDENTIAL_UNKNOWN_ID` / `CREDENTIAL_TYPE_MISMATCH` | error | see Credentials | |
| `MISSING_REQUIRED_INPUT`, `INVALID_PARAMETER`, `SET_INVALID_ASSIGNMENT` | error | n8n says `valid: true` but these are real errors | fix them |
| `N8N_ERROR`, `N8N_NODE_CONFIG` | error | n8n's own validator | read the message |

Errors block the test run. Warnings do not, but belong in the handover.
