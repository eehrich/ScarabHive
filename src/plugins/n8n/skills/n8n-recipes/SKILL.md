---
name: n8n-recipes
description: Proven n8n workflow patterns -- a webhook that answers and reports its execution id, schedules, sub-workflows, AI agents with subnodes, error workflows, and a workflow that ScarabHive agents can call as a tool over MCP. Load when a request matches one.
metadata:
  version: '0.1.0'
---

Node versions below are examples; confirm them with `n8n_get_node_types`.

# Webhook that answers -- callable by ScarabHive

POST, a Respond to Webhook node, and the execution id in the answer: that is how a caller finds the run again.

```javascript
const hook = trigger({ type: 'n8n-nodes-base.webhook', version: 2.1,
  config: { name: 'Hook', parameters: { httpMethod: 'POST', path: 'greet', responseMode: 'responseNode' } } });
const shape = node({ type: 'n8n-nodes-base.set', version: 3.4,
  config: { name: 'Shape', parameters: { assignments: { assignments: [
    { id: 'a1', name: 'greeting', value: expr('Hello {{ $json.body.name }}'), type: 'string' } ] } } } });
const respond = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.1,
  config: { name: 'Respond', parameters: { respondWith: 'json',
    responseBody: expr('{{ JSON.stringify({ greeting: $json.greeting, executionId: $execution.id }) }}') } } });
export default workflow('greet', 'Greet').add(hook).to(shape).to(respond);
```

Test with `trigger_input: [{"json": {"body": {"name": "Ada"}}}]`. `responseMode: responseNode` without the Respond node is green in a test and HTTP 500 in production.

# Schedule

`n8n-nodes-base.scheduleTrigger`; the rule shape comes from `n8n_get_node_types`. Remember `executeOnce: true` on nodes that must run once per schedule tick, not once per item.

# Sub-workflow

The callee starts with `n8n-nodes-base.executeWorkflowTrigger`. The caller uses `n8n-nodes-base.executeWorkflow` with `source: 'database'` and the callee's fixed workflow id -- inline JSON, files and URLs are refused. Build and test the callee on its own first: in the caller's test the call node is always pinned, so mock what the callee returns.

# AI Agent with subnodes

Read `n8n_get_sdk_reference` section `patterns_detailed` for `languageModel`, `memory`, `tool` and `outputParser`. Subnodes read data with `nodeJson(node, 'path')` or `$('Node').item.json…`, never `$json`. In a test the agent root is pinned unless it and every node under it may run live (local nodes always; others only when named in `live_nodes` and allowed by the rules in `n8n-testing`): usually, mock the agent's output.

# Error workflow

A workflow that starts with `n8n-nodes-base.errorTrigger` receives `execution {id, url, error, lastNodeExecuted}` and `workflow {id, name}`. n8n only accepts it as another workflow's `errorWorkflow` once it is PUBLISHED and contains the Error Trigger -- publishing is for a human. Build it, test it, hand over: "publish X, then I set it as errorWorkflow of Y" (`setWorkflowSettings`).

# A workflow as a tool for ScarabHive agents

Start with `@n8n/n8n-nodes-langchain.mcpTrigger` (version 2.x) and give it tool nodes; each tool's name is its NODE name, case-sensitive. Set the trigger's `authentication: 'bearerAuth'` with `credentials: { httpBearerAuth: newCredential('<name> token') }` -- n8n defaults to `none`, which leaves a published endpoint open to anyone who reaches n8n. The credential goes into `todo_for_user`; the same token goes into `<VAR>` below. After a human publishes it, the operator adds to `config/mcp_servers.yaml`:

```yaml
external_servers:
  remote_servers:
    <name>:
      enabled: true
      url: <n8n base url>/mcp/<path>
      transport: streaming        # sse answers 404
      auth: {type: bearer, bearer_token: "${<VAR>}"}
```

and `"<name>.*"` to the allowlist of the agent that should use it. Every call is an n8n execution. Put this snippet into the handover -- you do not edit `config/`.

A test run pins the MCP trigger, so its tools never run and show as `not_reached`. Prove each tool's logic as its own workflow (a sub-workflow behind a `toolWorkflow` node, tested on its own), or hand the tools over as NOT TESTED.
