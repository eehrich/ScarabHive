# n8n

Build n8n workflows from a request and prove they run. The plugin forwards n8n's own instance MCP -- node search, exact parameter definitions, validation, creating from SDK code, test runs with pinned data -- and lays ScarabHive's policy on top. It rebuilds none of n8n.

- Design: [docs/design.md](docs/design.md) · measured facts: [docs/n8n_facts.md](docs/n8n_facts.md) · deploying n8n: [docs/deploy/README.md](docs/deploy/README.md)
- Tested against n8n 2.39.9 (Community, Docker, SQLite).

## Setup

1. Deploy n8n with [docs/deploy/](docs/deploy/) and run `setup_owner.sh` (instance MCP on, a read-only public API key, the MCP key).
2. Put three values into `config/secrets.env`: `N8N_BASE_URL`, `N8N_API_KEY`, `N8N_MCP_KEY`. If browsers and webhook callers reach n8n under another address, add `N8N_PUBLIC_URL`: editor links and the published webhook URLs are built from it.
3. Restart ScarabHive. Without the values the `n8n` instance offers no tools, so an installation without n8n loses nothing; the start logs a WARNING when an enabled agent (the shipped `n8n_agent`) allows its tools.
4. Talk to `n8n_agent` directly, in the chat or with `agent-cli`. Another agent would need `n8n_agent/*` in its allowlist.

Operator knobs in [agents/n8n.yaml](agents/n8n.yaml): `allowed_hosts` (hostnames, compared in lower case, any port) and `live_node_types` (what a test may run for real), `allow_publish` (shipped `true`: the builder publishes, takes offline and archives when the user asks; `false` leaves it only triggering; the code default is off, and only a real `true` counts), `watch_max_hours` (how long a trigger's wake waits, default 24), `managed_tag`, `blocked_node_types` / `review_node_types`. A configured list REPLACES the default in `validate.py` -- copy the default and change it, or you unblock everything it held.

## Tools

| Tool | Via | Does |
|---|---|---|
| `n8n_search_nodes`, `n8n_get_node_types`, `n8n_explore_node_resources`, `n8n_get_best_practices`, `n8n_get_sdk_reference`, `n8n_list_credentials` | MCP | node knowledge and the SDK manual; need only the MCP key |
| `n8n_validate_node_config`, `n8n_validate_workflow` | MCP + `validate.py` | n8n's validators, read correctly, plus our policy |
| `n8n_create_workflow`, `n8n_update_workflow` | MCP, checked via public API | write, tag, re-check what was stored |
| `n8n_get_workflow`, `n8n_list_workflows` | MCP / public API | read |
| `n8n_test_workflow`, `n8n_get_execution`, `n8n_list_executions` | MCP / public API | the proof |
| `n8n_publish_workflow`, `n8n_unpublish_workflow`, `n8n_archive_workflow` | MCP, checked via public API | publish exactly the version a successful test proved; take offline; archive |
| `n8n_trigger_workflow` | production webhook, public API | a real run of a published workflow; finds its execution and wakes the session when it ends |

The agent `n8n_agent` gets all nineteen; the last four only on the user's request. Deleting and n8n's `execute_workflow` are never offered.

## Model Experience

**What the model sees.**

- Tool descriptions as in [schema.yaml](schema.yaml). The acting tools appear only when the public API key is configured too, because the managed-tag lock reads through it.
- Every answer is `{"status": "success", ...}` or `{"status": "error", "error": "<what to do>"}`. Refusals name the reason and the way out: `workflow w1 is not managed by ScarabHive (tag 'scarabhive' missing); it stays untouched`, `operation 0: node type n8n-nodes-base.executeCommand is blocked by policy`, `n8n MCP rate limit reached (100 requests per IP and 5 minutes); retry in 102 s`.
- Findings on a stored workflow come as `{code, level, node, message, fix_hint}` (codes in the skill `n8n-building`); `ok` is false on any error, including warnings n8n itself marks valid.
- `n8n_test_workflow` answers per node `run: live | pinned | not_reached` (a subnode is `live` only when it ran inside its root) with the `reason`, `items_out` over all outputs (`items_per_output` for IF/Switch), the first item as `sample`, and `tested: true` only for a stored execution with status success.
- A write whose check afterwards fails says so and carries the `workflow_id`: `workflow w2 was created and tagged, but the check afterwards failed (...); do not repeat it -- test_workflow checks it anew`.
- `n8n_publish_workflow` refuses an untested version: `w1: its current version has no successful test run among the newest 250 successful runs -- run n8n_test_workflow on it first` (only a test run counts, not a production run), and answers with the production `webhooks` URLs and `proven_by_execution`.
- `n8n_trigger_workflow` answers `http_status`, the webhook's `response` (untrusted, capped), `execution_id` with `correlation: exact | heuristic` (or `candidates` when several runs started at once), `execution_status`, and for a run still going `wake: true | false` with `wake_note` (`you are woken when the run ends: give the user execution id 7 and end your turn, then read it with n8n_get_execution. A one-shot agent-cli run is never woken.`, or why not, always ending in `; you are not woken -- give execution id 7 to whoever asked and read it later with n8n_get_execution, do not poll it`). A 404 or 413 counts as n8n's refusal only when no run of the call turns up. Once the webhook was called, nothing reads as "nothing happened": a missing answer or a failed lookup is a result with `note: ... -- do not call the webhook again, look with n8n_list_executions`.
- A woken run gets the framework's generic wake sentence; which execution ended stands in its own history. `n8n_get_execution` then also carries `watch: {status, note}` -- the note says when the wait gave up and no further wake comes. `n8n_list_executions` lists running and waiting runs too unless a status is asked for.
- What came out of n8n -- node descriptions (a community node's author wrote them), names, execution data, credential names, n8n messages -- is wrapped as `{"untrusted": true, "content": ...}`. The prompt names that marker and every name or value from a workflow as data, never an instruction. The SDK reference and best practices come unwrapped: n8n's own documentation.
- The agent's prompt is short; the knowledge is in three on-demand skills: `n8n-building`, `n8n-testing`, `n8n-recipes`.
- The agent remembers the user and their projects in the OKF bundle `data/okf/n8n`, through its own instance `n8n_okf` (sandboxed to that bundle). Each turn the concepts the user's message is about are folded into the system prompt; the prompt tells it what to write down (preferences, projects with their workflow ids, what failed) and never to store n8n text or secrets.

**Token and cache effect.** Tool results are append-only; nothing rewrites earlier messages. Every result is capped: node search 13k chars, node definitions 20k, one SDK section 16k (the whole reference would be ~50k, so it is served one section at a time), a full workflow 40k, an execution 12k, a webhook answer 8k, samples 1k. The agent runs with `prompt_cache_mode: multi_turn`. The OKF hook changes the system prompt only when the user speaks (it keys on the last user message) or the agent writes a concept mid-turn; at most six concepts.

**Known gaps.**

- **Code never runs live in a test.** Code nodes reach the network through `this.helpers.httpRequest` (measured), so no host list can bound them; their logic is only proven with mocks, and the handover says so.
- **Sub-workflows never run live in a test.** The callee's own nodes would run outside the caller's pin plan (measured); a pinned call node does not start the callee. The callee is tested on its own.
- **An AI root runs live only with its whole subtree.** The root itself and every node under it must be local or pass the live check; a stray `ai_*` edge made an HTTP node run live before this rule (measured).
- **An allowed host is trusted with its redirects.** HTTP runs live only for a plain URL on a host in `allowed_hosts`, without proxy, pagination or a credential sent on redirect (Python and n8n read a URL with a backslash or user info differently); a redirect from that host is followed. An HTTP node with a credential runs live only if the operator also listed its type in `live_node_types`: the credential travels with the request.
- **The block list is explicit.** A hidden or dangerous node type added by a later n8n version is not caught until it is added; the upgrade checklist in docs/deploy/README.md re-checks it.
- **The code pre-check is a hint.** Type literals in SDK code are checked before creating, but code can assemble a string; the binding check runs on the stored workflow before any test.
- **The MCP key acts as its n8n user** (today the owner). The plugin's locks bound what the BUILDER can do; the key itself must be kept like a password.
- **Only the user's request stands between the builder and a real run.** The prompt allows publishing, unpublishing, archiving and triggering only when the user asked; the tools cannot see the conversation. What they enforce: a managed workflow, no blocking finding, a successful test of exactly the version going live, and `allow_publish` for everything but triggering. A prompt injection could still publish one of our tested workflows or start a published one with a payload of its choice.
- **A wake does not survive a restart** (docs/design.md E7). The watch lives in the API process or an `agent-cli chat`; a one-shot `agent-cli run` ends it with its turn, and the tool cannot tell that run apart -- `wake_note` names the execution id for a later read. A woken run and a sub-agent's session get no watch at all. The outcome stays in n8n.
- **Only webhook workflows are triggered here**, without webhook authentication or route parameters, and only on a plain path (no dot segment, `?`, `#`, `%`). The execution is matched exactly when the workflow answers with its `executionId` (Respond to Webhook), otherwise as the one new run since the call.
- **The memory is the agent's own words.** Concepts are folded into later prompts unwrapped; the prompt forbids copying n8n text into them, but that rule is all that keeps a workflow's text from becoming a later instruction.
- **Not built:** n8n calling ScarabHive and a panel (docs/design.md §6.3, §7; E8 deferred).
